# SAT design: always-on PWM

## Purpose
In HEAT, every control cycle runs PWM. Continuous heating is a PWM period at
100% duty, not a separate mode, so there is no switching between PWM and
continuous control. This replaces the enable/disable machinery: the climate's
PWM gate, the PWM enabled flag, the IDLE status as a hand-off to continuous
heating, and the `pulse_width_modulation` service. Once implemented, this
document is folded into `design.md` ("Control cycle", "PWM cycle frequency").

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

## Duty
- UNRESTRICTED: the duty is 100%.
- MINIMUM: the duty from the duty formula, as today.
- A period whose off-time is 0 (today's "max" branch) is a 100% period.
- The boiler temperature the duty formula uses is sampled in the first 180 s
  of each period, also after a rollover.
- Without a flow temperature, PWM keeps the last duty; before the first duty,
  no pulse starts.

## 100% duty and rollover
- When a 100% period ends, the next period starts in the same update: the
  status stays ON and no OFF phase or heater OFF is requested in between.
- A rollover is not a pulse start: it does not count towards the
  pulses-per-hour cap, is not blocked by the cap or a hot water hold, and does
  not restart the minimum pulse. N is latched again at the rollover.
- When the duty drops below 100% during a period, the pulse ends once the new
  on-time has passed since the period started, but not before 180 s since
  the pulse started.
- A mode change during a pulse changes the requested setpoint and max
  relative modulation in place; it never ends the pulse.

## Decide phase
In HEAT, with all temperatures present and a valve open:

| PWM              | Control setpoint           | Max relative modulation | Heater |
|------------------|----------------------------|-------------------------|--------|
| OFF              | minimum (10 °C) or release | by mode                 | OFF    |
| ON, MINIMUM      | effective minimum setpoint | minimum                 | ON     |
| ON, UNRESTRICTED | calculated setpoint        | configured maximum      | ON     |

- Release on heater OFF, missing temperatures, no open valve and HVAC mode
  OFF are as in `design.md`. No open valve and a missing temperature reset
  PWM; a reset leaves PWM OFF, and the next pulse may start at once.
- Max relative modulation: hot water and cold (device setpoint at or below
  10 °C) as today; otherwise MINIMUM gives the minimum and UNRESTRICTED the
  configured maximum. The `PULSE_WIDTH_MODULATION_OFF` state is removed.
- The PWM status has no IDLE: PWM is ON or OFF.
- Overshoot handling remains a boiler status; it no longer changes PWM.

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
  "pwm OFF (reason)" on a reset; rollovers appear in the debug cycle line
  only.

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

## Plan
1. Remove dead code: `_force`, `_setpoint_offset`, unregistered overshoot
   service constants, the stale translation key.
2. Remove the manual duty cycle and its options.
3. PWM: a period with off-time 0 is a 100% period and rolls over without an
   OFF phase.
4. PWM outputs the modulation mode; the climate always runs PWM and decides
   from the table above. IDLE, the PWM gate, enable/disable and the
   stabilisation disable are removed.
5. Max relative modulation follows the mode.
6. Minimum setpoint required in the config flow; "Allow unrestricted
   modulation" replaces "Force pulse width modulation"; the
   `overshoot_protection` flag and the `pulse_width_modulation` service are
   removed. No config entry migration.
7. Flame health, dynamic minimum v1/v2, sensors and timeline follow the new
   model.
8. `design.md` and `known-issues.md` updated; this document is folded in.

## Open questions
- Whether step 4 is split further once the implementation is planned.
