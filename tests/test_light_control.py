"""
Tests for the direct light control (light_logic.py, light_control.py):
every light follows the schedule's curve at the pace of its own type; turn-on,
manual changes, pause and resume, groups.

Run from the repository root:
    python -m unittest discover -s tests
"""

import math
import unittest
from datetime import datetime, timedelta

from ha_stubs import BERLIN_TZ, Context, FakeHass

from keyframe_scheduler.const import RESUME_AFTER_MINUTES, RESUME_NEVER, RESUME_NEXT_KEYFRAME
from keyframe_scheduler.light_control import PAUSE_MANUAL, PAUSE_USER, KeyframeLightController
from keyframe_scheduler.light_logic import (
    LightProfile,
    SentCommand,
    group_members,
    is_manual_change,
    lights_from_options,
    plan_step,
    segment_length,
    should_resume,
)
from keyframe_scheduler.scheduler import Evaluator, spec_from_dict

CT = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2700, "max_color_temp_kelvin": 6500}
CT_RANGE = (2700, 6500)

# 20 % until 07:00, then one hour straight up to 100 %, at 22:00 down to 10 %
RAMP = [
    {"time": "06:00", "kelvin": 3000, "dim": 20, "mode": "instant"},
    {"time": "07:00", "kelvin": 5000, "dim": 100, "mode": "transition", "curve": "linear",
     "transitionSeconds": 3600, "transitionDirection": "after"},
    {"time": "22:00", "kelvin": 2700, "dim": 10, "mode": "instant"},
]


def evaluator(keyframes=RAMP):
    return Evaluator(spec_from_dict({"version": 1, "timezone": "Europe/Berlin", "keyframes": keyframes}))


class Clock:
    def __init__(self, hour, minute, second=0):
        self.now = datetime(2026, 6, 15, hour, minute, second, tzinfo=BERLIN_TZ)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeCoordinator:
    def __init__(self, evaluator):
        self.evaluator = evaluator
        self.data = None
        self._listeners = []
        self.keyframes = []

    def async_add_listener(self, fn):
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def update(self):
        for fn in list(self._listeners):
            fn()

    def keyframe_times_between(self, start, end):
        return [t for t in self.keyframes if start < t <= end]


class FakeSwitch:
    def __init__(self, is_on=True, pause_reason=None):
        self.is_on = is_on
        self.pause_reason = pause_reason

    def set_state(self, is_on, pause_reason):
        self.is_on, self.pause_reason = is_on, pause_reason


def setup(lights, states=None, clock=None, keyframes=RAMP, **options):
    """Controller started like in HA: states set, switches registered, then
    async_start — lights that are on get the current value at once."""
    hass = FakeHass()
    for entity_id, (state, attributes) in (states or {}).items():
        hass.states.set(entity_id, state, attributes)
    coord = FakeCoordinator(evaluator(keyframes))
    ctrl = KeyframeLightController(hass, coord, lights, **options)
    ctrl._now = clock or Clock(7, 10)
    switches = {}
    for entity_id in lights:
        switches[entity_id] = FakeSwitch()
        ctrl.register_switch(entity_id, switches[entity_id])
    hass.async_create_task(ctrl.async_start())
    hass.run()
    return hass, coord, ctrl, switches


class StartupTests(unittest.TestCase):
    """Waiting for HA to have started (group members appear late)."""

    def test_refresh_once_ha_has_started(self):
        hass, _, ctrl, _ = setup({"light.a": "generic"}, {"light.a": ("on", CT)})
        self.assertEqual(len(hass.at_started), 1)
        before = len(turn_on_calls(hass, "light.a"))
        hass.start()
        hass.run()
        self.assertEqual(len(turn_on_calls(hass, "light.a")), before + 1)   # forced apply

        # stopping after the callback ran must not try to remove it again
        # (HA logs "Unable to remove unknown job listener" for a fired once-listener)
        hass.async_create_task(ctrl.async_stop())
        hass.run()
        self.assertEqual(hass.at_started, [])

    def test_stop_before_ha_has_started_cancels_the_wait(self):
        hass, _, ctrl, _ = setup({"light.a": "generic"}, {"light.a": ("on", CT)})
        hass.async_create_task(ctrl.async_stop())
        hass.run()
        self.assertEqual(hass.at_started, [])

    def test_reload_while_ha_runs_does_not_wait(self):
        hass = FakeHass()
        hass.state = "running"
        hass.states.set("light.a", "on", CT)
        ctrl = KeyframeLightController(hass, FakeCoordinator(evaluator()), {"light.a": "generic"})
        ctrl._now = Clock(7, 10)
        hass.async_create_task(ctrl.async_start())
        hass.run()
        self.assertEqual(hass.at_started, [])


def turn_on_calls(hass, entity_id=None):
    return [c[2] for c in hass.services.calls
            if c[:2] == ("light", "turn_on") and entity_id in (None, c[2]["entity_id"])]


def fire_timer(hass, delay):
    """Run the pending timer with this delay (the light's next step)."""
    entry = next(t for t in hass.timers if abs(t[0] - delay) < 0.01)
    hass.timers.remove(entry)
    entry[1](None)
    hass.run()


def play_day(light_type, keyframes, hours=24):
    """Run the controller for a day in simulated time and let a simple light
    model execute the commands (linear fades). Returns (max brightness
    deviation from the curve in %, number of commands, longest fade)."""
    clock = Clock(0, 0)
    start = clock.now
    hass, coord, ctrl, _ = setup({"light.a": light_type}, clock=clock, keyframes=keyframes,
                                 states={"light.a": ("on", CT)})
    due = {}                                   # id(timer entry) -> (fire time, entry)
    end = start + timedelta(hours=hours)
    sent_at = [start] * len(hass.services.calls)   # when each command was sent
    while True:
        for entry in hass.timers:
            due.setdefault(id(entry), (clock.now + timedelta(seconds=entry[0]), entry))
        live = {id(e) for e in hass.timers}
        due = {k: v for k, v in due.items() if k in live}
        if not due:
            break
        key, (at, entry) = min(due.items(), key=lambda item: item[1][0])
        if at >= end:
            break
        del due[key]
        hass.timers.remove(entry)
        clock.now = at
        entry[1](None)
        hass.run()
        sent_at += [at] * (len(hass.services.calls) - len(sent_at))

    # The light: fades linearly from where it is to each command's target
    calls = [(c[2]["brightness_pct"], c[2]["transition"]) for c in hass.services.calls]
    worst, level, i, fade = 0.0, None, 0, None
    seconds = 0
    while start + timedelta(seconds=seconds) < end:
        now = start + timedelta(seconds=seconds)
        while i < len(calls) and sent_at[i] <= now:
            origin = level if fade is None else fade(sent_at[i])
            target, duration, t0 = calls[i][0], calls[i][1], sent_at[i]
            if origin is None:
                origin = target
            fade = (lambda o, tg, d, t: lambda at: tg if d <= 0 or (at - t).total_seconds() >= d
                    else o + (tg - o) * (at - t).total_seconds() / d)(origin, target, duration, t0)
            i += 1
        if fade is not None:
            level = fade(now)
            # The second in which the light jumps (instant keyframe, start) is not measured
            jumping = calls[i - 1][1] <= 1 and (now - sent_at[i - 1]).total_seconds() < 2
            if not jumping:
                worst = max(worst, abs(level - coord.evaluator.evaluate_at(now).dim))
        seconds += 20
    return worst, len(calls), max(c[1] for c in calls)


class LogicTest(unittest.TestCase):
    DALI = LightProfile(max_transition=90, min_interval=30)
    ZIGBEE = LightProfile(max_transition=600, min_interval=15)

    @staticmethod
    def rising(seconds):
        """20 % now, +1 % every 10 s; 3000 K, +5 K every 10 s."""
        return 20 + seconds / 10, 3000 + seconds / 2

    def at_curve(self, at=0.0):
        return SentCommand(at=at, brightness_pct=20, kelvin=3000, transition=1)

    def test_light_off_the_curve_jumps_there(self):
        step = plan_step(self.DALI, None, 1000, self.rising, None, CT_RANGE)
        self.assertEqual((step.action, step.command.brightness_pct, step.command.transition), ("send", 20, 1))
        self.assertEqual(step.next_in, 30)   # then DALI's min interval
        far = SentCommand(at=0, brightness_pct=80, kelvin=3000, transition=1)
        self.assertEqual(plan_step(self.DALI, far, 1000, self.rising, None, CT_RANGE).command.transition, 1)

    def test_each_type_fades_as_long_as_it_may(self):
        # Same curve, two types: the target is the curve at the END of the fade
        dali = plan_step(self.DALI, self.at_curve(), 1000, self.rising, None, CT_RANGE)
        self.assertEqual((dali.command.transition, dali.next_in), (90, 90))
        self.assertAlmostEqual(dali.command.brightness_pct, 29.0, delta=0.1)
        zigbee = plan_step(self.ZIGBEE, self.at_curve(), 1000, self.rising, None, CT_RANGE)
        self.assertEqual((zigbee.command.transition, zigbee.next_in), (600, 600))
        self.assertAlmostEqual(zigbee.command.brightness_pct, 80.0, delta=0.1)
        self.assertEqual(zigbee.command.kelvin, 3300)

    def test_fade_ends_at_the_breakpoint(self):
        step = plan_step(self.ZIGBEE, self.at_curve(), 1000, self.rising, 40, CT_RANGE)
        self.assertEqual((step.command.transition, step.next_in), (40, 40))
        self.assertAlmostEqual(step.command.brightness_pct, 24.0, delta=0.1)

    def test_waits_within_min_interval(self):
        step = plan_step(self.DALI, self.at_curve(at=995), 1000, self.rising, None, CT_RANGE)
        self.assertEqual((step.action, step.next_in), ("wait", 25))
        forced = plan_step(self.DALI, self.at_curve(at=995), 1000, self.rising, None, CT_RANGE, force=True)
        self.assertEqual(forced.action, "send")

    def test_flat_curve_sleeps_until_the_breakpoint(self):
        flat = lambda seconds: (20.0, 3000.0)
        step = plan_step(self.DALI, self.at_curve(), 1000, flat, 5000, CT_RANGE)
        self.assertEqual((step.action, step.next_in), ("idle", 5000))
        self.assertEqual(plan_step(self.DALI, self.at_curve(), 1000, flat, None, CT_RANGE).action, "idle")

    def test_sine_ramp_is_split_until_a_straight_fade_fits(self):
        # 0 -> 100 % as one sine ramp over 30 min
        sine = lambda seconds: (50 - 50 * math.cos(math.pi * min(seconds, 1800) / 1800), 3000.0)
        self.assertLess(segment_length(sine, 900, 30), 900)
        self.assertEqual(segment_length(lambda s: (20 + s / 10, 3000.0), 900, 30), 900)   # straight: one fade
        self.assertGreaterEqual(segment_length(sine, 900, 300), 300)                        # never below the floor

    def test_brightness_only_light_and_kelvin_clamp(self):
        hot = lambda seconds: (50.0, 7000.0)
        self.assertEqual(plan_step(self.DALI, None, 0, hot, None, CT_RANGE).command.kelvin, 6500)
        self.assertIsNone(plan_step(self.DALI, None, 0, hot, None, None).command.kelvin)

    def test_manual_change_rules(self):
        last = SentCommand(at=100, brightness_pct=80, kelvin=4000, transition=10)
        base = dict(own_context=False, context_id="x", user_id=None, parent_id=None, last_sent=last,
                    reported_brightness_pct=40, reported_kelvin=4000, now=200, detect_non_ha_changes=True)
        self.assertTrue(is_manual_change(**{**base, "user_id": "u1"}))                 # app / dashboard
        self.assertTrue(is_manual_change(**{**base, "parent_id": "automation"}))       # scene / other automation
        self.assertFalse(is_manual_change(**{**base, "user_id": "u1", "own_context": True}))
        self.assertFalse(is_manual_change(**{**base, "context_id": "picolightnode_restore"}))
        self.assertTrue(is_manual_change(**base))                                      # wall dimmer, clear deviation
        self.assertFalse(is_manual_change(**{**base, "reported_brightness_pct": 78}))   # within tolerance
        self.assertFalse(is_manual_change(**{**base, "now": 115}))                      # still fading
        self.assertFalse(is_manual_change(**{**base, "detect_non_ha_changes": False}))

    def test_resume_rules(self):
        t0 = datetime(2026, 6, 21, 12, 0, tzinfo=BERLIN_TZ)
        self.assertTrue(should_resume(RESUME_AFTER_MINUTES, t0, t0 + timedelta(minutes=30), 30))
        self.assertFalse(should_resume(RESUME_AFTER_MINUTES, t0, t0 + timedelta(minutes=29), 30))
        kf = [t0 + timedelta(hours=2)]
        self.assertTrue(should_resume(RESUME_NEXT_KEYFRAME, t0, t0 + timedelta(hours=3), 0, kf))
        self.assertFalse(should_resume(RESUME_NEXT_KEYFRAME, t0, t0 + timedelta(hours=1), 0, kf))
        self.assertFalse(should_resume(RESUME_NEVER, t0, t0 + timedelta(days=1), 30, kf))

    def test_options_migration(self):
        # Before direct control: follow_lights list + one hardware limit per instance
        self.assertEqual(
            lights_from_options({"follow_lights": ["light.a", "light.b"], "max_transition_seconds": 90}),
            {"light.a": "dali", "light.b": "dali"},
        )
        # The former 1620 s limit of DALI-2 Extended still maps to that type
        self.assertEqual(lights_from_options({"follow_lights": ["light.a"], "max_transition_seconds": 1620}),
                         {"light.a": "dali2_extended"})
        self.assertEqual(lights_from_options({"follow_lights": ["light.a"]}), {"light.a": "generic"})
        self.assertEqual(lights_from_options({"lights": {"light.a": "zigbee", "light.b": "unknown"}}),
                         {"light.a": "zigbee", "light.b": "generic"})
        self.assertEqual(lights_from_options({}), {})

    def test_group_members_nested(self):
        attrs = {
            "light.room": {"entity_id": ["light.a", "light.sub"]},
            "light.sub": {"entity_id": ["light.b", "light.a"]},
            "light.a": {}, "light.b": {},
        }
        self.assertEqual(group_members("light.room", attrs.get), ("light.a", "light.b"))
        self.assertEqual(group_members("light.single", attrs.get), ("light.single",))


class ControllerTest(unittest.TestCase):
    def test_sends_only_to_lights_that_are_on(self):
        hass, coord, ctrl, _ = setup({"light.on": "dali", "light.off": "zigbee"},
                                     states={"light.on": ("on", CT), "light.off": ("off", CT)})
        calls = turn_on_calls(hass)
        self.assertEqual(len(calls), 1)
        # 07:10 on the ramp 20 -> 100 % (07:00-08:00): 33.3 %, quick fade onto the curve
        self.assertEqual(calls[0], {"entity_id": "light.on", "brightness_pct": 33.3, "transition": 1,
                                    "color_temp_kelvin": 3333})

    def test_mixed_types_each_light_at_its_own_pace(self):
        clock = Clock(7, 10)
        hass, coord, ctrl, _ = setup({"light.dali": "dali", "light.zig": "zigbee"}, clock=clock,
                                     states={"light.dali": ("on", CT), "light.zig": ("on", CT)})
        self.assertEqual(sorted(t[0] for t in hass.timers), [15, 30])   # each waits its own min interval

        clock.advance(15)                       # 07:10:15 — Zigbee's turn: one 10-minute fade
        fire_timer(hass, 15)
        zig = turn_on_calls(hass, "light.zig")[-1]
        self.assertEqual(zig["transition"], 600)
        self.assertAlmostEqual(zig["brightness_pct"], 20 + 80 * (20.25 / 60), delta=0.1)   # curve at 07:20:15

        clock.advance(15)                       # 07:10:30 — DALI's turn: 90 s only
        fire_timer(hass, 30)
        dali = turn_on_calls(hass, "light.dali")[-1]
        self.assertEqual(dali["transition"], 90)
        self.assertAlmostEqual(dali["brightness_pct"], 20 + 80 * (12 / 60), delta=0.1)     # curve at 07:12:00

        self.assertEqual(len(turn_on_calls(hass, "light.zig")), 2)      # Zigbee got no extra command
        self.assertEqual(sorted(round(t[0]) for t in hass.timers), [90, 600])

    def test_long_transition_is_not_finished_early(self):
        # A one-hour transition on DALI-2 (15 min per fade): after the first
        # fade the light is a quarter of the way, not at the target
        clock = Clock(7, 0, 5)
        hass, coord, ctrl, _ = setup({"light.a": "dali2_extended"}, clock=clock, states={"light.a": ("on", CT)})
        clock.advance(30)
        fire_timer(hass, 30)
        call = turn_on_calls(hass)[-1]
        self.assertEqual(call["transition"], 900)
        self.assertAlmostEqual(call["brightness_pct"], 20 + 80 * (15 * 60 + 35) / 3600, delta=0.1)

    def test_instant_keyframe_jumps_at_its_time(self):
        clock = Clock(21, 50)
        hass, coord, ctrl, _ = setup({"light.a": "zigbee"}, clock=clock, states={"light.a": ("on", CT)})
        self.assertEqual(turn_on_calls(hass)[-1]["brightness_pct"], 100)
        clock.advance(15)
        fire_timer(hass, 15)                    # flat until 22:00: nothing to send, sleep until then
        self.assertEqual(len(turn_on_calls(hass)), 1)
        self.assertAlmostEqual(hass.timers[0][0], 585, delta=0.1)
        clock.advance(585)                      # 22:00:00
        fire_timer(hass, 585)
        call = turn_on_calls(hass)[-1]
        self.assertEqual((call["brightness_pct"], call["transition"], call["color_temp_kelvin"]), (10, 1, 2700))

    def test_a_whole_day_stays_on_the_curve(self):
        # Sine transitions, an interpolation and an instant keyframe: every
        # type keeps to its fade limit and stays close to the curve all day
        day = [
            {"time": "06:00", "kelvin": 3000, "dim": 20, "mode": "transition", "curve": "sinus",
             "transitionSeconds": 1800, "transitionDirection": "before"},
            {"time": "08:00", "kelvin": 5000, "dim": 100, "mode": "transition", "curve": "sinus",
             "transitionSeconds": 3600, "transitionDirection": "after"},
            {"time": "17:00", "kelvin": 3500, "dim": 60, "mode": "interpolate", "curve": "sinus"},
            {"time": "22:00", "kelvin": 2700, "dim": 10, "mode": "instant"},
        ]
        limits = {"dali": 90, "dali2_extended": 900, "zigbee": 600}
        commands = {}
        for light_type, limit in limits.items():
            worst, count, longest = play_day(light_type, day)
            commands[light_type] = count
            self.assertLessEqual(longest, limit, light_type)
            self.assertLess(worst, 1.5, f"{light_type}: {worst:.1f} % off the curve")
        # Longer fades mean fewer commands for the same curve
        self.assertLess(commands["dali2_extended"], commands["dali"])
        self.assertLess(commands["zigbee"], commands["dali"])

    # ---- services turn_on / toggle: switch on with the schedule's values ----

    ROOM = {"light.room": ("off", {"entity_id": ["light.a", "light.b"], **CT}),
            "light.a": ("off", CT), "light.b": ("off", CT)}

    def test_turn_on_group_in_one_call_with_the_schedule(self):
        hass, coord, ctrl, _ = setup({"light.room": "hue"}, states=self.ROOM)
        hass.async_create_task(ctrl.async_turn_on("light.room"))
        hass.run()
        calls = turn_on_calls(hass)
        # same command for both members -> one call to the group: all on at once
        self.assertEqual(calls, [{"entity_id": "light.room", "brightness_pct": 33.3,
                                  "color_temp_kelvin": 3333}])   # no fade when switching on
        # the members report "on" (with a context of their own): no correction
        for member in ("light.a", "light.b"):
            hass.change_state(member, "on", {**CT, "brightness": 85, "color_temp_kelvin": 3333})
        hass.run()
        self.assertEqual(len(turn_on_calls(hass)), 1)
        self.assertEqual([round(t[0]) for t in hass.timers], [10])   # the group's next step

    def test_group_follows_the_curve_with_one_call(self):
        # Steady state: same model, same curve -> one call to the group each step
        clock = Clock(7, 10)
        on = {"light.room": ("on", {"entity_id": ["light.a", "light.b"], **CT}),
              "light.a": ("on", CT), "light.b": ("on", CT)}
        hass, coord, ctrl, _ = setup({"light.room": "hue"}, clock=clock, states=on)
        self.assertEqual([c["entity_id"] for c in turn_on_calls(hass)], ["light.room"])
        clock.advance(10)
        fire_timer(hass, 10)
        calls = turn_on_calls(hass)
        self.assertEqual([c["entity_id"] for c in calls], ["light.room", "light.room"])
        self.assertEqual(calls[-1]["transition"], 600)
        self.assertEqual(len(hass.timers), 1)

    def test_group_partly_on_gets_member_calls(self):
        partly = {"light.room": ("on", {"entity_id": ["light.a", "light.b"], **CT}),
                  "light.a": ("on", CT), "light.b": ("off", CT)}
        hass, coord, ctrl, _ = setup({"light.room": "hue"}, states=partly)
        self.assertEqual([c["entity_id"] for c in turn_on_calls(hass)], ["light.a"])
        # the last member off stops the group's timer
        self.assertEqual(len(hass.timers), 1)
        hass.change_state("light.a", "off", CT)
        self.assertEqual(hass.timers, [])

    def test_hue_room_gets_member_calls(self):
        # A Hue room with a transition was measured to leave the lamps at a
        # wrong brightness: Hue rooms are never sent one group command
        hue_room = {**self.ROOM, "light.room": ("off", {"entity_id": ["light.a", "light.b"], "is_hue_group": True, **CT})}
        hass, coord, ctrl, _ = setup({"light.room": "hue"}, states=hue_room)
        hass.async_create_task(ctrl.async_turn_on("light.room"))
        hass.run()
        self.assertEqual(sorted(c["entity_id"] for c in turn_on_calls(hass)), ["light.a", "light.b"])

    def test_turn_on_mixed_group_per_member(self):
        states = {"light.room": ("off", {"entity_id": ["light.ct", "light.dim"]}),
                  "light.ct": ("off", {**CT, "min_color_temp_kelvin": 3500}),
                  "light.dim": ("off", {"supported_color_modes": ["brightness"]})}
        hass, coord, ctrl, _ = setup({"light.room": "zigbee"}, states=states)
        hass.async_create_task(ctrl.async_turn_on("light.room", 0.5))
        hass.run()
        calls = {c["entity_id"]: c for c in turn_on_calls(hass)}
        self.assertEqual(set(calls), {"light.ct", "light.dim"})
        self.assertEqual(calls["light.ct"]["color_temp_kelvin"], 3500)          # clamped to its range
        self.assertNotIn("color_temp_kelvin", calls["light.dim"])                # brightness only
        self.assertEqual(calls["light.dim"]["transition"], 0.5)

    def test_turn_on_one_member_and_resume(self):
        hass, coord, ctrl, switches = setup({"light.room": "hue"}, states=self.ROOM)
        ctrl.set_following("light.room", False, PAUSE_USER)
        hass.async_create_task(ctrl.async_turn_on("light.b"))
        hass.run()
        self.assertEqual([c["entity_id"] for c in turn_on_calls(hass)], ["light.b"])
        self.assertTrue(switches["light.room"].is_on)                            # follows again

    def test_group_members_reported_late_are_picked_up(self):
        # At start the group entity has no member list yet (its integration
        # loads later) — it is controlled as a whole until the members show up
        hass, coord, ctrl, switches = setup({"light.room": "hue"}, states={"light.room": ("off", CT)})
        self.assertEqual(ctrl.lights["light.room"].members, ("light.room",))
        hass.change_state("light.room", "off", {"entity_id": ["light.a", "light.b"], **CT})
        self.assertEqual(ctrl.lights["light.room"].members, ("light.a", "light.b"))
        self.assertEqual(switches["light.room"].is_on, True)
        # members of a group that changed are found on the next schedule tick, too
        hass.states.set("light.room", "off", {"entity_id": ["light.a"], **CT})
        coord.update()
        hass.run()
        self.assertEqual(ctrl.lights["light.room"].members, ("light.a",))

    def test_toggle(self):
        hass, coord, ctrl, _ = setup({"light.room": "hue"}, states=self.ROOM)
        hass.async_create_task(ctrl.async_toggle("light.room"))
        hass.run()
        self.assertEqual(turn_on_calls(hass)[-1]["entity_id"], "light.room")
        hass.change_state("light.a", "on", CT)
        hass.async_create_task(ctrl.async_toggle("light.room"))
        hass.run()
        self.assertEqual(hass.services.calls[-1][:3], ("light", "turn_off", {"entity_id": "light.room"}))

    def test_turn_on_applies_immediately(self):
        hass, coord, ctrl, _ = setup({"light.a": "zigbee"}, states={"light.a": ("off", CT)})
        self.assertEqual(turn_on_calls(hass), [])
        hass.change_state("light.a", "on", {**CT, "brightness": 255, "color_temp_kelvin": 2700},
                          context=Context(user_id="wall"))
        hass.run()
        calls = turn_on_calls(hass)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["transition"], 1)     # quick fade to the schedule

    def test_turned_off_light_stops_its_timer(self):
        hass, coord, ctrl, _ = setup({"light.a": "zigbee"}, states={"light.a": ("on", CT)})
        self.assertEqual(len(hass.timers), 1)
        hass.change_state("light.a", "off", CT)
        self.assertEqual(hass.timers, [])

    def test_new_schedule_restarts_the_lights(self):
        clock = Clock(12, 0)
        hass, coord, ctrl, _ = setup({"light.a": "zigbee"}, clock=clock, states={"light.a": ("on", CT)})
        self.assertEqual(turn_on_calls(hass)[-1]["brightness_pct"], 100)
        coord.evaluator = evaluator([{"time": "06:00", "kelvin": 3000, "dim": 40, "mode": "instant"}])
        clock.advance(60)
        coord.update()
        hass.run()
        self.assertEqual(turn_on_calls(hass)[-1]["brightness_pct"], 40)
        coord.update()                           # a plain sensor tick does not disturb the timers
        hass.run()
        self.assertEqual(len(turn_on_calls(hass)), 2)

    def test_manual_change_pauses_and_off_on_resumes(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        hass.change_state("light.a", "on", {**CT, "brightness": 50}, context=Context(user_id="app"))
        self.assertFalse(switches["light.a"].is_on)
        self.assertEqual(switches["light.a"].pause_reason, PAUSE_MANUAL)
        self.assertEqual(hass.timers, [])               # paused: no further steps
        coord.update(); hass.run()
        self.assertEqual(len(turn_on_calls(hass)), 1)   # paused: nothing sent
        hass.change_state("light.a", "off", CT)
        hass.change_state("light.a", "on", CT)
        hass.run()
        self.assertTrue(switches["light.a"].is_on)
        self.assertEqual(len(turn_on_calls(hass)), 2)   # resumed and applied

    def test_own_commands_and_pico_updates_do_not_pause(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        own = hass.services.calls[-1][3]
        hass.change_state("light.a", "on", {**CT, "brightness": 204}, context=own)
        hass.change_state("light.a", "on", {**CT, "brightness": 100}, context=Context(id="picolightnode_restore_1"))
        self.assertTrue(switches["light.a"].is_on)

    def test_user_pause_is_not_undone_by_off_on(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)})
        ctrl.set_following("light.a", False, PAUSE_USER)
        hass.change_state("light.a", "off", CT)
        hass.change_state("light.a", "on", CT)
        hass.run()
        self.assertFalse(switches["light.a"].is_on)
        self.assertEqual(len(turn_on_calls(hass)), 1)   # only the one from the start

    def test_resume_after_minutes_sets_timer(self):
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, states={"light.a": ("on", CT)}, resume_mode=RESUME_AFTER_MINUTES, resume_minutes=20)
        hass.change_state("light.a", "on", {**CT, "brightness": 50}, context=Context(user_id="app"))
        delay, action = hass.timers[-1]
        self.assertEqual(delay, 20 * 60)
        action(None)
        hass.run()
        self.assertTrue(switches["light.a"].is_on)

    def test_resume_at_next_keyframe(self):
        clock = Clock(7, 10)
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, clock=clock, states={"light.a": ("on", CT)}, resume_mode=RESUME_NEXT_KEYFRAME)
        hass.change_state("light.a", "on", {**CT, "brightness": 50}, context=Context(user_id="app"))
        self.assertFalse(switches["light.a"].is_on)
        coord.keyframes = [datetime.now(BERLIN_TZ) - timedelta(seconds=1)]  # fired after the pause began?
        ctrl.lights["light.a"].paused_since = datetime.now(BERLIN_TZ) - timedelta(minutes=5)
        coord.update(); hass.run()
        self.assertTrue(switches["light.a"].is_on)

    def test_group_is_expanded_with_member_capabilities(self):
        hass, coord, ctrl, _ = setup({"light.room": "dali"}, states={"light.room": ("on", {"entity_id": ["light.ct", "light.dim"]}), "light.ct": ("on", CT), "light.dim": ("on", {"supported_color_modes": ["brightness"]})})
        calls = {c["entity_id"]: c for c in turn_on_calls(hass)}
        self.assertEqual(set(calls), {"light.ct", "light.dim"})
        self.assertEqual(calls["light.ct"]["color_temp_kelvin"], 3333)
        self.assertNotIn("color_temp_kelvin", calls["light.dim"])

    def test_wall_dimmer_detected_after_fade(self):
        clock = Clock(12, 0)
        hass, coord, ctrl, switches = setup({"light.a": "generic"}, clock=clock, states={"light.a": ("on", CT)})
        clock.advance(60)                       # the 1 s fade is long over; the bus reports 30 %
        hass.change_state("light.a", "on", {**CT, "brightness": 77, "color_temp_kelvin": 5000})
        self.assertEqual(switches["light.a"].pause_reason, PAUSE_MANUAL)


if __name__ == "__main__":
    unittest.main()
