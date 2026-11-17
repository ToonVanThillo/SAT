"""Timeline lines of the PWM controller: the reset line with its reason, the mode lines, the missing values warning and the minimum pulse."""
import logging
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.const import BoilerStatus, ModulationMode, PWMStatus
from tests.conftest import TIMELINE
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


@pytest.fixture(autouse=True)
def timeline_info(caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)


def lines(caplog, prefix="") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.getMessage().startswith(prefix)]


def reset_lines(caplog) -> list[str]:
    return lines(caplog, "pwm OFF (")


# Reset line

def test_init_logs_nothing(clock, caplog):
    make_pwm()

    assert lines(caplog) == []


async def test_every_reset_from_on_logs_a_line_with_its_reason(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)

    pwm.reset("because")
    pwm.reset("because")

    assert reset_lines(caplog) == ["pwm OFF (because)", "pwm OFF (because)"]
    assert pwm.status == PWMStatus.OFF


async def test_every_reset_from_off_logs_a_line(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    await step(pwm, clock, 0.5, 450)
    assert pwm.status == PWMStatus.OFF
    caplog.clear()

    pwm.reset()
    pwm.reset()

    assert lines(caplog, "pwm") == ["pwm OFF (reset)", "pwm OFF (reset)"]
    assert pwm.status == PWMStatus.OFF


def test_every_reset_before_any_pulse_logs_a_line(clock, caplog):
    pwm = make_pwm()

    pwm.reset("valves closed")
    pwm.reset("valves closed")

    assert lines(caplog) == ["pwm OFF (valves closed)", "pwm OFF (valves closed)"]


async def test_missing_values_log_no_pwm_line_and_keep_the_shown_duty(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    shown = (pwm.duty_cycle, pwm.last_duty_cycle_percentage, pwm.duty_cycle_branch)
    assert None not in shown
    caplog.clear()

    await step(pwm, clock, 0.5, 5, flow=None)
    await step(pwm, clock, 0.5, 5, flow=None)

    assert lines(caplog, "pwm") == []
    assert pwm.status == PWMStatus.ON
    assert (pwm.duty_cycle, pwm.last_duty_cycle_percentage, pwm.duty_cycle_branch) == shown


async def test_a_higher_request_while_cooling_down_logs_no_pwm_off_line(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    # A burning flame lets the setpoint adjuster follow the boiler; a cooling down boiler with a higher requested setpoint no longer disables
    burning = SimpleNamespace(is_active=True, is_inactive=False, latest_on_time_seconds=30, average_on_time_seconds=100, seconds_since_hot_water_burn=None)
    clock.tick(5)
    await pwm.update(boiler(flame_active=True), burning, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]
    assert lines(caplog, "pwm OFF") == []

    clock.tick(5)
    await pwm.update(boiler(status=BoilerStatus.COOLING_DOWN, flame_active=True), burning, setpoint_for(1.5), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]

    assert pwm.status == PWMStatus.ON
    assert lines(caplog, "pwm OFF") == []


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
    pwm.reset("valves closed")
    for _ in range(3):
        await step(pwm, clock, 0.5, 5, flow=None)

    assert len(missing_warnings(caplog)) == 2


# Minimum pulse

HELD = "pwm ON held for the minimum pulse (180s)"


async def test_minimum_pulse_hold_is_logged_once_per_pulse(clock, caplog):
    pwm = make_pwm()
    for _ in range(2):
        caplog.clear()
        await start_pulse(pwm, clock, 0.5)

        # The duty cycle alone would now end the pulse: no on time at all
        for _ in range(3):
            await step(pwm, clock, 0.0, 10)
            assert pwm.status == PWMStatus.ON
        assert lines(caplog, "pwm ON held") == [HELD]

        # After the minimum pulse it ends
        await step(pwm, clock, 0.0, 180)
        assert pwm.status == PWMStatus.OFF
        assert lines(caplog, "pwm ON held") == [HELD]

        # Let the off phase pass so that the next pulse starts
        await step(pwm, clock, 0.5, 450)


async def test_no_minimum_pulse_line_when_the_duty_cycle_keeps_the_pulse_on(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)

    for _ in range(5):
        await step(pwm, clock, 0.5, 10)

    assert pwm.status == PWMStatus.ON
    assert lines(caplog, "pwm ON held") == []


# Mode lines

@pytest.mark.parametrize("allow", [True, False])
async def test_the_number_of_mode_lines_equals_the_number_of_mode_transitions(clock, caplog, allow):
    pwm = make_pwm()
    shown = [pwm.mode]
    # A scripted run: (duty, seconds, minimum setpoint) mixing both sides of the minimum, with resets in between
    script = [(0.5, 0, 10.0), (0.5, 5, 10.0), (0.5, 5, 40.0), (0.5, 5, 10.0), (0.5, 5, 40.0), (0.2, 300, 10.0), (0.9, 700, 10.0), (0.5, 5, 10.0)]
    resets = {3, 6}
    for index, (duty, seconds, minimum) in enumerate(script):
        if index in resets:
            pwm.reset("script")
            shown.append(pwm.mode)
        await step(pwm, clock, duty, seconds, minimum_setpoint=minimum, allow_unrestricted=allow)
        shown.append(pwm.mode)

    transitions = [mode for previous, mode in pairwise(shown) if mode != previous]
    assert [line.removeprefix("pwm mode ") for line in lines(caplog, "pwm mode ")] == [mode.name for mode in transitions]
    if allow:
        assert transitions
    else:
        assert transitions == []


async def test_mode_flips_during_a_pulse_log_one_line_each_keep_the_pulse_on_and_never_roll_over(clock, caplog):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    assert pwm.status == PWMStatus.ON
    caplog.clear()

    for index in range(6):
        unrestricted = index % 2 == 1  # MINIMUM, UNRESTRICTED, ...
        await step(pwm, clock, 0.5, 10, allow_unrestricted=unrestricted)
        assert pwm.status == PWMStatus.ON
        assert pwm.rolled_over is False
        assert pwm.mode == (ModulationMode.UNRESTRICTED if unrestricted else ModulationMode.MINIMUM)

    assert lines(caplog, "pwm ") == ["pwm mode MINIMUM", "pwm mode UNRESTRICTED"] * 3


async def test_a_reset_from_unrestricted_logs_the_minimum_mode_after_the_off_line(clock, caplog):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    caplog.clear()

    pwm.reset("because")
    pwm.reset("because")

    assert lines(caplog, "pwm") == ["pwm OFF (because)", "pwm mode MINIMUM", "pwm OFF (because)"]
    assert pwm.mode == ModulationMode.MINIMUM


# The rollover flag

@pytest.mark.parametrize("duty, allow_unrestricted", [(1.0, False), (0.5, True)], ids=["max", "unrestricted"])
async def test_rolled_over_is_set_only_by_the_update_that_rolls_a_100_percent_period_over(clock, caplog, duty, allow_unrestricted):
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
    assert not [line for line in lines(caplog) if "roll" in line]
