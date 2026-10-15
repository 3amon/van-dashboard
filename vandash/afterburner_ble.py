"""BLE fallback transport for the Afterburner.

The Afterburner's HC-05-style dual-mode module bridges its serial JSON stream
over a BLE UART (service 0xFFE0, or Nordic UART on some builds). The firmware
sends every JSON status message to Bluetooth as well as WebSocket/MQTT, and
parses commands like {"Refresh":1} from any transport — so this client speaks
the exact same protocol as the WiFi one, reassembled from 20-byte BLE chunks.

Policy: WiFi is preferred. This client only connects while heater data is
stale (WiFi down, e.g. Starlink off) and disconnects again once WiFi-sourced
data resumes, so the phone app can use the BLE slot when we don't need it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from bleak import BleakClient

from vandash.afterburner import FIELDS
from vandash.ble import BleScanner
from vandash.collector import PowerState

logger = logging.getLogger(__name__)

# (service, notify char, write char) candidates, tried in order
UART_PROFILES = [
    ("0000ffe0-0000-1000-8000-00805f9b34fb",
     "0000ffe1-0000-1000-8000-00805f9b34fb",
     "0000ffe1-0000-1000-8000-00805f9b34fb"),
    ("6e400001-b5a3-f393-e0a9-e50e24dcca9e",   # Nordic UART (USE_BLE_BLUETOOTH builds)
     "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
     "6e400002-b5a3-f393-e0a9-e50e24dcca9e"),
]

WIFI_FRESH_S = 60      # WiFi data younger than this → BLE stays out of the way
REFRESH_INTERVAL_S = 30
RETRY_S = 15
SCAN_WAIT_S = 15       # how long to wait for the shared scan to surface the heater


class JsonStreamAssembler:
    """Reassemble complete top-level JSON objects from a byte stream."""

    def __init__(self) -> None:
        self._buf = ""
        self._depth = 0
        self._in_str = False
        self._escape = False
        self._start = -1

    def feed(self, chunk: bytes) -> list[dict]:
        out = []
        for ch in chunk.decode("utf-8", errors="replace"):
            self._buf += ch
            if self._escape:
                self._escape = False
                continue
            if ch == "\\" and self._in_str:
                self._escape = True
            elif ch == '"':
                self._in_str = not self._in_str
            elif not self._in_str and ch == "{":
                if self._depth == 0:
                    self._start = len(self._buf) - 1
                self._depth += 1
            elif not self._in_str and ch == "}":
                self._depth -= 1
                if self._depth == 0 and self._start >= 0:
                    try:
                        out.append(json.loads(self._buf[self._start:]))
                    except json.JSONDecodeError:
                        pass
                    self._buf = ""
                    self._start = -1
                elif self._depth < 0:  # garbage; resync
                    self._buf, self._depth, self._start = "", 0, -1
        if len(self._buf) > 65536:  # runaway without closing brace; resync
            self._buf, self._depth, self._start = "", 0, -1
            self._in_str = self._escape = False
        return out


def _is_afterburner(device, adv) -> bool:
    name = (adv.local_name or device.name or "").lower()
    if "afterburner" in name:
        return True
    return any(u.lower().startswith("0000ffe0") for u in adv.service_uuids or [])


class AfterburnerBleClient:
    def __init__(
        self, state: PowerState, scanner: BleScanner, name: str = "Afterburner"
    ) -> None:
        self._state = state
        self._scanner = scanner
        self._name = name
        self._task: asyncio.Task | None = None
        self._device: Any = None       # BLEDevice, captured from the shared scan
        self._seen = asyncio.Event()

    def _on_advert(self, ble_device: Any, advert: Any) -> None:
        if _is_afterburner(ble_device, advert):
            self._device = ble_device
            self._seen.set()

    async def start(self) -> None:
        self._scanner.register(self._on_advert)
        self._task = asyncio.create_task(self._run(), name="afterburner-ble")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _wifi_is_fresh(self) -> bool:
        d = self._state.devices.get(self._name)
        return bool(
            d and d.get("source") == "wifi" and time.time() - d["ts"] < WIFI_FRESH_S
        )

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                if self._wifi_is_fresh():
                    await asyncio.sleep(RETRY_S)
                    continue
                # Wait for the shared scanner to surface the heater's advert.
                if self._device is None:
                    try:
                        await asyncio.wait_for(self._seen.wait(), timeout=SCAN_WAIT_S)
                    except asyncio.TimeoutError:
                        raise ConnectionError("not in BLE range")
                # Scan-while-connected verified on the Pi 5 radio (2026-07-30):
                # Victron adverts keep arriving throughout a GATT session, so
                # the shared scan keeps running — pausing it here would starve
                # the Victron collector for as long as the heater stays on BLE.
                await self._session(self._device)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures == 1 or failures % 20 == 0:
                    logger.warning(
                        "Afterburner BLE: %s — retrying every %ds (%d failures so far)",
                        e, RETRY_S, failures,
                    )
            await asyncio.sleep(RETRY_S)

    async def _session(self, device: Any) -> None:
        assembler = JsonStreamAssembler()
        fields: dict = {}
        got_data = asyncio.Event()

        def on_notify(_char, data: bytearray) -> None:
            for obj in assembler.feed(bytes(data)):
                for key, value in obj.items():
                    field = FIELDS.get(key)
                    if field is not None:
                        fields[field] = value.strip() if isinstance(value, str) else value
            if fields:
                self._state.set_heater(self._name, fields, source="ble")
                got_data.set()

        async with BleakClient(device, timeout=15) as client:
            notify_char = write_char = None
            for svc_uuid, notify_uuid, write_uuid in UART_PROFILES:
                for service in client.services:
                    if service.uuid.lower() != svc_uuid:
                        continue
                    chars = {c.uuid.lower(): c for c in service.characteristics}
                    notify_char = chars.get(notify_uuid)
                    write_char = chars.get(write_uuid)
                if notify_char and write_char:
                    break
            if notify_char is None or write_char is None:
                raise ConnectionError(
                    "no UART service found; services: "
                    + ", ".join(s.uuid for s in client.services)
                )

            await client.start_notify(notify_char, on_notify)
            logger.info("Afterburner BLE connected (%s)", device.address)
            try:
                while True:
                    await client.write_gatt_char(
                        write_char, b'{"Refresh":1}', response=False
                    )
                    got_data.clear()
                    await asyncio.sleep(REFRESH_INTERVAL_S)
                    if not got_data.is_set():
                        raise ConnectionError("connected but no JSON received")
                    if self._wifi_is_fresh():
                        logger.info("Afterburner WiFi is back — releasing BLE")
                        return
                    if not client.is_connected:
                        raise ConnectionError("BLE link dropped")
            finally:
                if client.is_connected:
                    try:
                        await client.stop_notify(notify_char)
                    except Exception:
                        pass
