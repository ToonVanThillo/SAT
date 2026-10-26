from __future__ import annotations

from collections import deque
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import Enum
from statistics import median
from time import monotonic
from typing import Deque, Optional, Tuple, Iterable, TYPE_CHECKING

from . import timeline
from .const import BoilerStatus, FlameStatus, PWMStatus

if TYPE_CHECKING:
    from .pwm import PWMState
    from .boiler import BoilerState


@dataclass(frozen=True, slots=True)
class FlameState:
    is_active: bool
    is_inactive: bool
    health_status: str

    latest_on_time_seconds: Optional[float]
    average_on_time_seconds: Optional[float]
    last_cycle_duration_seconds: Optional[float]

    sample_count_4h: int
    cycles_last_hour: float
    duty_ratio_last_15m: float
    median_on_duration_seconds_4h: Optional[float]

    # Seconds since the last hot water burn ended; None if none has ended yet
    seconds_since_hot_water_burn: float | None


class HotWaterSignal(str, Enum):
    """A sign in the device data that the boiler produces hot water; the value is its name on the timeline."""
    FLAG = "flag"  # the backend's hot water active flag (EMS-ESP: `tapwateractive`)
    BURNER_POWER = "burner_power"  # EMS-ESP: `selburnpow` at the configured hot water burner power
    FLOW_TEMPERATURE = "flow_temperature"  # EMS-ESP: `selflowtemp` at the configured hot water flow temperature, not sent by SAT


@dataclass(slots=True)
class _Burn:
    """The burn in progress: from flame on to flame off. It is hot water once a hot-water signal is seen while it is on, else heating."""
    started: float  # monotonic time the flame went on
    partial: bool  # already on at the first observation, so its duration is unknown
    average_before: float | None  # the average on time when it started, restored if it turns out to be hot water
    signals: set[HotWaterSignal] = field(default_factory=set)
    ticked: bool = False  # whether `update` has seen it; the first update only starts it

    @property
    def hot_water(self) -> bool:
        return bool(self.signals)

    def describe(self) -> str:
        """Its kind for the timeline: `heating`, or `hot water` with the signals seen, e.g. `hot water (flag, burner_power)`."""
        if not self.signals:
            return "heating"

        return "hot water (" + ", ".join(signal.value for signal in HotWaterSignal if signal in self.signals) + ")"


class Flame:
    """Tracks boiler flame on/off and its burns, maintains rolling statistics of heating burns, and classifies health.

    `observe` tracks burns from device data as it arrives; `update` does the same and also updates the average on time and the health.
    """

    # Thresholds
    MAX_CYCLES_PER_HOUR_PWM: float = 8.0
    MAX_CYCLES_PER_HOUR_NON_PWM: float = 3.0

    STUCK_OFF_SECONDS: float = 300.0
    STUCK_ON_WITHOUT_DEMAND_SECONDS: float = 120.0
    EXPECTED_MODULATING_DUTY_RATIO_WHEN_HEATING: float = 0.60

    # Windows
    DUTY_WINDOW_SECONDS: int = 15 * 60
    CYCLES_WINDOW_SECONDS: int = 60 * 60
    MEDIAN_WINDOW_SECONDS: int = 4 * 60 * 60

    # Debounce
    MEDIAN_TOLERANCE: float = 0.9
    MIN_ON_SAMPLES_FOR_HEALTH: int = 3
    MAX_DOMESTIC_HOT_WATER_IDLE_OFF_SECONDS: float = 30.0

    # Status buckets
    _TRANSIENT_STATUSES: Iterable[BoilerStatus] = (
        BoilerStatus.PREHEATING,
        BoilerStatus.PUMP_STARTING,
    )

    _HEATING_STATUSES: Iterable[BoilerStatus] = (
        BoilerStatus.HEATING_UP,
        BoilerStatus.NEAR_SETPOINT,
        BoilerStatus.AT_SETPOINT,
        BoilerStatus.COOLING_DOWN,
        BoilerStatus.OVERSHOOT_HANDLING,
        BoilerStatus.OVERSHOOT_STABILIZED,
    )

    def __init__(self, smoothing_alpha: float = 0.2) -> None:
        if not (0.0 <= smoothing_alpha <= 1.0):
            raise ValueError("smoothing_alpha must be within [0.0, 1.0].")

        self._smoothing_alpha: float = smoothing_alpha

        # Timing internals
        self._latest_on_time_seconds: float = 0.0
        self._average_on_time_seconds: Optional[float] = None
        self._last_cycle_duration_seconds: Optional[float] = None

        self._has_completed_first_cycle: bool = False
        # The observed flame state; None before the first observation
        self._flame_active: bool | None = None
        # The burn in progress, while the flame is on
        self._burn: _Burn | None = None
        self._flame_off_monotonic: Optional[float] = None
        self._hot_water_burn_end_monotonic: float | None = None

        # Stored states
        self._last_boiler_state: Optional[BoilerState] = None
        self._last_pulse_width_modulation_state: Optional[PWMState] = None

        # Rolling windows
        self._last_update_monotonic: Optional[float] = None
        self._cycle_end_times_window: Deque[float] = deque()
        self._on_deltas_window: Deque[Tuple[float, float]] = deque()
        self._on_durations_window: Deque[Tuple[float, float]] = deque()

        # Health
        self._health_status: str = FlameStatus.INSUFFICIENT_DATA

    @property
    def health_status(self) -> str:
        return self._health_status

    @property
    def is_active(self) -> bool:
        return bool(self._flame_active)

    @property
    def is_inactive(self) -> bool:
        return not self._flame_active

    @property
    def hot_water_burn(self) -> bool:
        """Whether the flame is on in a burn classified as hot water."""
        return self._burn is not None and self._burn.hot_water

    @property
    def seconds_since_hot_water_burn(self) -> float | None:
        """Seconds since the last hot water burn ended, partial ones included; None if none has ended yet."""
        return None if self._hot_water_burn_end_monotonic is None else monotonic() - self._hot_water_burn_end_monotonic

    @property
    def on_since(self) -> Optional[float]:
        return self._burn.started if self._burn is not None else None

    @property
    def off_since(self) -> Optional[float]:
        if not self._flame_active and self._flame_off_monotonic is not None:
            return self._flame_off_monotonic

        return None

    @property
    def latest_on_time_seconds(self) -> Optional[float]:
        return self._latest_on_time_seconds if self._burn is not None else None

    @property
    def average_on_time_seconds(self) -> Optional[float]:
        return self._average_on_time_seconds if self._has_completed_first_cycle else None

    @property
    def last_cycle_duration_seconds(self) -> Optional[float]:
        return self._last_cycle_duration_seconds

    @property
    def cycles_last_hour(self) -> float:
        return self._cycles_per_hour(monotonic())

    @property
    def duty_ratio_last_15m(self) -> float:
        return self._duty_ratio_last_window(monotonic())

    @property
    def median_on_duration_seconds_4h(self) -> Optional[float]:
        median_value, _ = self._median_on_duration(monotonic())
        return median_value

    @property
    def sample_count_4h(self) -> int:
        _, count = self._median_on_duration(monotonic())
        return count

    def observe(self, flame_active: bool, hot_water_signals: Collection[HotWaterSignal] = ()) -> None:
        """Track burns from device data as it arrives, live values only: `hot_water_signals` are the signals the data shows now.

        Opens a burn when the flame goes on, classifies it as hot water once a signal is seen while it is on (including in the
        data that switches it on), and records it when the flame goes off; heating burns only count in the statistics.
        """
        self._observe(monotonic(), flame_active, hot_water_signals)

    def update(self, boiler_state: BoilerState, pwm_state: Optional[PWMState] = None, hot_water_signals: Collection[HotWaterSignal] = ()) -> None:
        """Observe the flame of `boiler_state` as `observe` does, update the average on time of a heating burn in progress and recompute the health.

        `boiler_state.hot_water_active` is only read by the health; the classification uses `hot_water_signals`.
        """
        now = monotonic()

        self._last_boiler_state = boiler_state
        self._last_pulse_width_modulation_state = pwm_state or self._last_pulse_width_modulation_state

        self._observe(now, bool(boiler_state.flame_active), hot_water_signals)

        if (burn := self._burn) is not None:
            # The first update of a burn only starts it
            if not burn.ticked:
                burn.ticked = True
            else:
                self._latest_on_time_seconds = now - burn.started

                if self._has_completed_first_cycle and not burn.hot_water:
                    self._average_on_time_seconds = (
                        self._latest_on_time_seconds
                        if self._average_on_time_seconds is None
                        else (1.0 - self._smoothing_alpha) * self._average_on_time_seconds + self._smoothing_alpha * self._latest_on_time_seconds
                    )

        self._recompute_health(now)

    def _observe(self, now: float, flame_active: bool, hot_water_signals: Collection[HotWaterSignal]) -> None:
        """Apply the flame state and hot-water signals seen at `now`: open or close a burn when the flame switches, classify the burn in
        progress, and add a heating burn's on time since the previous observation to the duty window."""
        previously_active = self._flame_active
        elapsed = 0.0 if self._last_update_monotonic is None else max(0.0, now - self._last_update_monotonic)

        self._flame_active = flame_active
        self._last_update_monotonic = now

        # OFF -> ON, or ON at the first observation, which only seeds the state
        if flame_active and not previously_active:
            self._burn = _Burn(started=now, partial=previously_active is None, average_before=self._average_on_time_seconds)
            self._flame_off_monotonic = None

            if previously_active is not None:
                timeline.LOGGER.info("flame on")

        if (burn := self._burn) is not None:
            if flame_active:
                self._classify(burn, hot_water_signals)

            if previously_active and elapsed > 0.0 and not burn.hot_water:
                self._on_deltas_window.append((now, elapsed))

        self._prune_duty_window(now)

        # ON -> OFF
        if not flame_active and previously_active:
            self._close_burn(now)

        # OFF -> OFF, or OFF at the first observation
        elif not flame_active and self._flame_off_monotonic is None:
            self._flame_off_monotonic = now

    def _classify(self, burn: _Burn, hot_water_signals: Collection[HotWaterSignal]) -> None:
        """Add the signals seen while `burn` is on; a heating burn that turns hot water takes back its average on time and duty."""
        if burn.signals.issuperset(hot_water_signals):
            return

        was_heating = not burn.hot_water
        burn.signals.update(hot_water_signals)

        if was_heating:
            self._average_on_time_seconds = burn.average_before
            # Its on-time samples are the ones recorded after it started
            self._on_deltas_window = deque(entry for entry in self._on_deltas_window if entry[0] <= burn.started)

    def _close_burn(self, now: float) -> None:
        """End the burn in progress at `now` and log it with its kind; a heating burn is recorded in the statistics, unless it is partial."""
        burn = self._burn
        self._burn = None
        self._flame_off_monotonic = now

        if burn is None:
            return

        if burn.hot_water:
            self._hot_water_burn_end_monotonic = now

        # A partial burn adds no cycle and no duration sample
        if burn.partial:
            timeline.LOGGER.info("flame off, %s (burn started before the first observation, not counted)", burn.describe())
            return

        duration = now - burn.started

        # The statistics count heating burns only
        if burn.hot_water:
            timeline.LOGGER.info("flame off after %.1fs, %s, not counted", duration, burn.describe())
            return

        self._last_cycle_duration_seconds = duration

        self._prune_cycles_window(now)
        self._prune_median_window(now)
        self._cycle_end_times_window.append(now)
        self._on_durations_window.append((now, duration))

        self._has_completed_first_cycle = True

        timeline.LOGGER.info("flame off after %.1fs, heating (cycles_last_hour=%.1f, samples_4h=%d)", duration, self._cycles_per_hour(now), self.sample_count_4h)

    def _recompute_health(self, now: float) -> None:
        state = self._last_boiler_state
        if state is None:
            self._health_status = FlameStatus.INSUFFICIENT_DATA
            return

        last_on_seconds = self._latest_on_time_seconds if (state.flame_active and self._burn is not None) else 0.0
        last_off_seconds = (now - self._flame_off_monotonic) if (not state.flame_active and self._flame_off_monotonic) else 0.0

        cycles_per_hour = self._cycles_per_hour(now)
        duty_ratio = self._duty_ratio_last_window(now)
        median_on_seconds, sample_count = self._median_on_duration(now)

        domestic_hot_water_active = bool(state.hot_water_active)
        heating_demand = bool(state.is_active) or (state.status in self._HEATING_STATUSES)
        modulating_boiler = (state.relative_modulation_level is not None and isinstance(state.relative_modulation_level, (int, float)))

        # Thin history: still allow stuck checks, avoid cycling judgments
        if sample_count < self.MIN_ON_SAMPLES_FOR_HEALTH:
            if not heating_demand and not domestic_hot_water_active:
                if state.flame_active and last_on_seconds > self.STUCK_ON_WITHOUT_DEMAND_SECONDS:
                    self._health_status = FlameStatus.STUCK_ON
                else:
                    self._health_status = FlameStatus.IDLE_OK

                return

            timeout = self.MAX_DOMESTIC_HOT_WATER_IDLE_OFF_SECONDS if domestic_hot_water_active else self.STUCK_OFF_SECONDS
            if (not state.flame_active) and last_off_seconds > timeout and state.status not in self._TRANSIENT_STATUSES:
                self._health_status = FlameStatus.STUCK_OFF

            return

        # No demand and not domestic hot water
        if not heating_demand and not domestic_hot_water_active:
            if state.flame_active and last_on_seconds > self.STUCK_ON_WITHOUT_DEMAND_SECONDS:
                self._health_status = FlameStatus.STUCK_ON
            else:
                self._health_status = FlameStatus.IDLE_OK

            return

        # Domestic hot water: allow continuous flame; only stuck-off if off too long (and not transient)
        if domestic_hot_water_active:
            if (not state.flame_active) and last_off_seconds > self.MAX_DOMESTIC_HOT_WATER_IDLE_OFF_SECONDS and state.status not in self._TRANSIENT_STATUSES:
                self._health_status = FlameStatus.STUCK_OFF
            else:
                self._health_status = FlameStatus.HEALTHY

            return

        # Space heating demand
        if (not state.flame_active) and last_off_seconds > self.STUCK_OFF_SECONDS and state.status not in self._TRANSIENT_STATUSES:
            self._health_status = FlameStatus.STUCK_OFF
            return

        # PWM-driven demand
        if self._last_pulse_width_modulation_state is not None and self._last_pulse_width_modulation_state.status == PWMStatus.ON:
            if cycles_per_hour > self.MAX_CYCLES_PER_HOUR_PWM:
                self._health_status = FlameStatus.SHORT_CYCLING
                return

            if median_on_seconds is not None and median_on_seconds < self.MEDIAN_TOLERANCE * self._last_pulse_width_modulation_state.duty_cycle[0]:
                self._health_status = FlameStatus.PWM_SHORT
                return

            self._health_status = FlameStatus.HEALTHY
            return

        # Non-PWM: internal regulation; do not require continuous flame
        if modulating_boiler:
            if cycles_per_hour <= 2.0 and duty_ratio >= 0.8 * self.EXPECTED_MODULATING_DUTY_RATIO_WHEN_HEATING:
                self._health_status = FlameStatus.HEALTHY
                return

            if median_on_seconds is not None and median_on_seconds < self.MEDIAN_TOLERANCE * 180 and cycles_per_hour > self.MAX_CYCLES_PER_HOUR_NON_PWM:
                self._health_status = FlameStatus.SHORT_CYCLING
                return

            self._health_status = FlameStatus.HEALTHY
            return

        # Non-modulating boiler
        if median_on_seconds is not None and median_on_seconds < self.MEDIAN_TOLERANCE * 180 and cycles_per_hour > self.MAX_CYCLES_PER_HOUR_NON_PWM:
            self._health_status = FlameStatus.SHORT_CYCLING
            return

        self._health_status = FlameStatus.HEALTHY

    def _prune_duty_window(self, now: float) -> None:
        cutoff = now - self.DUTY_WINDOW_SECONDS
        while self._on_deltas_window and self._on_deltas_window[0][0] < cutoff:
            self._on_deltas_window.popleft()

    def _prune_cycles_window(self, now: float) -> None:
        cutoff = now - self.CYCLES_WINDOW_SECONDS
        while self._cycle_end_times_window and self._cycle_end_times_window[0] < cutoff:
            self._cycle_end_times_window.popleft()

    def _prune_median_window(self, now: float) -> None:
        cutoff = now - self.MEDIAN_WINDOW_SECONDS
        while self._on_durations_window and self._on_durations_window[0][0] < cutoff:
            self._on_durations_window.popleft()

    def _duty_ratio_last_window(self, now: float) -> float:
        self._prune_duty_window(now)
        on_seconds = sum(delta for _, delta in self._on_deltas_window)
        return min(1.0, max(0.0, on_seconds / float(self.DUTY_WINDOW_SECONDS)))

    def _cycles_per_hour(self, now: float) -> float:
        self._prune_cycles_window(now)
        return float(len(self._cycle_end_times_window))

    def _median_on_duration(self, now: float) -> Tuple[Optional[float], int]:
        self._prune_median_window(now)
        if not self._on_durations_window:
            return None, 0

        durations = [duration for _, duration in self._on_durations_window]
        return float(median(durations)), len(durations)
