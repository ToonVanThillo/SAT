"""PWM cycles per hour from the outside temperature, latching at pulse start, the 180 s minimum pulse and the 180 s minimum off phase."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.const import BoilerStatus, PWMStatus
from custom_components.sat.pwm import PWM, CycleConfig, CycleThresholds

DEFAULTS = {"cold_temperature": 0, "cold_cycles_per_hour": 4, "mild_temperature": 12, "mild_cycles_per_hour": 2}

BASE = 20.0
FLOW = 50.0


def config(**overrides) -> CycleConfig:
    return CycleConfig(**(DEFAULTS | overrides))


# Mapping

@pytest.mark.parametrize(("temperature", "expected"), [
    (-20, 4), (0, 4), (2.9, 4), (3, 4), (3.01, 3), (6, 3), (9, 3), (9.01, 2), (12, 2), (30, 2),
])
def test_default_mapping(temperature, expected):
    assert config().cycles_per_hour(temperature) == expected


def test_inverted_points():
    cycles = config(cold_temperature=12, mild_temperature=0)
    # Cold side is 12: N_cold at 12 and above, N_mild at 0 and below
    assert cycles.cycles_per_hour(15) == 4
    assert cycles.cycles_per_hour(12) == 4
    assert cycles.cycles_per_hour(-5) == 2
    assert cycles.cycles_per_hour(0) == 2
    assert cycles.cycles_per_hour(6) == 3


def test_inverted_cycle_counts():
    cycles = config(cold_cycles_per_hour=2, mild_cycles_per_hour=4)
    assert cycles.cycles_per_hour(-10) == 2
    assert cycles.cycles_per_hour(0) == 2
    assert cycles.cycles_per_hour(6) == 3
    assert cycles.cycles_per_hour(40) == 4


@pytest.mark.parametrize("temperature", [-10, 0, 5, 12, 40])
def test_equal_cycle_counts_give_a_fixed_n(temperature):
    assert config(cold_cycles_per_hour=3, mild_cycles_per_hour=3).cycles_per_hour(temperature) == 3


def test_halves_round_up():
    # 4 -> 1 over 0..6: at 1 degree x = 3.5 rounds to 4, at 3 degrees x = 2.5 rounds to 3
    cycles = config(mild_temperature=6, mild_cycles_per_hour=1)
    assert cycles.cycles_per_hour(1) == 4
    assert cycles.cycles_per_hour(3) == 3
    assert cycles.cycles_per_hour(5) == 2


def test_equal_temperatures_are_invalid():
    with pytest.raises(ValueError):
        config(mild_temperature=0)


@pytest.mark.parametrize("overrides", [
    {"cold_cycles_per_hour": 0}, {"mild_cycles_per_hour": 0}, {"mild_cycles_per_hour": -1},
    {"cold_cycles_per_hour": 7}, {"mild_cycles_per_hour": 7},
    {"cold_temperature": float("nan")}, {"mild_temperature": float("nan")},
    {"cold_temperature": float("inf")}, {"mild_temperature": float("inf")},
    {"cold_temperature": float("-inf")}, {"mild_temperature": float("-inf")},
])
def test_invalid_config_raises(overrides):
    with pytest.raises(ValueError):
        config(**overrides)


@pytest.mark.parametrize("overrides", [{"cold_cycles_per_hour": 6}, {"mild_cycles_per_hour": 6}, {"cold_cycles_per_hour": 1, "mild_cycles_per_hour": 1}])
def test_boundary_cycles_are_valid(overrides):
    config(**overrides)


# Thresholds

@pytest.mark.parametrize("n", [1, 2, 3, 4, 6])
def test_thresholds(n):
    thresholds = CycleThresholds.from_cycles_per_hour(n)
    upper = 3600 / n
    assert thresholds.on_time_lower == 180
    assert thresholds.on_time_upper == pytest.approx(upper)
    assert thresholds.on_time_max == pytest.approx(2 * upper)
    assert thresholds.duty_cycle_lower == pytest.approx(180 / upper)
    assert thresholds.duty_cycle_upper == pytest.approx(1 - 180 / upper)
    assert thresholds.duty_cycle_min == pytest.approx(90 / upper)
    assert thresholds.duty_cycle_max == pytest.approx(1 - 90 / upper)


@pytest.mark.parametrize(("n", "floor"), [(2, 0.05), (3, 0.075), (4, 0.10)])
def test_no_heat_floor(n, floor):
    assert CycleThresholds.from_cycles_per_hour(n).duty_cycle_min == pytest.approx(floor)


# PWM harness

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


CURVE = SimpleNamespace(value=30.0, base_offset=BASE)


def make_pwm(**overrides) -> PWM:
    CURVE.value = 30.0
    return PWM(config(**overrides), CURVE)  # type: ignore[arg-type]


def boiler(status=BoilerStatus.AT_SETPOINT, flame_active=False, hot_water=False, flow=FLOW):
    return SimpleNamespace(status=status, flame_active=flame_active, hot_water_active=hot_water, flow_temperature=flow, reliable_flow_temperature=flow)


def flame(active=False):
    return SimpleNamespace(is_active=active, is_inactive=not active, latest_on_time_seconds=None, average_on_time_seconds=100, seconds_since_hot_water_burn=None)


def setpoint_for(duty: float) -> float:
    """Requested setpoint that gives the duty cycle for a boiler temperature of FLOW."""
    return BASE + duty * (FLOW - BASE)


async def step(pwm: PWM, clock: Clock, duty: float, seconds: float = 0, minimum_setpoint: float = 10.0, allow_unrestricted: bool = False, **boiler_args):
    flame_active = boiler_args.get("flame_active", False)
    flame_state = flame(flame_active)
    boiler_state = boiler(**boiler_args)
    clock.tick(seconds)
    await pwm.update(boiler_state, flame_state, setpoint_for(duty), minimum_setpoint=minimum_setpoint, allow_unrestricted=allow_unrestricted)  # type: ignore[arg-type]


async def start_pulse(pwm: PWM, clock: Clock, duty: float = 0.5) -> None:
    await step(pwm, clock, duty)
    assert pwm.state.status == PWMStatus.ON


# None handling and start-up N

def test_startup_n_is_cold(clock):
    assert make_pwm().cycles_per_hour == 4
    assert make_pwm(cold_cycles_per_hour=3).cycles_per_hour == 3


def test_none_before_any_reading_keeps_cold(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(None)
    assert pwm.cycles_per_hour == 4


def test_none_keeps_last_n(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)
    assert pwm.cycles_per_hour == 2
    pwm.update_cycles_per_hour(None)
    assert pwm.cycles_per_hour == 2


def test_idle_uses_current_n(clock):
    pwm = make_pwm()
    for temperature, expected in [(20, 2), (6, 3), (-5, 4)]:
        pwm.update_cycles_per_hour(temperature)
        assert pwm.cycles_per_hour == expected


def test_n_change_is_logged_at_info(clock, caplog):
    pwm = make_pwm()
    with caplog.at_level(logging.INFO, logger="custom_components.sat.pwm"):
        pwm.update_cycles_per_hour(20)
    assert [r for r in caplog.records if r.levelno == logging.INFO and "ycles per hour" in r.getMessage()]


def test_unchanged_n_is_not_logged(clock, caplog):
    pwm = make_pwm()
    with caplog.at_level(logging.INFO, logger="custom_components.sat.pwm"):
        pwm.update_cycles_per_hour(-5)
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


# Latching

async def test_pulse_start_logs_latched_n(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)
    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await start_pulse(pwm, clock)
    assert "pwm ON (2 cycles/h)" in caplog.text


async def test_n_change_mid_on_is_latched(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    on_off = pwm.state.duty_cycle
    assert on_off == (450, 450)

    pwm.update_cycles_per_hour(20)
    assert pwm.cycles_per_hour == 4
    await step(pwm, clock, 0.5, 100)
    assert pwm.state.duty_cycle == on_off
    assert pwm.state.status == PWMStatus.ON


async def test_n_change_mid_off_is_latched(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.5, 450)
    assert pwm.state.status == PWMStatus.OFF

    pwm.update_cycles_per_hour(20)
    await step(pwm, clock, 0.5, 100)
    assert pwm.cycles_per_hour == 4
    assert pwm.state.duty_cycle == (450, 450)
    assert pwm.state.status == PWMStatus.OFF


async def test_idle_releases_the_latch_before_a_low_duty_off_phase(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    assert pwm.cycles_per_hour == 4

    # Missing values end the pulse; N changes while idle
    CURVE.value = None
    await step(pwm, clock, 0.5, 10)
    assert pwm.state.status == PWMStatus.IDLE
    CURVE.value = 30.0
    pwm.update_cycles_per_hour(20)

    # Below the minimum duty the PWM goes from idle to off without a pulse
    await step(pwm, clock, 0.0, 10)

    assert pwm.state.status == PWMStatus.OFF
    assert pwm.cycles_per_hour == 2


async def test_off_phase_beyond_twice_the_cycle_releases_latch(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.5, 900)
    assert pwm.state.status == PWMStatus.OFF

    pwm.update_cycles_per_hour(20)
    clock.tick(1800)
    assert pwm.cycles_per_hour == 4
    clock.tick(1)
    assert pwm.cycles_per_hour == 2


async def test_next_pulse_start_latches_the_new_n(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.5, 900)
    pwm.update_cycles_per_hour(20)
    await step(pwm, clock, 0.5, 900)
    assert pwm.state.status == PWMStatus.ON
    assert pwm.cycles_per_hour == 2
    pwm.update_cycles_per_hour(-5)
    assert pwm.cycles_per_hour == 2


async def test_reset_returns_to_current_n(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock)
    pwm.update_cycles_per_hour(20)
    assert pwm.cycles_per_hour == 4
    pwm.reset()
    assert pwm.cycles_per_hour == 2


async def test_disable_returns_to_current_n(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock)
    pwm.update_cycles_per_hour(20)
    pwm.disable()
    assert pwm.cycles_per_hour == 2


# Minimum pulse

async def test_duty_drop_to_zero_does_not_end_pulse_before_180s(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)

    await step(pwm, clock, 0.0, 10)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.0, 100)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.0, 69)  # 179 s
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.0, 1)  # 180 s
    assert pwm.state.status == PWMStatus.OFF


async def test_normal_end_applies_after_180s(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)  # N=4: on 450
    await step(pwm, clock, 0.5, 200)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 249)  # 449 s
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 1)  # 450 s
    assert pwm.state.status == PWMStatus.OFF


async def test_missing_flow_temperature_ends_pulse_at_once(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    await step(pwm, clock, 0.5, 5, flow=None)
    assert pwm.state.status == PWMStatus.IDLE


async def test_missing_heating_curve_ends_pulse_at_once(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    CURVE.value = None  # type: ignore[assignment]
    await step(pwm, clock, 0.5, 5)
    assert pwm.state.status == PWMStatus.IDLE


async def test_reset_ends_pulse_at_once(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    pwm.reset()
    assert pwm.status == PWMStatus.IDLE


async def test_disable_ends_pulse_at_once(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    pwm.disable()
    assert pwm.status == PWMStatus.IDLE
    assert not pwm.enabled


async def test_a_higher_request_while_cooling_down_keeps_the_pulse(clock):
    pwm = make_pwm()
    pwm.enable()
    await start_pulse(pwm, clock, 0.5)
    # Active flame sets an adjusted setpoint (flow - 3); a higher request while cooling down no longer disables PWM
    active = flame(True)
    active.latest_on_time_seconds = 60
    await pwm.update(boiler(), active, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]
    clock.tick(5)
    await pwm.update(boiler(status=BoilerStatus.COOLING_DOWN, flame_active=True), active, 60.0, minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]
    assert pwm.status == PWMStatus.ON
    assert pwm.enabled


# Low-duty branches

@pytest.mark.parametrize(("n", "temperature"), [(2, 20), (3, 6), (4, -5)])
async def test_branches(clock, n, temperature):
    upper = 3600 / n
    lower = 180 / upper
    minimum = lower / 2
    cases = {
        minimum * 0.5: (0, upper * 2),
        minimum: (180, 180 / minimum - 180),
        lower: (180, 180 / lower - 180),
        0.5: (upper * 0.5, upper * 0.5),
        1 - lower: (upper * (1 - lower), upper * lower),
        1 - minimum: (180 / minimum - 180, 180),
        1 - minimum * 0.5: (upper * 2, 0),
    }
    for duty, (on, off) in cases.items():
        pwm = make_pwm()
        pwm.update_cycles_per_hour(temperature)
        assert pwm.cycles_per_hour == n
        await step(pwm, clock, duty)
        got = pwm.state.duty_cycle
        assert got is not None
        # Branch results are truncated to whole seconds; allow for float noise at the boundaries
        assert abs(got[0] - on) <= 1 and abs(got[1] - off) <= 1, (n, duty, got, (on, off))


@pytest.mark.parametrize(("n", "floor"), [(2, 0.05), (3, 0.075), (4, 0.10)])
async def test_no_heat_floor_in_pwm(clock, n, floor):
    temperature = {2: 20, 3: 6, 4: -5}[n]
    pwm = make_pwm()
    pwm.update_cycles_per_hour(temperature)
    await step(pwm, clock, floor * 0.9)
    assert pwm.state.duty_cycle == (0, int(7200 / n))

    pwm = make_pwm()
    pwm.update_cycles_per_hour(temperature)
    await step(pwm, clock, floor * 1.1)
    assert pwm.state.duty_cycle[0] == 180  # type: ignore[index]


# Minimum duty: no pulse starts below it, whatever the flame state

@pytest.mark.parametrize("flame_active", [False, True])
@pytest.mark.parametrize("hot_water", [False, True])
async def test_below_minimum_duty_never_starts_a_pulse(clock, flame_active, hot_water):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)  # N=2, minimum duty 0.05
    for _ in range(3):
        await step(pwm, clock, 0.04, 4000, flame_active=flame_active, hot_water=hot_water)
        assert pwm.status == PWMStatus.OFF
        assert pwm.state.duty_cycle == (0, 3600)
        assert pwm.duty_cycle_branch == "low-duty"


@pytest.mark.parametrize("flame_active", [False, True])
async def test_just_above_minimum_duty_starts_a_pulse(clock, flame_active):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)
    await step(pwm, clock, 0.06, flame_active=flame_active)
    assert pwm.status == PWMStatus.ON
    assert pwm.duty_cycle_branch == "low"


async def test_tap_closed_flame_on_zero_duty_starts_no_pulse(clock):
    pwm = make_pwm()
    await step(pwm, clock, 0.0, flame_active=True, hot_water=False)
    assert pwm.status == PWMStatus.OFF
    assert pwm.duty_cycle_branch == "low-duty"

    await step(pwm, clock, 0.0, 7200, flame_active=True, hot_water=False)
    assert pwm.status == PWMStatus.OFF


@pytest.mark.parametrize("flame_active", [False, True])
async def test_running_pulse_held_to_180s_when_duty_falls_below_minimum(clock, flame_active):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)

    await step(pwm, clock, 0.0, 10, flame_active=flame_active)
    assert pwm.status == PWMStatus.ON
    assert pwm.duty_cycle_branch == "low-duty"
    await step(pwm, clock, 0.0, 169, flame_active=flame_active)  # 179 s
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.0, 1, flame_active=flame_active)  # 180 s
    assert pwm.status == PWMStatus.OFF

    # No new pulse while the duty stays below the minimum
    for _ in range(3):
        await step(pwm, clock, 0.0, 1000, flame_active=flame_active)
        assert pwm.status == PWMStatus.OFF


# No limit on the pulses per hour

async def run_pulses(pwm: PWM, clock: Clock, duty: float, half_cycle: float, steps: int) -> None:
    for _ in range(steps):
        await step(pwm, clock, duty, half_cycle)


async def test_a_fifth_pulse_within_the_hour_starts_at_n4(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: (450, 450) per cycle
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 450, 7)  # t=3150, four pulses started at 0, 900, 1800, 2700
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 0.5, 450)  # t=3600
    assert pwm.status == PWMStatus.ON


async def test_a_third_pulse_within_the_hour_starts_at_n2(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)  # N=2: (900, 900) per cycle
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 900, 3)  # OFF, second pulse at t=1800, OFF at t=2700
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 0.5, 900)  # t=3600
    assert pwm.status == PWMStatus.ON


# A mid duty cycle fills the period

@pytest.mark.parametrize(("temperature", "duty", "on_time"), [
    (20, 0.37, 666), (20, 0.61, 1097), (20, 0.5333, 959),
    (6, 0.37, 444), (6, 0.61, 731), (6, 0.5333, 639),
    (-5, 0.37, 333), (-5, 0.61, 548), (-5, 0.5333, 479),
])
async def test_a_mid_duty_cycle_fills_the_period_exactly(clock, temperature, duty, on_time):
    # The raw times are fractional: the on time is truncated and the off time is the rest of the period
    pwm = make_pwm()
    pwm.update_cycles_per_hour(temperature)
    await step(pwm, clock, duty)
    assert pwm.duty_cycle_branch == "mid"
    on, off = pwm.state.duty_cycle  # type: ignore[misc]
    assert on == on_time
    assert on + off == 3600 / pwm.cycles_per_hour


# Minimum off phase: an off phase after a pulse end lasts at least 180 s

def pwm_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(("pwm ON", "pwm OFF", "pwm IDLE", "pwm hold"))]


async def max_pulse_ended_late_in_its_period(pwm: PWM, clock: Clock) -> None:
    """At N=4: a pulse at 100% (1800 s on, 0 s off) from 0 s, ended at 1500 s by a dip to a duty of 0.85 (1020 s on)."""
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 1.0)
    await step(pwm, clock, 1.0, 1490)
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.85, 10)  # 1500 s
    assert pwm.status == PWMStatus.OFF


async def test_a_pulse_end_at_a_duty_jitter_near_100_percent_is_followed_by_the_minimum_off_phase(clock, caplog):
    pwm = make_pwm()
    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await max_pulse_ended_late_in_its_period(pwm, clock)

        # Back at 100%, whose off time is 0
        await step(pwm, clock, 1.0, 10)  # 10 s after the pulse end
        assert pwm.state.duty_cycle == (1800, 0)
        assert pwm.status == PWMStatus.OFF
        assert pwm.hold is None
        await step(pwm, clock, 1.0, 169)  # 179 s
        assert pwm.status == PWMStatus.OFF
        await step(pwm, clock, 1.0, 1)  # 180 s
        assert pwm.status == PWMStatus.ON
    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm OFF", "pwm ON (4 cycles/h)"]


@pytest.mark.parametrize(("duty", "branch"), [(0.15, "low"), (0.5, "mid"), (0.8, "mid"), (0.85, "high")])
async def test_the_off_time_of_a_low_mid_or_high_duty_is_unchanged(clock, duty, branch):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: (180, 1020), (450, 450), (720, 180), (1020, 180)
    await start_pulse(pwm, clock, duty)
    assert pwm.duty_cycle_branch == branch
    on, off = pwm.state.duty_cycle  # type: ignore[misc]
    assert off >= 180

    await step(pwm, clock, duty, on)
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, duty, off - 1)
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, duty, 1)
    assert pwm.status == PWMStatus.ON


async def test_a_rollover_after_an_earlier_pulse_end_has_no_off_phase(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await start_pulse(pwm, clock, 0.5)
        await step(pwm, clock, 0.5, 450)
        assert pwm.status == PWMStatus.OFF
        await step(pwm, clock, 1.0, 450)  # 900 s: a 100% pulse, 1800 s on, 0 s off
        assert pwm.status == PWMStatus.ON

        await step(pwm, clock, 1.0, 1799)
        await step(pwm, clock, 1.0, 1)  # 2700 s: rollover
        assert pwm.status == PWMStatus.ON
        await step(pwm, clock, 1.0, 10)
        assert pwm.status == PWMStatus.ON
    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm OFF", "pwm ON (4 cycles/h)"]


@pytest.mark.parametrize("duty", [1.0, 0.85])
async def test_a_start_from_idle_after_a_reset_is_not_held_by_the_minimum_off_phase(clock, duty):
    pwm = make_pwm()
    await max_pulse_ended_late_in_its_period(pwm, clock)
    pwm.reset()

    await step(pwm, clock, duty, 10)  # 10 s after the pulse end and the reset

    assert pwm.status == PWMStatus.ON


async def test_an_off_phase_entered_after_a_reset_is_not_held_by_the_minimum_off_phase(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: (450, 450) at 0.5
    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await start_pulse(pwm, clock, 0.5)
        await step(pwm, clock, 0.5, 450)
        assert pwm.status == PWMStatus.OFF
        pwm.reset()

        # A due start held by hot water goes from idle to off
        await step(pwm, clock, 0.5, hot_water=True)
        assert pwm.status == PWMStatus.OFF
        assert pwm.hold == "hot water"
        # Below the minimum duty no start is due, so the hold ends and the off phase goes on
        await step(pwm, clock, 0.05, hot_water=True)
        assert pwm.status == PWMStatus.OFF

        # At 100%, whose off time is 0: the off phase doesn't follow a pulse end
        await step(pwm, clock, 1.0, 10)
        assert pwm.status == PWMStatus.ON
    assert pwm_lines(caplog) == [
        "pwm ON (4 cycles/h)", "pwm OFF", "pwm IDLE (reset)", "pwm hold (hot water)", "pwm OFF", "pwm hold ended", "pwm ON (4 cycles/h)",
    ]


async def test_an_off_phase_before_the_first_pulse_is_not_held_by_the_minimum_off_phase(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    # Below the minimum duty the PWM goes from idle to off without a pulse
    await step(pwm, clock, 0.05)
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 1.0, 10)

    assert pwm.status == PWMStatus.ON


# Characterization with hard-coded values

@pytest.mark.parametrize(("n", "expected"), [
    (4, (900, 1800, 0.2, 0.8, 0.1, 0.9)),
    (2, (1800, 3600, 0.1, 0.9, 0.05, 0.95)),
])
def test_thresholds_hard_coded(n, expected):
    thresholds = CycleThresholds.from_cycles_per_hour(n)
    got = (thresholds.on_time_upper, thresholds.on_time_max, thresholds.duty_cycle_lower, thresholds.duty_cycle_upper, thresholds.duty_cycle_min, thresholds.duty_cycle_max)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize(("temperature", "duty", "flame_active", "expected"), [
    (-5, 0.05, False, (0, 1800)),
    (-5, 0.05, True, (0, 1800)),
    (-5, 0.15, False, (180, 1020)),
    (-5, 0.5, False, (450, 450)),
    (-5, 0.85, False, (1020, 180)),
    (-5, 0.95, False, (1800, 0)),
    (20, 0.03, False, (0, 3600)),
    (20, 0.07, False, (180, 2391)),
    (20, 0.5, False, (900, 900)),
])
async def test_branches_hard_coded(clock, temperature, duty, flame_active, expected):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(temperature)
    await step(pwm, clock, duty, flame_active=flame_active)
    got = pwm.state.duty_cycle
    assert got is not None
    assert abs(got[0] - expected[0]) <= 1 and abs(got[1] - expected[1]) <= 1, got


# Gaps from the review

async def test_idle_after_missing_values_uses_current_n(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock)
    pwm.update_cycles_per_hour(20)
    assert pwm.cycles_per_hour == 4

    await step(pwm, clock, 0.5, 5, flow=None)
    assert pwm.status == PWMStatus.IDLE
    assert pwm.cycles_per_hour == 2

    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await step(pwm, clock, 0.5, 5)
    assert pwm.status == PWMStatus.ON
    assert "pwm ON (2 cycles/h)" in caplog.text
