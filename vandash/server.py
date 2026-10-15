"""aiohttp server: serves the dashboard views and the live state API."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
import tomllib
from pathlib import Path

from aiohttp import web

from vandash import pihealth, starlink, tunnel
from vandash.uplink import Uplink
from vandash.afterburner import AfterburnerClient
from vandash.afterburner_ble import AfterburnerBleClient
from vandash.ble import BleScanner
from vandash.collector import Collector, ConfiguredDevice, PowerState
from vandash.demo import DemoFeeder
from vandash.epoch import EpochBmsClient
from vandash.storage import HistoryStore
import hmac

logger = logging.getLogger(__name__)

PI_HEALTH_INTERVAL = 15  # seconds; matches the standalone van-pihealth logger
INTERNET_CHECK_INTERVAL = 120  # one tiny TCP handshake every two minutes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
VIEWS_DIR = STATIC_DIR / "views"


def load_config() -> dict:
    with open(PROJECT_ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


async def index(request: web.Request) -> web.Response:
    dest = f"/v/{request.app['default_view']}"
    if request.query_string:  # preserve ?kiosk (and ?replay) through the redirect
        dest += "?" + request.query_string
    raise web.HTTPFound(dest)


async def view(request: web.Request) -> web.FileResponse:
    name = request.match_info["view"]
    path = VIEWS_DIR / f"{name}.html"
    if not name.isalnum() or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


def _pi_health_view(sample: dict) -> dict:
    """Shape a pihealth.sample() into the compact object the tile consumes."""
    thr = sample["throttled"]
    return {
        "ts": sample["epoch"],
        "ext5v": sample["ext5v"],
        "board_w": sample["board_w"],
        "temp": sample["temp"],
        "load1": sample["load1"],
        "wifi_ssid": sample.get("wifi_ssid"),
        "wifi_dbm": sample.get("wifi_dbm"),
        "throttled_hex": None if thr is None else f"0x{thr:x}",
        "flags": pihealth.throttled_labels(thr) if thr else [],
        "uv_now": bool(thr and thr & (1 << 0)),
        "throttled_now": bool(thr and thr & (1 << 2)),
        "uv_occurred": bool(thr and thr & (1 << 16)),
        "throttle_occurred": bool(thr and thr & (1 << 18)),
    }


async def _pi_health_loop(app: web.Application) -> None:
    """Poll the Pi's PMIC every PI_HEALTH_INTERVAL and cache it for /api/state.

    vcgencmd is a blocking subprocess, so sample in a thread to keep the event
    loop responsive. On a non-Pi (e.g. demo on a laptop) vcgencmd is absent and
    the fields come back None — the tile just shows "no data".
    """
    while True:
        try:
            sample = await asyncio.to_thread(pihealth.sample)
            app["pi_health"] = _pi_health_view(sample)
        except Exception:  # noqa: BLE001 - never let this kill the app
            logger.exception("pi-health sample failed")
        await asyncio.sleep(PI_HEALTH_INTERVAL)


async def _internet_check_loop(app: web.Application) -> None:
    """Check WAN reachability without downloading content or spawning a process."""
    while True:
        started = time.monotonic()
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("1.1.1.1", 443), timeout=5,
            )
            app["internet_health"] = {
                "internet_ok": True,
                "internet_latency_ms": round((time.monotonic() - started) * 1000),
                "internet_ts": time.time(),
            }
        except (OSError, asyncio.TimeoutError):
            app["internet_health"] = {
                "internet_ok": False,
                "internet_latency_ms": None,
                "internet_ts": time.time(),
            }
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        await asyncio.sleep(INTERNET_CHECK_INTERVAL)


def state_payload(app: web.Application) -> dict:
    """Everything /api/state serves — also what the uplink pushes to the mirror."""
    snapshot = app["state"].snapshot()
    pi = app.get("pi_health")
    snapshot["pi"] = ({**pi, **app.get("internet_health", {})} if pi else pi)
    snapshot["demo"] = app["demo"] is not None
    snapshot["sleep_until"] = app["sleep_until"]
    ctl = app.get("starlink")
    snapshot["starlink"] = ctl.status() if ctl is not None else None
    up = app.get("uplink")
    snapshot["uplink"] = up.status() if up is not None else None
    return snapshot


async def api_state(request: web.Request) -> web.Response:
    app = request.app
    if app["mirror"] is None:
        return web.json_response(state_payload(app))
    # Mirror: the van's last pushed snapshot, with history from our own store
    # (which the pushes keep filled) and our clock, so device ages go stale
    # honestly once the van drops off.
    pushed = app["mirror"].get("snapshot") or {}
    snapshot = app["state"].snapshot()
    snapshot["devices"] = pushed.get("devices", {})
    for key in ("pi", "sleep_until", "starlink"):
        snapshot[key] = pushed.get(key)
    snapshot["demo"] = False
    last_push = app["mirror"].get("last_push_ts")
    snapshot["mirror"] = {
        "last_push_ts": last_push,
        "van_now": pushed.get("now"),
        "stale": last_push is None or time.time() - last_push > 180,
    }
    return web.json_response(snapshot)


async def api_ingest(request: web.Request) -> web.Response:
    """Mirror mode: the van pushes {snapshot, rows}; we answer with our newest t."""
    mirror = request.app["mirror"]
    if mirror is None:
        raise web.HTTPNotFound(text="not a mirror")
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {mirror['token']}"):
        raise web.HTTPUnauthorized(text="bad token")
    body = await request.json()
    rows = [{**r, "t": int(r["t"])} for r in body.get("rows", [])
            if isinstance(r, dict) and isinstance(r.get("t"), (int, float))]
    request.app["state"].ingest(body.get("snapshot", {}).get("devices", {}), rows)
    mirror["snapshot"] = body.get("snapshot", {})
    mirror["last_push_ts"] = time.time()
    return web.json_response({"ok": True, "last_t": request.app["store"].max_t(), "rows": len(rows)})


async def api_starlink(request: web.Request) -> web.Response:
    """LAN-only. {"mode": "auto"|"on"|"off"} overrides the SoC policy;
    {"hold_hours": 12} keeps the dish on for that long; {"hold": false} cancels."""
    if request.headers.get("Cf-Connecting-Ip") or request.app["mirror"] is not None:
        raise web.HTTPForbidden(text="read-only over the tunnel")
    ctl = request.app.get("starlink")
    if ctl is None:
        raise web.HTTPNotFound(text="no [starlink] relay configured")
    body = await request.json()
    try:
        if "hold_hours" in body:
            ctl.set_hold(float(body["hold_hours"]))
        elif body.get("hold") is False:
            ctl.set_hold(None)
        else:
            ctl.set_mode(str(body.get("mode", "auto")))
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    await ctl.tick()  # apply immediately rather than on the next minute tick
    return web.json_response(ctl.status())


# ── bedtime: kill the panel backlight until morning or a wake tap ──────────
# The kiosk user (video group) can write brightness directly; on a non-Pi dev
# box there's no backlight device and sleep is UI-overlay-only.

DEFAULT_BRIGHTNESS = 15  # panel default, used if the saved level is unusable


def _brightness_file() -> Path | None:
    for p in sorted(Path("/sys/class/backlight").glob("*/brightness")):
        return p
    return None


def _next_wake(hour: int) -> float:
    now = dt.datetime.now()
    wake = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if wake <= now:
        wake += dt.timedelta(days=1)
    return wake.timestamp()


def _display_sleep(app: web.Application) -> None:
    app["sleep_until"] = _next_wake(app["wake_hour"])
    bl = _brightness_file()
    if bl is None:
        return
    try:
        if app["wake_brightness"] is None:  # don't clobber the saved level
            level = int(bl.read_text())
            app["wake_brightness"] = level if level > 0 else DEFAULT_BRIGHTNESS
        bl.write_text("0")
    except OSError:
        logger.exception("Could not turn the backlight off")


def _display_wake(app: web.Application) -> None:
    app["sleep_until"] = None
    bl = _brightness_file()
    if bl is None:
        return
    try:
        bl.write_text(str(app["wake_brightness"] or DEFAULT_BRIGHTNESS))
    except OSError:
        logger.exception("Could not turn the backlight on")
    app["wake_brightness"] = None


async def api_sleep(request: web.Request) -> web.Response:
    # The public tunnel is view-only: nobody on the internet gets to kill (or
    # wake) the van's panel. Cloudflare stamps Cf-Connecting-Ip on everything
    # it proxies; the kiosk and LAN talk to us directly, without it.
    if request.headers.get("Cf-Connecting-Ip") or request.app["mirror"] is not None:
        raise web.HTTPForbidden(text="read-only over the tunnel")
    body = await request.json()
    if body.get("sleep"):
        _display_sleep(request.app)
        logger.info(
            "Display sleeping until %s (or a tap)",
            dt.datetime.fromtimestamp(request.app["sleep_until"]),
        )
    else:
        _display_wake(request.app)
        logger.info("Display woken")
    return web.json_response({"sleep_until": request.app["sleep_until"]})


async def _sleep_loop(app: web.Application) -> None:
    while True:
        until = app["sleep_until"]
        if until is not None and time.time() >= until:
            logger.info("Morning — waking the display")
            _display_wake(app)
        await asyncio.sleep(20)


async def api_day(request: web.Request) -> web.Response:
    """Bucketed series for one local calendar day (days_ago=0 → today)."""
    try:
        days_ago = min(max(int(request.query.get("days_ago", 0)), 0), 365)
    except ValueError:
        raise web.HTTPBadRequest(text="days_ago must be an integer")
    try:
        bucket = min(max(int(request.query.get("bucket", 300)), 30), 3600)
    except ValueError:
        raise web.HTTPBadRequest(text="bucket must be an integer")
    day = dt.date.today() - dt.timedelta(days=days_ago)
    start = dt.datetime.combine(day, dt.time.min).timestamp()
    rows = request.app["store"].day_series(start, start + 86400, bucket=bucket)
    return web.json_response({"start": start, "rows": rows})


async def api_solar_max(request: web.Request) -> web.Response:
    """Daily maximum solar power for the most recent local calendar days."""
    try:
        days = min(max(int(request.query.get("days", 7)), 2), 31)
    except ValueError:
        raise web.HTTPBadRequest(text="days must be an integer")
    today = dt.date.today()
    first_day = today - dt.timedelta(days=days - 1)
    start = dt.datetime.combine(first_day, dt.time.min).timestamp()
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min).timestamp()
    stats = request.app["store"].daily_solar_stats(
        start, end, request.app["state"].bucket_seconds
    )
    rows = []
    for offset in range(days):
        day = first_day + dt.timedelta(days=offset)
        day_start = dt.datetime.combine(day, dt.time.min).timestamp()
        values = stats.get(day.isoformat(), {})
        rows.append({"day": day.isoformat(), "t": day_start + 43200,
                     "w": values.get("w"), "kwh": values.get("kwh")})
    return web.json_response({"start": start, "end": end, "rows": rows})


def build_app(config: dict) -> web.Application:
    history_cfg = config.get("history", {})
    db_path = history_cfg.get("db", "data/vandash.db")
    store = HistoryStore(PROJECT_ROOT / db_path)
    state = PowerState(
        bucket_seconds=history_cfg.get("bucket_seconds", 30),
        retain_hours=history_cfg.get("retain_hours", 6),
        store=store,
    )
    devices = [
        ConfiguredDevice(d["name"], d["kind"], d["key"])
        for d in config.get("devices", [])
    ]
    collector_cfg = config.get("collector", {})
    scanner = BleScanner(adapter=collector_cfg.get("adapter"))
    collector = Collector(
        devices,
        state,
        scanner,
        min_rssi=collector_cfg.get("min_rssi", -85),
    )

    heater_cfg = config.get("afterburner", {})
    heater = (
        AfterburnerClient(
            heater_cfg["host"],
            state,
            port=heater_cfg.get("port", 81),
        )
        if heater_cfg.get("host") and heater_cfg.get("wifi", True)
        else None
    )

    heater_ble = (
        AfterburnerBleClient(state, scanner) if heater_cfg.get("ble", False) else None
    )

    epoch_cfg = config.get("epoch", {})
    epoch = (
        EpochBmsClient(
            epoch_cfg.get("name", "Epoch"),
            epoch_cfg["address"],
            state,
            scanner,
            poll_s=epoch_cfg.get("poll_minutes", 5) * 60,
        )
        if epoch_cfg.get("address")
        else None
    )

    # Starlink relay (Shelly) duty-cycling — only when [starlink] names a host.
    starlink_ctl = starlink.from_config(
        config.get("starlink", {}), state, data_dir=(PROJECT_ROOT / db_path).parent)

    # Mirror mode ([mirror] token): no radios, just receive pushes from the van.
    mirror_cfg = config.get("mirror", {})
    mirror = {"token": mirror_cfg["token"], "snapshot": None, "last_push_ts": None} \
        if mirror_cfg.get("token") else None

    # VANDASH_DEMO=1 → feed synthetic data instead of scanning BLE (bench/UI work).
    demo = DemoFeeder(state) if os.environ.get("VANDASH_DEMO") else None

    @web.middleware
    async def no_store(request: web.Request, handler):
        response = await handler(request)
        # Local kiosk app: freshness beats caching, and stale JS after an
        # update is confusing. Assets are tiny.
        if request.path.startswith(("/static/", "/v/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[no_store])
    app["state"] = state
    app["scanner"] = scanner
    app["collector"] = collector
    app["demo"] = demo
    app["heater"] = heater
    app["heater_ble"] = heater_ble
    app["epoch"] = epoch
    app["starlink"] = starlink_ctl
    app["mirror"] = mirror
    app["uplink"] = None
    app["store"] = store
    app["pi_health"] = None
    app["internet_health"] = {}
    app["default_view"] = config.get("server", {}).get("default_view", "sky")
    app["wake_hour"] = config.get("server", {}).get("wake_hour", 7)
    app["sleep_until"] = None
    app["wake_brightness"] = None
    app.router.add_get("/", index)
    app.router.add_get("/v/{view}", view)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/sleep", api_sleep)
    app.router.add_post("/api/starlink", api_starlink)
    app.router.add_post("/api/ingest", api_ingest)
    app.router.add_get("/api/day", api_day)
    app.router.add_get("/api/solar-max", api_solar_max)
    app.router.add_static("/static/", STATIC_DIR)

    async def on_startup(app: web.Application) -> None:
        if app["mirror"] is not None:
            logger.warning("MIRROR MODE — serving pushes from the van, no local radios")
            return
        uplink_cfg = config.get("uplink", {})
        if uplink_cfg.get("url") and uplink_cfg.get("token"):
            app["uplink"] = Uplink(
                uplink_cfg["url"], uplink_cfg["token"],
                snapshot=lambda: state_payload(app), store=store,
                online=lambda: app.get("internet_health", {}).get("internet_ok") is True,
                interval_s=float(uplink_cfg.get("interval_seconds", 30)),
            )
            await app["uplink"].start()
        # Pi self-monitoring runs in both live and demo modes — it watches the
        # box, not the van, so it's useful even on the bench.
        app["pi_health_task"] = asyncio.create_task(_pi_health_loop(app))
        app["internet_check_task"] = asyncio.create_task(_internet_check_loop(app))
        app["sleep_task"] = asyncio.create_task(_sleep_loop(app))
        # Relay control runs in demo mode too (it's just an HTTP client), so
        # the schedule can be exercised on the bench against a real Shelly.
        if app["starlink"] is not None:
            await app["starlink"].start()
        # Remote-access tunnel (no-op unless this is the Pi / VANDASH_TUNNEL=1)
        app["tunnel_task"] = tunnel.maybe_start(
            PROJECT_ROOT, port=config.get("server", {}).get("port", 8080))
        if app["demo"] is not None:
            await app["demo"].start()
            return
        await app["scanner"].start()
        await app["collector"].start()
        if app["heater"] is not None:
            await app["heater"].start()
        if app["heater_ble"] is not None:
            await app["heater_ble"].start()
        if app["epoch"] is not None:
            await app["epoch"].start()

    async def on_cleanup(app: web.Application) -> None:
        if app["mirror"] is not None:
            state.flush()
            store.close()
            return
        if app["uplink"] is not None:
            await app["uplink"].stop()
        for task_key in ("pi_health_task", "internet_check_task", "sleep_task",
                         "tunnel_task"):
            task = app.get(task_key)
            if task is not None:
                task.cancel()
        if app["starlink"] is not None:
            await app["starlink"].stop()
        if app["demo"] is not None:
            await app["demo"].stop()
        else:
            if app["epoch"] is not None:
                await app["epoch"].stop()
            if app["heater_ble"] is not None:
                await app["heater_ble"].stop()
            if app["heater"] is not None:
                await app["heater"].stop()
            await app["collector"].stop()
            await app["scanner"].stop()
        state.flush()
        store.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    server_cfg = config.get("server", {})
    web.run_app(
        build_app(config),
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 8080),
    )


if __name__ == "__main__":
    main()
