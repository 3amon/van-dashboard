"""Demo data feeder — synthesizes plausible Victron + heater readings.

Enabled with the env var VANDASH_DEMO=1 (see server.py). Lets you see the
dashboard fully populated without the van's BLE gear in range — for UI work,
screenshots, and the shop bench. It writes straight into PowerState, so it
bypasses the BLE collector entirely (and its RSSI / plausibility filters).

Values model a parked 24V van (2x Battle Born in series): solar making a
gently rippling arc, alternator charger off (engine off), diesel heater running.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from vandash.collector import PowerState

logger = logging.getLogger(__name__)

TICK_S = 2
HISTORY_MIN = 90


class _Dev:
    """Minimal stand-in for ConfiguredDevice (PowerState only needs name/kind)."""

    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.kind = kind


class DemoFeeder:
    def __init__(self, state: PowerState) -> None:
        self._state = state
        self._task: asyncio.Task | None = None
        self._solar = _Dev("Solar", "solar")
        self._dcdc = _Dev("DC-DC", "dcdc")
        self._shunt = _Dev("Shunt", "shunt")
        self._ac = _Dev("Shore", "ac")
        self._yield_wh = 1850.0  # Wh "since midnight"
        # Fixed scenario anchors so every source appears on the chart: a drive
        # 75–50 min ago (alternator), then shore power from 25 min ago onward.
        self._t0 = time.time()

    # ── synthetic signals (continuous functions of absolute time so the
    #    back-filled history and the live feed join seamlessly) ──────────────
    @staticmethod
    def _solar_at(ts: float) -> tuple[float, float, float]:
        # Slow arc over ~40 min, plus a few incommensurate "cloud" waves so the
        # trace wanders naturally instead of as a regular ripple.
        base = 330 + 150 * math.sin(ts / 2400.0)
        clouds = (55 * math.sin(ts / 167.0)
                  + 38 * math.sin(ts / 71.0 + 2.0)
                  + 30 * math.sin(ts / 311.0 + 0.7)
                  + 18 * math.sin(ts / 43.0 + 1.1))
        w = max(0.0, base + clouds)
        v = 26.9 + 0.4 * math.sin(ts / 600.0)       # 26.5–27.3 V
        a = w / max(v, 1.0)                          # charge current ≈ P/V
        return w, v, a

    @staticmethod
    def _cabin_at(ts: float) -> tuple[float, float]:
        return 20.0 + 1.2 * math.sin(ts / 1200.0), 47 + 4 * math.sin(ts / 1500.0)

    def _alt_at(self, ts: float) -> float:
        # ~700 W while "driving" during the drive window, else 0.
        if not (self._t0 - 75 * 60 <= ts <= self._t0 - 50 * 60):
            return 0.0
        return max(0.0, 700 + 60 * math.sin(ts / 97.0) + 25 * math.sin(ts / 31.0))

    def _shore_at(self, ts: float) -> float:
        # IP22 on a 15 A circuit: ~340 W once plugged in, tapering slightly.
        if ts < self._t0 - 25 * 60:
            return 0.0
        return max(0.0, 340 - (ts - (self._t0 - 25 * 60)) / 60.0
                   + 12 * math.sin(ts / 53.0))

    def _backfill(self) -> None:
        now = time.time()
        now -= now % self._state.bucket_seconds
        rows = self._state._rows
        # Rows persisted by an earlier demo run reload from SQLite; appending
        # backfill over the same window would interleave timestamps and the
        # chart polyline would zig-zag across itself.
        newest = rows[-1]["t"] if rows else 0.0
        for i in range(HISTORY_MIN * 60 // 30, 0, -1):
            ts = now - i * 30
            if ts <= newest:
                continue
            w, v, a = self._solar_at(ts)
            temp, rh = self._cabin_at(ts)
            rows.append({"t": ts, "w": round(w, 1), "v": round(v, 2),
                         "a": round(a, 1), "temp": round(temp, 1), "rh": round(rh),
                         "alt_w": round(self._alt_at(ts), 1),
                         "ac_w": round(self._shore_at(ts), 1)})

    def _tick(self) -> None:
        ts = time.time()
        w, v, a = self._solar_at(ts)
        self._yield_wh += w * TICK_S / 3600.0
        # Shore charger first so the shunt's charge-source estimate sees a
        # fresh reading (unplugged IP22s stop advertising; ours stays plugged).
        shore_w = self._shore_at(ts)
        shore_a = shore_w / max(v, 1.0)
        self._state.update(self._ac, {
            "charge_state": "Bulk",
            "charger_error": "No Error",
            "output_voltage1": round(v, 2),
            "output_current1": round(shore_a, 1),
            "ac_current": round(shore_w / 230.0, 1),
        }, rssi=-60)
        loads = 4.2 + 1.5 * math.sin(ts / 210.0)          # cabin draw, A
        net = a + shore_a - loads
        self._state.update(self._shunt, {
            "voltage": round(v, 2),
            "current": round(net, 1),
            "soc": round(84 + 8 * math.sin(ts / 5000.0), 1),
            "consumed_ah": round(-18 + 6 * math.sin(ts / 4000.0), 1),
            "remaining_mins": 0xFFFF if net > 0 else 60 * 30,
        }, rssi=-58)
        self._state.update(self._solar, {
            "battery_voltage": round(v, 2),
            "battery_charging_current": round(a, 1),
            "solar_power": round(w, 1),
            "charge_state": "Bulk" if w > 200 else "Float",
            "charger_error": "No Error",
            "yield_today": round(self._yield_wh, 1),
        }, rssi=-52)
        self._state.update(self._dcdc, {
            "device_state": "Off",
            "off_reason": ["Engine shutdown"],
            "input_voltage": round(12.8 + 0.1 * math.sin(ts / 300.0), 2),
            "output_voltage": round(v, 2),
        }, rssi=-55)
        temp, rh = self._cabin_at(ts)
        self._state.set_heater("Afterburner", {
            "cabin_temp": round(temp, 1),
            "humidity": round(rh),
            "target_temp": 21.0,
            "run_state": 2, "run_string": "Running",
            "error_state": 0, "error_string": "",
            "fan_rpm": 2400, "pump_hz": 2.6, "glow_voltage": 0.0,
            "input_voltage": 12.9,
        }, source="wifi")

    async def _run(self) -> None:
        while True:
            self._tick()
            await asyncio.sleep(TICK_S)

    async def start(self) -> None:
        self._backfill()
        self._task = asyncio.create_task(self._run(), name="demo-feeder")
        logger.warning("DEMO MODE — feeding synthetic data (VANDASH_DEMO set)")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
