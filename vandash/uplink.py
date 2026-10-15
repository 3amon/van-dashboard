"""Push the van's state to a mirror so it's viewable while Starlink is off.

The Pi can only be reached while the dish is up, which is exactly when you
least need to look. So while there is internet, the Pi POSTs its current
snapshot (devices, Starlink relay status, Pi health) plus any history rows
the mirror doesn't have yet to ``[uplink] url`` every ``interval_seconds``.
The mirror (another vandash in mirror mode, see server.py) answers with the
newest history timestamp it holds, so after an outage the Pi backfills from
its own SQLite in chunks until the mirror has caught up.

Config on the Pi (machine-local config.toml)::

    [uplink]
    url = "https://van.example.net/api/ingest"
    token = "<shared secret, same as [mirror] token on the mirror>"
    interval_seconds = 30
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

CHUNK_ROWS = 3000  # ~25 h of 30 s buckets per push while backfilling


class Uplink:
    def __init__(
        self,
        url: str,
        token: str,
        snapshot: Callable[[], dict[str, Any]],
        store: Any,
        online: Callable[[], bool],
        interval_s: float = 30.0,
    ) -> None:
        self._url = url
        self._token = token
        self._snapshot = snapshot
        self._store = store
        self._online = online
        self._interval = interval_s
        self._ack_t: float | None = None   # newest row the mirror confirmed
        self._task: asyncio.Task | None = None
        self.last_ok_ts: float | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {"url": self._url, "last_ok_ts": self.last_ok_ts,
                "last_error": self.last_error, "ack_t": self._ack_t}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            while True:
                try:
                    if self._online():
                        caught_up = await self.push(session)
                        if not caught_up:
                            continue  # keep backfilling without sleeping
                except Exception:  # noqa: BLE001 - never let this kill the app
                    logger.exception("uplink push failed")
                await asyncio.sleep(self._interval)

    def _rows_after(self, t: float | None) -> list[dict[str, Any]]:
        rows = self._store.load_since(0 if t is None else t + 1)
        return rows[:CHUNK_ROWS]

    async def push(self, session: aiohttp.ClientSession) -> bool:
        """One push. Returns True when the mirror has every row we have."""
        rows = self._rows_after(self._ack_t) if self._ack_t is not None else []
        body = {"snapshot": self._snapshot(), "rows": rows, "sent_at": time.time()}
        try:
            async with session.post(
                self._url, json=body, headers={"Authorization": f"Bearer {self._token}"},
            ) as resp:
                if resp.status != 200:
                    self.last_error = f"HTTP {resp.status}: {(await resp.text())[:120]}"
                    logger.warning("uplink: %s", self.last_error)
                    return True
                reply = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("uplink: %s", self.last_error)
            return True
        self.last_ok_ts = time.time()
        self.last_error = None
        mirror_t = reply.get("last_t")
        self._ack_t = -1.0 if mirror_t is None else float(mirror_t)
        if rows and rows[-1]["t"] > self._ack_t:
            # The mirror didn't store what we sent — don't loop on it.
            logger.warning("uplink: mirror last_t %s behind sent %s", mirror_t, rows[-1]["t"])
            return True
        return len(self._rows_after(self._ack_t)) == 0
