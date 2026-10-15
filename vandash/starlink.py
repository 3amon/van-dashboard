"""Starlink Mini duty-cycling via a Shelly relay on its DC feed.

The Mini runs straight off the 24 V bank (12-48 V input, ~30 W) but at ~720 Wh
a day it is the van's biggest parked load. A Shelly 1 Gen4 (powered from the
same bank, dry contact in the Mini's +24 V lead) lets the dashboard cut it
when the battery is low and bring it back on a schedule so the Cloudflare
tunnel and the git auto-updater still get a few minutes online every hour.

Policy (pure, in :class:`Policy`, so it's unit-testable without hardware):

- ``healthy``: SoC >= ``soc_restore`` -> relay on, continuously.
- ``saving``:  SoC <  ``soc_low``     -> relay off, except a ``window_minutes``
  slot every ``period_minutes``. Hysteresis between the two thresholds stops
  the dish rebooting every time the fridge kicks.
- SoC unknown (BMS gone quiet AND shunt stale) -> leave things as they are;
  if nothing is known yet, fail toward connectivity (on).
- ``mode = on|off`` from the LAN-only ``/api/starlink`` endpoint overrides
  the policy until set back to ``auto``.
- A timed hold ("Stay on for 12 hours", the kiosk button) keeps the dish on
  until ``hold_until`` and then drops back to the policy by itself. It is
  persisted to a small JSON file so the deploy restarts don't forget it.

Every "on" during a saving window is sent with Shelly's ``toggle_after`` so
the relay turns itself off even if this process dies mid-window. Set the
Shelly's power-on default to ON in its own UI so a brownout can't strand you
offline.

Shelly Gen2+ RPC over plain HTTP on the van LAN (travel router keeps the
Shelly reachable while the Mini is dark):

    GET /rpc/Switch.Set?id=0&on=true&toggle_after=600
    GET /rpc/Switch.GetStatus?id=0        -> {"output": true, ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

MODES = ("auto", "on", "off")


@dataclass
class PolicyConfig:
    soc_low: float = 50.0        # enter saving below this
    soc_restore: float = 60.0    # leave saving at/above this (hysteresis)
    period_minutes: float = 60.0  # saving: one window per period
    window_minutes: float = 10.0  # saving: how long each window stays up
    soc_max_age_s: float = 1800.0  # readings older than this are "unknown"

    def __post_init__(self) -> None:
        if self.soc_restore < self.soc_low:
            raise ValueError("soc_restore must be >= soc_low")
        if self.window_minutes >= self.period_minutes:
            raise ValueError("window_minutes must be shorter than period_minutes")


@dataclass
class Decision:
    on: bool
    reason: str
    phase: str                 # healthy | saving | override | hold | unknown
    toggle_after_s: int | None = None  # Shelly self-off watchdog for windows
    next_window_ts: float | None = None


@dataclass
class Policy:
    cfg: PolicyConfig
    phase: str = "unknown"              # healthy | saving
    mode: str = "auto"
    hold_until: float | None = None      # timed "stay on" override, absolute ts
    window_started: float | None = None  # when the current saving window opened
    next_window: float | None = None
    last_on: bool | None = None

    def decide(self, soc: float | None, now: float) -> Decision:
        if self.hold_until is not None:
            if now < self.hold_until:
                return self._remember(Decision(True, "stay-on hold", "hold"))
            logger.info("Starlink: stay-on hold expired, back to %s", self.mode)
            self.hold_until = None
        if self.mode == "on":
            return self._remember(Decision(True, "manual on", "override"))
        if self.mode == "off":
            return self._remember(Decision(False, "manual off", "override"))

        if soc is None:
            if self.phase == "saving":
                return self._saving(now, "soc unknown, holding saving schedule")
            if self.last_on is None:
                return self._remember(Decision(True, "soc unknown, defaulting on", "unknown"))
            return self._remember(Decision(self.last_on, "soc unknown, holding", "unknown"))

        if self.phase != "saving" and soc < self.cfg.soc_low:
            self.phase = "saving"
            # We're online right now (we just had power), so the first window
            # is a full period away.
            self.window_started = None
            self.next_window = now + self.cfg.period_minutes * 60
            logger.info("Starlink: SoC %.0f%% < %.0f%% -> saving mode", soc, self.cfg.soc_low)
        elif self.phase == "saving" and soc >= self.cfg.soc_restore:
            self.phase = "healthy"
            self.window_started = self.next_window = None
            logger.info("Starlink: SoC %.0f%% >= %.0f%% -> back to always-on", soc, self.cfg.soc_restore)
        elif self.phase == "unknown":
            self.phase = "healthy"

        if self.phase == "healthy":
            return self._remember(Decision(True, f"soc {soc:.0f}% healthy", "healthy"))
        return self._saving(now, f"soc {soc:.0f}% low")

    def _saving(self, now: float, why: str) -> Decision:
        window_s = self.cfg.window_minutes * 60
        period_s = self.cfg.period_minutes * 60
        if self.next_window is None:
            self.next_window = now + period_s
        if self.window_started is not None:
            if now - self.window_started < window_s:
                remaining = int(window_s - (now - self.window_started))
                return self._remember(Decision(
                    True, f"{why}: sync window open", "saving",
                    toggle_after_s=max(remaining, 1), next_window_ts=self.next_window))
            self.window_started = None
        if now >= self.next_window:
            self.window_started = now
            self.next_window = now + period_s
            logger.info("Starlink: opening %.0f min sync window", self.cfg.window_minutes)
            return self._remember(Decision(
                True, f"{why}: sync window open", "saving",
                toggle_after_s=int(window_s), next_window_ts=self.next_window))
        return self._remember(Decision(
            False, f"{why}: waiting for next window", "saving",
            next_window_ts=self.next_window))

    def _remember(self, d: Decision) -> Decision:
        self.last_on = d.on
        return d


def pick_soc(devices: dict[str, dict[str, Any]], now: float, max_age_s: float) -> tuple[float | None, str | None]:
    """BMS SoC first (it's the one the dashboard headlines), shunt as fallback.

    The shunt reads ~13 points lower than the Epoch BMS until it's re-synced,
    so a config threshold tuned for one source is conservative for the other —
    acceptable, since the shunt only takes over when the BMS has gone quiet.
    """
    for kind in ("battery", "shunt"):
        for d in devices.values():
            if d.get("kind") == kind and d.get("soc") is not None and now - d["ts"] <= max_age_s:
                return float(d["soc"]), kind
    return None, None


class ShellyRelay:
    """Minimal Shelly Gen2+ RPC client for one switch channel."""

    def __init__(self, host: str, channel: int = 0, timeout_s: float = 5.0) -> None:
        self._base = f"http://{host}/rpc"
        self._channel = channel
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _rpc(self, method: str, **params: Any) -> dict[str, Any]:
        query = "&".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in params.items())
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(f"{self._base}/{method}?{query}") as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get(self) -> bool:
        status = await self._rpc("Switch.GetStatus", id=self._channel)
        return bool(status.get("output"))

    async def set(self, on: bool, toggle_after_s: int | None = None) -> None:
        params: dict[str, Any] = {"id": self._channel, "on": on}
        if on and toggle_after_s:
            params["toggle_after"] = toggle_after_s
        await self._rpc("Switch.Set", **params)


@dataclass
class StarlinkController:
    """Background task: policy -> relay, with status for /api/state."""

    relay: ShellyRelay
    state: Any                      # PowerState (reads .devices)
    policy: Policy
    tick_s: float = 60.0
    _task: asyncio.Task | None = field(default=None, repr=False)
    _last: dict[str, Any] = field(default_factory=dict, repr=False)
    _relay_on: bool | None = None
    _relay_error: str | None = None

    hold_path: Path | None = None   # persisted hold_until (survives restarts)

    def __post_init__(self) -> None:
        if self.hold_path is not None and self.hold_path.is_file():
            try:
                until = json.loads(self.hold_path.read_text()).get("hold_until")
                if until and until > time.time():
                    self.policy.hold_until = float(until)
                    logger.info("Starlink: restored stay-on hold until %s", time.ctime(until))
            except (OSError, ValueError):
                logger.exception("Could not read %s", self.hold_path)

    @property
    def mode(self) -> str:
        return self.policy.mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.policy.mode = mode
        logger.info("Starlink mode -> %s", mode)

    def set_hold(self, hours: float | None) -> None:
        """Keep the dish on for `hours` from now; None cancels the hold."""
        if hours is None:
            self.policy.hold_until = None
            logger.info("Starlink: stay-on hold cancelled")
        else:
            if not 0 < hours <= 24 * 7:
                raise ValueError("hold_hours must be between 0 and 168")
            self.policy.hold_until = time.time() + hours * 3600
            logger.info("Starlink: stay on for %.1f h (until %s)", hours, time.ctime(self.policy.hold_until))
        if self.hold_path is not None:
            try:
                self.hold_path.parent.mkdir(parents=True, exist_ok=True)
                self.hold_path.write_text(json.dumps({"hold_until": self.policy.hold_until}))
            except OSError:
                logger.exception("Could not persist the hold to %s", self.hold_path)

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.policy.mode,
            "hold_until": self.policy.hold_until,
            "relay_on": self._relay_on,
            "relay_error": self._relay_error,
            **self._last,
        }

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
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - never let this kill the app
                logger.exception("starlink tick failed")
            await asyncio.sleep(self.tick_s)

    async def tick(self, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        soc, source = pick_soc(self.state.devices, now, self.policy.cfg.soc_max_age_s)
        decision = self.policy.decide(soc, now)
        self._last = {
            "phase": decision.phase,
            "want_on": decision.on,
            "reason": decision.reason,
            "soc": soc,
            "soc_source": source,
            "next_window_ts": decision.next_window_ts,
            "ts": now,
        }
        try:
            actual = await self.relay.get()
            # Re-send "on" inside a window every tick so toggle_after tracks the
            # remaining time; otherwise only touch the relay when it disagrees.
            if actual != decision.on or (decision.on and decision.toggle_after_s):
                await self.relay.set(decision.on, decision.toggle_after_s)
                if actual != decision.on:
                    logger.info("Starlink relay -> %s (%s)", "on" if decision.on else "off", decision.reason)
            self._relay_on = decision.on
            self._relay_error = None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self._relay_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Starlink relay unreachable: %s", self._relay_error)
        return decision


def from_config(cfg: dict[str, Any], state: Any, data_dir: Path | None = None) -> StarlinkController | None:
    """Build from the ``[starlink]`` config table; None when there's no host."""
    host = cfg.get("host")
    if not host:
        return None
    policy_cfg = PolicyConfig(
        soc_low=float(cfg.get("soc_low", 50)),
        soc_restore=float(cfg.get("soc_restore", 60)),
        period_minutes=float(cfg.get("period_minutes", 60)),
        window_minutes=float(cfg.get("window_minutes", 10)),
        soc_max_age_s=float(cfg.get("soc_max_age_minutes", 30)) * 60,
    )
    policy = Policy(policy_cfg, mode=cfg.get("mode", "auto"))
    if policy.mode not in MODES:
        raise ValueError(f"[starlink] mode must be one of {MODES}")
    return StarlinkController(
        relay=ShellyRelay(host, channel=int(cfg.get("channel", 0))),
        state=state,
        policy=policy,
        tick_s=float(cfg.get("tick_seconds", 60)),
        hold_path=(data_dir / "starlink-hold.json") if data_dir is not None else None,
    )
