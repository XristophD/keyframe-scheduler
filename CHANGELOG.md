# Changelog

## Component

### [Unreleased]

#### Added
- Services `keyframe_scheduler.turn_on` and `keyframe_scheduler.toggle`: switch lights on with the schedule's current values, so they come on right instead of with their stored state and a visible correction a second later (e.g. a wall button automation with `light.toggle`). Per light: kelvin clamped to its range, brightness only without colour temperature; a group whose members get the same command is switched in one call (Hue room / Zigbee group: all at once); a paused light follows again; lights no instance controls are switched normally

#### Fixed
- Light groups whose members are reported only after start-up (a Hue room loads after the bridge) or change later are expanded again (on a schedule tick, a group state change, or `turn_on`); before, such a group stayed controlled as a whole. The follow switch's `members` attribute shows the expanded members

### [4.0.0] - 2026-10-08

#### Breaking
- **The integration controls the lights itself; the blueprint `keyframe_smart_light_follower` is removed.** Delete existing blueprint automations, otherwise two places control the same light. The previous follow lights are migrated (type from the previous hardware limit) — check them under Configure.

#### Added
- **Valid on** per keyframe (like the PICO's *valid on*): weekdays and/or a yearly period (inclusive, may span New Year). Web app: column "Valid" with an editor row (weekday chips, Mon–Fri / Sat–Sun, period); export `validOn: { weekdays, from, to }`; the integration honours it (`scheduler.is_valid_on`); PDF lists it. PICO export writes it as the entry's `days` filter — including shifted filters for entries that start the evening before, and negated filters so an interpolation gets a ramp from each of its predecessors
- Web app: plausibility hints for valid-on — days on which no keyframe fires ("every Sat, Sun", date ranges) with the longest pause and the value that stays on, interpolations that run over several days, keyframes that never fire (filters exclude each other, group always lost, sun event missing), periods covering the whole year; day view marks a day without keyframes and names the keyframe whose value holds
- PICO export: warns about pauses longer than the PICO's 8-day look-back (it loses the value there) and about multi-day interpolations, which the PICO does not reproduce
- Tests: valid-on parsing, weekdays, period across New Year, days without keyframes, half-year pause, midnight after a differing evening, overnight interpolation
- Web app: **PICO DailyScheduler** export — the schedule as the `DAILYSCHEDULER` behavior of a PICO lightnode `setup.json` (sun triggers with offset, not-before/not-after bounds as nested `EARLIEST`/`LATEST`, groups as composites; solar midnight as noon −12 h, golden hour as sunrise/sunset + the year's mean distance). Output selectable: **DALI** splits fades longer than 15 min into whole-minute pieces on the keyframe's curve and compensates the PICO's rounding of DALI fade times (exactly 900 s would become 16 min); **DMX / other** keeps one entry per keyframe. Preview with entry count, size and a list of differences from the simulation (linear fades instead of sine, fixed fade length of sun-anchored interpolations, changing predecessors, order changes during the year)
- Integration: direct light control — lights and light groups (expanded into members) are assigned in the options, each with a light type (DALI, DALI-2 Extended, Casambi, Zigbee, Hue, Z-Wave, Generic) that limits the fade per command and the minimum interval between commands (adjustable per instance). Only lights that are on get commands, the integration never switches lights on; a light that is switched on takes the current values at once
- Integration: manual changes (user, scene, other automation; optionally changes the light reports itself, e.g. a wall dimmer) pause the light via its follow switch; off/on always resumes, optionally also after N minutes or at the next keyframe; a follow switch turned off on purpose stays off (`pause_reason`)
- Integration: services `apply` and `set_manual_control`
- Web app: sun-based keyframes (dawn/dusk twilights, sunrise/sunset, golden hour, solar noon) with offset and optional not-before/not-after bounds
- Web app: location setting (latitude, longitude, time zone), prefilled from Home Assistant when opened as panel
- Web app: year overview heatmap — artificial light brightness, artificial light colour temperature and potential clear-sky daylight (log lux scale) — with sun curves; click a day to open it in the day view
- Web app: day view with date navigation, sun times and night/twilight shading
- Web app: solar midnight as sun trigger (solar noon relabelled "Sonnenmittag")
- Web app: keyframe groups (A–H, any number of members) with a priority rule — earliest applies / latest applies / all (chronological); per day only the winning member fires
- Web app: conflict detection for keyframe pairs that swap order during the year (e.g. sunset vs. 20:00) and are not grouped, independent of the table order; one-click actions to group them; keyframe traces in the year view
- Web app: year view readability — brightness as default mode on a fixed dark → warm-white scale over perceived lightness (CIE L*, so 10 % reads as dim, not off; bright = much light, identical in day and night theme), spread colour-temperature scale, colour legend, sun lines with halo, keyframe labels in a non-overlapping gutter, traces only for moving/skipped keyframes, sun/keyframe overlay toggles, "last keyframe" in the tooltip; conflict list collapsible
- Web app: ungrouped conflicts are marked red on the days of the less frequent order — strip and traces in the year view, markers and notice in the day view, notice with "create group" above the year view
- Web app: hints for setups that silently change the curve over the year — groups mixing modes, interpolate keyframes whose predecessor changes with the day (unless an open conflict already explains it), and interpolations running across midnight
- Web app: hover help on the not-before / not-after bounds of sun keyframes — the keyframe's range over the year, days without the event, and a one-click recommendation derived day by day from the other keyframes (so it never overtakes a neighbour) plus a stand-in time for days without the event
- Web app: sun keyframes show a labelled offset ("Versatz … min") with hover help that works out the viewed day's time; the table shows settings only — firing times are shown in the views
- Web app: day chart marks when each keyframe fires ("#n" above the plot, red for overlaps); legend moved below the chart
- Web app: three selectable examples — LKL office profile "Büroprofil Lichtdusche" (local time, two light showers), sun only, mixed (with conflicts in Berlin); loading an example also sets the schedule name
- Export: keyframe `id` and `group`, plus a `groups` array with each group's rule
- Export: `location` block and `trigger`/`sunEvent`/`offsetMinutes`/`notBefore`/`notAfter` per keyframe; `time` carries today's resolved time as fallback for older integration versions
- Integration: sun keyframes and keyframe groups — `astro.py` (1:1 port of the web app's sun model, identical times), `scheduler.resolve_day` resolves each day's keyframes (sun event + offset, bounds, fallback, group rules); evaluator and coordinator work on the resolved keyframes of today/tomorrow, so update times follow the sun day by day
- Integration: location from the schedule's `location` block, otherwise the Home Assistant home location
- Integration: sensor attribute `keyframes_today` with today's firing times, e.g. `["07:00", "22:03 (sunset +30 min)"]`
- Tests: `tests/` with unit tests for sun times, resolution, groups, evaluation and coordinator events (`python -m unittest discover -s tests`, no Home Assistant needed)

#### Changed
- **Light control: every light runs at the pace of its own type.** Lights of different types in one instance used to share the tick of the strictest type (a Zigbee light next to a DALI light got 90 s fades too). Now each light follows the curve on its own timer, in fades as long as its type allows; each command targets the curve's value at the end of its fade. Long transitions and interpolations no longer arrive early (the final value used to be sent with a clipped fade). Instant keyframes switch with a 1 s fade
- Integration: inside a transition window the evaluator (and the sensors) return the value on the way — as the web app's simulation — instead of the target
- Evaluation across midnight uses the real neighbouring days (web app `resolveTimeline`, integration `Evaluator.timeline_for`): the night after a differing evening follows that evening, and a day without keyframes holds the last value (as the PICO does). Previously the same day's list was wrapped onto itself
- Coordinator: transitions of yesterday that run past midnight are stepped through as well
- Web app restyled on the LKL UI style guide 1.0.0 (`www/styleguide/`): design tokens, glass/paper skins, day/night/auto theme with switcher in the header
- Chart colours follow the active theme; PDF export always renders the chart on white
- Keyframe table: brightness before colour temperature (also in the PDF); all columns aligned to the first line of the row
- Day chart: colour temperature on a real Kelvin axis spanning the schedule's own range (was normalised 2000–6500 K → 0–100); summary tiles below the chart removed

#### Removed
- One-shot mode ("Wiederholung: Einmalig", `wrapAround: false`) and `startDateTime`, in the web app and the integration: every schedule is a daily profile that continues across midnight. Both fields in older files are ignored; export no longer writes them.

#### Fixed
- Options: "Adjust light type timings" was ignored when no light was selected (the dialog saved at once); it now opens the timings of all types. The label says that the timings follow on the last page
- Web app: a transition of 0 s no longer divides by zero in the simulation
- Light type "DALI-2 Extended Fade": longest fade 900 s instead of 1620 s — DALI-2 extended fade time ends at 16 min, and no DALI fade may exceed 15 min
- Sidebar panel: `frontend` and `http` declared as dependencies; a failed panel registration is logged
- Reloading an instance no longer logs "Unable to remove unknown job listener … homeassistant_started": the controller waits for the start of Home Assistant with `async_at_started` (safe to cancel after it ran) and does not wait at all when Home Assistant already runs
- Web app simulation: value froze at midnight when the day's last keyframe was a transition followed by interpolate keyframes (wrapped keyframes lacked their index/day shift) — the curve now continues across midnight
- Web app simulation: after a transition, interpolation now only continues to the immediately following keyframe (with that keyframe's curve), matching the HA integration; previously it skipped ahead to the next interpolate keyframe
- Web app simulation: "before" transitions now ramp during their window instead of jumping at the keyframe time
- Web app: syntax error in the legacy `switchLanguage` script block (introduced with the restyle)
- `set_schedule` / `upload_from_file` now update the running coordinator in place — sensors (and lights) pick up the new schedule without reloading the integration
- Minimum Home Assistant version corrected to 2024.7 (the integration uses `StaticPathConfig`)
- The evaluator now takes the time of day from the local wall clock instead of counting minutes from the midnight at which it was created — after a DST switch the schedule no longer runs one hour off until HA is restarted
- Coordinator: a transition that has already started is now stepped through to its end (previously only the next day's occurrence of the keyframe was considered)
---

### [3.0.10] - 2026-03-13

#### Added
- HACS distribution support (`hacs.json`)
- Fixed documentation and issue tracker URLs in `manifest.json`

#### Changed
- Blueprint now ships with version header (`blueprint_version: 1.0`)

---

### [3.0.10] - Initial tracked release

Core features at this version:
- Time-based keyframe interpolation for brightness and color temperature
- Config flow UI for creating and managing scheduler sensors
- Event-based coordinator with DataUpdateCoordinator
- Context-aware PICO internal update detection (prevents false-positive manual override)
- `set_schedule` and `upload_from_file` services
- Bundled blueprint for PICOlightnode follower automation

---

## Blueprint: keyframe_smart_light_follower

### [1.0] - 2026-03-13

Initial versioned release. Corresponds to the blueprint formerly known as "v4.0" in the blueprint name.

Features:
- Applies Keyframe Scheduler sensor outputs to a target light via `light.turn_on`
- Context-aware manual override detection (true user actions vs. PICO internal updates)
- Ignores PICO Smart Restore context (`picolightnode` in context ID)
- Smooth sync on Follow External enable (3s transition)
- Handles missing/unavailable sensors gracefully
- Auto-scaled DIM input (0..100 or 0..1)
- Reads `transition_seconds` from Keyframe Scheduler sensor attributes
- Triggered every 60s and on sensor state change

Requires: Keyframe Scheduler Component v2.6.0+, PICOlightnode v2.0.18+ (for context tracking)
