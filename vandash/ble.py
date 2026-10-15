"""Shared BLE scanner — one `BleakScanner` for the whole app.

BlueZ (Linux/Raspberry Pi) has a single radio and rejects a *second* concurrent
discovery with `org.bluez.Error.InProgress` — unlike macOS CoreBluetooth, which
quietly lets multiple scanners coexist. So we run ONE scanner and fan its
adverts out to every consumer (the Victron collector, Afterburner discovery).

GATT clients (the Afterburner BLE fallback) connect while the scan keeps
running: scan-while-connected was verified working on the Pi 5 radio
(2026-07-30) — BlueZ time-slices the radio and adverts keep flowing, just at a
reduced rate. Only a second concurrent *discovery* is rejected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from bleak import BleakScanner

logger = logging.getLogger(__name__)

AdvertCallback = Callable[[Any, Any], None]


class BleScanner:
    # If NO adverts arrive for this long, the scanner is presumed wedged
    # (macOS sleep/wake, a BlueZ adapter hiccup) and is restarted.
    WATCHDOG_QUIET_S = 120

    def __init__(self, adapter: str | None = None) -> None:
        self._adapter = adapter or None
        self._scanner: BleakScanner | None = None
        self._callbacks: list[AdvertCallback] = []
        self._lock = asyncio.Lock()
        self._running = False
        self._last_advert = 0.0
        self._watchdog_task: asyncio.Task | None = None

    def register(self, cb: AdvertCallback) -> None:
        """Add an advert callback. Safe to call before or after start()."""
        self._callbacks.append(cb)

    def _dispatch(self, device: Any, advert: Any) -> None:
        # Any advert from anything proves the radio is alive.
        self._last_advert = time.time()
        for cb in self._callbacks:
            try:
                cb(device, advert)
            except Exception:
                logger.exception("BLE advert callback failed (continuing)")

    async def _open(self) -> None:
        kwargs: dict[str, Any] = {"detection_callback": self._dispatch}
        if self._adapter:
            kwargs["adapter"] = self._adapter
        self._scanner = BleakScanner(**kwargs)
        await self._scanner.start()
        self._running = True
        self._last_advert = time.time()

    async def _close(self) -> None:
        if self._scanner is not None and self._running:
            try:
                await self._scanner.stop()
            except Exception:
                logger.exception("Error stopping BLE scanner (continuing)")
        self._running = False

    async def start(self) -> None:
        async with self._lock:
            await self._open()
        self._watchdog_task = asyncio.create_task(self._watchdog(), name="ble-watchdog")
        logger.info(
            "Shared BLE scanner started%s",
            f" on adapter {self._adapter}" if self._adapter else "",
        )

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(30)
            async with self._lock:
                if not self._running:
                    continue  # intentionally stopped — not a fault
                quiet = time.time() - self._last_advert
                if quiet < self.WATCHDOG_QUIET_S:
                    continue
                logger.warning("No BLE adverts for %.0fs — restarting scanner", quiet)
                await self._close()
                try:
                    await self._open()
                    logger.info("Scanner restarted")
                except Exception:
                    logger.exception("Scanner restart failed — will retry in 30s")

    async def stop(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            await self._close()
