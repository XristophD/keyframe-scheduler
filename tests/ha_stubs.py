"""
Minimal stand-ins for the Home Assistant modules the integration imports,
plus a FakeHass that records service calls and timers — enough to test the
coordinator and the light controller without Home Assistant installed.
"""

import asyncio
import itertools
import os
import sys
import types
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")
_ids = itertools.count(1)


class Context:
    def __init__(self, user_id=None, parent_id=None, id=None):
        self.id = id or f"ctx{next(_ids)}"
        self.user_id = user_id
        self.parent_id = parent_id


class Event:
    def __init__(self, data):
        self.data = data


class State:
    def __init__(self, entity_id, state, attributes=None, context=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = dict(attributes or {})
        self.context = context or Context()


class DataUpdateCoordinator:
    def __init__(self, hass, logger, name=None, **_):
        self.hass, self.name = hass, name
        self.data = None
        self._listeners = []

    def async_add_listener(self, fn):
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)


class CoreState:
    starting = "starting"
    running = "running"


def _async_at_started(hass, at_start_cb):
    """Like HA: run when HA has started; the returned cancel is safe after it ran."""
    entry = [at_start_cb]
    hass.at_started.append(entry)
    return lambda: hass.at_started.remove(entry) if entry in hass.at_started else None


def _install():
    if "homeassistant" in sys.modules and getattr(sys.modules["homeassistant"], "_is_stub", False):
        return

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    class Platform:
        SENSOR = "sensor"
        SWITCH = "switch"

    module("homeassistant", _is_stub=True)
    module("homeassistant.components")
    module("homeassistant.components.frontend")
    module("homeassistant.components.websocket_api",
           websocket_command=lambda schema: (lambda func: func),
           async_response=lambda func: func,
           require_admin=lambda func: func,
           async_register_command=lambda hass, handler: None)
    sys.modules["homeassistant.components"].websocket_api = sys.modules["homeassistant.components.websocket_api"]
    module("homeassistant.components.http", StaticPathConfig=object)
    module("homeassistant.config_entries", ConfigEntry=object)
    module("homeassistant.const", Platform=Platform)
    module("homeassistant.core", HomeAssistant=object, ServiceCall=object, callback=lambda f: f,
           Context=Context, Event=Event, CoreState=CoreState)
    module("homeassistant.helpers.start", async_at_started=_async_at_started)
    module("homeassistant.helpers")
    module("homeassistant.helpers.event",
           async_track_point_in_time=None,
           async_call_later=lambda hass, delay, action: hass.call_later(delay, action),
           async_track_state_change_event=lambda hass, entity_ids, action: hass.track(entity_ids, action))
    module("homeassistant.helpers.storage", Store=object)
    module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator)
    module("homeassistant.util")
    module("homeassistant.util.dt", now=lambda: datetime.now(BERLIN_TZ))
    sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]


_install()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))


class _States:
    def __init__(self):
        self._states = {}

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, entity_id, state, attributes=None, context=None):
        self._states[entity_id] = State(entity_id, state, attributes, context)
        return self._states[entity_id]


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, dict(data), context))


class _Bus:
    def async_listen_once(self, event, fn):
        return lambda: None


class FakeHass:
    """States, recorded service calls, timers and state-change subscriptions."""

    def __init__(self):
        self.states = _States()
        self.services = _Services()
        self.bus = _Bus()
        self.timers = []       # (delay, action)
        self.state = CoreState.starting
        self.at_started = []   # [callback] waiting for HA to have started
        self.tracked = []      # (entity_ids, action)
        self._tasks = []

    def async_create_task(self, coro):
        self._tasks.append(coro)

    def call_later(self, delay, action):
        entry = [delay, action]
        self.timers.append(entry)
        return lambda: self.timers.remove(entry) if entry in self.timers else None

    def track(self, entity_ids, action):
        self.tracked.append((list(entity_ids), action))
        return lambda: None

    def start(self):
        """HA has started: run the async_at_started callbacks once."""
        self.state = CoreState.running
        waiting, self.at_started = self.at_started, []
        for (callback_,) in waiting:
            callback_(self)

    def run(self):
        """Run all pending tasks (repeatedly, tasks may create tasks)."""
        async def _drain():
            while self._tasks:
                tasks, self._tasks = self._tasks, []
                for coro in tasks:
                    await coro
        asyncio.run(_drain())

    def change_state(self, entity_id, state, attributes=None, context=None):
        """Set a state and notify subscribers like HA's state_changed event."""
        old = self.states.get(entity_id)
        new = self.states.set(entity_id, state, attributes, context)
        for entity_ids, action in self.tracked:
            if entity_id in entity_ids:
                action(Event({"entity_id": entity_id, "old_state": old, "new_state": new}))
        return new
