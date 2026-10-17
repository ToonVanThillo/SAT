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
- Closing all valves doesn't stop an active boiler; valves only gate turning
  the heater on.
- For EMS-ESP and OpenTherm over MQTT, the heater check compares with
  whether the boiler is heating (`device_active`), not with its heating-enabled
  setting. ON is resent every cycle while the boiler is enabled but idle and a
  valve is open. In HVAC OFF, OFF is resent every cycle while the boiler is
  heating, but never sent while it is enabled but idle. Migrating these
  backends to channels (plan steps 2h and 4) addresses this.
- The heating loop doesn't re-check the HVAC mode after its awaits, so a cycle
  that is running when the HVAC mode changes can still submit the previous
  mode's desired state afterwards; the next cycle corrects it.
- In HVAC OFF the boiler keeps reporting the last setpoint written in HEAT.
  While the boiler temperature is within `BOILER_DEADBAND` (2 °C) of it, the
  status is AT_SETPOINT, which flame health (`_HEATING_STATUSES` in `flame.py`)
  counts as heat demand. With the flame off for longer than `STUCK_OFF_SECONDS`
  (300 s), flame health reports STUCK_OFF although nothing is wrong.

## Climate entity
- `async_set_hvac_mode` with cascade stops at the first radiator (or room, with
  `sync_climates_with_mode`) that has no state or doesn't list the requested
  mode; the remaining climates, the state write and the loop scheduling are
  skipped. A climate without `hvac_modes` raises.
- Unreadable climate inputs have no diagnostic entity. A non-numeric inside,
  humidity or outside sensor (or weather `temperature` attribute) is treated as
  unavailable and only reported by a log warning, once per unreadable period of
  that entity; the next outside entity is used if one is readable. A radiator or
  room with a non-numeric target or current temperature is skipped by
  `valves_open` without a log.
- With thermal comfort, `current_temperature` is None when the humidity sensor
  is unreadable, unavailable or not configured (the config flow allows thermal
  comfort without one), so HEAT stops after the snapshot phase although the
  inside temperature is readable. Only a non-numeric humidity value is logged.
- Outside the control cycle, room target temperatures are still converted with
  a bare `float()` (the `_async_climate_changed` state listener and
  `_async_update_rooms_from_climates` at startup), so a room with a non-numeric
  target raises there.

## Device communication
- With the EMS max relative modulation floor option set above the requested
  value, SAT's EMS coordinator sends the floor as `burnmaxpower`. The base
  coordinator's `async_submit` compares the read-back floor with the requested
  value and resends every cycle. Plan step 2h addresses this.
- With PWM, relative modulation's hot water state makes the climate send the
  maximum relative modulation (default 100) as `burnmaxpower` at each tap water
  draw, and the PWM value again afterwards. EMS-ESP limits hot water power with
  `dhw.maxpower`, not `burnmaxpower`, so these writes have no use there. Plan
  step 2i addresses this.
- EMS-ESP on/off values are only recognised as "on"; with EMS-ESP's boolean
  format set to true/false or 1/0, `hot_water_active`, `device_active` and
  `flame_active` are always False.
- OpenTherm over MQTT uses bare `float()`, so a non-numeric payload such as
  "unavailable" raises on access instead of giving None.
- A failed MQTT publish is logged and swallowed, so the setter completes as if
  it succeeded. Plan step 2h addresses this for EMS-ESP; step 4 for the other
  MQTT backends.
- The simulator overrides the coordinator update step without calling the
  base, so `_device_on_since` is never set and its target stays at the minimum
  (10 °C, or the minimum setpoint); the simulator never heats towards the
  setpoint.
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
  values' age. A stored EMS-ESP `tapwateractive: "on"` (HA stopped during a
  draw) keeps hot water active until the first live message. In HVAC OFF the
  first loop can send heater OFF based on stored values; from plan step 2h,
  channel writes wait for a live read-back.
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
  restoring it). Calibration is also reported to have problems in general.
- Calibration in the config flow sets the SAT climate to OFF and drives the
  boiler through its own coordinator; the climate's OFF cycle keeps running and
  switches the heater off whenever it sees the boiler active, interrupting the
  calibration.
- `overshoot_protection.py` waits for the boiler temperature in a loop without
  `await` when it is missing or 0.0, which blocks the HA event loop;
  `asyncio.wait_for` cannot interrupt it.

## Setup
- Platforms are set up concurrently. If `sensor` or `binary_sensor` runs before
  `climate` has registered itself, `sensor` adds no entities and `binary_sensor`
  raises; nothing retries. Tests set up the base components first.
