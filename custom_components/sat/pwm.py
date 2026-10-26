import logging
import math
from dataclasses import dataclass
from time import monotonic
from typing import Optional, Tuple, TYPE_CHECKING

from homeassistant.core import State

from . import timeline
from .const import HEATER_STARTUP_TIMEFRAME, MINIMUM_SETPOINT, BoilerStatus, PWMStatus
from .heating_curve import HeatingCurve
from .setpoint_adjuster import SetpointAdjuster

if TYPE_CHECKING:
    from .flame import FlameState
    from .boiler import BoilerState

_LOGGER = logging.getLogger(__name__)

# Range of the configurable cycles per hour, shared with the options flow
MINIMUM_CYCLES_PER_HOUR = 1
MAXIMUM_CYCLES_PER_HOUR = 6


@dataclass(frozen=True, slots=True, kw_only=True)
class CycleConfig:
    """
    Encapsulates the cycle time of the non-automatic duty cycle and the mapping of the outside temperature to the cycles per hour.
    """
    maximum_time: int
    cold_temperature: float
    cold_cycles_per_hour: int
    mild_temperature: float
    mild_cycles_per_hour: int

    def __post_init__(self) -> None:
        if not (math.isfinite(self.cold_temperature) and math.isfinite(self.mild_temperature)):
            raise ValueError("The cold and mild temperatures must be finite.")

        if self.cold_temperature == self.mild_temperature:
            raise ValueError("The cold and mild temperatures must differ.")

        if not all(MINIMUM_CYCLES_PER_HOUR <= cycles <= MAXIMUM_CYCLES_PER_HOUR for cycles in (self.cold_cycles_per_hour, self.mild_cycles_per_hour)):
            raise ValueError(f"The cycles per hour must be between {MINIMUM_CYCLES_PER_HOUR} and {MAXIMUM_CYCLES_PER_HOUR}.")

    def cycles_per_hour(self, outside_temperature: float) -> int:
        """Interpolate linearly between the cold and mild points, clamped to their cycle counts, rounding halves up."""
        fraction = (outside_temperature - self.cold_temperature) / (self.mild_temperature - self.cold_temperature)
        cycles = self.cold_cycles_per_hour + fraction * (self.mild_cycles_per_hour - self.cold_cycles_per_hour)
        lowest, highest = sorted((self.cold_cycles_per_hour, self.mild_cycles_per_hour))

        return math.floor(min(max(cycles, lowest), highest) + 0.5)


@dataclass(frozen=True, slots=True, kw_only=True)
class CycleThresholds:
    """
    Timing (seconds) and duty cycle (fraction) thresholds of a PWM cycle, derived from the cycles per hour.
    """
    on_time_lower: float
    on_time_upper: float
    on_time_max: float
    duty_cycle_lower: float
    duty_cycle_upper: float
    duty_cycle_min: float
    duty_cycle_max: float

    @classmethod
    def from_cycles_per_hour(cls, cycles_per_hour: int) -> "CycleThresholds":
        on_time_lower = 180
        on_time_upper = 3600 / cycles_per_hour
        duty_cycle_lower = on_time_lower / on_time_upper
        duty_cycle_min = duty_cycle_lower / 2

        return cls(
            on_time_lower=on_time_lower,
            on_time_upper=on_time_upper,
            on_time_max=on_time_upper * 2,
            duty_cycle_lower=duty_cycle_lower,
            duty_cycle_upper=1 - duty_cycle_lower,
            duty_cycle_min=duty_cycle_min,
            duty_cycle_max=1 - duty_cycle_min,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PWMState:
    """
    Encapsulates the state of the PWM control.
    """
    enabled: bool
    status: PWMStatus
    duty_cycle: Optional[Tuple[int, int]]


class PWM:
    """Implements Pulse Width Modulation (PWM) control for managing boiler operations."""

    def __init__(self, cycles: CycleConfig, heating_curve: HeatingCurve, supports_relative_modulation_management: bool, automatic_duty_cycle: bool, force: bool = False, hot_water_delay: float = 0):
        """Initialize the PWM control; no pulse starts within `hot_water_delay` seconds after a hot water burn ends."""
        self._alpha: float = 0.2
        self._force: bool = force
        self._hot_water_delay: float = hot_water_delay
        self._last_boiler_temperature: float | None = None

        self._cycles: CycleConfig = cycles
        self._heating_curve: HeatingCurve = heating_curve
        self._automatic_duty_cycle: bool = automatic_duty_cycle

        # Cycles per hour from the latest outside temperature; the cold value until the first reading
        self._cycles_per_hour: int = cycles.cold_cycles_per_hour

        # Initialize some helpers
        self._setpoint: Optional[float] = None
        self._setpoint_adjuster = SetpointAdjuster()
        self._setpoint_offset: int = 0.5 if supports_relative_modulation_management else 1

        _LOGGER.debug(
            "Initialized PWM control with cycles per hour %d at %.1f°C to %d at %.1f°C, Offset: %d°C",
            cycles.cold_cycles_per_hour, cycles.cold_temperature, cycles.mild_cycles_per_hour, cycles.mild_temperature, self._setpoint_offset
        )

        self._status: PWMStatus = PWMStatus.IDLE
        # Edge flags, to log only changes: the cycles-per-hour cap blocked a pulse and still applies, values are missing, the minimum pulse held this pulse
        self._cycles_capped: bool = False
        self._values_missing: bool = False
        self._minimum_pulse_held: bool = False
        # Why a due pulse start is held (logged on change); it is due again as soon as the hold ends
        self._held: str | None = None
        self.reset()

    def reset(self, reason: str = "reset") -> None:
        """Reset the PWM control; leaving ON or OFF is logged on the timeline with `reason`."""
        self._go_idle(reason)
        self._lift_cycles_cap()
        self._set_held(None)
        # A missing-values episode ends with the reset, so the next one is warned about again
        self._values_missing = False

        self._enabled = False
        self._current_cycle: int = 0
        self._last_update: float = monotonic()
        self._duty_cycle: Tuple[int, int] | None = None
        self._latched_cycles_per_hour: int | None = None

        self._first_duty_cycle_start: float | None = None
        self._last_duty_cycle_percentage: float | None = None
        self._duty_cycle_branch: str | None = None
        self._hold: str | None = None

    def restore(self, state: State) -> None:
        """Restore the PWM controller from a saved state."""
        if enabled := state.attributes.get("pulse_width_modulation_enabled"):
            self._enabled = bool(enabled)

    def enable(self) -> None:
        """Enable the PWM control."""
        self._enabled = True

    def disable(self, reason: str = "disabled") -> None:
        """Disable the PWM control; leaving ON or OFF is logged on the timeline with `reason`."""
        self.reset(reason)
        self._enabled = False
        self._setpoint_adjuster.reset()

    def update_cycles_per_hour(self, outside_temperature: float | None) -> None:
        """Set the current cycles per hour from the outside temperature; a missing temperature keeps the last value."""
        if outside_temperature is None:
            return

        cycles_per_hour = self._cycles.cycles_per_hour(outside_temperature)
        if cycles_per_hour != self._cycles_per_hour:
            _LOGGER.info("Cycles per hour changed from %d to %d at an outside temperature of %.1f°C.", self._cycles_per_hour, cycles_per_hour, outside_temperature)
            self._cycles_per_hour = cycles_per_hour

    async def update(self, boiler: "BoilerState", flame: "FlameState", requested_setpoint: float) -> None:
        """Update the PWM state based on the output of a PID controller; the duty cycle and the setpoint adjuster read the reliable flow temperature."""
        flow_temperature = boiler.reliable_flow_temperature
        if not self._heating_curve.value or requested_setpoint is None or flow_temperature is None:
            self._go_idle("missing values")
            self._last_update = monotonic()
            # Only shown in the timeline's cycle line, which shouldn't show a stale duty next to IDLE
            self._last_duty_cycle_percentage = None
            self._duty_cycle_branch = None
            self._hold = None
            self._set_held(None)
            self._last_boiler_temperature = flow_temperature

            if not self._values_missing:
                self._values_missing = True
                _LOGGER.warning("PWM turned off due missing values.")

            return

        self._values_missing = False

        if self._last_boiler_temperature is None:
            self._last_boiler_temperature = flow_temperature
            _LOGGER.debug("Initialized last boiler temperature to %.1f°C", flow_temperature)

        if self._first_duty_cycle_start is None or (monotonic() - self._first_duty_cycle_start) > 3600:
            self._current_cycle = 0
            self._first_duty_cycle_start = monotonic()
            _LOGGER.info("CYCLES count reset for the rolling hour.")

        elapsed = monotonic() - self._last_update
        cycles_per_hour = self.cycles_per_hour
        if self._current_cycle < cycles_per_hour:
            self._lift_cycles_cap()

        thresholds = CycleThresholds.from_cycles_per_hour(cycles_per_hour)
        self._duty_cycle = self._calculate_duty_cycle(requested_setpoint, thresholds)

        # Update boiler temperature if heater has just started up
        if self._status == PWMStatus.ON:
            if elapsed <= HEATER_STARTUP_TIMEFRAME:
                self._last_boiler_temperature = (self._alpha * flow_temperature + (1 - self._alpha) * self._last_boiler_temperature)
            else:
                self._last_boiler_temperature = flow_temperature

        # Control the adjusted setpoint; hot water says nothing about heating, so it doesn't adjust
        if flame.is_active and not boiler.hot_water_active and boiler.status not in (BoilerStatus.PUMP_STARTING, BoilerStatus.OVERSHOOT_HANDLING) and flame.latest_on_time_seconds is not None and flame.latest_on_time_seconds > 6:
            target = flow_temperature - 3
            self._setpoint = self._setpoint_adjuster.adjust(target_setpoint=target)
        elif flame.is_inactive and not boiler.hot_water_active and (flame.average_on_time_seconds is None or flame.average_on_time_seconds < 60):
            target = flow_temperature + 10
            self._setpoint = self._setpoint_adjuster.force(target_setpoint=target)
        elif self._setpoint_adjuster.current is not None:
            self._setpoint = self._setpoint_adjuster.current

        if (
                # Check if we are above the overshoot temperature
                boiler.status == BoilerStatus.COOLING_DOWN and
                self._setpoint_adjuster.current is not None and math.floor(requested_setpoint) > math.floor(self._setpoint)
        ):
            _LOGGER.info("Setpoint stabilization detected, disabling Pulse Width Modulation.")
            self.disable("setpoint stabilization")
            return

        # A pulse that is on is not affected by a hold
        self._hold = None if self._status == PWMStatus.ON else self._hot_water_hold(boiler, flame)

        # State transitions for PWM
        start_due = self._status != PWMStatus.ON and self._duty_cycle[0] >= HEATER_STARTUP_TIMEFRAME and (
            elapsed >= self._duty_cycle[1] or self._status == PWMStatus.IDLE or self._held is not None
        )

        # A held pulse starts nothing: it isn't counted, latches no N and isn't capped; the next cycle decides again
        self._set_held(self._hold if start_due else None)
        if self._held is not None:
            # Held in the off phase: IDLE would have the climate heat without PWM
            if self._status == PWMStatus.IDLE:
                self._status = PWMStatus.OFF
                self._last_update = monotonic()
                timeline.LOGGER.info("pwm OFF")
            return

        if start_due:
            if self._current_cycle >= cycles_per_hour:
                if not self._cycles_capped:
                    self._cycles_capped = True
                    timeline.LOGGER.info("pwm cycles cap reached (%d/h)", cycles_per_hour)
                return

            self._current_cycle += 1
            self._status = PWMStatus.ON
            self._last_update = monotonic()
            self._last_boiler_temperature = flow_temperature
            self._latched_cycles_per_hour = self._cycles_per_hour
            self._minimum_pulse_held = False
            timeline.LOGGER.info("pwm ON (pulse %d this hour, %d cycles/h)", self._current_cycle, self._latched_cycles_per_hour)
            return

        # A started pulse runs at least the minimum on time
        if self._status == PWMStatus.ON and elapsed < thresholds.on_time_lower:
            # Logged once per pulse, when the duty cycle alone would end it
            if not self._minimum_pulse_held and (self._duty_cycle[0] < HEATER_STARTUP_TIMEFRAME or elapsed >= self._duty_cycle[0]):
                self._minimum_pulse_held = True
                timeline.LOGGER.info("pwm ON held for the minimum pulse (%.0fs)", thresholds.on_time_lower)
            return

        if self._status != PWMStatus.OFF and (self._duty_cycle[0] < HEATER_STARTUP_TIMEFRAME or elapsed >= self._duty_cycle[0] or self._status == PWMStatus.IDLE):
            self._status = PWMStatus.OFF
            self._last_update = monotonic()
            timeline.LOGGER.info("pwm OFF")
            return

    def _hot_water_hold(self, boiler: "BoilerState", flame: "FlameState") -> str | None:
        """Why no pulse may start: the boiler produces hot water, or the hot water delay after a hot water burn runs; None if none applies."""
        if boiler.hot_water_active:
            return "hot water"

        if (since := flame.seconds_since_hot_water_burn) is not None and since < self._hot_water_delay:
            return "hot water delay"

        return None

    def _set_held(self, reason: str | None) -> None:
        """Record why a due pulse start is held, None if none is; a change is logged on the timeline."""
        if reason == self._held:
            return

        if reason is None:
            timeline.LOGGER.info("pwm hold ended")
        else:
            timeline.LOGGER.info("pwm hold (%s)", reason)

        self._held = reason

    def _go_idle(self, reason: str) -> None:
        """Switch to IDLE, which ends the latched cycles per hour; leaving ON or OFF is logged on the timeline with `reason`."""
        self._latched_cycles_per_hour = None
        if self._status != PWMStatus.IDLE:
            timeline.LOGGER.info("pwm IDLE (%s)", reason)
            self._status = PWMStatus.IDLE

    def _lift_cycles_cap(self) -> None:
        """Forget that the cycles-per-hour cap blocked a pulse; lifting it is logged on the timeline."""
        if self._cycles_capped:
            self._cycles_capped = False
            timeline.LOGGER.info("pwm cycles cap lifted")

    def _calculate_duty_cycle(self, requested_setpoint: float, thresholds: CycleThresholds) -> Tuple[int, int]:
        """Calculate the duty cycle in seconds based on the output of a PID controller and a heating curve value; records the branch that applied."""
        on_time, off_time, self._duty_cycle_branch = self._duty_cycle_times(requested_setpoint, thresholds)
        return int(on_time), int(off_time)

    def _duty_cycle_times(self, requested_setpoint: float, thresholds: CycleThresholds) -> tuple[float, float, str]:
        """Return the on and off times in seconds and a short name of the duty cycle branch that applied."""
        base_offset = self._heating_curve.base_offset
        boiler_temperature = self._last_boiler_temperature

        # Ensure the boiler temperature is above the base offset
        boiler_temperature = max(boiler_temperature, base_offset + 1)

        # Calculate duty cycle percentage
        self._last_duty_cycle_percentage = (requested_setpoint - base_offset) / (boiler_temperature - base_offset)
        self._last_duty_cycle_percentage = min(max(self._last_duty_cycle_percentage, 0), 1)

        # If automatic duty cycle control is disabled
        if not self._automatic_duty_cycle:
            return self._last_duty_cycle_percentage * self._cycles.maximum_time, (1 - self._last_duty_cycle_percentage) * self._cycles.maximum_time, "manual"

        # Below the minimum duty no pulse starts, whatever the flame state
        if self._last_duty_cycle_percentage < thresholds.duty_cycle_min:
            return 0, thresholds.on_time_max, "low-duty"

        # Mapping duty cycle ranges to on/off times
        if self._last_duty_cycle_percentage <= thresholds.duty_cycle_lower:
            return thresholds.on_time_lower, (thresholds.on_time_lower / self._last_duty_cycle_percentage) - thresholds.on_time_lower, "low"

        if self._last_duty_cycle_percentage <= thresholds.duty_cycle_upper:
            return thresholds.on_time_upper * self._last_duty_cycle_percentage, thresholds.on_time_upper * (1 - self._last_duty_cycle_percentage), "mid"

        if self._last_duty_cycle_percentage <= thresholds.duty_cycle_max:
            return thresholds.on_time_lower / (1 - self._last_duty_cycle_percentage) - thresholds.on_time_lower, thresholds.on_time_lower, "high"

        # Handle cases where the duty cycle exceeds the maximum allowed percentage
        return thresholds.on_time_max, 0, "max"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def status(self) -> PWMStatus:
        return self._status

    @property
    def cycles_per_hour(self) -> int:
        """The cycles per hour in effect: the value latched at pulse start during its ON phase and its OFF phase up to the cycle's maximum length, otherwise the current value."""
        latched = self._latched_cycles_per_hour
        if latched is None or self._status == PWMStatus.IDLE:
            return self._cycles_per_hour

        if self._status == PWMStatus.OFF and monotonic() - self._last_update > CycleThresholds.from_cycles_per_hour(latched).on_time_max:
            return self._cycles_per_hour

        return latched

    @property
    def state(self):
        return PWMState(
            status=self._status,
            enabled=self._enabled,
            duty_cycle=self._duty_cycle
        )

    @property
    def duty_cycle(self) -> Optional[Tuple[int, int]]:
        """Current duty cycle as a tuple of (on_time, off_time) in seconds, or None if inactive."""
        return self._duty_cycle

    @property
    def duty_cycle_branch(self) -> str | None:
        """Short name of the duty cycle branch that applied in the last calculation, e.g. `mid` or `low-duty`; None after a reset."""
        return self._duty_cycle_branch

    @property
    def hold(self) -> str | None:
        """Why no pulse may start, as of the last update: `hot water` or `hot water delay`; None if a pulse may start, while a pulse is on, or after a reset."""
        return self._hold

    @property
    def last_duty_cycle_percentage(self) -> Optional[float]:
        """Returns the last calculated duty cycle percentage."""
        return round(self._last_duty_cycle_percentage * 100, 2) if self._last_duty_cycle_percentage is not None else None

    @property
    def setpoint(self) -> float:
        """Returns the adjusted setpoint when running an ON duty cycle."""
        return self._setpoint or MINIMUM_SETPOINT
