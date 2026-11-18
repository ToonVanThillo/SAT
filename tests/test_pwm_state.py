"""PWM state seen from outside: what a reset does, the missing flow warning, the minimum pulse flag, the mode and the rollover flag."""
import logging
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.const import BoilerStatus, ModulationMode, PWMStatus
from tests.test_pwm_cycles import (
    Clock,
    boiler,
    make_pwm,
    setpoint_for,
    start_pulse,
    step,
)


@pytest.fixture
def clock():
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


# Reset

async def test_a_reset_ends_a_running_pulse_at_once(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)

    pwm.reset()
    pwm.reset()

    assert pwm.status == PWMStatus.OFF


async def test_a_reset_in_an_off_phase_leaves_it_off(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    await step(pwm, clock, 0.5, 450)
    assert pwm.status == PWMStatus.OFF

    pwm.reset()
    pwm.reset()

    assert pwm.status == PWMStatus.OFF


def test_a_reset_before_any_pulse_leaves_it_off(clock):
    pwm = make_pwm()

    pwm.reset()
    pwm.reset()

    assert pwm.status == PWMStatus.OFF
    assert pwm.start_held is None
    assert not pwm.minimum_pulse_held


async def test_a_reset_from_unrestricted_gives_minimum_mode(clock):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    assert pwm.mode == ModulationMode.UNRESTRICTED

    pwm.reset()

    assert pwm.mode == ModulationMode.MINIMUM


async def test_missing_values_keep_the_shown_duty(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    shown = (pwm.duty_cycle, pwm.last_duty_cycle_percentage, pwm.duty_cycle_branch)
    assert None not in shown

    await step(pwm, clock, 0.5, 5, flow=None)
    await step(pwm, clock, 0.5, 5, flow=None)

    assert pwm.status == PWMStatus.ON
    assert (pwm.duty_cycle, pwm.last_duty_cycle_percentage, pwm.duty_cycle_branch) == shown


async def test_a_higher_request_while_cooling_down_keeps_the_pulse_on(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    # A burning flame lets the setpoint adjuster follow the boiler; a cooling down boiler with a higher requested setpoint no longer disables
    burning = SimpleNamespace(is_active=True, is_inactive=False, latest_on_time_seconds=30, average_on_time_seconds=100, seconds_since_hot_water_burn=None)
    clock.tick(5)
    await pwm.update(boiler(flame_active=True), burning, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]
    assert pwm.status == PWMStatus.ON

    clock.tick(5)
    await pwm.update(boiler(status=BoilerStatus.COOLING_DOWN, flame_active=True), burning, setpoint_for(1.5), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]

    assert pwm.status == PWMStatus.ON


# Missing values warning

def missing_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.getMessage() == "Flow temperature missing; PWM keeps its duty."]


async def test_missing_values_warning_is_logged_once_per_episode(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)

    for _ in range(4):
        await step(pwm, clock, 0.5, 5, flow=None)
    assert len(missing_warnings(caplog)) == 1

    # Values are back: a new missing episode logs it once more
    await step(pwm, clock, 0.5, 5)
    await step(pwm, clock, 0.5, 5)
    assert len(missing_warnings(caplog)) == 1
    for _ in range(3):
        await step(pwm, clock, 0.5, 5, flow=None)
    assert len(missing_warnings(caplog)) == 2


async def test_a_reset_ends_the_missing_values_episode(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    await step(pwm, clock, 0.5, 5, flow=None)
    assert len(missing_warnings(caplog)) == 1

    # PWM is reset while the values are still missing, then runs again with them still missing
    pwm.reset()
    for _ in range(3):
        await step(pwm, clock, 0.5, 5, flow=None)

    assert len(missing_warnings(caplog)) == 2


# Minimum pulse

async def test_the_minimum_pulse_flag_is_set_while_a_pulse_is_held_beyond_its_duty(clock):
    pwm = make_pwm()
    for _ in range(2):
        await start_pulse(pwm, clock, 0.5)
        assert not pwm.minimum_pulse_held

        # The duty cycle alone would now end the pulse: no on time at all
        for _ in range(3):
            await step(pwm, clock, 0.0, 10)
            assert pwm.status == PWMStatus.ON
            assert pwm.minimum_pulse_held

        # After the minimum pulse it ends, and the flag is gone
        await step(pwm, clock, 0.0, 180)
        assert pwm.status == PWMStatus.OFF
        assert not pwm.minimum_pulse_held

        # Let the off phase pass so that the next pulse starts
        await step(pwm, clock, 0.5, 450)


async def test_a_reset_clears_the_minimum_pulse_flag(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.0, 10)
    assert pwm.minimum_pulse_held

    pwm.reset()

    assert not pwm.minimum_pulse_held


async def test_no_minimum_pulse_flag_when_the_duty_cycle_keeps_the_pulse_on(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)

    for _ in range(5):
        await step(pwm, clock, 0.5, 10)
        assert not pwm.minimum_pulse_held

    assert pwm.status == PWMStatus.ON


# Mode

@pytest.mark.parametrize("allow", [True, False])
async def test_the_mode_changes_only_when_unrestricted_modulation_is_allowed(clock, allow):
    pwm = make_pwm()
    shown = [pwm.mode]
    # A scripted run: (duty, seconds, minimum setpoint) mixing both sides of the minimum, with resets in between
    script = [(0.5, 0, 10.0), (0.5, 5, 10.0), (0.5, 5, 40.0), (0.5, 5, 10.0), (0.5, 5, 40.0), (0.2, 300, 10.0), (0.9, 700, 10.0), (0.5, 5, 10.0)]
    resets = {3, 6}
    for index, (duty, seconds, minimum) in enumerate(script):
        if index in resets:
            pwm.reset()
            shown.append(pwm.mode)
        await step(pwm, clock, duty, seconds, minimum_setpoint=minimum, allow_unrestricted=allow)
        shown.append(pwm.mode)

    transitions = [mode for previous, mode in pairwise(shown) if mode != previous]
    if allow:
        assert transitions
    else:
        assert transitions == []


async def test_mode_flips_during_a_pulse_keep_the_pulse_on_and_never_roll_over(clock):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    assert pwm.status == PWMStatus.ON

    for index in range(6):
        unrestricted = index % 2 == 1  # MINIMUM, UNRESTRICTED, ...
        await step(pwm, clock, 0.5, 10, allow_unrestricted=unrestricted)
        assert pwm.status == PWMStatus.ON
        assert pwm.rolled_over is False
        assert pwm.mode == (ModulationMode.UNRESTRICTED if unrestricted else ModulationMode.MINIMUM)


# The rollover flag

@pytest.mark.parametrize("duty, allow_unrestricted", [(1.0, False), (0.5, True)], ids=["max", "unrestricted"])
async def test_rolled_over_is_set_only_by_the_update_that_rolls_a_100_percent_period_over(clock, duty, allow_unrestricted):
    pwm = make_pwm()  # N=4: a 100% period lasts 1800 s
    assert pwm.rolled_over is False

    await step(pwm, clock, duty, allow_unrestricted=allow_unrestricted)
    assert pwm.duty_cycle == (1800, 0)
    assert pwm.rolled_over is False
    await step(pwm, clock, duty, 1799, allow_unrestricted=allow_unrestricted)
    assert pwm.rolled_over is False
    await step(pwm, clock, duty, 1, allow_unrestricted=allow_unrestricted)
    assert pwm.rolled_over is True
    await step(pwm, clock, duty, 1, allow_unrestricted=allow_unrestricted)
    assert pwm.rolled_over is False

    # Rolled over again at 3600 s, then cleared by a reset
    await step(pwm, clock, duty, 1799, allow_unrestricted=allow_unrestricted)
    assert pwm.rolled_over is True
    pwm.reset()
    assert pwm.rolled_over is False

    assert pwm.status == PWMStatus.OFF
