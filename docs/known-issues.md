# Known issues

Bugs found while refactoring the communication layer. They are out of scope
unless a plan step is named. Tests that pin one are labelled "current behaviour,
known bug".

## Control logic
- Overshoot handling only enables PWM with the dynamic minimum setpoint (version
  2); in the default config `pwm.reset()` undoes it in the same cycle.
- `schedule_control_heating_loop(force=True)` only runs the loop immediately if
  a run is already pending; otherwise it waits the normal 10 s.
- `boiler_temperature_filtered` is not a filtered temperature but the mean
  absolute change between samples of the last 60 s. It is passed to the PIDs,
  which store it but never use it.
- Closing all valves doesn't stop an active boiler; valves only gate turning
  the heater on.
- For OpenTherm over MQTT, the heater check compares with whether the boiler
  is heating (`device_active`), not with its heating-enabled setting. ON is
  resent every cycle while the boiler is enabled but idle and a valve is open.
  In HVAC OFF, OFF is resent every cycle while the boiler is heating, but never
  sent while it is enabled but idle. EMS-ESP compares with the `heatingoff`
  setting. Plan step 4 addresses this.
- The heating loop doesn't re-check the HVAC mode after its awaits, so a cycle
  that is running when the HVAC mode changes can still submit the previous
  mode's desired state afterwards; the next cycle corrects it.
- On backends without channels (e.g. OpenTherm over MQTT), in HVAC OFF the
  boiler keeps reporting the last setpoint written in HEAT: the release does
  not clear the gateway's override (`CS=0`). While the boiler temperature is
  within `BOILER_DEADBAND` (2 °C) of it, the status is AT_SETPOINT, which flame
  health (`_HEATING_STATUSES` in `flame.py`) counts as heat demand. With the
  flame off for longer than `STUCK_OFF_SECONDS` (300 s), flame health reports
  STUCK_OFF although nothing is wrong. EMS-ESP reports `selflowtemp` 0 under
  `heatingoff` (status IDLE), unless it falls back to the boiler's knob value
  (see the open question in the design doc).
- With PWM, a minimum setpoint ≤ `COLD_SETPOINT` (28.2 °C) makes the ON phase
  request heater OFF.
- With PWM, the OFF phase requests `MINIMUM_SETPOINT`; the next cycle reads it
  back as ≤ 10, so relative modulation is COLD and requests the configured
  maximum. The first ON-phase cycle still decides from that read-back and keeps
  the maximum; the next requests 0 (or the floor) again. The max relative
  modulation is written twice per PWM cycle, and the boiler starts each ON
  phase at the maximum. Observed on EMS-ESP.

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
- EMS-ESP on/off values are only recognised as "on"/"off"; with EMS-ESP's
  boolean format set to true/false or 1/0, `hot_water_active`, `device_active`
  and `flame_active` are always False, and the heater channel never gets a
  read-back of `heatingoff`, so it never writes.
- OpenTherm over MQTT uses bare `float()`, so a non-numeric payload such as
  "unavailable" raises on access instead of giving None.
- For OpenTherm over MQTT, a failed publish is logged and swallowed, so the
  setter completes as if it succeeded. Plan step 4 addresses this.
- The simulator overrides the coordinator update step without calling the
  base, so `_device_on_since` is never set and its target stays at the minimum
  (10 °C, or the minimum setpoint); the simulator never heats towards the
  setpoint.
- Device data that changes more often than every 5 s never notifies listeners,
  because each update restarts the debounce. See the open question on cycle
  triggering in the design doc.
- On EMS-ESP, a configured maximum relative modulation or floor above the
  boiler's own `burnmaxpower` maximum is never confirmed: the boiler reads
  back its own value, so the configured value is resent after every new
  read-back and the channel stays `OUT_OF_SYNC`.

## Synchro sensors (replaced per backend as it migrates to channels; removed in plan step 4)
- Control Setpoint Synchro compares the unclamped climate setpoint with the
  clamped value sent, so it reports a mismatch above the maximum setpoint.
- Central Heating Synchro can never trigger in HEAT mode: it compares
  `hvac_action`, which is derived from `device_active`, with `device_active`.

## Startup and stored data
- When HA is already running (reload or new entry), the first control loop runs
  before the MQTT subscription; on a cold start it waits for HA to start, after
  subscribing. Either way the control logic doesn't wait for live data or check
  the stored values' age. A stored EMS-ESP `tapwateractive: "on"` (HA stopped
  during a draw) keeps hot water active until the first live message. In HVAC
  OFF the first loop can send heater OFF based on stored values, except on
  EMS-ESP, whose channel writes wait for a live read-back.
- The config flow's manufacturer step reads `member_id` from stored data
  without subscribing to MQTT. On a fresh install there is no stored
  `member_id`, so no manufacturer is preselected.
- The manufacturer step's default is the detected manufacturer's
  `friendly_name`, but the selector options use the `MANUFACTURERS` key. Where
  they differ (ATAG, BRÖTGE, De Dietrich, Worcester Bosch) the detected
  manufacturer is not preselected.
- Calibration (overshoot protection) in the config flow never subscribes to
  MQTT: on OpenTherm over MQTT it sends heating commands and waits for flame
  and boiler temperature that never update, so it times out (or hangs, see
  below). On timeout the boiler is left at heater ON with the calibration
  setpoint, because the reset in `OvershootProtection.calculate` only runs on
  cancellation, and the climate's previous HVAC mode is not restored (the
  config flow aborts before restoring it). Calibration is also reported to have problems in general.
- Calibration on EMS-ESP publishes nothing: the config flow's coordinator never
  subscribes to MQTT and never verifies a snapshot, so its channels wait for a
  live read-back forever.
- Calibration in the config flow sets the SAT climate to OFF and drives the
  boiler through its own coordinator; on OpenTherm over MQTT the climate's OFF
  cycle keeps running and switches the heater off whenever it sees the boiler
  active, interrupting the calibration.
- Calibration requests a fixed max relative modulation of 100
  (`MAXIMUM_RELATIVE_MODULATION`, used by `overshoot_protection.py`), regardless of
  the configured maximum relative modulation.
- `overshoot_protection.py` waits for the boiler temperature in a loop without
  `await` when it is missing or 0.0, which blocks the HA event loop;
  `asyncio.wait_for` cannot interrupt it.

## Setup
- Platforms are set up concurrently. If `sensor` or `binary_sensor` runs before
  `climate` has registered itself, `sensor` adds no entities and `binary_sensor`
  raises; nothing retries. Tests set up the base components first.
- The manifest registers `ems-esp/#` for MQTT discovery. HA never drops this
  subscription and only skips a message whose payload equals the previous one
  on the same topic, so every changed message on any EMS-ESP topic starts a
  discovery config flow, also when the entry exists. This includes SAT's own
  commands on `ems-esp/boiler`. `async_step_mqtt` aborts these with
  `already_configured` without logging. That is at least one flow every 10 s
  (`boiler_data`). With more than 10 distinct topics, these aborts also keep
  changing the entry's list of its last 10 discovery topics, and HA saves the
  config entries again.
- The manifest's OpenTherm discovery filter `OTGW/value/+` matches one level
  after `value/`, but the gateway's values are on `OTGW/value/<device>/<key>`,
  so OpenTherm over MQTT is never discovered. If the filter matched those
  topics, `async_step_mqtt` would use `<device>/<key>` as the unique ID,
  because it takes everything after `OTGW/value/`.
