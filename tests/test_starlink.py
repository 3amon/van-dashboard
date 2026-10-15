import asyncio
import unittest

from vandash.starlink import Policy, PolicyConfig, StarlinkController, pick_soc


class FakeRelay:
    def __init__(self, on=False):
        self.on = on
        self.calls = []

    async def get(self):
        return self.on

    async def set(self, on, toggle_after_s=None):
        self.calls.append((on, toggle_after_s))
        self.on = on


class FakeState:
    def __init__(self, devices):
        self.devices = devices


def make_policy(**kw):
    return Policy(PolicyConfig(soc_low=50, soc_restore=60, period_minutes=60,
                               window_minutes=10, **kw))


class PolicyTest(unittest.TestCase):
    def test_healthy_stays_on(self):
        p = make_policy()
        d = p.decide(80, 1000)
        self.assertTrue(d.on)
        self.assertEqual(d.phase, "healthy")
        self.assertIsNone(d.toggle_after_s)

    def test_low_soc_cuts_power_then_opens_window_each_period(self):
        p = make_policy()
        t0 = 10_000
        p.decide(80, t0)
        d = p.decide(45, t0)                      # drops below soc_low
        self.assertFalse(d.on)
        self.assertEqual(d.phase, "saving")
        self.assertEqual(d.next_window_ts, t0 + 3600)
        self.assertFalse(p.decide(45, t0 + 3599).on)
        d = p.decide(45, t0 + 3600)               # window opens
        self.assertTrue(d.on)
        self.assertEqual(d.toggle_after_s, 600)
        d = p.decide(45, t0 + 3600 + 300)         # mid-window: watchdog tracks remaining
        self.assertTrue(d.on)
        self.assertEqual(d.toggle_after_s, 300)
        d = p.decide(45, t0 + 3600 + 600)         # window closes
        self.assertFalse(d.on)
        self.assertEqual(d.next_window_ts, t0 + 7200)
        self.assertTrue(p.decide(45, t0 + 7200).on)

    def test_hysteresis_between_thresholds(self):
        p = make_policy()
        p.decide(45, 0)
        self.assertEqual(p.phase, "saving")
        p.decide(55, 1)                            # between low and restore: still saving
        self.assertEqual(p.phase, "saving")
        self.assertFalse(p.decide(55, 2).on)
        d = p.decide(60, 3)                        # restored
        self.assertEqual(d.phase, "healthy")
        self.assertTrue(d.on)
        self.assertIsNone(p.next_window)

    def test_unknown_soc_defaults_on_then_holds(self):
        p = make_policy()
        self.assertTrue(p.decide(None, 0).on)      # never known: fail toward connectivity
        p.decide(45, 1)
        self.assertFalse(p.decide(None, 2).on)     # saving schedule survives a quiet BMS
        self.assertTrue(p.decide(None, 3601).on)   # ...including its windows

    def test_manual_override(self):
        p = make_policy()
        p.decide(45, 0)
        p.mode = "on"
        d = p.decide(45, 1)
        self.assertTrue(d.on)
        self.assertEqual(d.phase, "override")
        p.mode = "off"
        self.assertFalse(p.decide(95, 2).on)
        p.mode = "auto"
        self.assertEqual(p.decide(45, 3).phase, "saving")

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            PolicyConfig(soc_low=60, soc_restore=50)
        with self.assertRaises(ValueError):
            PolicyConfig(period_minutes=10, window_minutes=10)


class PickSocTest(unittest.TestCase):
    def test_prefers_fresh_bms_then_shunt(self):
        now = 1000
        devices = {
            "Epoch": {"kind": "battery", "soc": 70, "ts": now - 10},
            "Shunt": {"kind": "shunt", "soc": 57, "ts": now - 10},
        }
        self.assertEqual(pick_soc(devices, now, 1800), (70.0, "battery"))
        devices["Epoch"]["ts"] = now - 7200                     # BMS gone quiet
        self.assertEqual(pick_soc(devices, now, 1800), (57.0, "shunt"))
        devices["Shunt"]["soc"] = None
        self.assertEqual(pick_soc(devices, now, 1800), (None, None))


class ControllerTest(unittest.TestCase):
    def test_tick_only_writes_relay_when_it_disagrees(self):
        relay = FakeRelay(on=True)
        state = FakeState({"Epoch": {"kind": "battery", "soc": 80, "ts": 0}})
        ctl = StarlinkController(relay=relay, state=state, policy=make_policy())
        asyncio.run(ctl.tick(now=1))
        self.assertEqual(relay.calls, [])               # already on, nothing sent
        state.devices["Epoch"]["soc"] = 40
        asyncio.run(ctl.tick(now=2))
        self.assertEqual(relay.calls, [(False, None)])  # cut
        state.devices["Epoch"]["ts"] = 3600             # keep the reading fresh
        asyncio.run(ctl.tick(now=2 + 3600))
        self.assertEqual(relay.calls[-1], (True, 600))  # window with watchdog
        st = ctl.status()
        self.assertEqual(st["phase"], "saving")
        self.assertEqual(st["soc_source"], "battery")
        self.assertTrue(st["relay_on"])

    def test_relay_failure_is_reported_not_raised(self):
        class DeadRelay:
            async def get(self):
                raise OSError("no route to host")

            async def set(self, on, toggle_after_s=None):
                raise AssertionError("should not be called")

        state = FakeState({})
        ctl = StarlinkController(relay=DeadRelay(), state=state, policy=make_policy())
        asyncio.run(ctl.tick(now=1))
        self.assertIn("no route to host", ctl.status()["relay_error"])


class HoldTest(unittest.TestCase):
    def test_hold_forces_on_then_expires_back_to_policy(self):
        p = make_policy()
        p.decide(40, 0)                               # saving
        p.hold_until = 1000
        d = p.decide(40, 10)
        self.assertTrue(d.on)
        self.assertEqual(d.phase, "hold")
        d = p.decide(40, 1000)                        # expired
        self.assertIsNone(p.hold_until)
        self.assertEqual(d.phase, "saving")
        self.assertFalse(d.on)

    def test_controller_persists_and_restores_hold(self):
        import tempfile, pathlib, time
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "hold.json"
            ctl = StarlinkController(relay=FakeRelay(), state=FakeState({}), policy=make_policy(), hold_path=path)
            ctl.set_hold(12)
            self.assertTrue(path.is_file())
            ctl2 = StarlinkController(relay=FakeRelay(), state=FakeState({}), policy=make_policy(), hold_path=path)
            self.assertAlmostEqual(ctl2.policy.hold_until, time.time() + 12 * 3600, delta=5)
            ctl2.set_hold(None)
            ctl3 = StarlinkController(relay=FakeRelay(), state=FakeState({}), policy=make_policy(), hold_path=path)
            self.assertIsNone(ctl3.policy.hold_until)
            with self.assertRaises(ValueError):
                ctl.set_hold(0)
