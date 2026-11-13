# SAT design: always-on PWM

## Purpose
In HEAT, every control cycle runs PWM. Continuous heating is a PWM period at
100% duty, not a separate mode, so there is no switching between PWM and
continuous control. This replaces the enable/disable machinery: the climate's
PWM gate, the PWM enabled flag, the IDLE status as a hand-off to continuous
heating, and the `pulse_width_modulation` service. Once implemented, this
document is folded into `design.md` ("Control cycle", "PWM cycle frequency").

## Scope
Only EMS-ESP is reviewed for always-on PWM. Tests of other backends that the
overhaul breaks are marked strict xfail, with a reason that refers to one
known-issues entry; they are fixed later.

## Minimum setpoint
- Backends with setpoint management require a minimum setpoint: calibrated
  with the built-in overshoot protection calibration, or entered by the user.
  The config flow does not finish without one. It must be above the cold
  threshold (28.2 °C), so a pulse at the minimum heats.
- The effective minimum setpoint is the configured one, raised by the dynamic
  minimum setpoint (v1 or v2) when that is enabled, as today.
- The `overshoot_protection` flag is no longer read. Entries are not migrated;
  an entry whose minimum is at or below the cold threshold logs an error at
  setup.
- The calibrate menu's PID-controller entry, which skips the minimum, is
  removed; after the minimum the flow continues to the automatic gains step.
  A calibration result at or below the cold threshold shows the manual step
  with an error.
- "Allow unrestricted modulation" is offered for every backend with setpoint
  management.

## Modulation mode
PWM outputs, every HEAT cycle, a status (ON or OFF), a duty (0–100%) and a
modulation mode:
- UNRESTRICTED when the option "Allow unrestricted modulation" is on and the
  calculated setpoint is above the effective minimum setpoint; otherwise
  MINIMUM.
- Backends without setpoint management are always MINIMUM.
- The option replaces "Force pulse width modulation" and is on by default.
  With it off, the boiler burns at the minimum setpoint and minimum
  modulation even at 100% duty.
- The mode compares the calculated setpoint with the effective minimum as of
  the start of the cycle; with dynamic minimum v2 that is the setpoint
  adjuster's value from the previous cycle. The mode has no hysteresis: the
  calculated setpoint is already low-pass filtered.
- A reset sets the mode to MINIMUM.

## Duty
- UNRESTRICTED: the duty is 100%.
- MINIMUM: the duty from the duty formula, as today.
- In the mid branch the off-time is the period minus the on-time, so a
  mid-duty cycle lasts exactly 3600/N.
- A period whose off-time is 0 (today's "max" branch) is a 100% period.
- A 100% period lasts twice the normal period (2·3600/N).
- The boiler temperature the duty formula uses is smoothed over the first
  180 s of each period, also after a rollover, and follows the flow
  temperature for the rest of the pulse.
- Without a flow temperature, UNRESTRICTED still gives 100%; MINIMUM keeps
  the last duty, and before the first duty since start or a reset, no pulse
  starts.

## Off phase
- Every off phase lasts at least 180 s, also when the duty has risen to 100%
  meanwhile; only a rollover goes from one period to the next without an off
  phase. Of the duty formula's off-times, only the 100% one is shorter.
- Before the first pulse since start, a start is due at once.
- There is no limit on the number of pulses per hour: the period follows the
  duty, and the minimum pulse and minimum off phase bound short cycling.

## 100% duty and rollover
- When a 100% period ends, the next period starts in the same update: the
  status stays ON and no OFF phase or heater OFF is requested in between.
- A rollover is not a pulse start: it is not held by a hot water hold, and
  does not restart the minimum pulse. N is latched again at the rollover.
- When the duty drops below 100% during a period, the pulse ends once the new
  on-time has passed since the period started, but not before 180 s since
  the pulse started.
- A mode change during a pulse changes the requested setpoint and max
  relative modulation in place; it never ends the pulse.
- A start from OFF is a pulse start in either mode: it waits for the off
  phase and a hot water hold.

## Decide phase
In HEAT, with all temperatures present and a valve open:

| PWM              | Control setpoint           | Max relative modulation | Heater |
|------------------|----------------------------|-------------------------|--------|
| OFF              | minimum (10 °C) or release | by mode                 | OFF    |
| ON, MINIMUM      | effective minimum setpoint | minimum                 | ON     |
| ON, UNRESTRICTED | calculated setpoint        | configured maximum      | ON     |

- Release on heater OFF, missing temperatures, no open valve and HVAC mode
  OFF are as in `design.md`. No open valve and a missing temperature reset
  PWM; a reset ends a running pulse like a normal pulse end, and the next
  pulse waits for the off phase.
- Max relative modulation: hot water and cold (device setpoint at or below
  10 °C) as today; otherwise MINIMUM gives the minimum and UNRESTRICTED the
  configured maximum. The `PULSE_WIDTH_MODULATION_OFF` state is removed.
- The PWM status has no IDLE: PWM is ON or OFF.
- Overshoot handling remains a boiler status; it no longer changes PWM.
- The heater is OFF whenever the requested setpoint is at or below the cold
  threshold, which only a legacy minimum can cause.
- The relative modulation states are COLD, HOT_WATER, MINIMUM and
  UNRESTRICTED.
- A reset logs "pwm OFF (reason)" once, on the first reset since PWM last
  ran.

## Consumers
- Flame health: the PWM rules (`PWM_SHORT`, 8 cycles/h) apply to pulses
  below 100% duty; the continuous rules (3 cycles/h) to 100% periods.
- Dynamic minimum setpoint v1: its normal mode requires UNRESTRICTED instead
  of PWM IDLE.
- Dynamic minimum setpoint v2: the setpoint adjuster is reset on a control
  state reset (target or HVAC mode change), as `disable()` did.
- Sensors: PWM Status shows ON or OFF; a diagnostic "PWM Mode" sensor
  (minimum, unrestricted, disabled by default, no attributes) shows the mode.
  PWM Duty Cycle shows 100% for 100% periods.
- Timeline: mode changes are logged at info; "pwm IDLE (reason)" becomes
  "pwm OFF (reason)" on a reset; rollovers are not logged at info; the cycle
  line shows the mode and marks a rollover.

## Removed
- The climate's `pulse_width_modulation_enabled` and the continuous branch in
  the setpoint decision.
- `PWM.enable()`, `disable()`, the enabled flag, the setpoint stabilisation
  disable, and the unused `_force` and `_setpoint_offset`.
- The `pulse_width_modulation` service, its schema and translations.
- The options "Force pulse width modulation" (replaced) and the
  `overshoot_protection` flag; the unregistered overshoot service constants;
  the stale `system_configuration.overshoot_protection` translation.
- The manual duty cycle: the options "Automatic duty cycle" and "Duty
  cycle".
- The pulses-per-hour cap: its counter, hour window, "cycles cap
  reached/lifted" timeline lines and the pulse count in the "pwm ON" line.

## Plan
1. Remove dead code: `_force`, `_setpoint_offset`, unregistered overshoot
   service constants, the stale translation key.
2. Remove the manual duty cycle and its options.
3. PWM: a period with off-time 0 is a 100% period and rolls over without an
   OFF phase.
4. PWM: the pulses-per-hour cap is removed; every off phase lasts at least
   180 s.
5. PWM computes the modulation mode, and UNRESTRICTED gives a 100% duty; not
   yet used: the climate does not allow unrestricted modulation yet.
6. The climate always runs PWM and decides from the table above; max
   relative modulation follows the mode; dynamic minimum v1 requires
   UNRESTRICTED. Until step 9, unrestricted modulation is allowed unless
   "Force pulse width modulation" is on. The PWM gate, the overshoot enable
   and the stabilisation disable are removed. Until step 8, the climate
   treats PWM IDLE as OFF.
7. The `pulse_width_modulation` service and PWM enable/disable are removed; a
   control state reset resets the setpoint adjuster.
8. The PWM status is ON or OFF; IDLE is removed. A reset ends a running
   pulse like a normal pulse end. Without a flow temperature, PWM keeps the
   last duty. An off phase follows only a pulse end.
9. Minimum setpoint required in the config flow; "Allow unrestricted
   modulation" replaces "Force pulse width modulation"; the
   `overshoot_protection` flag is removed. No config entry migration.
10. Flame health follows the mode.
11. A "PWM Mode" sensor; timeline mode lines and the cycle line's mode and
    rollover marker.
12. `design.md` and `known-issues.md` updated; this document is folded in.

## Open questions
- Whether the mode needs hysteresis.
- Which resets remain: a target or HVAC mode change only changes the duty in
  this model.
