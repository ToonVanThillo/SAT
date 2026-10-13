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
- EMS-ESP raises the max relative modulation (`burnmaxpower`) to at least 31 for
  every boiler, not only the Junkers Cerapur. The climate compares the read-back
  31 with the requested 0 and resends every cycle. Plan step 2 addresses both.
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
- Relative Modulation Synchro reports a mismatch on EMS because of the 31 floor.
- Central Heating Synchro can never trigger in HEAT mode: it compares
  `hvac_action`, which is derived from `device_active`, with `device_active`.

## Setup
- Platforms are set up concurrently. If `sensor` or `binary_sensor` runs before
  `climate` has registered itself, `sensor` adds no entities and `binary_sensor`
  raises; nothing retries. Tests set up the base components first.
