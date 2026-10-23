import logging
import math
from dataclasses import dataclass
from time import monotonic
from typing import Optional, Tuple, TYPE_CHECKING

from homeassistant.core import State

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

    def __init__(self, cycles: CycleConfig, heating_curve: HeatingCurve, supports_relative_modulation_management: bool, automatic_duty_cycle: bool, force: bool = False):
        """Initialize the PWM control."""
        self._alpha: float = 0.2
        self._force: bool = force
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

        self.reset()

    def reset(self) -> None:
        """Reset the PWM control."""
        self._enabled = False
        self._current_cycle: int = 0
        self._status: PWMStatus = PWMStatus.IDLE
        self._last_update: float = monotonic()
        self._duty_cycle: Tuple[int, int] | None = None
        self._latched_cycles_per_hour: int | None = None

        self._first_duty_cycle_start: float | None = None
        self._last_duty_cycle_percentage: float | None = None

    def restore(self, state: State) -> None:
        """Restore the PWM controller from a saved state."""
        if enabled := state.attributes.get("pulse_width_modulation_enabled"):
            self._enabled = bool(enabled)

    def enable(self) -> None:
        """Enable the PWM control."""
        self._enabled = True

    def disable(self) -> None:
        """Disable the PWM control."""
        self.reset()
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
        """Update the PWM state based on the output of a PID controller."""
        if not self._heating_curve.value or requested_setpoint is None or boiler.flow_temperature is None:
            self._status = PWMStatus.IDLE
            self._last_update = monotonic()
            self._last_boiler_temperature = boiler.flow_temperature

            _LOGGER.warning("PWM turned off due missing values.")

            return

        if self._last_boiler_temperature is None:
            self._last_boiler_temperature = boiler.flow_temperature
            _LOGGER.debug("Initialized last boiler temperature to %.1f°C", boiler.flow_temperature)

        if self._first_duty_cycle_start is None or (monotonic() - self._first_duty_cycle_start) > 3600:
            self._current_cycle = 0
            self._first_duty_cycle_start = monotonic()
            _LOGGER.info("CYCLES count reset for the rolling hour.")

        elapsed = monotonic() - self._last_update
        cycles_per_hour = self.cycles_per_hour
        thresholds = CycleThresholds.from_cycles_per_hour(cycles_per_hour)
        self._duty_cycle = self._calculate_duty_cycle(requested_setpoint, boiler, thresholds)

        # Update boiler temperature if heater has just started up
        if self._status == PWMStatus.ON:
            if elapsed <= HEATER_STARTUP_TIMEFRAME:
                self._last_boiler_temperature = (self._alpha * boiler.flow_temperature + (1 - self._alpha) * self._last_boiler_temperature)

                _LOGGER.debug("Updated last boiler temperature with weighted average during startup phase.")
            else:
                self._last_boiler_temperature = boiler.flow_temperature
                _LOGGER.debug("Updated last boiler temperature to %.1f°C", boiler.flow_temperature)

        # Control the adjusted setpoint
        if flame.is_active and boiler.status not in (BoilerStatus.PUMP_STARTING, BoilerStatus.OVERSHOOT_HANDLING) and flame.latest_on_time_seconds is not None and flame.latest_on_time_seconds > 6:
            target = boiler.flow_temperature - 3
            self._setpoint = self._setpoint_adjuster.adjust(target_setpoint=target)
            _LOGGER.debug("Adjusting setpoint for active flame - Flow temp: %.1f°C, Target: %.1f°C, New setpoint: %.1f°C", boiler.flow_temperature, target, self._setpoint)
        elif flame.is_inactive and (flame.average_on_time_seconds is None or flame.average_on_time_seconds < 60):
            target = boiler.flow_temperature + 10
            self._setpoint = self._setpoint_adjuster.force(target_setpoint=target)
            _LOGGER.debug("Forcing setpoint up due to short flame cycles - Flow temp: %.1f°C, Target: %.1f°C, New setpoint: %.1f°C", boiler.flow_temperature, target, self._setpoint)
        elif self._setpoint_adjuster.current is not None:
            self._setpoint = self._setpoint_adjuster.current
            _LOGGER.debug("Maintaining current setpoint at %.1f°C", self._setpoint)

        if (
                # Check if we are above the overshoot temperature
                boiler.status == BoilerStatus.COOLING_DOWN and
                self._setpoint_adjuster.current is not None and math.floor(requested_setpoint) > math.floor(self._setpoint)
        ):
            _LOGGER.info("Setpoint stabilization detected, disabling Pulse Width Modulation.")
            self.disable()
            return

        # State transitions for PWM
        if self._status != PWMStatus.ON and self._duty_cycle[0] >= HEATER_STARTUP_TIMEFRAME and (elapsed >= self._duty_cycle[1] or self._status == PWMStatus.IDLE):
            if self._current_cycle >= cycles_per_hour:
                _LOGGER.info("Reached max cycles per hour (%d), preventing new duty cycle.", cycles_per_hour)
                return

            self._current_cycle += 1
            self._status = PWMStatus.ON
            self._last_update = monotonic()
            self._last_boiler_temperature = boiler.flow_temperature
            self._latched_cycles_per_hour = self._cycles_per_hour
            _LOGGER.info("Starting new duty cycle (ON state) at %d cycles per hour. Current CYCLES count: %d", self._latched_cycles_per_hour, self._current_cycle)
            return

        # A started pulse runs at least the minimum on time
        if self._status == PWMStatus.ON and elapsed < thresholds.on_time_lower:
            _LOGGER.debug("Keeping the ON state for the minimum pulse of %.0f seconds, elapsed: %.0f seconds.", thresholds.on_time_lower, elapsed)
            return

        if self._status != PWMStatus.OFF and (self._duty_cycle[0] < HEATER_STARTUP_TIMEFRAME or elapsed >= self._duty_cycle[0] or self._status == PWMStatus.IDLE):
            self._status = PWMStatus.OFF
            self._last_update = monotonic()
            _LOGGER.info("Duty cycle completed. Switching to OFF state.")
            return

        _LOGGER.debug("Cycle time elapsed: %.0f seconds in state: %s", elapsed, self._status)

    def _calculate_duty_cycle(self, requested_setpoint: float, boiler: "BoilerState", thresholds: CycleThresholds) -> Tuple[int, int]:
        """Calculate the duty cycle in seconds based on the output of a PID controller and a heating curve value."""
        base_offset = self._heating_curve.base_offset
        boiler_temperature = self._last_boiler_temperature

        # Ensure the boiler temperature is above the base offset
        boiler_temperature = max(boiler_temperature, base_offset + 1)

        # Calculate duty cycle percentage
        self._last_duty_cycle_percentage = (requested_setpoint - base_offset) / (boiler_temperature - base_offset)
        self._last_duty_cycle_percentage = min(max(self._last_duty_cycle_percentage, 0), 1)

        _LOGGER.debug(
            "Duty cycle calculation - Requested setpoint: %.1f°C, Boiler temperature: %.1f°C, Duty cycle percentage: %.2f%%",
            requested_setpoint, boiler_temperature, self._last_duty_cycle_percentage * 100
        )

        # If automatic duty cycle control is disabled
        if not self._automatic_duty_cycle:
            on_time = self._last_duty_cycle_percentage * self._cycles.maximum_time
            off_time = (1 - self._last_duty_cycle_percentage) * self._cycles.maximum_time

            _LOGGER.debug(
                "Calculated on_time: %.0f seconds, off_time: %.0f seconds.",
                on_time, off_time
            )

            return int(on_time), int(off_time)

        # Handle special low-duty cycle cases
        if self._last_duty_cycle_percentage < thresholds.duty_cycle_min:
            if boiler.flame_active and not boiler.hot_water_active:
                on_time = thresholds.on_time_lower
                off_time = thresholds.on_time_max - thresholds.on_time_lower

                _LOGGER.debug(
                    "Special low-duty case with flame active. Setting on_time: %d seconds, off_time: %d seconds.",
                    on_time, off_time
                )

                return int(on_time), int(off_time)

            _LOGGER.debug("Special low-duty case without flame. Setting on_time: 0 seconds, off_time: %d seconds.", thresholds.on_time_max)
            return 0, int(thresholds.on_time_max)

        # Mapping duty cycle ranges to on/off times
        if self._last_duty_cycle_percentage <= thresholds.duty_cycle_lower:
            on_time = thresholds.on_time_lower
            off_time = (thresholds.on_time_lower / self._last_duty_cycle_percentage) - thresholds.on_time_lower

            _LOGGER.debug(
                "Low duty cycle range, cycles this hour: %d. Calculated on_time: %d seconds, off_time: %d seconds.",
                self._current_cycle, on_time, off_time
            )

            return int(on_time), int(off_time)

        if self._last_duty_cycle_percentage <= thresholds.duty_cycle_upper:
            on_time = thresholds.on_time_upper * self._last_duty_cycle_percentage
            off_time = thresholds.on_time_upper * (1 - self._last_duty_cycle_percentage)

            _LOGGER.debug(
                "Mid-range duty cycle, cycles this hour: %d. Calculated on_time: %d seconds, off_time: %d seconds.",
                self._current_cycle, on_time, off_time
            )

            return int(on_time), int(off_time)

        if self._last_duty_cycle_percentage <= thresholds.duty_cycle_max:
            on_time = thresholds.on_time_lower / (1 - self._last_duty_cycle_percentage) - thresholds.on_time_lower
            off_time = thresholds.on_time_lower

            _LOGGER.debug(
                "High duty cycle range, cycles this hour: %d. Calculated on_time: %d seconds, off_time: %d seconds.",
                self._current_cycle, on_time, off_time
            )

            return int(on_time), int(off_time)

        # Handle cases where the duty cycle exceeds the maximum allowed percentage
        on_time = thresholds.on_time_max
        off_time = 0

        _LOGGER.debug("Maximum duty cycle exceeded. Setting on_time: %d seconds, off_time: %d seconds.", on_time, off_time)
        return int(on_time), int(off_time)

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
    def last_duty_cycle_percentage(self) -> Optional[float]:
        """Returns the last calculated duty cycle percentage."""
        return round(self._last_duty_cycle_percentage * 100, 2) if self._last_duty_cycle_percentage is not None else None

    @property
    def setpoint(self) -> float:
        """Returns the adjusted setpoint when running an ON duty cycle."""
        return self._setpoint or MINIMUM_SETPOINT
