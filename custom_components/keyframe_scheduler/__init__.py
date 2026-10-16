"""Keyframe Scheduler integration - Event-based coordinator."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ENTRY_ID,
    ATTR_FILE_PATH,
    ATTR_MANUAL,
    ATTR_SCHEDULE,
    CONF_DETECT_NON_HA_CHANGES,
    CONF_RESUME_MINUTES,
    CONF_RESUME_MODE,
    CONF_TYPE_OVERRIDES,
    DEFAULT_DIM,
    DEFAULT_KELVIN,
    DEFAULT_STEP_MINUTES,
    DOMAIN,
    PLATFORMS,
    RESUME_NEVER,
    SERVICE_APPLY,
    SERVICE_TOGGLE,
    SERVICE_TURN_ON,
    SERVICE_SET_MANUAL_CONTROL,
    SERVICE_SET_SCHEDULE,
    SERVICE_UPLOAD_FROM_FILE,
)
from .scheduler import Evaluator, Keyframe, kelvin_to_mired, kf_minutes, spec_from_dict
from .light_control import PAUSE_MANUAL, KeyframeLightController
from .light_logic import lights_from_options
from .store import ScheduleStore

_LOGGER = logging.getLogger(__name__)


class HybridSchedulerCoordinator(DataUpdateCoordinator):
    """Event-based coordinator with hardware limits support."""

    def __init__(
        self,
        hass: HomeAssistant,
        evaluator: Evaluator,
        spec,
        name: str,
        max_transition_seconds: int = 300,
    ):
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=name,
        )
        
        self.evaluator = evaluator
        self.spec = spec
        self.max_transition_seconds = max_transition_seconds
        self.user_step_seconds = spec.step_minutes * 60
        self._unsub_next_update = None
        
    async def async_setup(self):
        """Setup coordinator after init."""
        await self.async_refresh()
        self._schedule_next_update()

    async def async_set_schedule(self, evaluator: Evaluator, spec) -> None:
        """Switch to a new schedule in place — entities and the light
        controller stay attached to this coordinator."""
        self.evaluator = evaluator
        self.spec = spec
        self.user_step_seconds = spec.step_minutes * 60
        await self.async_refresh()
        self._schedule_next_update()
        
    @callback
    def _schedule_next_update(self):
        """Schedule next update based on events."""
        if self._unsub_next_update:
            self._unsub_next_update()
            self._unsub_next_update = None
        
        now = dt_util.now()
        next_time = self._calculate_next_update_time(now)
        
        if next_time:
            delay = (next_time - now).total_seconds()
            
            # Minimum 1 second delay
            if delay < 1:
                delay = 1
            
            _LOGGER.debug(
                "%s: Next update in %.1f seconds at %s",
                self.name,
                delay,
                next_time.strftime("%H:%M:%S")
            )
            
            self._unsub_next_update = async_track_point_in_time(
                self.hass,
                self._handle_scheduled_update,
                next_time
            )
    
    @callback
    async def _handle_scheduled_update(self, _now):
        """Handle scheduled update."""
        await self.async_request_refresh()
        self._schedule_next_update()
    
    def _parse_time(self, time_str: str) -> int:
        """Parse HH:MM to minutes."""
        h, m = map(int, time_str.split(':'))
        return h * 60 + m

    @staticmethod
    def _same_keyframe(a: Keyframe, b: Keyframe) -> bool:
        """Same schedule entry, ignoring the (daily resolved) time."""
        return replace(a, time="", minutes=None) == replace(b, time="", minutes=None)

    def _occurrences(self, now: datetime) -> List[Tuple[Keyframe, datetime]]:
        """Resolved keyframes of yesterday, today and tomorrow with their local
        datetimes (yesterday: a transition may still run past midnight).

        Sun keyframes, groups and valid-on filters make the firing times differ
        from day to day, so every day is resolved on its own (see
        scheduler.resolve_day).
        """
        local = self.evaluator.local_now(now)
        result = []
        for offset in (-1, 0, 1):
            day = local.date() + timedelta(days=offset)
            for kf in self.evaluator.keyframes_for(day):
                h, m = map(int, kf.time.split(':'))
                result.append((kf, datetime(day.year, day.month, day.day, h, m, tzinfo=local.tzinfo)))
        result.sort(key=lambda o: o[1])
        return result

    def keyframe_times_between(self, start: datetime, end: datetime) -> List[datetime]:
        """Firing times of all keyframes in (start, end] — used to resume a
        manually paused light at the next keyframe."""
        s = self.evaluator.local_now(start)
        e = self.evaluator.local_now(end)
        times = []
        day = s.date()
        while day <= e.date():
            for kf in self.evaluator.keyframes_for(day):
                h, m = map(int, kf.time.split(':'))
                dt = datetime(day.year, day.month, day.day, h, m, tzinfo=s.tzinfo)
                if start < dt <= end:
                    times.append(dt)
            day += timedelta(days=1)
        return times

    def _find_keyframe_segment(self, now: datetime):
        """Previous and next keyframe around now, on today's timeline (which
        holds the neighbouring days with keyframes, see Evaluator.timeline_for)."""
        local = self.evaluator.local_now(now)
        timeline = self.evaluator.timeline_for(local.date())
        sorted_kf = sorted(timeline, key=kf_minutes)
        now_minutes = local.hour * 60 + local.minute

        prev_kf = None
        next_kf = None

        for kf in reversed(sorted_kf):
            if kf_minutes(kf) <= now_minutes:
                prev_kf = kf
                break

        for kf in sorted_kf:
            if kf_minutes(kf) > now_minutes:
                next_kf = kf
                break
        
        # Wrap around midnight (every schedule is a daily profile)
        if not prev_kf and sorted_kf:
            prev_kf = sorted_kf[-1]
        if not next_kf and sorted_kf:
            next_kf = sorted_kf[0]
        
        return prev_kf, next_kf
    
    def _calculate_transition_events(
        self,
        kf,
        kf_dt: datetime,
        now: datetime
    ) -> List[Tuple[str, datetime]]:
        """Calculate transition events with multi-step support."""
        events = []
        
        if kf.mode != "transition":
            return events
        
        total_duration = kf.transition_seconds
        
        # Calculate window
        if kf.transition_direction == "before":
            transition_start = kf_dt - timedelta(seconds=total_duration)
            transition_end = kf_dt
        else:
            transition_start = kf_dt
            transition_end = kf_dt + timedelta(seconds=total_duration)
        
        # Only future events
        if transition_end <= now:
            return events
        
        # Step size: Should be smaller than transition duration for smooth updates
        # Use minimum of: user preference, hardware limit, and half of transition duration
        step_seconds = min(
            self.user_step_seconds,
            self.max_transition_seconds,
            total_duration / 2  # At least 2 updates during transition
        )
        
        # Minimum step: 5 seconds (for very short transitions)
        step_seconds = max(5, step_seconds)
        
        # Generate intermediate steps
        current = max(transition_start, now + timedelta(seconds=1))
        
        # Align to next step boundary
        if current > transition_start:
            elapsed = (current - transition_start).total_seconds()
            steps_passed = int(elapsed / step_seconds)
            current = transition_start + timedelta(seconds=(steps_passed + 1) * step_seconds)
        
        while current < transition_end:
            events.append(("TRANS_STEP", current))
            current += timedelta(seconds=step_seconds)
        
        # Final event at transition end
        if transition_end > now:
            events.append(("TRANS_END", transition_end))
        
        return events
    
    def _calculate_next_update_time(self, now: datetime) -> Optional[datetime]:
        """Calculate next update time."""
        events = []

        occurrences = self._occurrences(now)

        # Transition events (today's occurrence covers a transition that is
        # already running, tomorrow's the next one)
        for kf, kf_dt in occurrences:
            trans_events = self._calculate_transition_events(kf, kf_dt, now)
            events.extend(trans_events)
            
            if kf.mode == "instant":
                if kf_dt > now:
                    events.append(("KEYFRAME", kf_dt))
            
            # BUGFIX: Add interpolate keyframe events
            if kf.mode == "interpolate":
                if kf_dt > now:
                    events.append(("INTERPOLATE_KF", kf_dt))
        
        # Interpolate interval events
        prev_kf, next_kf = self._find_keyframe_segment(now)
        
        # Generate interpolate events if:
        # - Previous is interpolate, OR
        # - Previous is transition and next is interpolate (continue after transition)
        should_interpolate = False
        if prev_kf and next_kf and next_kf.mode == "interpolate":
            if prev_kf.mode == "interpolate":
                should_interpolate = True
            elif prev_kf.mode == "transition":
                # After transition ends, interpolate to next keyframe if it's interpolate mode
                should_interpolate = True
        
        next_kf_dt = None
        if should_interpolate:
            next_kf_dt = next(
                (dt for kf, dt in occurrences if dt > now and self._same_keyframe(kf, next_kf)),
                None,
            )
            should_interpolate = next_kf_dt is not None

        if should_interpolate:

            # CRITICAL: Adjust interval based on hardware limits!
            # If hardware max is less than user's stepMinutes, use hardware max
            # to avoid flickering from interrupted transitions
            user_step_seconds = self.user_step_seconds
            effective_step_seconds = min(user_step_seconds, self.max_transition_seconds)
            
            # OPTIMIZATION: Adaptive step size based on change rate
            # Calculate change rate per minute
            time_to_next_minutes = (next_kf_dt - now).total_seconds() / 60
            kelvin_change_rate = abs(next_kf.kelvin - prev_kf.kelvin) / max(time_to_next_minutes, 1)
            brightness_change_rate = abs(next_kf.dim - prev_kf.dim) / max(time_to_next_minutes, 1)
            
            # If changes are minimal, increase step size (fewer updates)
            # Thresholds are hardware-agnostic
            if kelvin_change_rate < 30 and brightness_change_rate < 0.5:
                # Very slow change - update less frequently
                # Scale based on hardware: use 2-3x the max transition time
                effective_step_seconds = min(effective_step_seconds * 3, self.max_transition_seconds * 2)
            elif kelvin_change_rate < 60 and brightness_change_rate < 1:
                # Moderate change - update moderately
                effective_step_seconds = min(effective_step_seconds * 2, self.max_transition_seconds * 1.5)
            # else: Fast change - use normal step size (hardware max transition)
            
            interval = timedelta(seconds=effective_step_seconds)
            current = now + interval
            
            # Align to interval boundary
            # Use effective step for alignment (not user step!)
            minutes_since_midnight = current.hour * 60 + current.minute
            step_minutes = int(effective_step_seconds // 60)
            if step_minutes < 1:
                step_minutes = 1  # Minimum 1 minute alignment
            
            aligned_minutes = int(((minutes_since_midnight // step_minutes) + 1) * step_minutes)
            
            # Handle day overflow (aligned_minutes >= 1440)
            if aligned_minutes >= 1440:
                # Next day - add timedelta instead of replace
                overflow_minutes = aligned_minutes - 1440
                current = current.replace(hour=0, minute=0, second=0, microsecond=0)
                current = current + timedelta(days=1, minutes=overflow_minutes)
            else:
                current = current.replace(
                    hour=int(aligned_minutes // 60),
                    minute=int(aligned_minutes % 60),
                    second=0,
                    microsecond=0
                )
            
            while current < next_kf_dt:
                events.append(("INTERPOLATE_TICK", current))
                current += interval
        
        # Return earliest event
        if not events:
            return now + timedelta(seconds=self.user_step_seconds)
        
        events.sort(key=lambda e: e[1])
        event_type, event_time = events[0]
        
        _LOGGER.debug("%s: Next event type: %s", self.name, event_type)
        
        return event_time
    
    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data."""
        now = dt_util.now()
        
        # Evaluate schedule
        values = self.evaluator.evaluate_at(now)
        
        # Calculate next event
        next_update = self._calculate_next_update_time(now)
        
        if next_update:
            time_to_next = (next_update - now).total_seconds()
            
            # Transition time is the MINIMUM of:
            # 1. Time until next event (avoid interrupts)
            # 2. Schedule's intended transition time
            # 3. Hardware maximum
            transition_seconds = max(
                1,  # Minimum 1 second
                min(
                    time_to_next,              # Don't exceed time to next event
                    values.transition_seconds, # Respect schedule's transition ← ADDED!
                    self.max_transition_seconds # Hardware limit
                )
            )
        else:
            # Fallback if no next event calculated
            transition_seconds = min(
                values.transition_seconds,
                self.max_transition_seconds
            )
        
        kelvin_int = int(round(values.kelvin))

        # Today's firing times, e.g. ["06:00", "19:29 (sunset +30 min)"]
        keyframes_today = []
        for kf in sorted(
            self.evaluator.keyframes_for(self.evaluator.local_now(now).date()),
            key=lambda k: self._parse_time(k.time),
        ):
            label = kf.time
            if kf.trigger == "sun":
                offset = f" {kf.offset_minutes:+d} min" if kf.offset_minutes else ""
                label += f" ({kf.sun_event}{offset})"
            keyframes_today.append(label)

        return {
            "kelvin": kelvin_int,
            "mired": kelvin_to_mired(kelvin_int),
            "brightness": float(values.dim),
            "transition_seconds": int(transition_seconds),
            "next_change": next_update or (now + timedelta(seconds=self.user_step_seconds)),
            "keyframes_today": keyframes_today,
        }
    
    async def async_shutdown(self):
        """Shutdown coordinator."""
        if self._unsub_next_update:
            self._unsub_next_update()
            self._unsub_next_update = None


_PANEL_URL_PATH = "keyframe-scheduler"
_STATIC_URL_PATH = "/keyframe_scheduler"


def _home_location(hass: HomeAssistant) -> Optional[Tuple[float, float]]:
    """Home location configured in HA — used for sun keyframes when the
    schedule file carries no location of its own."""
    if hass.config.latitude is None or hass.config.longitude is None:
        return None
    return (float(hass.config.latitude), float(hass.config.longitude))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Keyframe Scheduler from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register static path for the bundled webapp (once per HA process)
    if not hass.data[DOMAIN].get("static_path_registered"):
        webapp_dir = os.path.join(os.path.dirname(__file__), "www")
        if os.path.isdir(webapp_dir):
            try:
                await hass.http.async_register_static_paths([
                    StaticPathConfig(_STATIC_URL_PATH, webapp_dir, cache_headers=False)
                ])
                hass.data[DOMAIN]["static_path_registered"] = True
            except Exception as err:
                _LOGGER.warning("Could not register static path for webapp: %s", err)

    # Register sidebar panel (once — panels are global, not per entry)
    if not hass.data[DOMAIN].get("panel_registered"):
        try:
            frontend.async_register_built_in_panel(
                hass,
                component_name="iframe",
                sidebar_title="Keyframe Scheduler",
                sidebar_icon="mdi:chart-timeline-variant",
                frontend_url_path=_PANEL_URL_PATH,
                config={"url": f"{_STATIC_URL_PATH}/index.html"},
                require_admin=False,
            )
            hass.data[DOMAIN]["panel_registered"] = True
        except Exception as err:
            _LOGGER.warning("Could not register the sidebar panel: %s", err)

    # Initialize store
    if "store" not in hass.data[DOMAIN]:
        store = ScheduleStore(hass)
        await store.async_load()
        hass.data[DOMAIN]["store"] = store
    else:
        store = hass.data[DOMAIN]["store"]

    # Load schedule
    schedule_dict = await store.async_get(entry.entry_id)
    
    # Check options
    if entry.options.get("schedule_json"):
        try:
            schedule_dict = json.loads(entry.options["schedule_json"])
            await store.async_set(entry.entry_id, schedule_dict)
        except Exception as err:
            _LOGGER.error("Invalid schedule_json in options: %s", err)

    # Default schedule
    if not schedule_dict:
        schedule_dict = {
            "version": 1,
            "timezone": hass.config.time_zone or "Europe/Berlin",
            "stepMinutes": DEFAULT_STEP_MINUTES,
            "horizonHours": 48,
            "keyframes": [],
        }
        await store.async_set(entry.entry_id, schedule_dict)

    # Lights controlled by this instance (with their type) and control options
    lights = lights_from_options(entry.options)
    controller_options = dict(
        type_overrides=entry.options.get(CONF_TYPE_OVERRIDES) or {},
        resume_mode=entry.options.get(CONF_RESUME_MODE, RESUME_NEVER),
        resume_minutes=float(entry.options.get(CONF_RESUME_MINUTES, 0) or 0),
        detect_non_ha_changes=bool(entry.options.get(CONF_DETECT_NON_HA_CHANGES, True)),
    )

    # Create evaluator
    try:
        spec = spec_from_dict(schedule_dict, _home_location(hass))
        evaluator = Evaluator(spec, DEFAULT_KELVIN, DEFAULT_DIM)
    except Exception as err:
        _LOGGER.error("Failed to create evaluator: %s", err)
        return False

    # Tick limit of the coordinator. It only feeds the sensors — the lights
    # are driven by the controller, each at the pace of its own type.
    max_transition_seconds = entry.options.get("max_transition_seconds", 300)

    # Create hybrid coordinator
    coordinator = HybridSchedulerCoordinator(
        hass,
        evaluator=evaluator,
        spec=spec,
        name=f"{DOMAIN}_{entry.title}",
        max_transition_seconds=max_transition_seconds,
    )

    # Setup coordinator
    await coordinator.async_setup()

    controller = KeyframeLightController(hass, coordinator, lights, **controller_options)

    # Store
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "evaluator": evaluator,
        "controller": controller,
        "schedule": schedule_dict,
        "title": entry.title,
    }

    _async_register_services(hass)
    _async_register_websocket(hass)

    # Reload when options change (lights, types, resume mode, schedule)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Forward to platforms (the follow switches register with the controller)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()

    return True


async def _async_replace_schedule(hass: HomeAssistant, entry_id: str, schedule_obj: Dict[str, Any]) -> None:
    """Validate, store and activate a new schedule for an instance."""
    try:
        spec = spec_from_dict(schedule_obj, _home_location(hass))
    except Exception as err:
        raise ValueError(f"Invalid schedule: {err}") from err

    store = hass.data[DOMAIN]["store"]
    await store.async_set(entry_id, schedule_obj)

    # The options' schedule wins at every start (async_setup_entry) — keep it
    # in step, or the new schedule would only last until the next restart.
    # Saving the options must not reload the instance: it is updated in place.
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None and entry.options.get("schedule_json"):
        schedule_json = json.dumps(schedule_obj, ensure_ascii=False, indent=2)
        if schedule_json != entry.options["schedule_json"]:   # unchanged: no update, no listener call
            hass.data[DOMAIN].setdefault("skip_reload", set()).add(entry_id)
            hass.config_entries.async_update_entry(entry, options={**entry.options, "schedule_json": schedule_json})

    if entry_id in hass.data[DOMAIN]:
        data = hass.data[DOMAIN][entry_id]
        evaluator = Evaluator(spec, DEFAULT_KELVIN, DEFAULT_DIM)
        await data["coordinator"].async_set_schedule(evaluator, spec)
        data["evaluator"] = evaluator
        data["schedule"] = schedule_obj


def _controlled_entities(hass: HomeAssistant, call: ServiceCall):
    """(controller, light entity_id) pairs a service call refers to."""
    entry_id = call.data.get(ATTR_ENTRY_ID)
    wanted = call.data.get("entity_id")
    if isinstance(wanted, str):
        wanted = [wanted]
    for key, data in hass.data[DOMAIN].items():
        if not isinstance(data, dict) or "controller" not in data:
            continue
        if entry_id and key != entry_id:
            continue
        controller = data["controller"]
        for light_id in controller.lights:
            if not wanted or light_id in wanted:
                yield controller, light_id


@websocket_api.websocket_command({"type": "keyframe_scheduler/schedules"})
@websocket_api.async_response
async def _ws_schedules(hass: HomeAssistant, connection, msg: Dict[str, Any]) -> None:
    """The active schedule of every instance, for the web app's example list:
    {"schedules": [{"entry_id", "title", "schedule"}]}."""
    store = hass.data.get(DOMAIN, {}).get("store")
    schedules = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        schedule = data.get("schedule") if isinstance(data, dict) else None
        if not schedule and store is not None:
            schedule = await store.async_get(entry.entry_id)
        if schedule:
            schedules.append({"entry_id": entry.entry_id, "title": entry.title, "schedule": schedule})
    connection.send_result(msg["id"], {"schedules": schedules})


@websocket_api.require_admin
@websocket_api.websocket_command({
    "type": "keyframe_scheduler/save_schedule",
    "entry_id": str,
    "schedule": dict,
})
@websocket_api.async_response
async def _ws_save_schedule(hass: HomeAssistant, connection, msg: Dict[str, Any]) -> None:
    """Save a schedule from the web app into an instance — validated, active
    at once and kept across restarts (admins only)."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "Unknown Keyframe Scheduler instance")
        return
    try:
        await _async_replace_schedule(hass, entry.entry_id, msg["schedule"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_schedule", str(err))
        return
    _LOGGER.info("Schedule saved from the web app for %s", entry.title)
    connection.send_result(msg["id"], {"entry_id": entry.entry_id, "title": entry.title})


def _async_register_websocket(hass: HomeAssistant) -> None:
    """Register the web app's websocket commands once."""
    if hass.data[DOMAIN].get("ws_registered"):
        return
    websocket_api.async_register_command(hass, _ws_schedules)
    websocket_api.async_register_command(hass, _ws_save_schedule)
    hass.data[DOMAIN]["ws_registered"] = True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the services once for all instances."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    async def handle_set_schedule(call: ServiceCall) -> None:
        """Handle set_schedule service call."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        schedule = call.data.get(ATTR_SCHEDULE)

        if not entry_id:
            raise ValueError("entry_id is required")

        # Parse schedule
        if isinstance(schedule, str):
            schedule_obj = json.loads(schedule)
        elif isinstance(schedule, dict):
            schedule_obj = schedule
        else:
            raise ValueError("schedule must be a dict or JSON string")

        await _async_replace_schedule(hass, entry_id, schedule_obj)
        _LOGGER.info("Schedule updated for entry %s", entry_id)

    async def handle_upload_from_file(call: ServiceCall) -> None:
        """Handle upload_from_file service call - reads JSON from file system."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        file_path = call.data.get(ATTR_FILE_PATH)

        if not entry_id:
            raise ValueError("entry_id is required")

        if not file_path:
            raise ValueError("file_path is required")

        # Read file
        try:
            _LOGGER.info("Reading schedule from file: %s", file_path)

            def _read() -> str:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read()

            file_content = await hass.async_add_executor_job(_read)

            # Remove BOM if present
            if file_content.startswith(chr(0xFEFF)):
                file_content = file_content[1:]

            # Parse JSON
            schedule_obj = json.loads(file_content)

        except FileNotFoundError:
            raise ValueError(f"File not found: {file_path}") from None
        except PermissionError:
            raise ValueError(f"Permission denied reading file: {file_path}") from None
        except json.JSONDecodeError as err:
            raise ValueError(
                f"Invalid JSON in file at line {err.lineno}, column {err.colno}: {err.msg}"
            ) from err
        except Exception as err:
            raise ValueError(f"Error reading file: {err}") from err

        await _async_replace_schedule(hass, entry_id, schedule_obj)
        _LOGGER.info("Schedule uploaded from file %s for entry %s", file_path, entry_id)

    async def handle_apply(call: ServiceCall) -> None:
        """Send the current schedule values to lights now (paused lights stay paused)."""
        by_controller: Dict[Any, List[str]] = {}
        for controller, light_id in _controlled_entities(hass, call):
            by_controller.setdefault(controller, []).append(light_id)
        for controller, light_ids in by_controller.items():
            await controller.async_apply(force=True, only=light_ids)

    async def handle_set_manual_control(call: ServiceCall) -> None:
        """Pause (manual: true) or resume (manual: false) following for lights."""
        manual = bool(call.data.get(ATTR_MANUAL, True))
        for controller, light_id in _controlled_entities(hass, call):
            controller.set_following(light_id, not manual, PAUSE_MANUAL if manual else None)

    def _lights_by_controller(call: ServiceCall):
        """(controller or None, entity_id) for every light of the call: the
        instance whose light (or light group member) it is — None when no
        instance controls it."""
        wanted = call.data.get("entity_id") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        controllers = [
            data["controller"] for data in hass.data[DOMAIN].values()
            if isinstance(data, dict) and "controller" in data
        ]
        for entity_id in wanted:
            yield next((c for c in controllers if c.light_for(entity_id) is not None), None), entity_id

    async def handle_turn_on(call: ServiceCall) -> None:
        """Switch lights on with the schedule's current values (a light no
        instance controls is switched on normally)."""
        transition = call.data.get("transition")
        for controller, entity_id in _lights_by_controller(call):
            if controller is None:
                await hass.services.async_call("light", "turn_on", {"entity_id": entity_id}, blocking=False, context=call.context)
            elif transition is None:
                await controller.async_turn_on(entity_id)
            else:
                await controller.async_turn_on(entity_id, float(transition))

    async def handle_toggle(call: ServiceCall) -> None:
        """Off if on, otherwise on with the schedule's current values."""
        transition = call.data.get("transition")
        for controller, entity_id in _lights_by_controller(call):
            if controller is None:
                await hass.services.async_call("light", "toggle", {"entity_id": entity_id}, blocking=False, context=call.context)
            else:
                await controller.async_toggle(entity_id, None if transition is None else float(transition))

    hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, handle_set_schedule)
    hass.services.async_register(DOMAIN, SERVICE_UPLOAD_FROM_FILE, handle_upload_from_file)
    hass.services.async_register(DOMAIN, SERVICE_APPLY, handle_apply)
    hass.services.async_register(DOMAIN, SERVICE_SET_MANUAL_CONTROL, handle_set_manual_control)
    hass.services.async_register(DOMAIN, SERVICE_TURN_ON, handle_turn_on)
    hass.services.async_register(DOMAIN, SERVICE_TOGGLE, handle_toggle)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are changed (not when only a new
    schedule was written back, see _async_replace_schedule)."""
    skip = hass.data.get(DOMAIN, {}).get("skip_reload", set())
    if entry.entry_id in skip:
        skip.discard(entry.entry_id)
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Stop controlling lights, shutdown coordinator
    if entry.entry_id in hass.data[DOMAIN]:
        data = hass.data[DOMAIN][entry.entry_id]
        await data["controller"].async_stop()
        await data["coordinator"].async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Remove panel when the last config entry is removed
        _meta_keys = {"store", "static_path_registered", "panel_registered", "ws_registered", "skip_reload"}
        remaining_entries = [k for k in hass.data[DOMAIN] if k not in _meta_keys]
        if not remaining_entries and hass.data[DOMAIN].pop("panel_registered", False):
            try:
                frontend.async_remove_panel(hass, _PANEL_URL_PATH)
            except Exception:
                pass

    return unload_ok
