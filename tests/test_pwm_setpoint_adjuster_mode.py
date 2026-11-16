"""The dynamic minimum v2 setpoint adjuster inside PWM.update: its adjust branch (flame on, learns towards the flow - 3 stepwise) and its
force branch (flame off, short or missing average on time, no hot water: flow + 10) run only when the update's modulation mode is MINIMUM,
whatever the PWM status, and whether MINIMUM follows from unrestricted modulation not being allowed or from a requested setpoint at or
below the minimum setpoint; in UNRESTRICTED mode the adjusted value is kept."""
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.sat.const import MINIMUM_SETPOINT, ModulationMode, PWMStatus
from custom_components.sat.pwm import PWM
from tests.test_pwm_cycles import Clock, boiler, setpoint_for
from tests.test_pwm_hot_water import flame
from tests.test_pwm_hot_water import make_pwm as make_hot_water_pwm


@pytest.fixture
def clock() -> Iterator[Clock]:
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


def burning() -> SimpleNamespace:
    return flame(active=True, latest=30)


def cold(average: float | None) -> SimpleNamespace:
    return SimpleNamespace(**vars(flame()) | {"average_on_time_seconds": average})


async def update(pwm: PWM, flame_state: SimpleNamespace, unrestricted: bool, minimum_setpoint: float = 10.0, **boiler_args: Any) -> None:
    """One update at a requested setpoint of 35 (setpoint_for(0.5)), with the boiler's flame following `flame_state`."""
    await pwm.update(boiler(flame_active=flame_state.is_active, **boiler_args), flame_state, setpoint_for(0.5), minimum_setpoint=minimum_setpoint, allow_unrestricted=unrestricted)  # type: ignore[arg-type]


async def test_switching_to_minimum_mid_burn_adjusts_in_that_update(clock):
    pwm = make_hot_water_pwm()
    await update(pwm, burning(), True, flow=60.0)
    assert (pwm.mode, pwm.setpoint) == (ModulationMode.UNRESTRICTED, MINIMUM_SETPOINT)

    clock.tick(30)
    await update(pwm, burning(), False, flow=60.0)
    assert (pwm.status, pwm.mode, pwm.setpoint) == (PWMStatus.ON, ModulationMode.MINIMUM, 57.0)


async def test_switching_to_unrestricted_stops_the_adjuster_and_keeps_the_value(clock):
    pwm = make_hot_water_pwm()
    await update(pwm, burning(), False, flow=60.0)
    assert pwm.setpoint == 57.0

    # The flow follows the calculated setpoint now: neither the adjust nor the force branch moves the value
    for flow in (80.0, 90.0):
        clock.tick(30)
        await update(pwm, burning(), True, flow=flow)
        assert (pwm.mode, pwm.setpoint) == (ModulationMode.UNRESTRICTED, 57.0)

    clock.tick(30)
    await update(pwm, cold(30), True, flow=90.0)
    assert (pwm.mode, pwm.setpoint) == (ModulationMode.UNRESTRICTED, 57.0)


async def test_the_adjusted_value_resumes_learning_after_an_unrestricted_stretch(clock):
    pwm = make_hot_water_pwm()
    await update(pwm, burning(), False, flow=60.0)
    clock.tick(30)
    await update(pwm, burning(), True, flow=90.0)
    assert pwm.setpoint == 57.0

    # Back in MINIMUM it steps from the kept value, not from a reset one
    clock.tick(30)
    await update(pwm, burning(), False, flow=50.0)
    assert pwm.setpoint == 56.0


async def test_the_adjust_branch_learns_towards_the_flow_minus_three_stepwise(clock):
    pwm = make_hot_water_pwm()
    await update(pwm, burning(), False, flow=60.0)
    assert pwm.setpoint == 57.0

    seen = []
    for _ in range(4):
        clock.tick(30)
        await update(pwm, burning(), False, flow=50.0)
        seen.append(pwm.setpoint)
    assert seen == [56.0, 55.0, 54.0, 53.0]

    # And upwards
    for expected in (54.0, 55.0):
        clock.tick(30)
        await update(pwm, burning(), False, flow=70.0)
        assert pwm.setpoint == expected


@pytest.mark.parametrize(("average", "forced"), [(None, True), (59.0, True), (60.0, False), (300.0, False)], ids=["missing", "short", "at-limit", "long"])
async def test_the_force_branch_needs_a_short_or_missing_average_on_time(clock, average, forced):
    pwm = make_hot_water_pwm()

    await update(pwm, cold(average), False, flow=60.0)

    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.setpoint == (70.0 if forced else MINIMUM_SETPOINT)


async def test_hot_water_blocks_the_adjust_and_the_force_branch(clock):
    pwm = make_hot_water_pwm()

    await update(pwm, cold(None), False, flow=60.0, hot_water=True)
    assert pwm.setpoint == MINIMUM_SETPOINT

    await update(pwm, burning(), False, flow=60.0, hot_water=True)
    assert pwm.setpoint == MINIMUM_SETPOINT


async def test_a_forced_value_is_kept_when_the_mode_turns_unrestricted_in_the_off_phase(clock):
    pwm = make_hot_water_pwm()
    flame_state = cold(100)

    # A MINIMUM pulse with a long average on time forces nothing, until it ends
    for _ in range(100):
        await update(pwm, flame_state, False, flow=50.0)
        if pwm.status == PWMStatus.OFF:
            break
        clock.tick(60)
    assert pwm.status == PWMStatus.OFF

    await update(pwm, cold(None), False, flow=50.0)
    assert pwm.setpoint == 60.0

    # The off phase continues in UNRESTRICTED mode with a different flow: no force
    clock.tick(10)
    await update(pwm, cold(None), True, flow=70.0)
    assert (pwm.status, pwm.mode, pwm.setpoint) == (PWMStatus.OFF, ModulationMode.UNRESTRICTED, 60.0)


async def test_the_force_branch_runs_in_the_idle_status_in_minimum_mode(clock):
    pwm = make_hot_water_pwm()

    # A duty of 0 starts no pulse: the first update moves PWM from IDLE to OFF, and the force branch runs in that same update
    await pwm.update(boiler(flow=60.0), cold(None), setpoint_for(0.0), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]

    assert (pwm.status, pwm.mode) == (PWMStatus.OFF, ModulationMode.MINIMUM)
    assert pwm.setpoint == 70.0


# The production path on EMS-ESP: unrestricted modulation is allowed (PWM not forced), MINIMUM because the requested 35 is at or below the
# minimum setpoint

@pytest.mark.parametrize(("flame_state", "adjusted"), [(burning(), 57.0), (cold(30), 70.0)], ids=["adjust", "force"])
async def test_the_adjuster_runs_in_minimum_mode_with_unrestricted_modulation_allowed(clock, flame_state, adjusted):
    pwm = make_hot_water_pwm()

    await update(pwm, flame_state, True, minimum_setpoint=60.0, flow=60.0)

    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.setpoint == adjusted


# Resetting the adjuster

async def test_reset_setpoint_adjuster_restores_the_default_and_keeps_the_pulse(clock):
    pwm = make_hot_water_pwm()
    await update(pwm, burning(), False, flow=60.0)
    assert (pwm.status, pwm.setpoint) == (PWMStatus.ON, 57.0)

    pwm.reset_setpoint_adjuster()
    assert (pwm.status, pwm.setpoint) == (PWMStatus.ON, MINIMUM_SETPOINT)

    # The adjuster starts afresh at the flow - 3, instead of stepping from the old value towards it
    clock.tick(30)
    await update(pwm, burning(), False, flow=50.0)
    assert pwm.setpoint == 47.0
