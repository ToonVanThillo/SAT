# SAT design: device communication layer

## Purpose
Make communication with the boiler explicit and verifiable: every control cycle
decides from one consistent view of the boiler, and every command we send is
tracked until we know whether it stuck. This design is for this fork only;
upstream compatibility is not a goal.

## Control cycle
Each cycle has three phases, in this order:

1. **Snapshot**: the climate captures its inputs, then the coordinator runs its
   update step and builds one immutable `BoilerSnapshot`.
2. **Decide**: the control logic (climate, PWM, PID, relative modulation) reads
   only the snapshot, the inputs, and the coordinator's capabilities and
   configured limits, and produces a `DesiredState`. No device writes happen
   here.
3. **Reconcile**: the coordinator hands the `DesiredState` to its channels, which
   write what differs and track confirmation.

Every write goes through this path, including HVAC mode OFF and pushing the
setpoint to the thermostat.

## Snapshot
- Built once per cycle by `coordinator.snapshot()`, right after the
  coordinator's update step (flame state, temperature history, tracker). It is a
  frozen dataclass filled from the coordinator's public properties, so backends
  need no changes.
- It holds the device and derived values the control logic reads (boiler state
  including device status, flame state) and the manufacturer, each read once per
  cycle. Capabilities (`supports_*`) and configured setpoint limits stay on the
  coordinator; they don't change within a cycle. Presentation (entity state and
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
  setpoint, heater on/off, max relative modulation, max setpoint, hot water
  setpoint, thermostat setpoint.
- A requested value becomes the channel's target and stays the target until a
  different value is requested. `None` means "keep the current target".
- Submitting the same desired state twice therefore has no extra effect.

## Channels
One channel per controllable quantity per backend.

**Definition**
- `encode(requested) -> wire value`: clamps, unit conversion and device quirks,
  gated by manufacturer where they only apply to some boilers.
- `write(wire value)`: the backend-specific send. A failed write raises inside
  the channel layer; reconciliation catches it per channel, logs a warning and
  counts a failed attempt. A failure never stops the control loop or other
  channels.
- `read(snapshot) -> value | None`: reads the device setting back.
- A channel has a read, a write, or both. Without a read it is write-only;
  without a write it is read-only. Quantities a backend can neither read nor
  write have no channel.
- Tolerance, confirmation timeout, maximum retries, and a resend interval for
  write-only channels.

**Tracked state:** target value, sent (wire) value and time, read-back value
and time, attempt count, drift count, status.

**Statuses:** `IDLE` (no target yet), `PENDING`, `IN_SYNC`, `DRIFTED`,
`FAILED`, `WRITE_ONLY`, `READ_ONLY`.

**Reconcile rules** (every cycle, every channel)
1. Verify: if a value was sent and a read-back newer than the send exists,
   compare it with the sent value. A mismatch on an `IN_SYNC` channel gives
   `DRIFTED`. This runs whether or not a new value was requested: with an
   unchanged target, the read-back in cycle x+1 must equal the value sent in
   cycle x.
2. Write: encode the target. Write it if the wire value differs from the last
   sent value, or if the channel is `DRIFTED`, and go to `PENDING`.
3. Confirm: `PENDING` with a matching read-back newer than the send gives
   `IN_SYNC`. After the timeout, retry. Once the maximum number of retries is
   reached, go to `FAILED`.
4. Write-only: resend after the resend interval.
5. Read-only: a requested value is ignored and a warning is logged once.

Read-backs are compared with the sent value, not the requested one, so clamps
and quirks don't count as mismatches.

**Confirmation:** a channel confirms that the device accepted a value (its
setting reads back as sent). Whether the boiler acts on it is out of scope.

## Coordinator responsibilities
- Store raw device data, build snapshots, own the channel set.
- `async_submit(desired: DesiredState)` runs reconciliation.
- Backends supply channel definitions. Backends not yet migrated keep their
  current `async_set_*` path.

## Diagnostics
Each channel gets a diagnostic entity whose state is a filtered status, so
short transients such as `PENDING` don't show:
- `OK`: the channel behaves as intended, including `PENDING` within its
  confirmation timeout.
- `DRIFTED`: the setting drifted from the sent value at least once within the
  diagnostic window. It stays until a full window passes without drift, so a
  value that keeps drifting and being rewritten stays visible.
- `FAILED`: the latest value could not be written or was not accepted after
  all retries. It stays until a write is confirmed.
- `UNVERIFIED` (write-only), `READ_ONLY` (read-only), `IDLE` (no target yet).

Attributes: the internal status, target, sent and read-back values with
timestamps, and attempt and drift counts. These entities replace the Control
Setpoint, Relative Modulation and Central Heating synchro sensors.

## Testing
- Characterization tests at the MQTT boundary pin current behaviour before any
  refactor. Tests that pin known bugs are labelled as such and listed in
  `docs/known-issues.md`.
- Characterization tests assert observable behaviour (published MQTT messages,
  public properties, entity states, logs), not private attributes, so they
  survive the refactor.
- Channel logic is unit tested separately from the backends.
- Each step is either a refactor (all tests pass unchanged) or a behaviour
  change (the task names the tests it changes), never both.

## Plan
1. Snapshot and climate inputs; the climate loop reads only from them.
2. Receive timestamps and restored marker; `DesiredState` and channels for
   EMS-ESP.
3. Channel diagnostics; remove the synchro sensors.
4. Migrate the other backends.
5. Update the PID and heating curve inside the control cycle from the captured
   inputs, instead of on sensor events.

## Open questions
- EMS-ESP read-back keys and echo behaviour (for example, whether sending 10 as
  0 is echoed as 0).
- Default values for the confirmation timeout, retries and diagnostic window.
- The listener debounce can starve while the device keeps publishing changes;
  how control cycles are triggered is outside this layer but needs fixing.
- Whether confirmed channels need a periodic refresh for devices that revert
  settings; decide from observed drift.
- Whether areas should capture their inputs once per cycle.