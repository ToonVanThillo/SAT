"""PWM cycles per hour from the outside temperature, latching at pulse start and the 180 s minimum pulse."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.const import BoilerStatus, PWMStatus
from custom_components.sat.pwm import PWM, CycleConfig, CycleThresholds

DEFAULTS = {"maximum_time": 900, "cold_temperature": 0, "cold_cycles_per_hour": 4, "mild_temperature": 12, "mild_cycles_per_hour": 2}

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


def make_pwm(automatic=True, **overrides) -> PWM:
    CURVE.value = 30.0
    return PWM(config(**overrides), CURVE, False, automatic)  # type: ignore[arg-type]


def boiler(status=BoilerStatus.AT_SETPOINT, flame_active=False, hot_water=False, flow=FLOW):
    return SimpleNamespace(status=status, flame_active=flame_active, hot_water_active=hot_water, flow_temperature=flow)


def flame(active=False):
    return SimpleNamespace(is_active=active, is_inactive=not active, latest_on_time_seconds=None, average_on_time_seconds=100, seconds_since_hot_water_burn=None)


def setpoint_for(duty: float) -> float:
    """Requested setpoint that gives the duty cycle for a boiler temperature of FLOW."""
    return BASE + duty * (FLOW - BASE)


async def step(pwm: PWM, clock: Clock, duty: float, seconds: float = 0, **boiler_args):
    flame_active = boiler_args.get("flame_active", False)
    flame_state = flame(flame_active)
    boiler_state = boiler(**boiler_args)
    clock.tick(seconds)
    await pwm.update(boiler_state, flame_state, setpoint_for(duty))  # type: ignore[arg-type]


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
    assert "pwm ON (pulse 1 this hour, 2 cycles/h)" in caplog.text


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
    await start_pulse(pwm, clock, 0.5)  # N=4: on 900
    await step(pwm, clock, 0.5, 200)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 700)
    assert pwm.state.status == PWMStatus.OFF


async def test_minimum_pulse_on_fixed_duty_path(clock):
    pwm = make_pwm(automatic=False)
    await start_pulse(pwm, clock, 0.5)  # on 450
    await step(pwm, clock, 0.0, 50)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.0, 129)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.0, 1)
    assert pwm.state.status == PWMStatus.OFF


async def test_minimum_pulse_fixed_duty_normal_end(clock):
    pwm = make_pwm(automatic=False)
    await start_pulse(pwm, clock, 0.5)  # on 450
    await step(pwm, clock, 0.5, 449)
    assert pwm.state.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 1)
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


async def test_setpoint_stabilization_disables_at_once(clock):
    pwm = make_pwm()
    pwm.enable()
    await start_pulse(pwm, clock, 0.5)
    # Active flame sets an adjusted setpoint (flow - 3); a higher request while cooling down disables PWM
    active = flame(True)
    active.latest_on_time_seconds = 60
    await pwm.update(boiler(), active, setpoint_for(0.5))  # type: ignore[arg-type]
    clock.tick(5)
    await pwm.update(boiler(status=BoilerStatus.COOLING_DOWN, flame_active=True), active, 60.0)  # type: ignore[arg-type]
    assert pwm.status == PWMStatus.IDLE
    assert not pwm.enabled


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


# Hourly cap

async def run_pulses(pwm: PWM, clock: Clock, duty: float, half_cycle: float, steps: int) -> None:
    for _ in range(steps):
        await step(pwm, clock, duty, half_cycle)


async def test_cap_blocks_fifth_pulse_at_n4(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: (450, 450) per cycle
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 450, 7)  # t=3150, four pulses started at 0, 900, 1800, 2700
    assert pwm.status == PWMStatus.OFF

    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await step(pwm, clock, 0.5, 450)  # t=3600, still inside the rolling hour
        assert pwm.status == PWMStatus.OFF
        assert "pwm cycles cap reached (4/h)" in caplog.text
        await step(pwm, clock, 0.5, 1)  # new rolling hour
    assert pwm.status == PWMStatus.ON


async def test_cap_blocks_third_pulse_at_n2(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)  # N=2: (900, 900) per cycle
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 900, 1)  # OFF
    await run_pulses(pwm, clock, 0.5, 900, 1)  # second pulse, t=1800
    assert pwm.status == PWMStatus.ON
    await run_pulses(pwm, clock, 0.5, 900, 1)  # OFF at t=2700
    assert pwm.status == PWMStatus.OFF

    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await step(pwm, clock, 0.5, 900)  # t=3600, still inside the rolling hour
    assert pwm.status == PWMStatus.OFF
    assert "pwm cycles cap reached (2/h)" in caplog.text


async def test_cap_allows_third_pulse_at_n4(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 450, 4)  # OFF, ON (2), OFF, ON (3)
    assert pwm.status == PWMStatus.ON


async def test_cap_uses_latched_n_not_the_new_one(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(20)  # N=2
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 900, 3)  # OFF, ON (2), OFF at t=2700
    pwm.update_cycles_per_hour(-5)  # would allow N=4, but the latched N=2 governs this off phase
    assert pwm.cycles_per_hour == 2

    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await step(pwm, clock, 0.5, 900)
    assert pwm.status == PWMStatus.OFF
    assert "pwm cycles cap reached (2/h)" in caplog.text


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
    assert ", 2 cycles/h)" in caplog.text


async def test_cap_uses_current_n_after_the_latch_expired(clock, caplog):
    # N=4 up to 0 degrees, N=6 from 12 degrees; fixed duty path with a 400 s cycle gives (200, 200)
    pwm = make_pwm(automatic=False, maximum_time=400, mild_cycles_per_hour=6)
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock)
    await run_pulses(pwm, clock, 0.5, 200, 7)  # starts at 0, 400, 800, 1200; OFF from 1400
    assert pwm.status == PWMStatus.OFF
    pwm.update_cycles_per_hour(20)
    assert pwm.cycles_per_hour == 4

    with caplog.at_level(logging.INFO, logger="custom_components.sat.timeline"):
        await step(pwm, clock, 0.5, 200)  # t=1600, latched N=4 caps
        assert "pwm cycles cap reached (4/h)" in caplog.text
        assert pwm.status == PWMStatus.OFF
        await step(pwm, clock, 0.5, 1600)  # off phase of exactly 2 * 3600 / 4 s: the latch still holds
        assert pwm.status == PWMStatus.OFF
        assert pwm.cycles_per_hour == 4
        await step(pwm, clock, 0.5, 1)  # latch expired, the current N=6 applies
    assert pwm.status == PWMStatus.ON
    assert ", 6 cycles/h)" in caplog.text
