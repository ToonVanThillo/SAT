# SAT design: device communication layer

## Purpose
Make communication with the boiler explicit and verifiable: every control cycle
decides from one consistent view of the boiler, and every command we send is
tracked until we know whether it stuck. This design is for this fork only;
upstream compatibility is not a goal.

## Control cycle
Each cycle has three phases, in this order:

1. **Snapshot**: the coordinator builds one immutable `BoilerSnapshot`.
2. **Decide**: the control logic (climate, PWM, PID, relative modulation) reads
   only the snapshot and produces a `DesiredState`. No device writes happen here.
3. **Reconcile**: the coordinator hands the `DesiredState` to its channels, which
   write what differs and track confirmation.

Every write goes through this path, including HVAC mode OFF and pushing the
setpoint to the thermostat.

## Snapshot
- Built once per cycle by `coordinator.snapshot()`. It is a frozen dataclass.
- Each device value carries the time it was received. Values restored from
  storage are marked as restored until the first live message arrives.
- Derived values (device status, flame state, boiler temperature statistics) are
  computed once while the snapshot is built, not on each read.
- The climate captures its own inputs (inside, outside and humidity sensors,
  valve state) once per cycle in the same way.

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
  refactor. Tests that pin known bugs are labelled as such.
- Channel logic is unit tested separately from the backends.

## Plan
1. Characterization tests for the coordinator and MQTT layers.
2. Snapshot; climate reads only from the snapshot. No behaviour change.
3. `DesiredState` and channels for EMS-ESP.
4. Channel diagnostics; remove the synchro sensors.
5. Migrate the other backends.

## Open questions
- EMS-ESP read-back keys and echo behaviour (for example, whether sending 10 as
  0 is echoed as 0).
- Default values for the confirmation timeout, retries and diagnostic window.
- The listener debounce can starve while the device keeps publishing changes;
  how control cycles are triggered is outside this layer but needs fixing.
- `boiler_temperature_filtered` is a noise measure but is used as PID input;
  investigate separately.
