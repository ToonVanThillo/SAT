"""Circulation after hot water (EMS-ESP): run the boiler's pump without firing, so the heat left in the boiler after a hot water burn
reaches the radiators.

After hot water some combi boilers stop their pump and keep it off for minutes, also with heat demand. Requesting heating with a
control setpoint well below the flow temperature makes the boiler run its pump without firing.
"""
from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from . import timeline
from .const import PWMStatus
from .flame import Flame

if TYPE_CHECKING:
    from .boiler import BoilerState
    from .flame import FlameState

# Circulation needs a flow temperature at least this far above its setpoint, so the boiler doesn't fire
FLOW_MARGIN = 10


class Circulation:
    """Decides per HEAT control cycle whether to override a heater OFF decision with heater ON at the circulation setpoint.

    It starts at most once per hot water burn and ends at the first cycle where a condition for it fails or its duration has passed.
    It only overrides the cycle's decision; it never changes PWM.
    """

    def __init__(self, setpoint: float, duration: float) -> None:
        """`setpoint` is the control setpoint requested while circulating (°C), `duration` the longest it runs (seconds)."""
        self._setpoint: float = setpoint
        self._duration: float = duration
        # Monotonic start time while circulating, else None
        self._started_at: float | None = None
        # The `FlameState.hot_water_burns` of the last start: the burn already circulated after
        self._burn: int | None = None
        # Whether the pump was seen on, or its restart wait cleared, during the current circulation
        self._pump_started: bool = False

    @property
    def setpoint(self) -> float:
        """The control setpoint requested while circulating."""
        return self._setpoint

    @property
    def active(self) -> bool:
        """Whether circulation runs, as of the last update."""
        return self._started_at is not None

    def update(self, boiler: BoilerState, flame: FlameState, *, valves_open: bool, error: float, pwm_status: PWMStatus, heater_off: bool) -> bool:
        """Start, continue or end circulation in a HEAT cycle and return whether this cycle requests it.

        `boiler` and `flame` are the cycle's snapshot, `valves_open` the captured valve state, `error` the worst error the PID controls
        on (positive: below target), `pwm_status` PWM's status after its update, `heater_off` whether the cycle's own decision is
        heater OFF.

        It starts when the pump restart is awaited after a hot water burn other than the one last circulated after, the pump is off,
        neither hot water nor the flame is on, it would end before flame health's stuck-off limit (`_fits_before_stuck_off`), and
        nothing in `_blocked_by` applies. It ends, logging the reason, at the first update
        where hot water is on or a hot water burn has ended since the start, the flame is on (also warned about), something in
        `_blocked_by` applies, or the duration has passed since the start. The pump coming on, or its restart wait clearing, doesn't
        end it; if neither was seen by the end, a warning is logged.
        """
        if self._started_at is None:
            if (
                    boiler.awaiting_pump_restart and boiler.pump_active is False and not boiler.hot_water_active and not boiler.flame_active
                    and flame.hot_water_burns != self._burn and self._fits_before_stuck_off(flame)
                    and self._blocked_by(boiler, valves_open=valves_open, error=error, pwm_status=pwm_status, heater_off=heater_off) is None
            ):
                self._start(boiler, flame)
                return True

            return False

        self._observe_pump(boiler)

        # Also a hot water burn that started and ended since the last cycle; checked first, so a draw firing the burner is no warning
        if boiler.hot_water_active or flame.hot_water_burns != self._burn:
            self.stop("hot water")
            return False

        if boiler.flame_active:
            self.stop("flame on")
            timeline.LOGGER.warning("circulation: the boiler fired during circulation")
            return False

        if (reason := self._blocked_by(boiler, valves_open=valves_open, error=error, pwm_status=pwm_status, heater_off=heater_off)) is not None:
            self.stop(reason)
            return False

        if monotonic() - self._started_at >= self._duration:
            self.stop("duration")
            return False

        return True

    def stop(self, reason: str, boiler: BoilerState | None = None) -> None:
        """End circulation, if it runs, logging `reason`; warn if the pump wasn't seen starting. `boiler` is the cycle's snapshot, if the
        caller has one, observed for the pump first."""
        if self._started_at is None:
            return

        if boiler is not None:
            self._observe_pump(boiler)

        self._started_at = None
        timeline.LOGGER.info("circulation end (%s)", reason)

        if not self._pump_started:
            timeline.LOGGER.warning("circulation: the pump did not start during circulation")

    def _blocked_by(self, boiler: BoilerState, *, valves_open: bool, error: float, pwm_status: PWMStatus, heater_off: bool) -> str | None:
        """Why circulation may not run in this cycle, None if it may: no PWM pulse is on, a valve is open, heating is wanted (error at
        least 0), the raw flow temperature is at least `FLOW_MARGIN` above the setpoint, and the cycle's own decision is heater OFF."""
        if pwm_status == PWMStatus.ON:
            return "pwm on"

        if not valves_open:
            return "valves closed"

        if error < 0:
            return "no heat demand"

        if boiler.flow_temperature is None or boiler.flow_temperature < self._setpoint + FLOW_MARGIN:
            return "flow temperature"

        if not heater_off:
            return "heater on"

        return None

    def _fits_before_stuck_off(self, flame: FlameState) -> bool:
        """Whether a circulation started now ends before flame health counts the flame as stuck off: the time since the last hot water
        burn ended (its flame off) plus the duration stays below `Flame.STUCK_OFF_SECONDS`."""
        since = flame.seconds_since_hot_water_burn
        return since is not None and since + self._duration < Flame.STUCK_OFF_SECONDS

    def _start(self, boiler: BoilerState, flame: FlameState) -> None:
        """Start circulating after the last hot water burn, logged with the trigger."""
        self._started_at = monotonic()
        self._burn = flame.hot_water_burns
        self._pump_started = False
        timeline.LOGGER.info(
            "circulation start (pump off after hot water, flow=%s, setpoint %g for %gs)", timeline.fmt(boiler.flow_temperature), self._setpoint, self._duration
        )

    def _observe_pump(self, boiler: BoilerState) -> None:
        """Note that the pump started: it is on, or its restart wait cleared (it went from off to on since)."""
        if boiler.pump_active or not boiler.awaiting_pump_restart:
            self._pump_started = True
