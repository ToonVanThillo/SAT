"""PWM and hot water (plan step 14): no pulse starts while the boiler produces hot water, nor within the hot water delay after a
hot water burn ends; a pulse that is on is not affected. The setpoint adjuster does not adjust during a hot water burn."""
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HOT_WATER_DELAY,
    CONF_MINIMUM_SETPOINT,
    MINIMUM_SETPOINT,
    PWMStatus,
)
from custom_components.sat.pwm import PWM
from tests.conftest import TIMELINE
from tests.const import IDLE, with_data
from tests.test_pwm_cycles import CURVE, Clock, boiler, config, setpoint_for

PWM_LINES = ("pwm ON", "pwm OFF", "pwm IDLE", "pwm cycles cap", "pwm hold")


@pytest.fixture
def clock() -> Iterator[Clock]:
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


def make_pwm(hot_water_delay: float = 0) -> PWM:
    CURVE.value = 30.0
    pwm = PWM(config(), CURVE, False, True, hot_water_delay=hot_water_delay)  # type: ignore[arg-type]
    # 2 cycles per hour: a duty of 0.5 gives 900 s on, 900 s off
    pwm.update_cycles_per_hour(20)
    return pwm


def flame(active: bool = False, latest: float | None = None, since: float | None = None) -> SimpleNamespace:
    """A flame state; `since` is the seconds since the last hot water burn ended."""
    return SimpleNamespace(is_active=active, is_inactive=not active, latest_on_time_seconds=latest, average_on_time_seconds=100, seconds_since_hot_water_burn=since)


async def update(pwm: PWM, clock: Clock, seconds: float = 0, duty: float = 0.5, hot_water: bool = False, since: float | None = None) -> None:
    clock.tick(seconds)
    await pwm.update(boiler(hot_water=hot_water), flame(since=since), setpoint_for(duty))  # type: ignore[arg-type]


def pwm_lines(caplog) -> list[str]:
    return [message for record in caplog.records if record.name == TIMELINE and (message := record.getMessage()).startswith(PWM_LINES)]


@pytest.fixture(autouse=True)
def timeline_info(caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)


# The hold

async def test_no_pulse_starts_while_the_boiler_produces_hot_water(clock, caplog):
    pwm = make_pwm()

    await update(pwm, clock, hot_water=True)

    # Held in the off phase, so the climate requests no heating
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"

    await update(pwm, clock, 10)

    assert pwm.status == PWMStatus.ON
    assert pwm.hold is None
    assert pwm_lines(caplog) == ["pwm hold (hot water)", "pwm OFF", "pwm hold ended", "pwm ON (pulse 1 this hour, 2 cycles/h)"]


@pytest.mark.parametrize("since, held", [(0, True), (299, True), (300, False), (None, False)])
async def test_no_pulse_starts_within_the_hot_water_delay(clock, since, held):
    pwm = make_pwm(hot_water_delay=300)

    await update(pwm, clock, since=since)

    assert pwm.status == (PWMStatus.OFF if held else PWMStatus.ON)
    assert pwm.hold == ("hot water delay" if held else None)


async def test_without_a_delay_only_hot_water_holds(clock):
    pwm = make_pwm()

    await update(pwm, clock, since=0)

    assert pwm.status == PWMStatus.ON


async def test_a_held_start_in_the_off_phase_starts_once_the_hold_ends(clock, caplog):
    pwm = make_pwm(hot_water_delay=300)
    await update(pwm, clock)
    await update(pwm, clock, 900)
    assert pwm.status == PWMStatus.OFF

    # The off time has passed, but a hot water burn ended 100 s ago
    await update(pwm, clock, 900, since=100)
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water delay"

    await update(pwm, clock, 200, since=300)
    assert pwm.status == PWMStatus.ON
    assert pwm_lines(caplog)[-1] == "pwm ON (pulse 2 this hour, 2 cycles/h)"


async def test_a_start_held_from_idle_starts_at_the_next_cycle_after_the_hold(clock):
    pwm = make_pwm(hot_water_delay=300)
    await update(pwm, clock, since=0)
    assert pwm.status == PWMStatus.OFF

    # Not after a full off time from the hold's start
    await update(pwm, clock, 300, since=300)

    assert pwm.status == PWMStatus.ON


async def test_a_pulse_that_is_on_is_not_affected(clock, caplog):
    pwm = make_pwm(hot_water_delay=300)
    await update(pwm, clock)

    await update(pwm, clock, 300, hot_water=True)
    assert pwm.status == PWMStatus.ON
    assert pwm.hold is None

    await update(pwm, clock, 600, since=0)
    assert pwm.status == PWMStatus.OFF
    assert pwm_lines(caplog) == ["pwm ON (pulse 1 this hour, 2 cycles/h)", "pwm OFF"]


async def test_a_held_start_latches_no_cycles_per_hour(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    pwm.update_cycles_per_hour(-5)

    assert pwm.cycles_per_hour == 4


async def test_a_held_start_after_idle_does_not_revive_an_old_latch(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)
    await update(pwm, clock)
    assert pwm.cycles_per_hour == 4

    # Missing values end the pulse; N changes while idle
    CURVE.value = None
    await update(pwm, clock, 10)
    CURVE.value = 30.0
    pwm.update_cycles_per_hour(20)

    await update(pwm, clock, 10, hot_water=True)

    assert pwm.status == PWMStatus.OFF
    assert pwm.cycles_per_hour == 2


async def test_a_held_start_is_not_capped(clock, caplog):
    pwm = make_pwm()
    # Two pulses well within the hour: a duty of 0.9 gives a 180 s off time, 0.1 a 180 s pulse
    await update(pwm, clock)
    await update(pwm, clock, 900)
    await update(pwm, clock, 180, duty=0.9)
    await update(pwm, clock, 180, duty=0.1)
    assert pwm_lines(caplog)[-1] == "pwm OFF"

    # The third start in the hour is due: held, the cap has no effect
    await update(pwm, clock, 180, duty=0.9, hot_water=True)
    assert pwm.hold == "hot water"
    assert not [line for line in pwm_lines(caplog) if line.startswith("pwm cycles cap")]

    await update(pwm, clock, 10, duty=0.9)
    assert pwm_lines(caplog)[-1] == "pwm cycles cap reached (2/h)"


async def test_a_reset_clears_the_hold(clock):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    pwm.reset()

    assert pwm.hold is None


# Hold lines on the timeline

async def test_hold_lines_are_logged_on_change_of_the_reason(clock, caplog):
    pwm = make_pwm(hot_water_delay=300)

    await update(pwm, clock, hot_water=True)
    await update(pwm, clock, 10, hot_water=True)
    await update(pwm, clock, 10, since=0)
    await update(pwm, clock, 10, since=100)
    await update(pwm, clock, 200, since=300)

    assert pwm_lines(caplog) == [
        "pwm hold (hot water)",
        "pwm OFF",
        "pwm hold (hot water delay)",
        "pwm hold ended",
        "pwm ON (pulse 1 this hour, 2 cycles/h)",
    ]


async def test_no_hold_line_while_no_start_is_due(clock, caplog):
    pwm = make_pwm()
    await update(pwm, clock)
    await update(pwm, clock, 900)

    # The off time hasn't passed yet
    await update(pwm, clock, 100, hot_water=True)

    assert pwm.hold == "hot water"
    assert pwm_lines(caplog) == ["pwm ON (pulse 1 this hour, 2 cycles/h)", "pwm OFF"]


async def test_a_hold_ends_when_the_start_is_no_longer_due(clock, caplog):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)

    # Below the minimum duty no pulse starts, so nothing is held any more
    await update(pwm, clock, 10, duty=0.0, hot_water=True)

    assert pwm_lines(caplog)[-1] == "pwm hold ended"
    assert pwm.status == PWMStatus.OFF


@pytest.mark.parametrize("end, lines", [
    (lambda pwm: pwm.reset(), ["pwm IDLE (reset)", "pwm hold ended"]),
    (lambda pwm: pwm.disable(), ["pwm IDLE (disabled)", "pwm hold ended"]),
])
async def test_a_reset_or_disable_ends_a_hold(clock, caplog, end, lines):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)
    caplog.clear()

    end(pwm)

    assert pwm_lines(caplog) == lines


async def test_missing_values_end_a_hold(clock, caplog):
    pwm = make_pwm()
    await update(pwm, clock, hot_water=True)
    caplog.clear()

    CURVE.value = None
    await update(pwm, clock, 10, hot_water=True)

    assert pwm_lines(caplog) == ["pwm IDLE (missing values)", "pwm hold ended"]


# The setpoint adjuster

async def test_the_setpoint_adjuster_does_not_adjust_during_a_hot_water_burn(clock):
    pwm = make_pwm()
    burning = flame(active=True, latest=30)

    await pwm.update(boiler(flame_active=True, hot_water=True, flow=60.0), burning, setpoint_for(0.5))  # type: ignore[arg-type]
    assert pwm.setpoint == MINIMUM_SETPOINT

    await pwm.update(boiler(flame_active=True, flow=60.0), burning, setpoint_for(0.5))  # type: ignore[arg-type]
    assert pwm.setpoint == 57.0


async def test_the_setpoint_adjuster_still_forces_with_the_hot_water_flag_on(clock):
    pwm = make_pwm()

    # Flame off with a short average on time: the force step, unchanged
    await pwm.update(boiler(hot_water=True, flow=60.0), SimpleNamespace(**vars(flame()) | {"average_on_time_seconds": 30}), setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.setpoint == 70.0


# Through the climate loop on EMS-ESP

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_HOT_WATER_DELAY: 300}))
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
    cycle_lines = [record.getMessage() for record in caplog.records if record.name == TIMELINE and record.getMessage().startswith("cycle ")]
    assert "hold=hot water delay" in cycle_lines[-1]

    freezer.tick(299)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF

    freezer.tick(1)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON
