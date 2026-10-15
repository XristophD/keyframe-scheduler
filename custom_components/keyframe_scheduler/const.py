"""Constants for Keyframe Scheduler integration."""

from homeassistant.const import Platform

DOMAIN = "keyframe_scheduler"

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]

# ---- Options ------------------------------------------------------------------

# Legacy: list of light entity ids that got a follow switch (before the
# integration controlled lights itself). Migrated to CONF_LIGHTS.
CONF_FOLLOW_LIGHTS = "follow_lights"

# Lights controlled by an instance: {entity_id: light type}. Light groups are
# expanded into their members at runtime; the members inherit the type.
CONF_LIGHTS = "lights"

# What happens after a manual change (dashboard, app, scene, wall dimmer).
# Turning a light off and on again always resumes the schedule.
CONF_RESUME_MODE = "resume_mode"
RESUME_NEVER = "never"            # only off/on (or the follow switch) resumes
RESUME_AFTER_MINUTES = "minutes"  # resume after CONF_RESUME_MINUTES
RESUME_NEXT_KEYFRAME = "next_keyframe"  # resume when the next keyframe fires
RESUME_MODES = [RESUME_NEVER, RESUME_AFTER_MINUTES, RESUME_NEXT_KEYFRAME]
CONF_RESUME_MINUTES = "resume_minutes"

# Also treat changes the light reports by itself (e.g. a wall dimmer on the
# bus) as manual, by comparing the reported state with what was sent.
CONF_DETECT_NON_HA_CHANGES = "detect_non_ha_changes"

# Per-instance overrides of the light type profiles:
# {light type: {"max_transition": s, "min_interval": s}}
CONF_TYPE_OVERRIDES = "type_overrides"

# ---- Light types --------------------------------------------------------------
#
# max_transition: longest fade the hardware accepts in one command (seconds)
# min_interval:   minimum time between two commands to the same light (seconds)
#                 — protects slow buses (DALI, Bluetooth mesh) from flooding.
# Every light is driven at its own pace: it follows the curve in pieces of at
# most max_transition, each command fading to the curve's value at the end of
# the piece (light_logic.plan_step).
# The max_transition values are the hardware limits the integration has always
# used; the min_interval values are conservative defaults — adjust them per
# instance (options) to what your installation handles.
LIGHT_TYPES = {
    "dali":           {"max_transition": 90,   "min_interval": 30},
    # DALI-2 extended fade time ends at 16 min; no fade longer than 15 min
    "dali2_extended": {"max_transition": 900,  "min_interval": 30},
    "casambi":        {"max_transition": 600,  "min_interval": 30},
    "zigbee":         {"max_transition": 600,  "min_interval": 15},
    "hue":            {"max_transition": 600,  "min_interval": 10},
    "zwave":          {"max_transition": 300,  "min_interval": 30},
    "generic":        {"max_transition": 300,  "min_interval": 15},
}
DEFAULT_LIGHT_TYPE = "generic"

# Manual-change detection tolerances (reported state vs. last command)
BRIGHTNESS_TOLERANCE_PCT = 5
KELVIN_TOLERANCE = 150
# Grace period after a command's transition before reported states are judged
TRANSITION_GRACE_SECONDS = 10
# Fade used when a light is switched on (or following is re-enabled)
TURN_ON_TRANSITION_SECONDS = 1
# A fade is a straight line: on a curved stretch (sine) it may leave the
# curve by at most this much — otherwise the stretch is split further
SEGMENT_BRIGHTNESS_TOLERANCE_PCT = 1.0
SEGMENT_KELVIN_TOLERANCE = 30

# Storage
STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.schedules"

# Services
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_UPLOAD_FROM_FILE = "upload_from_file"
SERVICE_APPLY = "apply"
SERVICE_SET_MANUAL_CONTROL = "set_manual_control"
SERVICE_TURN_ON = "turn_on"
SERVICE_TOGGLE = "toggle"

# Service attributes
ATTR_ENTRY_ID = "entry_id"
ATTR_SCHEDULE = "schedule"
ATTR_FILE_PATH = "file_path"
ATTR_MANUAL = "manual"

# Default values
DEFAULT_KELVIN = 4000
DEFAULT_DIM = 50
DEFAULT_STEP_MINUTES = 5
