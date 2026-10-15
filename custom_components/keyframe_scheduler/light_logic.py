"""
Keyframe Scheduler - light control decisions.

Pure logic without Home Assistant imports, so it can be tested on its own.
The HA side (light_control.py) gathers the facts — light state, context of
a change, time — and asks these functions what to do:

- plan_step:          what to send to a light now, and when to look at it again?
- is_manual_change:   was a reported change made by someone else?
- should_resume:      may a manually paused light follow again?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

try:
    from .const import (
        BRIGHTNESS_TOLERANCE_PCT,
        CONF_FOLLOW_LIGHTS,
        CONF_LIGHTS,
        DEFAULT_LIGHT_TYPE,
        KELVIN_TOLERANCE,
        LIGHT_TYPES,
        RESUME_AFTER_MINUTES,
        RESUME_NEXT_KEYFRAME,
        SEGMENT_BRIGHTNESS_TOLERANCE_PCT,
        SEGMENT_KELVIN_TOLERANCE,
        TRANSITION_GRACE_SECONDS,
        TURN_ON_TRANSITION_SECONDS,
    )
except ImportError:  # loaded outside the package (tests)
    from const import (  # type: ignore[no-redef]
        BRIGHTNESS_TOLERANCE_PCT,
        CONF_FOLLOW_LIGHTS,
        CONF_LIGHTS,
        DEFAULT_LIGHT_TYPE,
        KELVIN_TOLERANCE,
        LIGHT_TYPES,
        RESUME_AFTER_MINUTES,
        RESUME_NEXT_KEYFRAME,
        SEGMENT_BRIGHTNESS_TOLERANCE_PCT,
        SEGMENT_KELVIN_TOLERANCE,
        TRANSITION_GRACE_SECONDS,
        TURN_ON_TRANSITION_SECONDS,
    )

# Context ids of PICOlightnode's internal updates (Smart Restore, MQTT sync)
# — never a manual change.
PICO_CONTEXT_MARKER = "picolightnode"


@dataclass(frozen=True)
class LightProfile:
    """Timing limits of a light type."""
    max_transition: float
    min_interval: float

    @classmethod
    def for_type(cls, light_type: str, overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> "LightProfile":
        base = dict(LIGHT_TYPES.get(light_type, LIGHT_TYPES[DEFAULT_LIGHT_TYPE]))
        base.update({k: v for k, v in ((overrides or {}).get(light_type) or {}).items() if v is not None})
        return cls(max_transition=float(base["max_transition"]), min_interval=float(base["min_interval"]))


@dataclass(frozen=True)
class SentCommand:
    """What was last sent to a light (monotonic time in seconds)."""
    at: float
    brightness_pct: float
    kelvin: Optional[int]
    transition: float


@dataclass(frozen=True)
class Command:
    brightness_pct: float
    kelvin: Optional[int]
    transition: float


@dataclass(frozen=True)
class Step:
    """Result of plan_step: what to do with one light now, and when to look
    at it again. action is 'send', 'wait' (min interval) or 'idle'."""
    action: str
    next_in: float
    command: Optional[Command] = None


# The curve is sampled a hair before a breakpoint, so a jump AT the
# breakpoint (instant keyframe) is not part of the fade towards it
_BEFORE_BREAKPOINT = 0.05  # seconds
# How long a light is left alone when nothing at all is coming up
_IDLE_SECONDS = 3600.0

# (brightness %, kelvin) of the schedule `seconds` from now
Curve = Callable[[float], Tuple[float, float]]


def build_command(
    brightness_pct: float,
    kelvin: float,
    transition: float,
    kelvin_range: Optional[Tuple[float, float]],
) -> Command:
    """A command for one light: kelvin clamped to its range, or left out for
    a light without colour temperature (kelvin_range None)."""
    k: Optional[int] = None
    if kelvin_range is not None:
        lo, hi = kelvin_range
        k = int(round(min(max(kelvin, lo), hi)))
    bri = round(max(0.0, min(100.0, brightness_pct)), 1)
    return Command(brightness_pct=bri, kelvin=k, transition=max(0.0, transition))


def _differs(last: SentCommand, command: Command, brightness: float, kelvin: float) -> bool:
    if abs(last.brightness_pct - command.brightness_pct) >= brightness:
        return True
    return command.kelvin is not None and last.kelvin is not None and abs(last.kelvin - command.kelvin) >= kelvin


def segment_length(curve: Curve, limit: float, floor: float) -> float:
    """Longest fade <= limit over which a straight line stays on the curve.

    Between two breakpoints the curve is a straight line or one sine ramp. A
    sine ramp is halved until the quarter points of the piece lie on the
    straight line within the tolerance — but never shorter than `floor`.
    """
    length = limit
    while length / 2 >= floor:
        b0, k0 = curve(0.0)
        b1, k1 = curve(max(0.0, length - _BEFORE_BREAKPOINT))
        straight = True
        for f in (0.25, 0.5, 0.75):
            b, k = curve(length * f)
            if (
                abs(b - (b0 + (b1 - b0) * f)) > SEGMENT_BRIGHTNESS_TOLERANCE_PCT
                or abs(k - (k0 + (k1 - k0) * f)) > SEGMENT_KELVIN_TOLERANCE
            ):
                straight = False
                break
        if straight:
            break
        length /= 2
    return length


def plan_step(
    profile: LightProfile,
    last_sent: Optional[SentCommand],
    now: float,
    curve: Curve,
    to_breakpoint: Optional[float],
    kelvin_range: Optional[Tuple[float, float]],
    force: bool = False,
    catch_up: float = TURN_ON_TRANSITION_SECONDS,
) -> Step:
    """Decide what to send to one light that is on and following — at its
    own pace, independent of the other lights of the instance.

    The light follows the curve piece by piece: each command fades to the
    value the curve has at the END of the piece, over the length of the piece.

    - within min_interval of the last command: wait (unless forced)
    - the light is not where the curve is (just switched on, resumed, an
      instant keyframe fired): jump there with a short fade
    - otherwise fade along the curve. A piece is at most the light type's
      max_transition long, never crosses a breakpoint (`to_breakpoint`
      seconds ahead, None = none known) and is shortened where the curve
      bends (segment_length)
    - nothing changes during the piece: idle — until the breakpoint if the
      curve stays flat all the way
    """
    if last_sent is not None and not force:
        elapsed = now - last_sent.at
        if elapsed < profile.min_interval:
            return Step("wait", profile.min_interval - elapsed)

    brightness, kelvin = curve(0.0)
    here = build_command(brightness, kelvin, catch_up, kelvin_range)
    if force or last_sent is None or _differs(last_sent, here, BRIGHTNESS_TOLERANCE_PCT, KELVIN_TOLERANCE):
        return Step("send", max(1.0, catch_up, profile.min_interval), here)

    limit = profile.max_transition
    if to_breakpoint is not None:
        limit = min(limit, to_breakpoint)
    limit = max(1.0, limit)
    floor = min(limit, max(1.0, profile.min_interval))
    length = segment_length(curve, limit, floor)
    brightness, kelvin = curve(max(0.0, length - _BEFORE_BREAKPOINT))
    command = build_command(brightness, kelvin, length, kelvin_range)
    if _differs(last_sent, command, 0.5, 10):
        return Step("send", length, command)

    # Nothing to do for this piece. Sleep until the breakpoint if the curve
    # stays where the light is, otherwise look again after the piece.
    if to_breakpoint is None:
        return Step("idle", _IDLE_SECONDS)
    flat = all(
        not _differs(last_sent, build_command(*curve(to_breakpoint * f), 0, kelvin_range), 0.5, 10)
        for f in (0.25, 0.5, 0.75)
    ) and not _differs(
        last_sent, build_command(*curve(max(0.0, to_breakpoint - _BEFORE_BREAKPOINT)), 0, kelvin_range), 0.5, 10
    )
    return Step("idle", max(1.0, to_breakpoint if flat else length))


def is_manual_change(
    *,
    own_context: bool,
    context_id: str,
    user_id: Optional[str],
    parent_id: Optional[str],
    last_sent: Optional[SentCommand],
    reported_brightness_pct: Optional[float],
    reported_kelvin: Optional[float],
    now: float,
    detect_non_ha_changes: bool,
) -> bool:
    """Was a change of an ON light made by someone other than the scheduler?

    - our own commands and PICOlightnode's internal updates: no
    - a command from a user or another automation/script/scene (the context
      has a user or a parent): yes
    - a change the light reports by itself (no user, no parent — e.g. a
      dimmer on the bus): yes if it clearly differs from what was sent and
      the last fade is over — only with detect_non_ha_changes
    """
    if own_context or PICO_CONTEXT_MARKER in (context_id or ""):
        return False
    if user_id is not None or parent_id is not None:
        return True
    if not detect_non_ha_changes or last_sent is None:
        return False
    if now < last_sent.at + last_sent.transition + TRANSITION_GRACE_SECONDS:
        return False  # still fading towards our target
    if reported_brightness_pct is not None and abs(reported_brightness_pct - last_sent.brightness_pct) > BRIGHTNESS_TOLERANCE_PCT:
        return True
    if reported_kelvin is not None and last_sent.kelvin is not None and abs(reported_kelvin - last_sent.kelvin) > KELVIN_TOLERANCE:
        return True
    return False


def should_resume(
    mode: str,
    paused_since: datetime,
    now: datetime,
    resume_minutes: float,
    keyframe_times: Iterable[datetime] = (),
) -> bool:
    """May a light that was paused by a manual change follow again?

    (Switching the light off and on again always resumes — that is handled
    where the turn-on is seen, not here.)
    """
    if mode == RESUME_AFTER_MINUTES:
        return resume_minutes > 0 and now - paused_since >= timedelta(minutes=resume_minutes)
    if mode == RESUME_NEXT_KEYFRAME:
        return any(paused_since < t <= now for t in keyframe_times)
    return False


def brightness_pct_from_state(attributes: Mapping[str, Any]) -> Optional[float]:
    """HA brightness 0-255 -> percent (None if not reported)."""
    value = attributes.get("brightness")
    return None if value is None else round(float(value) / 255 * 100, 1)


def kelvin_range_from_state(attributes: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """(min, max) Kelvin if the light supports colour temperature, else None."""
    modes = attributes.get("supported_color_modes") or []
    if "color_temp" not in modes:
        return None
    lo = attributes.get("min_color_temp_kelvin") or 2000
    hi = attributes.get("max_color_temp_kelvin") or 6500
    return (float(lo), float(hi))


def group_members(entity_id: str, get_attributes, depth: int = 0) -> Tuple[str, ...]:
    """Expand a light group into its member lights (nested groups too).

    get_attributes(entity_id) returns the entity's attributes or None.
    """
    attrs: Optional[Dict[str, Any]] = get_attributes(entity_id)
    members = (attrs or {}).get("entity_id")
    # A light group helper reports a list, a Hue room / zone a set (shown as
    # a list in the API) — any collection of entity ids, in a fixed order
    if isinstance(members, (set, frozenset)):
        members = sorted(members)
    if depth > 5 or not isinstance(members, (list, tuple)) or not members:
        return (entity_id,)
    result = []
    for member in members:
        if isinstance(member, str) and member.startswith("light."):
            for m in group_members(member, get_attributes, depth + 1):
                if m not in result:
                    result.append(m)
    return tuple(result) or (entity_id,)


def lights_from_options(options: Mapping[str, Any]) -> Dict[str, str]:
    """{light entity_id: light type} from the instance options.

    Migrates the options from before direct control: a plain follow_lights
    list plus one hardware limit (max_transition_seconds) per instance ->
    every light gets the type with that limit.
    """
    lights = options.get(CONF_LIGHTS)
    if isinstance(lights, dict):
        return {e: (t if t in LIGHT_TYPES else DEFAULT_LIGHT_TYPE) for e, t in lights.items()}
    legacy_max = options.get("max_transition_seconds")
    # Up to 4.0.0b1 "DALI-2 Extended Fade" was offered with 1620 s; its limit
    # is 900 s now (no DALI fade longer than 15 min)
    if legacy_max == 1620:
        return {e: "dali2_extended" for e in options.get(CONF_FOLLOW_LIGHTS) or []}
    legacy_type = next(
        (t for t, p in LIGHT_TYPES.items() if p["max_transition"] == legacy_max),
        DEFAULT_LIGHT_TYPE,
    )
    return {e: legacy_type for e in options.get(CONF_FOLLOW_LIGHTS) or []}
