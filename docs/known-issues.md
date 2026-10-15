# Known issues

Bugs found while refactoring the communication layer. They are out of scope
unless a plan step is named. Tests that pin one are labelled "current behaviour,
known bug".

## Control logic
- Overshoot handling only enables PWM with the dynamic minimum setpoint (version
  2); in the default config `pwm.reset()` undoes it in the same cycle.
- `schedule_control_heating_loop(force=True)` only runs the loop immediately if
  a run is already pending; otherwise it waits the normal 10 s.
- `MAX_BOILER_TEMPERATURE_AGE` is 60 s in `const.py` (used by the coordinator)
  and 300 s in `pid.py` (unused).
- `boiler_temperature_filtered` is not a filtered temperature but the mean
  absolute change between samples of the last 60 s. It is passed to the PIDs,
  which store it but never use it.

## Climate entity
- `async_set_hvac_mode` with cascade stops at the first radiator (or room, with
  `sync_climates_with_mode`) that has no state or doesn't list the requested
  mode; the remaining climates, the state write and the loop scheduling are
  skipped. A climate without `hvac_modes` raises.
- `valves_open` raises when a radiator or room is on, reports no `hvac_action`,
  has a current temperature, and has a missing or non-numeric target
  temperature. This aborts the control cycle.
- `current_outside_temperature` raises on a sensor with a non-numeric state, or
  a weather entity without a numeric `temperature` attribute. This aborts the
  control cycle.

## Device communication
- With the EMS max relative modulation floor option set above the requested
  value, SAT's EMS coordinator sends the floor as `burnmaxpower`. The climate
  compares the read-back floor with the requested value and resends every
  cycle. Plan step 2f addresses this.
- OpenTherm over MQTT uses bare `float()`, so a non-numeric payload such as
  "unavailable" raises on access instead of giving None.
- A failed MQTT publish is logged and swallowed, so the setter completes as if
  it succeeded. Plan step 2 addresses this.
- Device data that changes more often than every 5 s never notifies listeners,
  because each update restarts the debounce. See the open question on cycle
  triggering in the design doc.

## Synchro sensors (replaced in plan step 3)
- Control Setpoint Synchro compares the unclamped climate setpoint with the
  clamped value sent, so it reports a mismatch above the maximum setpoint.
- Relative Modulation Synchro reports a mismatch on EMS when the max relative
  modulation floor option raises the requested value.
- Central Heating Synchro can never trigger in HEAT mode: it compares
  `hvac_action`, which is derived from `device_active`, with `device_active`.

## Startup and stored data
- When HA is already running (reload or new entry), the first control loop runs
  before the MQTT subscription; on a cold start it waits for HA to start, after
  subscribing. Either way nothing waits for live data or checks the stored
  values' age.
- The config flow's manufacturer step reads `member_id` from stored data
  without subscribing to MQTT. On a fresh install there is no stored
  `member_id`, so no manufacturer is preselected.
- The manufacturer step's default is the detected manufacturer's
  `friendly_name`, but the selector options use the `MANUFACTURERS` key. Where
  they differ (ATAG, BRÖTGE, De Dietrich, Worcester Bosch) the detected
  manufacturer is not preselected.
- Calibration (overshoot protection) in the config flow never subscribes to
  MQTT: it sends heating commands and waits for flame and boiler temperature
  that never update, so it times out (or hangs, see below). On timeout the
  boiler is left at heater ON with the calibration setpoint, because the reset
  in `OvershootProtection.calculate` only runs on cancellation, and the
  climate's previous HVAC mode is not restored (the config flow aborts before
  restoring it).
- `overshoot_protection.py` waits for the boiler temperature in a loop without
  `await` when it is missing or 0.0, which blocks the HA event loop;
  `asyncio.wait_for` cannot interrupt it.

## Setup
- Platforms are set up concurrently. If `sensor` or `binary_sensor` runs before
  `climate` has registered itself, `sensor` adds no entities and `binary_sensor`
  raises; nothing retries. Tests set up the base components first.
