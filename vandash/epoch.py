"""JBD-protocol BLE poller for the Epoch battery's internal BMS.

The Epoch Essential's BMS is a stock JBD (Jiabaida) module: GATT service
0xFF00, notifications on 0xFF01, write-without-response commands on 0xFF02,
classic ``DD A5 <reg> 00 <chk> <chk> 77`` reads. No handshake is needed for
read-only registers (verified against this pack 2026-08-19).

Radio reality (why this poller is advert-driven rather than on a timer):

- The BMS advertises SPARSELY while it thinks it is idle — and its current
  sense has a ~2 A deadband, so the van's overnight trickle load still counts
  as idle. The shared scanner may only catch an advert every minute or two.
- It accepts ONE connection at a time: while we're connected the phone's
  Epoch app can't see the battery, and vice versa (a phone app left open
  is the expected cause of long "not seen" stretches). So we connect,
  read, and disconnect quickly instead of holding the link.
- The first write after connecting is often silently dropped — hence retries.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any

from bleak import BleakClient

from vandash.ble import BleScanner
from vandash.collector import PowerState

logger = logging.getLogger(__name__)

NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

REG_BASIC = 0x03
REG_CELLS = 0x04

# JBD protection status bitfield (register 0x03, bytes 16-17)
PROTECTION_BITS = {
    0x0001: "Cell overvoltage",
    0x0002: "Cell undervoltage",
    0x0004: "Pack overvoltage",
    0x0008: "Pack undervoltage",
    0x0010: "Charge overtemp",
    0x0020: "Charge undertemp",
    0x0040: "Discharge overtemp",
    0x0080: "Discharge undertemp",
    0x0100: "Charge overcurrent",
    0x0200: "Discharge overcurrent",
    0x0400: "Short circuit",
    0x0800: "Frontend IC error",
    0x1000: "Charge FET locked",
}


def _jbd_read(reg: int) -> bytes:
    chk = 0x10000 - reg  # checksum over {reg, len=0}
    return bytes([0xDD, 0xA5, reg, 0x00, (chk >> 8) & 0xFF, chk & 0xFF, 0x77])


def _parse_basic(p: bytes) -> dict[str, Any]:
    volts, amps, remain, capacity, cycles = struct.unpack_from(">HhHHH", p, 0)
    protection = struct.unpack_from(">H", p, 16)[0]
    rsoc, fet, _ncell, nntc = p[19], p[20], p[21], p[22]
    temps = [
        round(t / 10 - 273.15, 1)
        for (t,) in struct.iter_unpack(">H", p[23:23 + nntc * 2])
    ]
    return {
        "voltage": volts / 100,
        "current": amps / 100,
        "soc": rsoc,
        "remaining_ah": remain / 100,
        "capacity_ah": capacity / 100,
        "cycles": cycles,
        "charge_fet": bool(fet & 1),
        "discharge_fet": bool(fet & 2),
        "protection": [l for bit, l in PROTECTION_BITS.items() if protection & bit],
        "temps": temps,
        "temp": max(temps) if temps else None,
    }


def _parse_cells(p: bytes) -> dict[str, Any]:
    mv = [x[0] for x in struct.iter_unpack(">H", p)]
    return {"cells_mv": mv, "cell_delta_mv": max(mv) - min(mv) if mv else None}


class EpochBmsClient:
    """Polls the pack whenever an advert shows it reachable and a poll is due."""

    def __init__(
        self,
        name: str,
        address: str,
        state: PowerState,
        scanner: BleScanner,
        poll_s: float = 300,
    ) -> None:
        self._name = name
        self._address = address.upper()
        self._state = state
        self._scanner = scanner
        self._poll_s = poll_s
        self._task: asyncio.Task | None = None
        self._device: Any = None  # BLEDevice from the shared scan
        self._rssi: int | None = None
        self._seen = asyncio.Event()
        self._last_ok = 0.0

    def _on_advert(self, ble_device: Any, advert: Any) -> None:
        if ble_device.address.upper() == self._address:
            self._device = ble_device
            self._rssi = advert.rssi
            self._seen.set()

    async def start(self) -> None:
        self._scanner.register(self._on_advert)
        self._task = asyncio.create_task(self._run(), name="epoch-bms")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        failures = 0
        while True:
            await self._seen.wait()
            self._seen.clear()
            if time.time() - self._last_ok < self._poll_s:
                continue
            try:
                await self._poll(self._device)
                self._last_ok = time.time()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    logger.warning(
                        "Epoch BMS poll failed: %s (%d failures so far)", e, failures
                    )
                # Back off so a failing pack doesn't get hammered on every advert.
                await asyncio.sleep(30)

    async def _poll(self, device: Any) -> None:
        async with BleakClient(device, timeout=30) as client:
            buf = bytearray()
            got = asyncio.Event()

            def on_notify(_char, data: bytearray) -> None:
                buf.extend(data)
                # frame: DD <reg> <status> <len> <payload…> <chk×2> 77
                if len(buf) >= 4 and len(buf) >= buf[3] + 7 and buf.endswith(b"\x77"):
                    got.set()

            await client.start_notify(NOTIFY_UUID, on_notify)

            async def request(reg: int) -> bytes:
                for _ in range(5):
                    buf.clear()
                    got.clear()
                    await client.write_gatt_char(
                        WRITE_UUID, _jbd_read(reg), response=False
                    )
                    try:
                        await asyncio.wait_for(got.wait(), 4)
                    except asyncio.TimeoutError:
                        continue
                    if buf[0] == 0xDD and buf[1] == reg and buf[2] == 0x00:
                        return bytes(buf[4:4 + buf[3]])
                raise ConnectionError(f"no response to register 0x{reg:02x}")

            fields = _parse_basic(await request(REG_BASIC))
            fields.update(_parse_cells(await request(REG_CELLS)))
            self._state.set_battery(self._name, fields, rssi=self._rssi)
            logger.info(
                "Epoch BMS: %.2fV %.2fA %d%% Δ%dmV",
                fields["voltage"], fields["current"], fields["soc"],
                fields["cell_delta_mv"] or 0,
            )
