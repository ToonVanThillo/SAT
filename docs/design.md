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
   only the snapshot, the inputs, and the coordinator's capabilities and
   configured limits, and produces a `DesiredState`. No device writes happen
   here.
3. **Reconcile**: the coordinator hands the `DesiredState` to its channels,
   which write what is needed.

In HVAC mode OFF the decide phase requests heater OFF and releases the control
setpoint. In HEAT with a missing or unreadable temperature, only the snapshot
phase runs.

Every write goes through this path, including HVAC mode OFF and pushing the
setpoint to the thermostat. Hot water control is out of scope; where a backend
supports the hot water setpoint, it keeps its direct write path.

## Snapshot
- Built once per cycle by `coordinator.snapshot()`, right after the
  coordinator's update step (flame state, temperature history, tracker). It is a
  frozen dataclass filled from the coordinator's public properties and its raw
  device values (`readings`, by device key), so backends need no changes.
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
- `async_submit(desired: DesiredState)` runs reconciliation.
- `verify(snapshot)` runs right after the cycle's snapshot; backends with channels
  verify the previous cycle's writes, others do nothing.
- Backends supply channel definitions. Backends not yet migrated keep their
  current `async_set_*` path.

## Diagnostics
Each channel gets a diagnostic entity, "<Quantity> Synchro", for
troubleshooting current behaviour: every problem shows, and a problem that
keeps recurring shows as one ongoing state instead of switching back and
forth. A problem shows from the moment it occurs until the diagnostic window
(5 minutes) has passed since it last occurred:
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

On backends with channels these entities replace the Control Setpoint,
Relative Modulation and Central Heating synchro sensors; backends without
channels keep the synchro sensors until they migrate.

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
   b. Channel diagnostic entities on backends with channels, replacing their
      synchro sensors; the coordinator owns the channel set.
   c. Coordinators receive their config entry explicitly instead of through
      HA's setup context.
   d. A diagnostics download with the channel details.
4. Migrate the other backends; remove the synchro sensors.
5. Update the PID and heating curve inside the control cycle from the captured
   inputs, instead of on sensor events.
6. Relative modulation keeps the current max relative modulation while the
   boiler setpoint is at the minimum (COLD), instead of requesting the maximum.

## Open questions
- EMS-ESP: with the thermostat contact bridged, `selflowtemp` reads back the
  knob value; a written setpoint must be below it and reverts to it after a
  few minutes. Whether 0 (sent for 10) is echoed as 0 is unknown.
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
  publish, so a read-back within that second is ignored.