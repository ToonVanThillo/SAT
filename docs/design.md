# SAT design: device communication layer

## Purpose
Make communication with the boiler explicit and verifiable: every control cycle
decides from one consistent view of the boiler, and every command we send is
tracked until we know whether it stuck. This design is for this fork only;
upstream compatibility is not a goal.

## Control cycle
Each cycle has three phases, in this order:

1. **Snapshot**: the climate captures its inputs, then the coordinator runs its
   update step and builds one immutable `BoilerSnapshot`. The channels verify
   the previous cycle's writes against it.
2. **Decide**: the control logic (climate, PWM, PID, relative modulation) reads
   only the snapshot, the inputs, the coordinator's capabilities and configured
   limits, and which sent values the channels have confirmed (as of this
   cycle's verify), and produces a `DesiredState`. No device writes happen
   here.
3. **Reconcile**: the coordinator hands the `DesiredState` to its channels,
   which write what is needed.

In HVAC mode OFF the decide phase requests heater OFF and releases the control
setpoint. In HEAT, when it requests heater OFF (the PWM off phase, a calculated
setpoint that is too cold, or a PWM minimum setpoint at or below the cold
threshold), it releases the control setpoint only on backends that switch
heating off with a separate command, and only once the heater channel has
confirmed heating off; until then, and on a backend whose boiler is kept off by
a low setpoint, it requests the computed setpoint (10 °C, or the PWM minimum
setpoint). On EMS-ESP that command is `heatingoff`, under which EMS-ESP itself
keeps sending the boiler a flow temperature of 0, so writing it as well is
redundant. For now only EMS-ESP releases: on backends without channels a
release leaves the last setpoint on the device, so they keep requesting the
minimum until they migrate. In HEAT with a missing or unreadable
temperature, only the snapshot phase and the PWM cycle-frequency update run.

Every write goes through this path, including HVAC mode OFF and pushing the
setpoint to the thermostat. Hot water control is out of scope; where a backend
supports the hot water setpoint, it keeps its direct write path.

## Snapshot
- Built once per cycle by `coordinator.snapshot()`, right after the
  coordinator's update step (flame state, temperature history, tracker). It is a
  frozen dataclass filled from the coordinator's public properties and its raw
  device values (`readings`, by device key), so backends need no changes.
- Each snapshot carries a cycle number, incremented per snapshot.
- It holds the device and derived values the control logic reads (boiler state
  including device status, flame state) and the manufacturer, each read once per
  cycle. Capabilities (such as `supports_*`) and configured setpoint limits stay
  on the coordinator; they don't change within a cycle. Presentation (entity state and
  attributes) keeps reading the coordinator live.
- A cycle does not see device data that arrives while it runs, including the
  device echoing the cycle's own writes; the next cycle does.
- Each device value carries the time it was received. Values restored from
  storage are marked as restored until the first live message arrives.
- The climate captures its own inputs (target temperature, inside temperature
  including thermal comfort, outside temperature, valve state) once at the start
  of each cycle, in a frozen dataclass. Areas still read their sensors on
  access.

## Desired state
- A `DesiredState` holds one optional value per controllable quantity: control
  setpoint, heater on/off, max relative modulation, max setpoint, thermostat
  setpoint.
- A requested value becomes the channel's target and stays the target until a
  different value is requested. `None` means "keep the current target";
  `RELEASE` means "no target".
- A released channel stops writing and comparing read-backs until a value is
  requested again, which it handles as a first write. Its status is `IDLE`.
- Submitting the same desired state twice therefore has no extra effect.

## Channels
One channel per controllable quantity per backend.

**Definition**
- `encode(requested) -> value`: the value the device should hold: clamps and
  device quirks, gated by manufacturer or configuration where they only apply
  to some boilers.
- `write(value)`: the backend-specific send, including the device's
  representation (units, payload format, inverted flags).
- `read(snapshot) -> reading | None`: reads the device setting back, in the
  same terms as `encode`, with the time it was received.
- An error in a channel's encode, write or read is caught per channel and
  logged, with a traceback only for the first of consecutive failures of
  reading, or of encoding and writing. A
  failed encode or write leaves the sent value unchanged and gives
  `OUT_OF_SYNC` (a write-only channel stays `WRITE_ONLY`). A failure never
  stops the control loop or other channels.
- A channel has a read, a write, or both. Without a read it is write-only;
  without a write it is read-only. Quantities a backend can neither read nor
  write have no channel.
- Tolerance, and whether to rewrite every cycle (for settings that revert on
  the timescale of a cycle).

**Tracked state:** target value, sent value and time, read-back value
and time, drift count, the times the channel was last `DRIFTED` and last
`OUT_OF_SYNC` (refreshed after every verify and write), status.

**Statuses:** `IDLE` (no target), `WAITING` (a target, waiting for a live
read-back before the first write), `PENDING`, `IN_SYNC`, `OUT_OF_SYNC`,
`DRIFTED`, `WRITE_ONLY`, `READ_ONLY`.

**Verify** (read-write channels, every cycle, right after the snapshot)
Compare the latest read-back received strictly after the last send with the
sent value:
- a match gives `IN_SYNC`;
- a mismatch gives `DRIFTED` if the channel was `IN_SYNC`, else `OUT_OF_SYNC`;
- without such a read-back the status is unchanged.

**Write** (every cycle, after the decide phase)
- Read-write: encode the target. A value that differs from the last sent value
  is written and gives `PENDING`. The same value is rewritten, keeping the
  status, if this cycle's verify found a mismatch or the channel rewrites every
  cycle.
- A read-write channel writes only once its read-back is live (received, not
  restored); until then its status is `WAITING` (an `OUT_OF_SYNC` channel keeps
  its status). If it waits for more than
  three cycles, a warning is logged once.
  If the latest live read-back matches the encoded target (within tolerance)
  when the first write is due, it is taken as sent: the channel is `IN_SYNC`
  without writing.
- Write-only: write the target every cycle; it cannot be verified.
- Read-only: a requested value is ignored and a warning is logged once.
- Reading a write-only channel returns nothing and logs a warning once.

Read-backs are compared with the sent value, not the requested one, so clamps
and quirks don't count as mismatches. Read-backs received before the last send
are ignored: they describe the device before the write.

**Confirmation:** a channel confirms that the device accepted a value (its
setting reads back as sent). Whether the boiler acts on it is out of scope.

## Coordinator responsibilities
- Store raw device data, build snapshots, own the channel set.
- `async_submit(desired: DesiredState)` runs reconciliation and returns the
  writes it made, each marked as a new value, a rewrite after a mismatch, or a
  routine rewrite.
- `verify(snapshot)` runs right after the cycle's snapshot; backends with
  channels verify the previous cycle's writes and return the status changes,
  others do nothing.
- `confirmed_value(quantity)` gives a channel's sent value while the device has
  confirmed it (`IN_SYNC`), else None; the decide phase uses it to act only on
  confirmed device state.
- Backends supply channel definitions. Backends not yet migrated keep their
  current `async_set_*` path.
- On unload the platforms are unloaded first; then the coordinator releases its
  subscriptions, listeners and timers. When setup fails, no platform has been
  set up, so only the coordinator releases them. A torn-down coordinator is
  not started again; a reload creates a new one. Only one coordinator per
  config entry receives device data.
- The entry reloads only when its data or options change; other entry updates
  (such as MQTT discovery bookkeeping) don't reload it.

## Diagnostics
Each channel gets a diagnostic entity, "<Quantity> Synchro", for
troubleshooting current behaviour: every problem shows, and a problem that
keeps recurring shows as one ongoing state instead of switching back and
forth. A problem shows from the moment it occurs until the diagnostic window
(1 minute) has passed since it last occurred:
- `OUT_OF_SYNC`: the channel was `OUT_OF_SYNC` within the window.
- `DRIFTED`: the channel was `DRIFTED` within the window.
- `IN_SYNC`: neither; the channel behaves as intended, including `PENDING`.
- `WAITING`, `WRITE_ONLY`, `READ_ONLY`, `IDLE`: the channel's own status, also
  within the window of an earlier problem.

`OUT_OF_SYNC` takes precedence over `DRIFTED`. The state is derived from the
channel's tracked state and the current time, and is updated after every
verify and write, and when the window expires.

The entities have no attributes: an attribute that changes every cycle would
record a new state each time. The channel details (internal status, target,
sent and read-back values with their times, drift count, last-problem times)
are in the integration's diagnostics download, together with the config entry
(device address, unique ID and discovery keys redacted) and the raw device
values with their receive times.

Backends without channels have no synchro diagnostics until they migrate.

## Timeline
The logger `custom_components.sat.timeline` records what the boiler and SAT
did, in order, so a run can be reconstructed from the log alone.
- At info, as they happen: changes of the device values each backend declares
  as traced (EMS-ESP: `heatingpump`, `heatingactive`, `tapwateractive`,
  `burngas`, `selflowtemp`, `selburnpow`, `servicecode`, `heatingoff`), logged
  on arrival and only on change, never for values restored from storage;
  channel writes, logged before the backend sends: a new value (`send`, with
  the requested value when encoding changed it), a rewrite after a mismatch
  (`resend`, with the read-back) and a release; channel transitions into or
  out of `DRIFTED` and `OUT_OF_SYNC`; PWM switching on, off or to idle (idle
  with the reason); a pulse held on for the minimum pulse length beyond its
  duty cycle; overshoot handling starting and ending (evaluated in HEAT cycles
  with all temperatures); the PWM cycles-per-hour cap being reached and
  lifted; flame on and off, the flame-off line with the burn's kind and the
  hot-water signals seen; the control setpoint released or requested again,
  with the reason, on backends that release it (those with a control-setpoint
  channel), also across HVAC mode changes; coordinator setup and removal.
  Neither a release before the first request nor the first request itself is
  logged. Rewrites of a channel that rewrites every cycle, or of a write-only
  channel, are not logged at info.
- At debug, one line per cycle after reconciliation, numbered with the
  snapshot's cycle number: the inputs, boiler values, PWM state, duty and duty
  rule, the setpoint rule, the desired state as requested, the writes as
  encoded, and the verify transitions.
- Flame lines come from the coordinator's debounced flame listener or its
  update step, whichever runs first, so they can follow the matching `burngas`
  line by up to the listener debounce.
- Backends without channels log every write of the control setpoint, heater
  state, max relative modulation and max setpoint as `send`, since they keep
  no sent value until they migrate; the thermostat setpoint is not logged.
- A failure to build a log line is caught and logged; it never stops the
  control loop.

## Calculated setpoint sensor
A "Calculated Setpoint" sensor shows the flow temperature the control loop
aims for: the heating curve plus the PID output, after the low-pass filter. It
is a regular sensor (temperature device class, measurement state class), not a
diagnostic, so its history can be compared with the boiler's flow temperature.
- It shows the value before PWM and before the minimum and maximum setpoint
  limits. With PWM active it still shows the calculated value, not the
  setpoint PWM applies.
- It updates once per cycle, after the decide phase, and is unknown when the
  climate has no calculated setpoint. It has no attributes.

## PWM cycle frequency
The number of PWM cycles per hour, N, follows the outside temperature captured
at the start of each cycle.
- N is linear between two configured points (default 4 at 0 °C, 2 at 12 °C),
  clamped to their range and rounded to the nearest integer, halves up.
  Without an outside temperature PWM keeps the last N; before the first
  reading it uses the N of the colder point. If the configured points are
  invalid, the defaults apply and an error is logged.
- N sets the cycle timing: the normal period 3600/N, the duty ranges in which
  the on-time is held at 180 s or the off-time at 180 s, the duty below which
  no pulse starts, and the maximum number of pulses started per hour.
- N is latched when a pulse starts and holds for that pulse and the off phase
  after it, until the next pulse starts or the off phase exceeds twice the
  latched normal period.
- A started pulse lasts at least 180 s; only a reset, a disable, missing
  values or an HVAC mode other than HEAT end it earlier.
- Below the minimum duty no pulse starts, also when the flame is on: PWM does
  not take over burns it did not start.
- N is updated every cycle, also in HVAC mode OFF and when a temperature is
  missing. A diagnostic "PWM Cycles Per Hour" sensor (measurement, unit
  cycles/h, no attributes) shows the N in effect.

## Burns
A burn lasts from flame on to flame off. It is hot water if a hot-water signal
is seen at any moment during it; otherwise it is heating, including burns SAT
did not request.
- Hot-water signals; any one suffices:
  - the backend's hot water active flag (EMS-ESP: `tapwateractive`);
  - EMS-ESP: `selburnpow` equal to the configured hot water burner power;
  - EMS-ESP: `selflowtemp` equal to the configured hot water flow temperature
    while it differs from the control setpoint SAT last sent.

  None catches every short draw on its own; together they catch most.
- The last two depend on boiler settings. Their configured values are unset by
  default, and an unset value disables its signal. The burner power signal
  holds only while SAT never writes that power itself: if the configured
  maximum relative modulation is at or above it, a warning is logged and the
  signal is not used. The flow temperature signal holds only while the
  boiler's own heating temperature differs from the hot water flow
  temperature.
- Burns and their signals are tracked from device data as it arrives, not per
  control cycle, so a burn shorter than a cycle is still seen and classified.
- While a hot water burn is on, SAT treats the boiler as producing hot water,
  also after the hot water active flag has gone off.
- Flame statistics count heating burns only: cycles in the last hour, the 4 h
  median on-time, the average on-time and the 15 min duty ratio. Flame health
  and the setpoint adjuster read these.

## Testing
- Characterization tests at the MQTT boundary pin current behaviour before any
  refactor. Tests that pin known bugs are labelled as such and listed in
  `docs/known-issues.md`.
- Characterization tests assert observable behaviour (published MQTT messages,
  public properties, entity states, logs), not private attributes, so they
  survive the refactor.
- Channel logic is unit tested separately from the backends.
- Each step is either a refactor (observable behaviour unchanged; the task
  names any test of an internal contract it changes) or a behaviour change
  (the task names the tests it changes), never both.

## Plan
1. Snapshot and climate inputs; the climate loop reads only from them.
2. Receive timestamps and restored marker; `DesiredState` and channels for
   EMS-ESP:
   a. Receive timestamps and restored marker on device values.
   b. `DesiredState` and the channel engine, unit tested, not yet used.
   c. All writers submit a `DesiredState`; backends not yet migrated map it to
      their `async_set_*`.
   d. EMS-ESP: configurable minimum for the max relative modulation (the
      Junkers Cerapur needs 31).
   e. EMS-ESP: hot water active read from `tapwateractive`; the hot water
      setpoint is dropped.
   f. The climate requests the full desired state; backends not yet migrated
      keep their write-if-different checks in the base async_submit.
   g. In HVAC mode OFF the cycle still runs and requests heater OFF.
   h. EMS-ESP migrated to channels; a failed publish raises.
   i. EMS-ESP: no max relative modulation writes for hot water.
   j. EMS-ESP: the max relative modulation option and its minimum allow values
      above 100, up to the range EMS-ESP accepts for `burnmaxpower`.
3. Channel diagnostics:
   a. Last-seen problem times, the `WAITING` status, the diagnostic status and
      public channel states; not yet used.
   b. Channel diagnostic entities on backends with channels; the coordinator
      owns the channel set.
   c. Coordinators receive their config entry explicitly instead of through
      HA's setup context.
   d. A diagnostics download with the channel details.
4. Remove the synchro binary sensors.
5. Update the PID and heating curve inside the control cycle from the captured
   inputs, instead of on sensor events.
6. Relative modulation keeps the current max relative modulation while the
   boiler setpoint is at the minimum (COLD), instead of requesting the maximum.
7. A Calculated Setpoint sensor.
8. Migrate the other backends to channels; those that switch heating off with a
   separate command then also release the control setpoint on heater OFF in
   HEAT.
9. EMS-ESP: in HEAT, heater OFF releases the control setpoint instead of
   requesting the minimum.
10. PWM cycles per hour from the outside temperature, and a minimum pulse of
    180 s.
11. Timeline logging: traced boiler values on arrival, channel writes and edges
    at info, one cycle line per cycle at debug.
12. PWM starts no pulse below the minimum duty, also with the flame on.
13. Burns classified as hot water or heating; flame statistics count heating
    burns only.

## Open questions
- EMS-ESP: with the thermostat contact bridged, `selflowtemp` reads back the
  knob value; a written setpoint must be below it and reverts to it after a
  few minutes. Under `heatingoff`, EMS-ESP itself sends `selflowtemp` 0 to the
  boiler, which reads back 5.
- A read-back sampled before a write but received after it (e.g. EMS-ESP
  publishing on an interval) would give a false `OUT_OF_SYNC`. Not observed
  yet; the channel diagnostics should show whether it happens. If so, it is
  fixed in the channel (e.g. a grace period after a send), not in the
  diagnostic.
- The listener debounce can starve while the device keeps publishing changes;
  how control cycles are triggered is outside this layer but needs fixing.
- MQTT brokers redeliver retained messages on reconnect; they are stamped as
  new and can look like a mismatch after a send.
- Serial (pyotgw) values are timestamped per status section, not per field;
  channels for serial need per-field times.
- Whether areas should capture their inputs once per cycle.
- Whether measurements (e.g. `curflowtemp`) need a freshness check or
  diagnostic based on their receive time.
- MQTT backends record the send time after the 1 s wait that follows a
  publish, so a read-back within that second is ignored. The timeline logs
  the send before that wait, so a read-back echo can follow a `send` line and
  still be ignored.
- What flame health's `PWM_SHORT` should detect; a candidate is the boiler
  ending a heating burn while a PWM pulse is still on.
- After hot water the pump does not run, so the flow temperature stays high
  and stale while the boiler sits idle; PWM computes its duty from it.