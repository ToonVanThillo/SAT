"""Timeline lines of the PWM controller: idle edge with its reason and the cycles-per-hour cap edges."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.const import BoilerStatus, PWMStatus
from tests.conftest import TIMELINE
from tests.test_pwm_cycles import (
    CURVE,
    Clock,
    boiler,
    make_pwm,
    run_pulses,
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


def idle_lines(caplog) -> list[str]:
    return lines(caplog, "pwm IDLE")


# Idle edge

async def test_reset_from_on_logs_idle_once(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)

    pwm.reset()
    pwm.reset()

    assert len(idle_lines(caplog)) == 1
    assert idle_lines(caplog)[0].startswith("pwm IDLE (") and idle_lines(caplog)[0].endswith(")")
    assert pwm.status == PWMStatus.IDLE


async def test_disable_from_on_and_from_off_logs_idle_once_each(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    pwm.disable()
    assert len(idle_lines(caplog)) == 1

    await start_pulse(pwm, clock)
    await step(pwm, clock, 0.5, 450)
    assert pwm.status == PWMStatus.OFF
    caplog.clear()
    pwm.disable()
    assert len(idle_lines(caplog)) == 1


async def test_no_idle_line_when_already_idle(clock, caplog):
    pwm = make_pwm()
    pwm.reset()
    pwm.disable()

    assert idle_lines(caplog) == []


async def test_reset_reason_is_in_the_line(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)

    pwm.reset("because")

    assert idle_lines(caplog) == ["pwm IDLE (because)"]


async def test_missing_values_log_idle_once_and_clear_the_shown_duty(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    assert pwm.last_duty_cycle_percentage is not None and pwm.duty_cycle_branch is not None
    duty_cycle = pwm.duty_cycle

    await step(pwm, clock, 0.5, 5, flow=None)
    await step(pwm, clock, 0.5, 5, flow=None)

    assert idle_lines(caplog) == ["pwm IDLE (missing values)"]
    assert (pwm.last_duty_cycle_percentage, pwm.duty_cycle_branch) == (None, None)
    # The climate's duty cycle attribute is unchanged by this
    assert pwm.duty_cycle == duty_cycle


async def test_missing_heating_curve_value_logs_idle(clock, caplog):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    CURVE.value = None

    await step(pwm, clock, 0.5, 5)

    assert idle_lines(caplog) == ["pwm IDLE (missing values)"]
    assert pwm.status == PWMStatus.IDLE


async def test_setpoint_stabilization_disables_with_idle_line(clock, caplog):
    pwm = make_pwm()
    pwm.enable()
    await start_pulse(pwm, clock)
    assert pwm.enabled
    # A burning flame lets the setpoint adjuster follow the boiler; then a cooling down boiler with a higher requested setpoint disables
    burning = SimpleNamespace(is_active=True, is_inactive=False, latest_on_time_seconds=30, average_on_time_seconds=100)
    clock.tick(5)
    await pwm.update(boiler(flame_active=True), burning, setpoint_for(0.5))  # type: ignore[arg-type]
    assert idle_lines(caplog) == []

    clock.tick(5)
    await pwm.update(boiler(status=BoilerStatus.COOLING_DOWN, flame_active=True), burning, setpoint_for(1.5))  # type: ignore[arg-type]

    assert pwm.enabled is False
    assert pwm.status == PWMStatus.IDLE
    assert len(idle_lines(caplog)) == 1
    assert "stabili" in idle_lines(caplog)[0]


# Cap edges

async def capped_pwm(clock: Clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: four pulses started at 0, 900, 1800, 2700
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 450, 7)
    return pwm


async def test_cap_reached_is_logged_once_and_lifted_once(clock, caplog):
    pwm = await capped_pwm(clock)
    caplog.clear()

    await step(pwm, clock, 0.5, 450)  # t=3600, still inside the rolling hour
    await step(pwm, clock, 0.5, 0)
    await step(pwm, clock, 0.5, 0)
    assert lines(caplog, "pwm cycles cap") == ["pwm cycles cap reached (4/h)"]
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 0.5, 1)  # new rolling hour
    assert lines(caplog, "pwm cycles cap") == ["pwm cycles cap reached (4/h)", "pwm cycles cap lifted"]
    assert pwm.status == PWMStatus.ON
    # The next pulse follows the lift
    assert [m for m in lines(caplog) if m.startswith(("pwm cycles", "pwm ON"))][-2:] == ["pwm cycles cap lifted", "pwm ON (pulse 1 this hour, 4 cycles/h)"]

    caplog.clear()
    await step(pwm, clock, 0.5, 450)
    assert lines(caplog, "pwm cycles cap") == []


async def test_no_cap_lines_when_never_capped(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await start_pulse(pwm, clock, 0.5)
    await run_pulses(pwm, clock, 0.5, 450, 5)

    assert lines(caplog, "pwm cycles cap") == []


async def test_cap_lifted_when_the_pwm_is_reset(clock, caplog):
    pwm = await capped_pwm(clock)
    await step(pwm, clock, 0.5, 450)
    assert lines(caplog, "pwm cycles cap reached") == ["pwm cycles cap reached (4/h)"]

    pwm.reset()
    pwm.reset()

    assert lines(caplog, "pwm cycles cap lifted") == ["pwm cycles cap lifted"]


async def test_old_cap_line_is_gone(clock, caplog):
    caplog.set_level(logging.DEBUG)
    pwm = await capped_pwm(clock)
    await step(pwm, clock, 0.5, 450)

    assert "Reached max cycles per hour" not in caplog.text


# Missing values warning

def missing_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.getMessage() == "PWM turned off due missing values."]


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


@pytest.mark.parametrize("stop", ["reset", "disable"])
async def test_a_reset_ends_the_missing_values_episode(clock, caplog, stop):
    pwm = make_pwm()
    await start_pulse(pwm, clock)
    await step(pwm, clock, 0.5, 5, flow=None)
    assert len(missing_warnings(caplog)) == 1

    # PWM stops being enabled while the values are still missing, then runs again with them still missing
    getattr(pwm, stop)("not enabled")
    pwm.enable()
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
