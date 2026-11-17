"""PWM 100% periods (off-time 0) roll over into the next period without an OFF phase; a rollover is not a pulse start."""
import logging
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from custom_components.sat.const import PWMStatus
from tests.conftest import TIMELINE
from tests.test_pwm_cycles import Clock, make_pwm, start_pulse, step
from tests.test_pwm_hot_water import make_pwm as make_hot_water_pwm
from tests.test_pwm_hot_water import update as hot_water_update

PWM_LINES = ("pwm ON", "pwm OFF", "pwm hold", "pwm mode")


@pytest.fixture
def clock() -> Iterator[Clock]:
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


@pytest.fixture(autouse=True)
def timeline_info(caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)


def pwm_lines(caplog) -> list[str]:
    return [message for record in caplog.records if record.name == TIMELINE and (message := record.getMessage()).startswith(PWM_LINES)]


# (a) A 100% period stays ON across period ends

async def test_a_100_percent_burn_stays_on_across_period_ends(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: a duty of 1.0 gives 1800 s on, 0 s off
    await start_pulse(pwm, clock, 1.0)
    assert pwm.state.duty_cycle == (1800, 0)

    # Four period ends, at 1800, 3600, 5400 and 7200 s
    for _ in range(24):
        await step(pwm, clock, 1.0, 300)
        assert pwm.status == PWMStatus.ON
        assert pwm.state.duty_cycle == (1800, 0)

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)"]


# (b) After a rollover, the off phase runs from the pulse end

async def test_after_a_rollover_the_off_phase_runs_from_the_pulse_end(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    await start_pulse(pwm, clock, 1.0)
    await step(pwm, clock, 1.0, 1800)  # rollover at 1800 s
    assert pwm.status == PWMStatus.ON

    # Below the minimum duty the pulse ends at once; a duty of 0.85 has a 180 s off time
    await step(pwm, clock, 0.05, 10)  # 1810 s
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.85, 179)  # 1989 s: 189 s after the rollover
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.85, 1)  # 1990 s: 180 s after the pulse end
    assert pwm.status == PWMStatus.ON

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm OFF", "pwm ON (4 cycles/h)"]


# (c) A hot water hold doesn't stop a rollover

@pytest.mark.parametrize(("hot_water", "since"), [(True, None), (False, 0)])
async def test_a_hot_water_hold_does_not_stop_a_rollover(clock, caplog, hot_water, since):
    pwm = make_hot_water_pwm(hot_water_delay=300)  # N=2: a duty of 1.0 gives 3600 s on, 0 s off
    await hot_water_update(pwm, clock, duty=1.0)
    assert pwm.status == PWMStatus.ON
    assert pwm.state.duty_cycle == (3600, 0)

    # Hot water (or its delay) from just before the period end, at 3600 s, until after it
    await hot_water_update(pwm, clock, 3590, duty=1.0, hot_water=hot_water, since=since)
    assert pwm.rolled_over is False
    for index in range(3):
        await hot_water_update(pwm, clock, 10, duty=1.0, hot_water=hot_water, since=since)
        assert pwm.status == PWMStatus.ON
        assert pwm.hold is None
        # Rolled over at 3600 s, the first of these updates
        assert pwm.rolled_over is (index == 0)

    assert pwm_lines(caplog) == ["pwm ON (2 cycles/h)"]


# (d) A duty drop below 100% ends the pulse at period start + new on-time, not before the minimum pulse

async def test_a_duty_drop_in_a_later_period_ends_the_pulse_at_the_new_on_time_from_the_period_start(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: a duty of 1.0 gives 1800 s on, 0 s off; 0.5 gives 450 s on
    await start_pulse(pwm, clock, 1.0)
    await step(pwm, clock, 1.0, 1800)  # rollover: the second period starts at 1800 s
    assert pwm.status == PWMStatus.ON

    await step(pwm, clock, 0.5, 300)  # 2100 s
    assert pwm.state.duty_cycle == (450, 450)
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 149)  # 2249 s: 449 s into the period
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 1)  # 2250 s: 450 s into the period
    assert pwm.status == PWMStatus.OFF

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm OFF"]


async def test_a_duty_drop_shortly_after_the_pulse_start_holds_the_minimum_pulse(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: below a duty of 0.1 there is no on-time
    await start_pulse(pwm, clock, 1.0)

    await step(pwm, clock, 0.05, 10)
    assert pwm.state.duty_cycle == (0, 1800)
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.05, 169)  # 179 s
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.05, 1)  # 180 s
    assert pwm.status == PWMStatus.OFF

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm ON held for the minimum pulse (180s)", "pwm OFF"]


# (e) No rollover when the duty is no longer 100% at the period end

async def test_no_rollover_when_the_duty_is_below_100_percent_at_the_period_end(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    await start_pulse(pwm, clock, 1.0)
    for _ in range(5):
        await step(pwm, clock, 1.0, 300)  # 1500 s
    assert pwm.status == PWMStatus.ON

    await step(pwm, clock, 0.5, 300)  # 1800 s, the period end, with 450 s on and 450 s off
    assert pwm.status == PWMStatus.OFF

    # The off-phase runs from the pulse end, then a new pulse starts
    await step(pwm, clock, 0.5, 449)
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.5, 1)
    assert pwm.status == PWMStatus.ON

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm OFF", "pwm ON (4 cycles/h)"]


# (f) N is latched again at a rollover

async def test_a_rollover_latches_the_current_n(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: a duty of 1.0 gives 1800 s on
    await start_pulse(pwm, clock, 1.0)

    pwm.update_cycles_per_hour(20)  # N=2 from now, but N=4 is latched for this period
    await step(pwm, clock, 1.0, 1799)
    assert pwm.cycles_per_hour == 4
    assert pwm.state.duty_cycle == (1800, 0)

    await step(pwm, clock, 1.0, 1)  # rollover at 1800 s
    assert pwm.status == PWMStatus.ON
    assert pwm.cycles_per_hour == 2

    await step(pwm, clock, 1.0, 10)
    assert pwm.state.duty_cycle == (3600, 0)

    # The new N is latched for the new period
    pwm.update_cycles_per_hour(-5)
    await step(pwm, clock, 1.0, 1800)  # 3610 s: no period end with N=2
    assert pwm.cycles_per_hour == 2
    assert pwm.state.duty_cycle == (3600, 0)
    assert pwm.status == PWMStatus.ON


# (g) The boiler temperature is sampled again in the first 180 s after a rollover

async def test_a_rollover_resamples_the_boiler_temperature(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    await start_pulse(pwm, clock, 1.0)  # requested setpoint 50 at a flow of 50
    await step(pwm, clock, 1.0, 1800)  # rollover at 1800 s
    assert pwm.status == PWMStatus.ON

    # The flow jumps from 50 to 80 within 180 s of the period start: the duty formula sees 0.2 * 80 + 0.8 * 50 = 56,
    # so a duty of (50 - 20) / (56 - 20); without the resampling it would see 80, a duty of (50 - 20) / (80 - 20)
    await step(pwm, clock, 1.0, 30, flow=80.0)
    await step(pwm, clock, 1.0, 30, flow=80.0)
    assert pwm.last_duty_cycle_percentage == pytest.approx(round(100 * 30 / 36, 2))
    assert pwm.status == PWMStatus.ON


# (h) Gaps: the resampling stops after 180 s, and a rollover doesn't restart the minimum pulse

async def test_the_boiler_temperature_is_not_smoothed_after_180_s_into_a_rolled_over_period(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    await start_pulse(pwm, clock, 1.0)
    await step(pwm, clock, 1.0, 1800)  # rollover at 1800 s

    # Past 180 s into the new period the duty formula sees the flow itself, from the next update: (50 - 20) / (80 - 20)
    await step(pwm, clock, 1.0, 200, flow=80.0)
    await step(pwm, clock, 1.0, 10, flow=80.0)
    assert pwm.last_duty_cycle_percentage == pytest.approx(50.0)


async def test_a_rollover_does_not_restart_the_minimum_pulse(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: below a duty of 0.1 there is no on-time
    await start_pulse(pwm, clock, 1.0)
    await step(pwm, clock, 1.0, 1800)  # rollover at 1800 s
    assert pwm.status == PWMStatus.ON

    # The pulse started 1810 s ago, so no minimum pulse is left to hold
    await step(pwm, clock, 0.05, 10)
    assert pwm.status == PWMStatus.OFF


async def test_the_rollover_boundary_is_the_on_time_since_the_period_start(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: 1800 s on, 0 s off
    await start_pulse(pwm, clock, 1.0)
    await step(pwm, clock, 1.0, 1799)
    await step(pwm, clock, 1.0, 1)  # period end, first rollover at 1800 s
    await step(pwm, clock, 1.0, 1799)  # 3599 s: 1799 s into the second period
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 1)  # 3600 s: the second period ends with a duty of 0.5 (450 s on): no rollover
    assert pwm.status == PWMStatus.OFF
