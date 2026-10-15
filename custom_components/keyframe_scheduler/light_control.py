"""
Keyframe Scheduler - direct light control.

The integration drives its lights itself (no blueprint automation):

- Every assigned light that is ON and follows the schedule is led along
  the schedule's curve at its own pace (its own timer): fades as long as
  its type allows, each one to the curve's value at the end of the fade.
  The integration never switches a light on.
- When a light is switched on (app, wall switch, presence ...) it gets the
  current values at once.
- A change made by someone else pauses the light (its follow switch turns
  off). Switching the light off and on again always resumes; depending on
  the instance option also after some minutes or at the next keyframe.
- Every light has a type (DALI, Zigbee, ...) that limits how long one fade
  may be and how often commands may be sent.

The decisions themselves live in light_logic.py.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional

from homeassistant.core import Context, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_LIGHT_TYPE,
    RESUME_AFTER_MINUTES,
    RESUME_NEVER,
    RESUME_NEXT_KEYFRAME,
    SWITCH_ON_TRANSITION_SECONDS,
    TURN_ON_TRANSITION_SECONDS,
)
from .light_logic import (
    Command,
    LightProfile,
    SentCommand,
    build_command,
    brightness_pct_from_state,
    group_members,
    is_manual_change,
    kelvin_range_from_state,
    plan_step,
    should_resume,
)

_LOGGER = logging.getLogger(__name__)

PAUSE_MANUAL = "manual"  # paused by a detected manual change
PAUSE_USER = "user"      # follow switch turned off on purpose


class ControlledLight:
    """One assigned light (or light group) of an instance."""

    def __init__(self, entity_id: str, light_type: str, profile: LightProfile) -> None:
        self.entity_id = entity_id
        self.light_type = light_type
        self.profile = profile
        self.members: tuple = (entity_id,)
        # Follow state, mirrored by the follow switch (which persists it)
        self.following = True
        self.pause_reason: Optional[str] = None
        self.paused_since: Optional[datetime] = None
        # Per member: what was sent last (wall-clock timestamps); our own context ids
        self.last_sent: Dict[str, SentCommand] = {}
        self.own_contexts: Deque[str] = deque(maxlen=50)
        self.switch: Any = None  # KeyframeFollowSwitch, set when it is added
        self._deferred: Dict[str, Callable[[], None]] = {}  # member -> cancel of its next step
        self._resume_timer: Optional[Callable[[], None]] = None

    def cancel_timers(self) -> None:
        for cancel in self._deferred.values():
            cancel()
        self._deferred.clear()
        if self._resume_timer:
            self._resume_timer()
            self._resume_timer = None


class KeyframeLightController:
    """Drives the lights of one Keyframe Scheduler instance."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
        lights: Dict[str, str],
        type_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        resume_mode: str = RESUME_NEVER,
        resume_minutes: float = 0,
        detect_non_ha_changes: bool = True,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.resume_mode = resume_mode
        self.resume_minutes = resume_minutes
        self.detect_non_ha_changes = detect_non_ha_changes
        self.lights: Dict[str, ControlledLight] = {
            entity_id: ControlledLight(
                entity_id,
                light_type,
                LightProfile.for_type(light_type or DEFAULT_LIGHT_TYPE, type_overrides),
            )
            for entity_id, light_type in lights.items()
        }
        self._by_member: Dict[str, ControlledLight] = {}
        self._unsub: List[Callable[[], None]] = []
        self._unsub_state: Optional[Callable[[], None]] = None
        self._evaluator: Any = None  # the schedule the lights currently run on

    # ---- setup ------------------------------------------------------------

    async def async_start(self) -> None:
        self._refresh_members()
        self._evaluator = getattr(self.coordinator, "evaluator", None)
        self._unsub.append(self.coordinator.async_add_listener(self._on_schedule_update))
        # Group members can appear late during startup: refresh once HA runs.
        # async_at_started, not bus.async_listen_once: removing a once-listener
        # that has already fired logs "Unable to remove unknown job listener"
        # when the instance is reloaded later. On a reload HA already runs and
        # the members are there, so nothing to wait for.
        if self.hass.state is not CoreState.running:
            self._unsub.append(async_at_started(self.hass, self._on_started))
        # Lights that are already on get the current values right away
        self.hass.async_create_task(self.async_apply())

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        for light in self.lights.values():
            light.cancel_timers()

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._refresh_members()
        self.hass.async_create_task(self.async_apply(force=True))

    def _group_attrs(self, entity_id: str):
        state = self.hass.states.get(entity_id)
        return dict(state.attributes) if state else None

    def _refresh_members_if_changed(self) -> None:
        """Groups report their members only once their integration has loaded
        (a Hue room after the bridge, for example) and members can change —
        expand again whenever the result would differ."""
        if any(group_members(l.entity_id, self._group_attrs) != l.members for l in self.lights.values()):
            self._refresh_members()

    def _refresh_members(self) -> None:
        """Expand groups and (re)subscribe to state changes of all members."""
        attrs = self._group_attrs

        self._by_member = {}
        for light in self.lights.values():
            light.members = group_members(light.entity_id, attrs)
            for member in light.members:
                self._by_member[member] = light
            # A group entity itself also reports changes — watch it too
            self._by_member.setdefault(light.entity_id, light)
        if self._unsub_state:
            self._unsub_state()
        self._unsub_state = async_track_state_change_event(
            self.hass, list(self._by_member), self._on_light_state_change
        )
        # The follow switches show the members — write them out again
        for light in self.lights.values():
            if light.switch is not None:
                light.switch.set_state(light.following, light.pause_reason)

    def register_switch(self, entity_id: str, switch) -> None:
        light = self.lights.get(entity_id)
        if light is None:
            return
        light.switch = switch
        light.following = switch.is_on
        light.pause_reason = switch.pause_reason
        if light.pause_reason == PAUSE_MANUAL:
            # Paused before a restart: count the pause from now on
            self._pause(light)

    # ---- sending ------------------------------------------------------------
    #
    # Every assigned light runs on its own timer (the members of a group share
    # it): a step sends each member at most one command — a fade along the
    # curve, as long as the light's type allows — and schedules the next step
    # for when that fade ends (light_logic.plan_step). Lights of different
    # types in one instance therefore get different fades and command rates
    # for the same curve. Identical commands for all members of a group go
    # out as one call to the group entity (_send).

    def _now(self) -> datetime:
        return dt_util.now()

    @callback
    def _on_schedule_update(self) -> None:
        self._check_resume_next_keyframe()
        self._refresh_members_if_changed()
        # A new schedule (set_schedule, options): start over on its curve.
        # Otherwise only lights without a running timer need a step.
        evaluator = getattr(self.coordinator, "evaluator", None)
        replaced = evaluator is not self._evaluator
        self._evaluator = evaluator
        self.hass.async_create_task(self.async_apply(restart=replaced))

    async def async_apply(self, force: bool = False, only: Optional[List[str]] = None,
                          transition: Optional[float] = None, restart: bool = True) -> None:
        """Bring the lights onto the schedule's curve.

        force: send the current value even if nothing changed and within the
        min interval (turn-on, resume, service). restart=False leaves lights
        alone whose timer is already running.
        """
        for light in self.lights.values():
            if only is not None and light.entity_id not in only:
                continue
            if not light.following:
                continue
            if not restart and not force and light._deferred:
                continue
            await self._step(light, force, transition)

    async def _step(self, light: ControlledLight, force: bool = False,
                    catch_up: Optional[float] = None) -> None:
        """One step for one assigned light: plan every member that is on (each
        with its own capabilities), send, and set the light's timer for the
        next step. The members of a group share one timer, so they stay in
        step and their commands can go out together (_send)."""
        self._cancel_step(light)
        evaluator = getattr(self.coordinator, "evaluator", None)
        if evaluator is None or not light.following:
            return
        states = {m: self.hass.states.get(m) for m in light.members}
        on = [m for m, state in states.items() if state is not None and state.state == "on"]
        if not on:
            return  # never switch a light on; a turn-on starts it again

        now = self._now()

        def curve(seconds: float):
            values = evaluator.evaluate_at(now + timedelta(seconds=seconds))
            return float(values.dim), float(values.kelvin)

        breakpoint_at = evaluator.next_breakpoint(now)
        to_breakpoint = None if breakpoint_at is None else (breakpoint_at - now).total_seconds()
        sends: Dict[str, Command] = {}
        next_in = None
        for member in on:
            step = plan_step(
                light.profile,
                light.last_sent.get(member),
                now.timestamp(),
                curve,
                to_breakpoint,
                kelvin_range_from_state(states[member].attributes),
                force=force,
                catch_up=TURN_ON_TRANSITION_SECONDS if catch_up is None else catch_up,
            )
            next_in = step.next_in if next_in is None else min(next_in, step.next_in)
            if step.action == "send":
                sends[member] = step.command
        self._schedule(light, next_in)
        await self._send(light, sends, now)

    async def _send(self, light: ControlledLight, commands: Dict[str, Command], now: datetime) -> None:
        """Send commands to members of a light and remember them.

        All members of a group get the same command (the usual case: same
        model, same curve) -> ONE call to the group entity: a Zigbee group
        changes all lamps at the same moment instead of one after the other.
        Otherwise — and always for Hue rooms / zones — one call per member.
        A transition of 0 is left out (switch at once).
        """
        if not commands:
            return
        group_state = self.hass.states.get(light.entity_id)
        group_call = (
            light.entity_id not in light.members
            and set(commands) == set(light.members)
            and len(set(commands.values())) == 1
            # Not for Hue rooms / zones: HA splits a room command into one
            # request per lamp anyway, and a room command with a transition
            # was measured to leave the lamps at a wrong brightness
            and not (group_state is not None and group_state.attributes.get("is_hue_group"))
        )
        calls = [(light.entity_id, next(iter(commands.values())))] if group_call else list(commands.items())
        context = Context()
        light.own_contexts.append(context.id)
        for member, command in commands.items():
            light.last_sent[member] = SentCommand(
                at=now.timestamp(), brightness_pct=command.brightness_pct,
                kelvin=command.kelvin, transition=command.transition,
            )
        for target, command in calls:
            service_data: Dict[str, Any] = {"entity_id": target, "brightness_pct": command.brightness_pct}
            if command.transition > 0:
                service_data["transition"] = command.transition
            if command.kelvin is not None:
                service_data["color_temp_kelvin"] = command.kelvin
            _LOGGER.debug("%s: %s", target, service_data)
            await self.hass.services.async_call("light", "turn_on", service_data, blocking=False, context=context)

    # ---- switching on with the schedule's values (services turn_on / toggle) --
    #
    # A light switched on with a plain light.turn_on / toggle comes on with its
    # stored state and is corrected a second later — a visible jump. Switched
    # on through these services it comes on with the schedule's values at once.

    def light_for(self, entity_id: str) -> Optional[ControlledLight]:
        """The assigned light (or light group) an entity belongs to."""
        return self.lights.get(entity_id) or self._by_member.get(entity_id)

    def _targets(self, light: ControlledLight, entity_id: str) -> tuple:
        """The member lights a call for entity_id refers to."""
        return light.members if entity_id == light.entity_id else (entity_id,)

    def is_on(self, entity_id: str) -> bool:
        light = self.light_for(entity_id)
        targets = self._targets(light, entity_id) if light else (entity_id,)
        return any((s := self.hass.states.get(t)) is not None and s.state == "on" for t in targets)

    async def async_turn_on(self, entity_id: str, transition: float = SWITCH_ON_TRANSITION_SECONDS) -> None:
        """Switch a light (or one member) on with the schedule's current values.

        Switching on this way means "follow the schedule": a pause ends.
        Every member gets what it can show (kelvin clamped to its range,
        brightness only without colour temperature), without a fade by
        default (SWITCH_ON_TRANSITION_SECONDS). If all members get the same
        command, it goes to the group entity in one call (_send).
        """
        self._refresh_members_if_changed()
        light = self.light_for(entity_id)
        evaluator = getattr(self.coordinator, "evaluator", None)
        if light is None or evaluator is None:
            return
        if not light.following:
            light.following = True
            light.pause_reason = None
            light.paused_since = None
            if light._resume_timer:
                light._resume_timer()
                light._resume_timer = None
            if light.switch is not None:
                light.switch.set_state(True, None)

        now = self._now()
        values = evaluator.evaluate_at(now)
        commands: Dict[str, Command] = {}
        for member in self._targets(light, entity_id):
            state = self.hass.states.get(member)
            kelvin_range = kelvin_range_from_state(state.attributes) if state else None
            commands[member] = build_command(float(values.dim), float(values.kelvin), transition, kelvin_range)

        self._schedule(light, max(1.0, transition, light.profile.min_interval))
        await self._send(light, commands, now)

    async def async_toggle(self, entity_id: str, transition: Optional[float] = None) -> None:
        """Off if any of the light's targets is on, otherwise on with the schedule."""
        if self.is_on(entity_id):
            service_data: Dict[str, Any] = {"entity_id": entity_id}
            if transition is not None:
                service_data["transition"] = transition
            await self.hass.services.async_call("light", "turn_off", service_data, blocking=False, context=Context())
            return
        await self.async_turn_on(entity_id, SWITCH_ON_TRANSITION_SECONDS if transition is None else transition)

    def _schedule(self, light: ControlledLight, seconds: float) -> None:
        """Run the light's next step in `seconds` (one timer per assigned light)."""
        self._cancel_step(light)

        @callback
        def _later(_now) -> None:
            light._deferred.pop(light.entity_id, None)
            self.hass.async_create_task(self._step(light))

        light._deferred[light.entity_id] = async_call_later(self.hass, max(1.0, seconds), _later)

    @staticmethod
    def _cancel_step(light: ControlledLight) -> None:
        cancel = light._deferred.pop(light.entity_id, None)
        if cancel:
            cancel()

    # ---- watching the lights -------------------------------------------------

    @callback
    def _on_light_state_change(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        light = self._by_member.get(entity_id)
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if light is None or new is None:
            return
        if entity_id == light.entity_id and new.attributes.get("entity_id") is not None:
            self._refresh_members_if_changed()   # a group reporting (new) members

        if new.state != "on":
            light.last_sent.pop(entity_id, None)  # next turn-on gets a fresh command
            # The light's timer stops once none of its members is on
            if not any((s := self.hass.states.get(m)) is not None and s.state == "on" for m in light.members):
                self._cancel_step(light)
            return

        if old is None or old.state != "on":
            # Switched on by the turn_on / toggle service with the schedule's
            # values: nothing to correct, its next step is already planned.
            # (The member's state may carry a context of its own when the
            # call went to the group entity — the fresh command tells.)
            last = light.last_sent.get(entity_id)
            if last is not None and self._now().timestamp() - last.at < last.transition + 5:
                return
            # Switched on (app, wall switch, presence, power back ...):
            # off/on always ends a manual pause, then apply at once.
            if light.pause_reason == PAUSE_MANUAL:
                self._resume(light)
            elif light.following:
                self.hass.async_create_task(
                    self.async_apply(force=True, only=[light.entity_id], transition=TURN_ON_TRANSITION_SECONDS)
                )
            return

        if not light.following:
            return
        context = new.context
        if is_manual_change(
            own_context=context.id in light.own_contexts,
            context_id=context.id,
            user_id=context.user_id,
            parent_id=context.parent_id,
            last_sent=light.last_sent.get(entity_id),
            reported_brightness_pct=brightness_pct_from_state(new.attributes),
            reported_kelvin=new.attributes.get("color_temp_kelvin"),
            now=self._now().timestamp(),
            detect_non_ha_changes=self.detect_non_ha_changes,
        ):
            _LOGGER.info("%s: manual change detected, pausing %s", entity_id, light.entity_id)
            self._pause(light)

    # ---- pause / resume ------------------------------------------------------

    def _pause(self, light: ControlledLight) -> None:
        light.cancel_timers()
        light.following = False
        light.pause_reason = PAUSE_MANUAL
        light.paused_since = dt_util.now()
        if light.switch is not None:
            light.switch.set_state(False, PAUSE_MANUAL)
        if self.resume_mode == RESUME_AFTER_MINUTES and self.resume_minutes > 0:
            @callback
            def _resume_later(_now) -> None:
                light._resume_timer = None
                if light.pause_reason == PAUSE_MANUAL:
                    self._resume(light)

            light._resume_timer = async_call_later(self.hass, self.resume_minutes * 60, _resume_later)

    def _resume(self, light: ControlledLight) -> None:
        self.set_following(light.entity_id, True, None)

    def set_following(self, entity_id: str, following: bool, reason: Optional[str]) -> None:
        """Follow switch, service or resume changed whether a light follows."""
        light = self.lights.get(entity_id)
        if light is None:
            return
        light.following = following
        light.pause_reason = None if following else reason
        light.paused_since = None if following else dt_util.now()
        if following:
            light.cancel_timers()
            light.last_sent.clear()
            self.hass.async_create_task(
                self.async_apply(force=True, only=[entity_id], transition=TURN_ON_TRANSITION_SECONDS)
            )
        if light.switch is not None:
            light.switch.set_state(following, light.pause_reason)

    def _check_resume_next_keyframe(self) -> None:
        if self.resume_mode != RESUME_NEXT_KEYFRAME:
            return
        now = dt_util.now()
        for light in self.lights.values():
            if light.pause_reason != PAUSE_MANUAL or light.paused_since is None:
                continue
            times = self.coordinator.keyframe_times_between(light.paused_since, now)
            if should_resume(RESUME_NEXT_KEYFRAME, light.paused_since, now, 0, times):
                self._resume(light)
