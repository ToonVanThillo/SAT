# Known issues

Bugs found while refactoring the communication layer. They are out of scope
unless a plan step is named. Tests that pin one are labelled "current behaviour,
known bug".

## Control logic
- Overshoot handling only enables PWM with the dynamic minimum setpoint (version
  2); in the default config `pwm.reset()` undoes it in the same cycle.
- `schedule_control_heating_loop(force=True)` only runs the loop immediately if
  a run is already pending; otherwise it waits the normal 10 s.
- Closing all valves doesn't stop an active boiler; valves only gate turning
  the heater on.
- For OpenTherm over MQTT, the heater check compares with whether the boiler
  is heating (`device_active`), not with its heating-enabled setting. ON is
  resent every cycle while the boiler is enabled but idle and a valve is open.
  Whenever heater OFF is requested (HVAC OFF, no valve open, the PWM off
  phase), OFF is resent every cycle while the boiler is heating, but never sent
  while it is enabled but idle. EMS-ESP compares with the `heatingoff` setting.
  Plan step 8 addresses this.
- The heating loop doesn't re-check the HVAC mode after its awaits, so a cycle
  that is running when the HVAC mode changes can still submit the previous
  mode's desired state afterwards; the next cycle corrects it.
- On backends without channels (e.g. OpenTherm over MQTT), in HVAC OFF the
  boiler keeps reporting the last setpoint written in HEAT: the release does
  not clear the gateway's override (`CS=0`). While the boiler temperature is
  within `BOILER_DEADBAND` (2 °C) of it, the status is AT_SETPOINT, which flame
  health (`_HEATING_STATUSES` in `flame.py`) counts as heat demand. With the
  flame off for longer than `STUCK_OFF_SECONDS` (300 s), flame health reports
  STUCK_OFF although nothing is wrong. EMS-ESP reports `selflowtemp` 5 under
  `heatingoff` (status IDLE).
- With PWM, a minimum setpoint ≤ `COLD_SETPOINT` (28.2 °C) makes the ON phase
  request heater OFF; on EMS-ESP it then also releases the control setpoint.
- With PWM, the OFF phase requests `MINIMUM_SETPOINT` (on EMS-ESP only until
  the heater channel confirms heating off; then it releases the setpoint). The
  boiler reads back ≤ 10 (EMS-ESP: 5 under `heatingoff`), so relative
  modulation is COLD and requests the configured maximum. The first ON-phase
  cycle still decides from that read-back and keeps the maximum; the next
  requests 0 (or the floor) again. The max relative modulation is written twice
  per PWM cycle, and the boiler starts each ON phase at the maximum. Observed
  on EMS-ESP. The same happens after the valves reopen: the boiler reads back
  ≤ 10, so the first heating cycle requests the configured maximum. Plan step 6
  addresses this.
- Flame health stores no average on-time when a cycle completes: the ON→OFF
  transition doesn't record the duration, and the next ON phase seeds the
  average from its first seconds. PWM treats a missing or short (< 60 s)
  average as short cycling and, while the flame is off and SAT is not producing
  hot water, forces its setpoint to the reliable flow temperature + 10 °C.
- The boiler's anti-cycle period (burner min period, minimum 3 min) appears to
  start when heating is re-enabled, not when the flame goes out. In a field
  test the flame relit 3:13 after `heatingoff` was switched off, 6:20 after it
  went out. PWM counts the wait as ON time. Needs further testing. On EMS-ESP,
  valves closing and reopening toggle `heatingoff`, so if this holds, each
  reopen may wait about 3 min before the flame lights. A missing temperature
  toggles `heatingoff` once per episode.
- The temperature tracker skips only a boiler setpoint of exactly
  `MINIMUM_SETPOINT`. EMS-ESP reads back 5 under `heatingoff`, so switching
  heating off counts as a setpoint decrease and starts stabilisation mode at
  the first reliable flow temperature reading under `heatingoff` (the tracker
  takes reliable readings only; on EMS-ESP not while the pump is off).
- On EMS-ESP, PREHEATING effectively never shows (except right after a draw
  without the hot water signals configured, see below). The pump is usually
  off before a heat demand and the cold temperature only takes reliable
  readings, so it is not above the flow temperature when the pump starts. This has no
  control effect: the boiler status shows HEATING_UP, WAITING_FOR_FLAME or
  AT_SETPOINT instead. Flame health no longer treats that phase as transient,
  so it can report STUCK_OFF after `STUCK_OFF_SECONDS` (300 s) of heat demand
  without a flame. Accepted.
- On EMS-ESP without a configured hot water burner power or flow temperature,
  which is every EMS-ESP user with the default options, the pump overrun after
  a draw counts as reliable at once: only the hot water active flag is a
  signal, and it is already off. The trapped water at 55–60 °C then reaches
  the reliable value, so the PWM duty and the boiler status; the tracker; the
  history, derivative and cold temperature (a cold value of about 58 °C for up
  to 60 s, so a heating start within that minute can show PREHEATING); and the
  setpoint adjuster's force step (flow + 10, so a pulse at about 68 °C with the
  dynamic minimum setpoint v2). Accepted for the tested setup; supporting
  EMS-ESP beyond it means making both signals required.
- On EMS-ESP, while SAT's last sent control setpoint equals the configured hot
  water flow temperature, `selflowtemp` at that value is no signal. After a
  draw the pump overrun can then count as reliable, if the burner power has
  already dropped (seen: 134 → 31 at flame-off). Accepted.
- On EMS-ESP, the hot water flow temperature signal can classify a heating burn
  as hot water from a stale `selflowtemp`: while SAT's control setpoint equals
  the configured hot water flow temperature and the flame is on, a payload that
  still shows that `selflowtemp` after the channel's sent value has changed
  counts as the signal. This happens when SAT moves its setpoint away from that
  value (until the boiler reads back the new one) and when SAT releases the
  setpoint on heater OFF (the sent value becomes none). The burn then counts as
  hot water: it is left out of the flame statistics, SAT treats the boiler as
  producing hot water until the flame goes off, and PWM starts no pulse until
  the hot water delay has passed after it. Flow temperature readings are
  unreliable while the stale `selflowtemp` shows, also with the flame off, and
  a pump start in such a payload is not recorded. Accepted.
- The hot water delay is timed from the end of the last hot water burn as the
  flame tracking saw it, which is kept in memory only. A reload or restart
  forgets it: saving any option reloads the entry, so saved 60 s after a hot
  water burn, PWM can start a pulse inside the boiler's heating lockout. A hot
  water burn that ends during a reload or restart is never observed at all.
  The same holds for the held climate inputs and a pending PID reset: an input
  missing after a restart has nothing to hold. Accepted for now.
- `Flame.latest_on_time_seconds` returns the previous burn's on time until the
  second update (listener or control cycle) of a new burn, because the first
  update only starts the burn. It feeds the setpoint adjuster's `> 6` check in
  PWM and flame health's STUCK_ON rule. Since burns are tracked on arrival, the
  flame reports on before any update has run, so the stale value is visible
  from the moment the flame goes on.
- A started PWM pulse is held ON for at least 180 s, while the exposed PWM
  duty cycle keeps following the recalculated on time. With automatic duty
  cycle it can read 0 s on while the status is ON (the low-duty case), and
  flame health's PWM_SHORT check cannot fire then. With a fixed
  duty cycle the on time can read small but non-zero, and PWM_SHORT can still
  fire although the pulse runs 180 s. Follows from the minimum-pulse rule.
- The "valves closed" PWM reset, like the other PWM resets, restarts the hourly
  pulse count and disables PWM. Valves that open and close quickly can start
  more than N pulses per hour: each reopen can start a pulse at once from
  IDLE. With the dynamic minimum setpoint (version 2), PWM stays disabled
  after the valves reopen until overshoot handling enables it again. The
  missing-temperature PWM reset does the same once a missing temperature
  outlasts its 5-minute hold; a held input doesn't reset PWM.
- The PID's age reset (`sensor_max_value_age`) compares the time since the
  PID's last update, not the sensors' age. It fires whenever the PID hasn't
  updated for longer than the max age, which with a short max age means at
  most sensor events. It sets the last and previous errors to 0, and the
  sample-time limit (default 60 s) drops the update in the same call, so the
  proportional term stays 0 until the next state change at least 60 s later.
- While the live inside or target temperature is missing, the event-driven
  updates skip the heating curves and autotune along with the PID, so the
  heating curves don't follow an outside or target change during the gap. This
  only matters within the 5-minute hold; after it the boiler is off.
- Autotune: the learned coefficient (Autotune Coefficient sensor) is only
  reported, never used by the control loop. Its two fuzzy adjustment blocks
  both apply to every sample (the positive block's `< 0.5` branch also catches
  every negative derivative), so the net adjustment is asymmetric at the
  boundaries: a derivative of exactly 1.0 gets +0.1 and one of exactly -1.0
  gets -0.1, against the direction of their neighbours (-0.1 just below 1.0,
  +0.1 just above -1.0). The first sample sets the derivative to the
  coefficient itself instead of a difference, which biases it downward (a
  coefficient above 1 gets -0.2), and a truthiness check treats a previous
  coefficient of 0.0 as none. It runs on the event-driven updates in any HVAC
  mode and before the boiler-temperature gate, and it converts the requested
  setpoint, so the estimate absorbs whatever PID output is present.
- The dynamic minimum setpoint (version 1) raises TypeError when a base return
  temperature is set and the boiler then reports no return temperature during
  HEATING_UP: `MinimumSetpoint.warming_up` passes None to `update_state`, which
  calls `math.isclose` with it, and the control cycle stops there.
  `calculate()` has the same weakness through `relative_modulation_level`.
  Pinned by `tests/test_minimum_setpoint.py::test_warming_up_value_then_none_again`
  (strict xfail).

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
  `valves_open` without a log. With every radiator unavailable or off,
  `valves_open` is False, so the boiler is switched off (accepted; without
  radiators configured the valves count as open). An unreadable inside,
  humidity or outside value is held for 5 minutes, then the boiler is switched
  off.
- With thermal comfort, `current_temperature` is None when the humidity sensor
  is unreadable, unavailable or not configured (the config flow allows thermal
  comfort without one), so HEAT switches the boiler off once the humidity's
  5-minute hold has run out (at once without a sensor), although the inside
  temperature is readable. Only a non-numeric humidity value is logged.
- Staleness of the inside and humidity sensors (`sensor_max_value_age`) uses
  `last_reported`, which moves on every report, also of the same value.
  Integrations that don't write a report when the value is unchanged (likely
  HA MQTT entities without `force_update`, e.g. Zigbee2MQTT, and template
  sensors) still go stale after the max age without a change: the input is
  held for 5 minutes, then the boiler is switched off. Mitigation: set the max
  age to 0, or enable `force_update`.
- While an input is held, the climate's `current_temperature` and the Error
  Value sensor read the live sensors: unknown, and an error of 0. Once the hold
  has run out, the control setpoint (diagnostics download) and the Calculated
  Setpoint sensor keep their last values while the boiler is off.
  Hold lines are also logged in HVAC OFF, where nothing reads the inputs;
  harmless.
- Outside the control cycle, room target temperatures are still converted with
  a bare `float()` (the `_async_climate_changed` state listener and
  `_async_update_rooms_from_climates` at startup), so a room with a non-numeric
  target raises there.
- Flame Health is unavailable while the flame status is INSUFFICIENT_DATA. In
  HA, unavailable means the device can't be reached; missing data should be
  unknown. On backends that store device data (MQTT, serial) the status stays
  INSUFFICIENT_DATA until the first live flame value arrives.
- The default activity and away presets are both 10 °C, and setting a target
  through `async_set_temperature` selects the first preset with that
  temperature, so setting 10 °C selects activity, never away. Leaving a preset
  with PRESET_NONE restores its return target through
  `async_set_target_temperature`, without this matching, but closing the window
  restores the target from before the window opened through
  `async_set_temperature`, so the matching runs. The climate stays in activity
  after the window closes: with preset away (10 °C), an open window past the
  minimum open time switches to activity, and on closing, the return target of
  10 °C matches activity again, which `async_set_preset_mode` skips as already
  set.
- The `pulse_width_modulation` service sets the PWM enabled flag, but the
  climate decides every cycle from its configuration and setpoints whether PWM
  runs (`pulse_width_modulation_enabled`), and resets PWM when it doesn't. The
  flag only counts with overshoot protection, a calculated setpoint and
  version 2 of the dynamic minimum setpoint, so otherwise the service lasts
  only until the next cycle, enable or disable. The clearest case is forced
  PWM, or a boiler without setpoint management: PWM always runs, so disabling
  it through the service is undone by the next cycle. Where the flag counts,
  any PWM reset clears it (valves closed, a missing temperature, an error
  change, or an HVAC mode or target change), and overshoot handling sets it
  each cycle, undoing a disable.
- After the climate is renamed (HA removes and re-adds the entity), room
  temperature sensors are no longer tracked: their listeners are released on
  removal, but `_sensors` keeps their IDs, so `async_track_sensor_temperature`
  returns early on re-add.
- A radiator change that only touches its attributes, such as `hvac_action`
  (a valve opening or closing), doesn't schedule the control loop:
  `_async_main_climate_changed` only reacts to a change of the radiator's
  state. The boiler follows the valves on the next 30 s tick, so at most 40 s
  later; other triggers, such as a room or sensor change, can run the loop
  sooner.

## Device communication
- EMS-ESP on/off values are only recognised as "on"/"off"; with EMS-ESP's
  boolean format set to true/false or 1/0, `hot_water_active`, `device_active`
  and `flame_active` are always False, and the heater channel never gets a
  read-back of `heatingoff`, so it never writes.
- OpenTherm over MQTT uses bare `float()`, so a non-numeric payload such as
  "unavailable" raises on access instead of giving None.
- For OpenTherm over MQTT, a failed publish is logged and swallowed, so the
  setter completes as if it succeeded. Plan step 8 addresses this.
- The simulator overrides the coordinator update step without calling the
  base, so `_device_on_since` is never set and its target stays at the minimum
  (10 °C, or the minimum setpoint); the simulator never heats towards the
  setpoint.
- Device data that changes more often than every 5 s never notifies listeners,
  because each update restarts the debounce. Burns are tracked as data arrives
  (backends that store data per message: MQTT and serial), so their count,
  durations and kind are not affected; the average on time and flame health
  are only updated by the listener and the control cycle. See the open
  question on cycle triggering in the design doc.
- On EMS-ESP, a configured maximum relative modulation or floor above the
  boiler's own `burnmaxpower` maximum is never confirmed: the boiler reads
  back its own value, so the configured value is resent after every new
  read-back and the channel stays `OUT_OF_SYNC`.
- On EMS-ESP, whenever heater OFF follows heater ON (each PWM ON→OFF, the
  first cycle of a missing temperature), the control setpoint is sent as 0
  until heater OFF is confirmed, while the boiler reads back 5 under
  `heatingoff`. The control setpoint channel is briefly `OUT_OF_SYNC` (sent 0,
  read back 5) until it is released.

## Startup and stored data
- When HA is already running (reload or new entry), the first control loop runs
  before the MQTT subscription; on a cold start it waits for HA to start, after
  subscribing. Either way the control logic doesn't wait for live data or check
  the stored values' age. A stored hot water active flag (EMS-ESP
  `tapwateractive`, OpenTherm MQTT `domestichotwater`) is ignored by SAT's
  view of hot water, so after a restart during a draw the boiler counts as
  producing hot water only from the first live flag (or another live
  hot-water signal) on. In HVAC OFF the first loop can send heater OFF based on
  stored values, except on EMS-ESP, whose channel writes wait for a live
  read-back. The same goes for HEAT when an input has not been valid since
  start, so nothing is held: the first loop takes the missing-temperature path
  and, on backends without channels, can switch the heater off from stored
  values. OpenTherm over MQTT doesn't mark its flow temperature live-only,
  so after a restart a restored reading can become the reliable flow
  temperature, and it enters the temperature history, cold temperature and
  tracker.
- EMS-ESP values are stored only on unload, so after an HA restart the restored
  values can be much older than the restart. In a field test the first cycle
  decided on a restored flow temperature of 48.7 °C and flame on (actual: about
  27 °C, flame off): until the first reliable reading the raw flow temperature
  stands in for the reliable one, restored or not. A restored flow temperature
  is never reliable itself. The temperature tracker starts inactive and only
  activates on a reliable reading with the flame off, so after a restart a
  heat-up that starts before such a reading shows as OVERSHOOT_HANDLING until
  the first flame-off with a reliable reading. Flame tracking (burns, flame
  statistics, flame health) ignores a restored flame and starts with the first
  live one. `device_status`, `BoilerState.flame_active` and the temperature
  tracker still read the restored flame, while PWM reads the flame tracking,
  where an unknown flame is inactive: until the first live flame value the
  setpoint adjuster runs its force step on the restored flow temperature.
- The config flow's manufacturer step reads `member_id` from stored data
  without subscribing to MQTT. On a fresh install there is no stored
  `member_id`, so no manufacturer is preselected.
- The manufacturer step's default is the detected manufacturer's
  `friendly_name`, but the selector options use the `MANUFACTURERS` key. Where
  they differ (ATAG, BRÖTGE, De Dietrich, Worcester Bosch) the detected
  manufacturer is not preselected.
- Calibration (overshoot protection) in the config flow never subscribes to
  MQTT: on OpenTherm over MQTT it sends heating commands and waits for flame
  and boiler temperature that never update, so it times out. Calibration is
  also reported to have problems in general.
- Calibration on EMS-ESP publishes nothing: the config flow's coordinator never
  subscribes to MQTT and never verifies a snapshot, so its channels wait for a
  live read-back forever.
- Calibration in the config flow sets the SAT climate to OFF and drives the
  boiler through its own coordinator; on OpenTherm over MQTT the climate's OFF
  cycle keeps running and switches the heater off whenever it sees the boiler
  active, interrupting the calibration.
- Calibration while reconfiguring a serial entry connects a second coordinator
  to the live entry's port. It receives device data while it runs, and its
  teardown sends control setpoint 0 to the gateway (`_graceful_disconnect`).
- Calibration requests a fixed max relative modulation of 100
  (`MAXIMUM_RELATIVE_MODULATION`, used by `overshoot_protection.py`), regardless of
  the configured maximum relative modulation.
- `OvershootProtection.__init__` calls `min()` before checking that the heating
  system's calibration setpoint is known, so an unknown heating system raises
  `TypeError` instead of the intended `ValueError`.
- If the boiler temperature becomes unavailable during calibration's
  stable-temperature phase, `float(None)` raises and calibration aborts. A
  reading of 0.0 with relative modulation 0 submits 0.0 as the control
  setpoint.
- `start_calibration` in the config flow catches `CancelledError` without
  re-raising it, against asyncio and HA convention.
- The calibration step reads the SAT climate's state without a None check
  (`hass.states.get(climate_id).state`), so it raises if the entity is
  registered but has no state.
- If cleaning up the calibration coordinator raises (e.g. a storage error), the
  previous HVAC mode is not restored and the climate stays OFF. If the climate
  was unavailable when calibration started, the restore fails. Either error is
  only logged (as "Task exception was never retrieved") when calibration
  succeeded.
- The calibration step awaits turning the climate OFF before it records the
  calibration task, so a second submit during that call starts a second
  calibration and the climate is restored to OFF. The frontend normally
  prevents this by disabling the button while a request is pending.

## Setup
- Platforms are set up concurrently. If `sensor` or `binary_sensor` runs before
  `climate` has registered itself, `sensor` adds no entities and `binary_sensor`
  raises; nothing retries. Tests set up the base components first.
- The reverse also happens: with window sensors, on a fresh install the climate
  fails to add. `_register_event_listeners` looks up SAT's window binary sensor
  in the entity registry before `binary_sensor` has registered it, so the
  entity ID is None and tracking it raises `AttributeError: 'NoneType' object
  has no attribute 'lower'`. Tests register the window sensor first.
- The manifest registers `ems-esp/#` for MQTT discovery. HA never drops this
  subscription and only skips a message whose payload equals the previous one
  on the same topic, so every changed message on any EMS-ESP topic starts a
  discovery config flow, also when the entry exists. This includes SAT's own
  commands on `ems-esp/boiler`. `async_step_mqtt` aborts these with
  `already_configured` without logging. That is at least one flow every 10 s
  (`boiler_data`). With more than 10 distinct topics, these aborts also keep
  changing the entry's list of its last 10 discovery topics, and HA saves the
  config entries again. These updates don't reload SAT, which reloads only when
  the entry's data or options change.
- A control loop that is already running when the entry unloads (e.g. waiting
  the second after an MQTT publish) still finishes, and can write once more
  through the old coordinator.
- The ESPHome backend's teardown has no test: the test environment lacks
  `aioesphomeapi`, so the ESPHome modules can't be imported.
- If saving the device data fails while an MQTT entry unloads, the error
  escapes `async_unload_entry` after the platforms are unloaded: the entry ends
  in FAILED_UNLOAD and its `hass.data` entry is left behind.
- Reconfigure reloads the entry twice when its data changed:
  `async_update_reload_and_abort` schedules a reload, and so does the update
  listener.
- A coordinator whose start fails (e.g. MQTT not available) logs the error and
  isn't started again until the entry reloads; until then it receives no
  device data.
- The manifest's OpenTherm discovery filter `OTGW/value/+` matches one level
  after `value/`, but the gateway's values are on `OTGW/value/<device>/<key>`,
  so OpenTherm over MQTT is never discovered. If the filter matched those
  topics, `async_step_mqtt` would use `<device>/<key>` as the unique ID,
  because it takes everything after `OTGW/value/`.
