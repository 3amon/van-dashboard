"""WebSocket client for the Afterburner diesel heater controller.

The Afterburner pushes JSON deltas over a WebSocket on port 81; sending
{"Refresh": 1} makes it re-emit full state. We merge deltas into a field dict
and refresh periodically so readings stay verifiably fresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from vandash.collector import PowerState

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_S = 30

# Afterburner JSON key -> our field name
FIELDS = {
    "TempCurrent": "cabin_temp",
    "TempDesired": "target_temp",
    "RunState": "run_state",
    "RunString": "run_string",
    "ErrorState": "error_state",
    "ErrorString": "error_string",
    "InputVoltage": "input_voltage",
    "FanRPM": "fan_rpm",
    "PumpActual": "pump_hz",
    "GlowVoltage": "glow_voltage",
    "GlowCurrent": "glow_current",
    "Humidity": "humidity",
    "Pressure": "pressure",
    "Altitude": "altitude",
}


class AfterburnerClient:
    def __init__(
        self, host: str, state: PowerState, port: int = 81, name: str = "Afterburner"
    ) -> None:
        self._uri = f"ws://{host}:{port}/"
        self._state = state
        self._name = name
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="afterburner-client")

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
            try:
                await self._session()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                # Log the first failure, then once every ~5 minutes of retrying
                if failures == 1 or failures % 60 == 0:
                    logger.warning(
                        "Afterburner (%s): %s — retrying every 5s (%d failures so far)",
                        self._uri, e, failures,
                    )
            await asyncio.sleep(5)

    async def _session(self) -> None:
        async with websockets.connect(self._uri, open_timeout=10) as ws:
            logger.info("Afterburner connected: %s", self._uri)
            fields: dict = {}
            last_refresh = 0.0
            while True:
                if time.time() - last_refresh > REFRESH_INTERVAL_S:
                    await ws.send(json.dumps({"Refresh": 1}))
                    last_refresh = time.time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=REFRESH_INTERVAL_S + 5)
                except TimeoutError:
                    continue  # quiet heater; loop back and refresh
                delta = json.loads(msg)
                for key, value in delta.items():
                    field = FIELDS.get(key)
                    if field is not None:
                        fields[field] = value.strip() if isinstance(value, str) else value
                if fields:
                    self._state.set_heater(self._name, fields, source="wifi")
