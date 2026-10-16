"""
Tests for the event scheduling of the coordinator (__init__.py) with sun
keyframes and groups. Home Assistant is not installed here, so the few HA
modules the package imports are replaced by minimal stand-ins.

Run from the repository root:
    python -m unittest discover -s tests
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")


import ha_stubs  # noqa: E402,F401  (installs the Home Assistant stand-ins)

from keyframe_scheduler import HybridSchedulerCoordinator  # noqa: E402
from keyframe_scheduler.scheduler import Evaluator, spec_from_dict  # noqa: E402


def kf(time, kelvin, dim, mode, **extra):
    data = {"time": time, "kelvin": kelvin, "dim": dim, "mode": mode,
            "curve": "linear", "transitionSeconds": 600, "transitionDirection": "after"}
    data.update(extra)
    return data


def coordinator(keyframes, **extra):
    data = {"version": 1, "timezone": "Europe/Berlin", "stepMinutes": 5,
            "location": {"latitude": 52.52, "longitude": 13.405}, "keyframes": keyframes}
    data.update(extra)
    spec = spec_from_dict(data)
    return HybridSchedulerCoordinator(None, Evaluator(spec), spec, "test", max_transition_seconds=300)


def at(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=BERLIN_TZ)


class NextUpdateTest(unittest.TestCase):
    SUNSET_EVENING = [
        kf("07:00", 4000, 100, "instant"),
        kf("20:00", 2700, 30, "instant", trigger="sun", sunEvent="sunset"),
    ]

    def test_next_event_is_todays_sunset(self):
        c = coordinator(self.SUNSET_EVENING)
        self.assertEqual(c._calculate_next_update_time(at(2026, 6, 21, 20, 0)).strftime("%H:%M"), "21:33")
        self.assertEqual(c._calculate_next_update_time(at(2026, 12, 21, 12, 0)).strftime("%H:%M"), "15:54")

    def test_after_last_keyframe_next_is_tomorrow(self):
        c = coordinator(self.SUNSET_EVENING)
        nxt = c._calculate_next_update_time(at(2026, 6, 21, 22, 0))
        self.assertEqual((nxt.day, nxt.strftime("%H:%M")), (22, "07:00"))
        nxt = c._calculate_next_update_time(at(2026, 6, 22, 8, 0))
        self.assertEqual(nxt.strftime("%H:%M"), "21:34")  # the 22nd's own sunset, a minute later

    def test_grouped_loser_triggers_no_update(self):
        c = coordinator([
            kf("07:00", 4000, 100, "instant"),
            kf("20:00", 2700, 30, "instant", trigger="sun", sunEvent="sunset", group="A"),
            kf("20:00", 2700, 50, "instant", group="A"),
        ], groups=[{"id": "A", "rule": "earliest"}])
        # June: 20:00 wins over sunset (21:33) -> after 20:00 nothing until tomorrow 07:00
        nxt = c._calculate_next_update_time(at(2026, 6, 21, 20, 30))
        self.assertEqual((nxt.day, nxt.strftime("%H:%M")), (22, "07:00"))

    def test_running_transition_keeps_stepping(self):
        # A 30 min "after" transition that started at 20:00: at 20:10 the
        # next step must come before the transition ends at 20:30.
        c = coordinator([
            kf("07:00", 4000, 100, "instant"),
            kf("20:00", 2700, 30, "transition", transitionSeconds=1800),
        ])
        nxt = c._calculate_next_update_time(at(2026, 6, 21, 20, 10))
        self.assertLess(nxt, at(2026, 6, 21, 20, 30) + timedelta(seconds=1))

    def test_interpolation_ticks_towards_sun_keyframe(self):
        c = coordinator([
            kf("12:00", 5000, 100, "interpolate"),
            kf("20:00", 2700, 40, "interpolate", trigger="sun", sunEvent="sunset"),
        ])
        nxt = c._calculate_next_update_time(at(2026, 6, 21, 18, 0))
        # Regular ticks (adaptive step, here 10 min), not only at the sunset at 21:33
        self.assertLessEqual(nxt, at(2026, 6, 21, 18, 30))


class UpdateDataTest(unittest.TestCase):
    def test_values_and_keyframes_today(self):
        c = coordinator([
            kf("07:00", 4000, 100, "instant"),
            kf("20:00", 2700, 30, "instant", trigger="sun", sunEvent="sunset", offsetMinutes=30),
        ])
        import keyframe_scheduler
        original_now = keyframe_scheduler.dt_util.now
        self.addCleanup(setattr, keyframe_scheduler.dt_util, "now", original_now)  # shared stub module
        keyframe_scheduler.dt_util.now = lambda: at(2026, 6, 21, 21, 0)
        data = asyncio.run(c._async_update_data())
        self.assertEqual(data["brightness"], 100.0)  # before 22:03
        self.assertEqual(data["keyframes_today"], ["07:00", "22:03 (sunset +30 min)"])
        keyframe_scheduler.dt_util.now = lambda: at(2026, 6, 21, 22, 10)
        self.assertEqual(asyncio.run(c._async_update_data())["brightness"], 30.0)


if __name__ == "__main__":
    unittest.main()


class WebsocketSchedulesTest(unittest.TestCase):
    """keyframe_scheduler/schedules: the web app lists the active schedule of
    every instance in its example list."""

    def test_lists_every_instance(self):
        import keyframe_scheduler as ks

        class Entry:
            def __init__(self, entry_id, title):
                self.entry_id, self.title = entry_id, title

        class Store:
            async def async_get(self, entry_id):
                return {"version": 1, "name": "from store", "keyframes": []} if entry_id == "b" else None

        class Entries:
            def async_entries(self, domain):
                return [Entry("a", "Astro Light"), Entry("b", "Flur"), Entry("c", "Empty")]

        class Hass:
            config_entries = Entries()
            data = {ks.DOMAIN: {"store": Store(), "a": {"schedule": {"version": 1, "name": "Astro", "keyframes": []}}}}

        class Connection:
            def send_result(self, msg_id, result):
                self.sent = (msg_id, result)

        connection = Connection()
        asyncio.run(ks._ws_schedules(Hass(), connection, {"id": 7, "type": "keyframe_scheduler/schedules"}))
        msg_id, result = connection.sent
        self.assertEqual(msg_id, 7)
        self.assertEqual([(s["entry_id"], s["title"], s["schedule"]["name"]) for s in result["schedules"]],
                         [("a", "Astro Light", "Astro"), ("b", "Flur", "from store")])
