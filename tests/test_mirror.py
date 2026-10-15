"""Van -> mirror push, end to end, with two real aiohttp apps in one process."""
import os
import tempfile
import time
import unittest

import aiohttp
from aiohttp.test_utils import TestServer

from vandash.server import build_app
from vandash.uplink import Uplink


def base_config(directory, name, **extra):
    cfg = {"server": {"port": 0}, "history": {"db": os.path.join(directory, name + ".db"),
                                                "bucket_seconds": 30, "retain_hours": 8},
           "afterburner": {"wifi": False, "ble": False}, "devices": []}
    cfg.update(extra)
    return cfg


class MirrorPushTest(unittest.IsolatedAsyncioTestCase):
    async def test_rows_and_snapshot_arrive_and_backfill(self):
        os.environ["VANDASH_DEMO"] = "1"  # van app: synthetic feed, no BLE
        try:
            with tempfile.TemporaryDirectory() as d:
                mirror_app = build_app(base_config(d, "mirror", mirror={"token": "s3cret"}))
                van_app = build_app(base_config(d, "van"))
                async with TestServer(mirror_app) as mirror, TestServer(van_app) as van:
                    # Seed the van's history with 5 old rows + 1 device, as if it had run.
                    now = int(time.time()) // 30 * 30
                    rows = [{"t": now - 300 + 30 * i, "w": 10.0 * i, "v": 26.5, "a": None,
                             "temp": None, "rh": None, "soc": 90.0, "solar_wh": None,
                             "alt_w": None, "ac_w": None} for i in range(5)]
                    for r in rows:
                        van_app["store"].upsert(r)
                    van_app["state"].devices["Epoch"] = {"kind": "battery", "soc": 90, "ts": now}

                    url = f"http://127.0.0.1:{mirror.port}/api/ingest"
                    from vandash.server import state_payload
                    up = Uplink(url, "s3cret", snapshot=lambda: state_payload(van_app),
                                store=van_app["store"], online=lambda: True)
                    async with aiohttp.ClientSession() as session:
                        first = await up.push(session)       # learns mirror's last_t (None)
                        self.assertFalse(first)              # rows still to send
                        second = await up.push(session)      # backfills the 5 rows
                        self.assertTrue(second)
                        self.assertIsNone(up.last_error)

                        async with session.get(f"http://127.0.0.1:{mirror.port}/api/state") as r:
                            st = await r.json()
                    self.assertIn("Epoch", st["devices"])
                    self.assertEqual(len(st["history"]), 5)
                    self.assertFalse(st["mirror"]["stale"])
                    self.assertEqual(mirror_app["store"].max_t(), int(rows[-1]["t"]))

                    # Bad token is refused; the mirror is read-only for controls.
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json={"snapshot": {}, "rows": []},
                                                headers={"Authorization": "Bearer nope"}) as r:
                            self.assertEqual(r.status, 401)
                        async with session.post(f"http://127.0.0.1:{mirror.port}/api/sleep",
                                                json={"sleep": True}) as r:
                            self.assertEqual(r.status, 403)
                        # The van itself is not a mirror: ingest is 404 there.
                        async with session.post(f"http://127.0.0.1:{van.port}/api/ingest",
                                                json={}) as r:
                            self.assertEqual(r.status, 404)
        finally:
            os.environ.pop("VANDASH_DEMO", None)
