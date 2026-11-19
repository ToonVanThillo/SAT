"""PWM and hot water (plan step 14): no pulse starts while the boiler produces hot water, nor within the hot water delay after a
hot water burn ends; a pulse that is on is not affected. The setpoint adjuster does not adjust during a hot water burn."""
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_HOT_WATER_DELAY,
    CONF_MINIMUM_SETPOINT,
    MINIMUM_SETPOINT,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.pwm import PWM
from tests.conftest import TIMELINE, last_cycle
from tests.const import IDLE, with_data
from tests.test_pwm_cycles import (
    COLD,
    CURVE,
    FLOW,
    MILD,
    Clock,
    boiler,
    config,
    setpoint_for,
)


@pytest.fixture
def clock() -> Iterator[Clock]:
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


def make_pwm(hot_water_delay: float = 0) -> PWM:
    CURVE.value = 30.0
    return PWM(config(), CURVE, lambda: None, hot_water_delay=hot_water_delay)  # type: ignore[arg-type]


def flame(active: bool = False, latest: float | None = None, since: float | None = None) -> SimpleNamespace:
    """A flame state; `since` is the seconds since the last hot water burn ended."""
    return SimpleNamespace(is_active=active, is_inactive=not active, latest_on_time_seconds=latest, average_on_time_seconds=100, seconds_since_hot_water_burn=since)


async def update(
        pwm: PWM, clock: Clock, seconds: float = 0, duty: float = 0.5, hot_water: bool = False, since: float | None = None, allow_unrestricted: bool = False,
        flow: float | None = FLOW, outside_temperature: float = MILD,
) -> None:
    """Update PWM; the default outside temperature gives 2 cycles per hour: a duty of 0.5 gives 900 s on, 900 s off."""
    clock.tick(seconds)
    await pwm.update(boiler(hot_water=hot_water, flow=flow), flame(since=since), setpoint_for(duty), minimum_setpoint=10.0, allow_unrestricted=allow_unrestricted, outside_temperature=outside_temperature)  # type: ignore[arg-type]


# The hold

async def test_no_pulse_starts_while_the_boiler_produces_hot_water(clock):
    pwm = make_pwm()

    await update(pwm, clock, hot_water=True)

    # Held in the off phase, so the climate requests no heating
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"
    assert pwm.start_held == "hot water"

    await update(pwm, clock, 10)

    assert pwm.status == PWMStatus.ON
    assert pwm.hold is None
    assert pwm.start_held is None


@pytest.mark.parametrize("since, held", [(0, True), (299, True), (300, False), (None, False)])
async def test_no_pulse_starts_within_the_hot_water_delay(clock, since, held):
    pwm = make_pwm(hot_water_delay=300)

    await update(pwm, clock, since=since)

    assert pwm.status == (PWMStatus.OFF if held else PWMStatus.ON)
    assert pwm.hold == ("hot water delay" if held else None)
    assert pwm.start_held == ("hot water delay" if held else None)


async def test_without_a_delay_only_hot_water_holds(clock):
    pwm = make_pwm()

    await update(pwm, clock, since=0)

    assert pwm.status == PWMStatus.ON


async def test_a_held_start_in_the_off_phase_starts_once_the_hold_ends(clock):
    pwm = make_pwm(hot_water_delay=300)
    await update(pwm, clock)
    await update(pwm, clock, 900)
    assert pwm.status == PWMStatus.OFF

    # The off time has passed, but a hot water burn ended 100 s ago
    await update(pwm, clock, 900, since=100)
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water delay"
    assert pwm.start_held == "hot water delay"

    await update(pwm, clock, 200, since=300)
    assert pwm.status == PWMStatus.ON
    assert pwm.start_held is None


async def test_a_start_held_before_the_first_pulse_starts_at_the_next_cycle_after_the_hold(clock):
    pwm = make_pwm(hot_water_delay=300)
    await update(pwm, clock, since=0)
    assert pwm.status == PWMStatus.OFF

    # Not after a full off time from the hold's start
    await update(pwm, clock, 300, since=300)

    assert pwm.status == PWMStatus.ON


async def test_a_pulse_that_is_on_is_not_affected(clock):
    pwm = make_pwm(hot_water_delay=300)
    await update(pwm, clock)

    await update(pwm, clock, 300, hot_water=True)
    assert pwm.status == PWMStatus.ON
    assert pwm.hold is None

    await update(pwm, clock, 600, since=0)
    assert pwm.status == PWMStatus.OFF
    assert pwm.start_held is None


async def test_a_held_start_latches_no_cycles_per_hour(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    await update(pwm, clock, hot_water=True, outside_temperature=COLD)

    assert pwm.cycles_per_hour == 4


async def test_a_held_start_after_the_latch_ran_out_does_not_revive_it(clock):
    pwm = make_pwm()
    await update(pwm, clock, outside_temperature=COLD)  # N=4: (450, 450)
    assert pwm.cycles_per_hour == 4
    await update(pwm, clock, 450, outside_temperature=COLD)
    assert pwm.status == PWMStatus.OFF

    # N changes in the off phase; the latch runs out 2 * 3600 / 4 s after the pulse end, and the due start is held
    await update(pwm, clock, 1801, hot_water=True, outside_temperature=MILD)

    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"
    assert pwm.cycles_per_hour == 2


async def test_hot_water_outranks_its_delay(clock):
    pwm = make_pwm(hot_water_delay=300)

    await update(pwm, clock, hot_water=True, since=0)

    assert pwm.hold == "hot water"


async def test_a_held_third_start_in_the_hour_starts_once_the_hold_ends(clock):
    pwm = make_pwm()
    # Two pulses well within the hour: a duty of 0.9 gives a 180 s off time, 0.1 a 180 s pulse
    await update(pwm, clock)
    await update(pwm, clock, 900)
    await update(pwm, clock, 180, duty=0.9)
    await update(pwm, clock, 180, duty=0.1)
    assert pwm.status == PWMStatus.OFF

    # The third start in the hour is due: held by hot water, then started, as there is no limit on the pulses per hour
    await update(pwm, clock, 180, duty=0.9, hot_water=True)
    assert pwm.hold == "hot water"
    assert pwm.start_held == "hot water"
    assert pwm.status == PWMStatus.OFF

    await update(pwm, clock, 10, duty=0.9)  # 1450 s
    assert pwm.status == PWMStatus.ON
    assert pwm.start_held is None


async def test_a_reset_clears_the_hold(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    pwm.reset()

    assert pwm.hold is None
    assert pwm.start_held is None


# The held start

async def test_the_held_start_follows_the_reason_for_the_hold(clock):
    pwm = make_pwm(hot_water_delay=300)
    held = []

    for seconds, kwargs in [(0, {"hot_water": True}), (10, {"hot_water": True}), (10, {"since": 0}), (10, {"since": 100}), (200, {"since": 300})]:
        await update(pwm, clock, seconds, **kwargs)
        held.append(pwm.start_held)

    assert held == ["hot water", "hot water", "hot water delay", "hot water delay", None]
    assert pwm.status == PWMStatus.ON


async def test_no_start_is_held_while_no_start_is_due(clock):
    pwm = make_pwm()
    await update(pwm, clock)
    await update(pwm, clock, 900)

    # The off time hasn't passed yet
    await update(pwm, clock, 100, hot_water=True)

    assert pwm.hold == "hot water"
    assert pwm.start_held is None


async def test_a_hold_ends_when_the_start_is_no_longer_due(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    # Below the minimum duty no pulse starts, so nothing is held any more
    await update(pwm, clock, 10, duty=0.0, hot_water=True)

    assert pwm.start_held is None
    assert pwm.status == PWMStatus.OFF


async def test_a_reset_ends_a_held_start(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)
    assert pwm.start_held == "hot water"

    pwm.reset()

    assert pwm.start_held is None


async def test_a_missing_flow_temperature_keeps_a_hold(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    # The start stays due on the last duty, and stays held
    await update(pwm, clock, 10, hot_water=True, flow=None)

    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"
    assert pwm.start_held == "hot water"


async def test_a_reset_during_a_hold_before_the_first_pulse_ends_it_and_adds_no_off_phase(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)
    assert pwm.hold == "hot water"

    pwm.reset()
    assert pwm.hold is None
    assert pwm.start_held is None

    # No pulse has ended: a start is due at once when hot water stops
    await update(pwm, clock, 1)
    assert pwm.status == PWMStatus.ON


async def test_a_reset_while_off_does_not_restart_the_off_phase(clock):
    pwm = make_pwm()
    await update(pwm, clock)
    await update(pwm, clock, 900)  # the pulse ends
    assert pwm.status == PWMStatus.OFF
    await update(pwm, clock, 900, hot_water=True)  # the start is due, and held
    assert pwm.hold == "hot water"
    pwm.reset()
    assert pwm.hold is None

    # The off phase from the pulse end 900 s ago is over, so the start is due as soon as the duty is back
    await update(pwm, clock, 1)
    assert pwm.status == PWMStatus.ON


async def held_at_the_minimum_off_phase_in_unrestricted(pwm: PWM, clock: Clock) -> None:
    """At N=2: a pulse at a duty of 0.5 (900 s on, 900 s off) ends; 180 s later UNRESTRICTED (off time 0) makes a start due, held by hot water."""
    await update(pwm, clock)
    await update(pwm, clock, 900)
    assert pwm.status == PWMStatus.OFF

    await update(pwm, clock, 180, hot_water=True, allow_unrestricted=True)
    assert pwm.mode == ModulationMode.UNRESTRICTED
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"


async def test_a_held_start_survives_a_mode_change_while_the_new_duty_allows_a_pulse(clock):
    pwm = make_pwm()
    await held_at_the_minimum_off_phase_in_unrestricted(pwm, clock)

    # Back in MINIMUM at 0.5, whose 900 s off time hasn't passed: the held start stays due and held
    await update(pwm, clock, 10, hot_water=True)
    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.status == PWMStatus.OFF
    assert pwm.start_held == "hot water"

    await update(pwm, clock, 10)
    assert pwm.status == PWMStatus.ON
    assert pwm.start_held is None


async def test_a_held_start_is_dropped_when_the_new_duty_is_below_the_minimum_pulse(clock):
    pwm = make_pwm()
    await held_at_the_minimum_off_phase_in_unrestricted(pwm, clock)

    # Accepted: back in MINIMUM at a duty below the minimum pulse no start is due, so the hold ends while hot water still runs
    await update(pwm, clock, 10, duty=0.0, hot_water=True)

    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.status == PWMStatus.OFF
    assert pwm.start_held is None


# The setpoint adjuster

async def test_the_setpoint_adjuster_does_not_adjust_during_a_hot_water_burn(clock):
    pwm = make_pwm()
    burning = flame(active=True, latest=30)

    await pwm.update(boiler(flame_active=True, hot_water=True, flow=60.0), burning, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False, outside_temperature=MILD)  # type: ignore[arg-type]
    assert pwm.setpoint == MINIMUM_SETPOINT

    await pwm.update(boiler(flame_active=True, flow=60.0), burning, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False, outside_temperature=MILD)  # type: ignore[arg-type]
    assert pwm.setpoint == 57.0


async def test_the_setpoint_adjuster_does_not_force_with_the_hot_water_flag_on(clock):
    pwm = make_pwm()
    short_burns = SimpleNamespace(**vars(flame()) | {"average_on_time_seconds": 30})

    # Flame off with a short average on time: the force step, paused while producing hot water
    await pwm.update(boiler(hot_water=True, flow=60.0), short_burns, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False, outside_temperature=MILD)  # type: ignore[arg-type]
    assert pwm.setpoint == MINIMUM_SETPOINT

    await pwm.update(boiler(flow=60.0), short_burns, setpoint_for(0.5), minimum_setpoint=10.0, allow_unrestricted=False, outside_temperature=MILD)  # type: ignore[arg-type]
    assert pwm.setpoint == 70.0


# Through the climate loop on EMS-ESP

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False, CONF_HOT_WATER_DELAY: 300}))
async def test_pwm_waits_for_the_hot_water_delay_after_a_hot_water_burn(hass, entry, climate, boiler_data, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE)
    await boiler_data(**IDLE | {"burngas": "on", "tapwateractive": "on"})
    freezer.tick(60)
    await boiler_data(**IDLE)

    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["pwm"].hold == "hot water delay"
    assert last_cycle(caplog)["pwm"].start_held == "hot water delay"

    freezer.tick(299)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF

    freezer.tick(1)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON
