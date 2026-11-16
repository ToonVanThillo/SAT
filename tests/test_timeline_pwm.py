"""Timeline lines of the PWM controller: the reset line with its reason, the missing values warning and the minimum pulse."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.const import BoilerStatus, PWMStatus
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
