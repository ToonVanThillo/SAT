"""PWM modulation mode (overhaul plan step 5): UNRESTRICTED when unrestricted modulation is allowed and the requested setpoint is above
the minimum setpoint, otherwise MINIMUM; UNRESTRICTED gives a 100% period, and a mode change while ON starts a new period."""
import json
import logging
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    MINIMUM_SETPOINT,
    ModulationMode,
    PWMStatus,
)
from tests.conftest import TIMELINE
from tests.const import IDLE, with_data
from tests.test_pwm_cycles import Clock, make_pwm, setpoint_for, start_pulse, step
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


# (a) The mode rule; setpoint_for(0.5) is 35

@pytest.mark.parametrize(("allow_unrestricted", "minimum_setpoint", "expected"), [
    (False, 30.0, ModulationMode.MINIMUM),
    (True, 30.0, ModulationMode.UNRESTRICTED),
    (True, setpoint_for(0.5), ModulationMode.MINIMUM),
    (True, 40.0, ModulationMode.MINIMUM),
], ids=["not-allowed", "allowed-above-minimum", "at-minimum", "below-minimum"])
async def test_the_mode_rule(clock, allow_unrestricted, minimum_setpoint, expected):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, minimum_setpoint=minimum_setpoint, allow_unrestricted=allow_unrestricted)

    assert pwm.mode == expected
    assert pwm.state.mode == expected


async def test_the_mode_is_computed_every_update(clock):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    assert pwm.mode == ModulationMode.UNRESTRICTED
    await step(pwm, clock, 0.5, 10, minimum_setpoint=40.0, allow_unrestricted=True)
    assert pwm.mode == ModulationMode.MINIMUM
    await step(pwm, clock, 0.5, 10, allow_unrestricted=True)
    assert pwm.mode == ModulationMode.UNRESTRICTED


# (b) UNRESTRICTED gives a 100% period of 2 * 3600 / N, whatever the duty formula gives

@pytest.mark.parametrize(("outside_temperature", "duty_cycle"), [(-5, (1800, 0)), (20, (3600, 0))], ids=["N=4", "N=2"])
async def test_unrestricted_gives_a_100_percent_period(clock, outside_temperature, duty_cycle):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(outside_temperature)
    await step(pwm, clock, 0.5, allow_unrestricted=True)  # the duty formula would give 50%

    assert pwm.duty_cycle == duty_cycle
    assert pwm.duty_cycle_branch == "unrestricted"
    assert pwm.last_duty_cycle_percentage == 100.0
    assert pwm.state.duty_cycle == duty_cycle
    assert pwm.state.mode == ModulationMode.UNRESTRICTED
    assert pwm.state.status == PWMStatus.ON


# (c) UNRESTRICTED rolls over

async def test_unrestricted_rolls_over_without_an_off_phase(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: 1800 s on, 0 s off
    await step(pwm, clock, 0.5, allow_unrestricted=True)

    # Four period ends, at 1800, 3600, 5400 and 7200 s
    for _ in range(24):
        await step(pwm, clock, 0.5, 300, allow_unrestricted=True)
        assert pwm.status == PWMStatus.ON
        assert pwm.duty_cycle == (1800, 0)

    assert pwm_lines(caplog) == ["pwm mode UNRESTRICTED", "pwm ON (4 cycles/h)"]


# (d) A start in UNRESTRICTED is a pulse start

async def test_an_unrestricted_start_after_a_pulse_end_waits_for_the_minimum_off_phase(clock, caplog):
    pwm = make_pwm()  # N=4: a duty of 0.5 gives 450 s on, 450 s off
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.5, 450)
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 0.5, 179, allow_unrestricted=True)  # 179 s after the pulse end
    assert pwm.status == PWMStatus.OFF
    assert pwm.duty_cycle == (1800, 0)
    await step(pwm, clock, 0.5, 1, allow_unrestricted=True)  # 180 s after the pulse end
    assert pwm.status == PWMStatus.ON

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm OFF", "pwm mode UNRESTRICTED", "pwm ON (4 cycles/h)"]


async def test_an_unrestricted_start_after_a_reset_waits_for_the_minimum_off_phase(clock):
    pwm = make_pwm()
    await start_pulse(pwm, clock, 0.5)
    pwm.reset()
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 0.5, 179, allow_unrestricted=True)
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.5, 1, allow_unrestricted=True)
    assert pwm.status == PWMStatus.ON
    assert pwm.mode == ModulationMode.UNRESTRICTED


async def test_a_hot_water_hold_holds_an_unrestricted_start(clock, caplog):
    pwm = make_hot_water_pwm()  # N=2: a duty of 0.5 gives 900 s on
    await hot_water_update(pwm, clock)
    await hot_water_update(pwm, clock, 900)
    assert pwm.status == PWMStatus.OFF

    await hot_water_update(pwm, clock, 180, hot_water=True, allow_unrestricted=True)
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"
    await hot_water_update(pwm, clock, 10, allow_unrestricted=True)
    assert pwm.status == PWMStatus.ON

    assert pwm_lines(caplog) == ["pwm ON (2 cycles/h)", "pwm OFF", "pwm mode UNRESTRICTED", "pwm hold (hot water)", "pwm hold ended", "pwm ON (2 cycles/h)"]


async def test_a_hot_water_hold_holds_an_unrestricted_start_before_the_first_pulse(clock):
    pwm = make_hot_water_pwm()
    await hot_water_update(pwm, clock, hot_water=True, allow_unrestricted=True)
    assert pwm.status == PWMStatus.OFF
    assert pwm.hold == "hot water"


# (e) MINIMUM ON -> UNRESTRICTED starts a new period

async def test_a_change_to_unrestricted_while_on_starts_a_new_period(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: a duty of 0.5 gives 450 s on; unrestricted gives 1800 s on
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.5, 400, allow_unrestricted=True)  # 400 s: the new period starts
    assert pwm.status == PWMStatus.ON
    assert pwm.duty_cycle == (1800, 0)

    # A rollover latches N again: N=2 from now on shows when the period ends
    pwm.update_cycles_per_hour(20)
    await step(pwm, clock, 0.5, 1400, allow_unrestricted=True)  # 1800 s: the end of a period from the pulse start
    assert pwm.status == PWMStatus.ON
    assert pwm.cycles_per_hour == 4
    await step(pwm, clock, 0.5, 399, allow_unrestricted=True)  # 2199 s: 1799 s into the new period
    assert pwm.status == PWMStatus.ON
    assert pwm.cycles_per_hour == 4
    await step(pwm, clock, 0.5, 1, allow_unrestricted=True)  # 2200 s: the new period ends and rolls over
    assert pwm.status == PWMStatus.ON
    assert pwm.cycles_per_hour == 2

    assert pwm_lines(caplog) == ["pwm ON (4 cycles/h)", "pwm mode UNRESTRICTED"]


# (f) UNRESTRICTED ON -> MINIMUM starts a new period: the new on-time runs from the mode change

async def test_a_change_to_minimum_late_in_an_unrestricted_period_runs_the_new_on_time_from_the_change(clock, caplog):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: unrestricted gives 1800 s on; a duty of 0.5 gives 450 s on
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    await step(pwm, clock, 0.5, 1000, allow_unrestricted=True)
    assert pwm.status == PWMStatus.ON

    await step(pwm, clock, 0.5, 0)  # 1000 s into the period, past the new on-time
    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.duty_cycle == (450, 450)
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 449)  # 449 s after the mode change
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 1)  # 450 s after the mode change
    assert pwm.status == PWMStatus.OFF

    assert pwm_lines(caplog) == ["pwm mode UNRESTRICTED", "pwm ON (4 cycles/h)", "pwm mode MINIMUM", "pwm OFF"]


async def test_after_a_mode_change_the_minimum_pulse_still_runs_from_the_pulse_start(clock):
    pwm = make_pwm()  # N=4: below a duty of 0.1 there is no on-time
    await step(pwm, clock, 0.5, allow_unrestricted=True)
    await step(pwm, clock, 0.05, 100)  # the mode changes to MINIMUM at 100 s
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.05, 79)  # 179 s after the pulse start
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.05, 1)  # 180 s after the pulse start
    assert pwm.status == PWMStatus.OFF


# (g) A mode change while ON latches N again and restarts the boiler temperature smoothing

async def test_a_mode_change_while_on_latches_the_current_n(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4: a duty of 0.5 gives 450 s on
    await start_pulse(pwm, clock, 0.5)

    pwm.update_cycles_per_hour(20)  # N=2 from now, but N=4 is latched for this period
    await step(pwm, clock, 0.5, 10)
    assert pwm.cycles_per_hour == 4

    await step(pwm, clock, 0.5, 10, allow_unrestricted=True)
    assert pwm.cycles_per_hour == 2
    assert pwm.duty_cycle == (3600, 0)


async def test_a_mode_change_while_on_restarts_the_boiler_temperature_smoothing(clock):
    pwm = make_pwm()
    pwm.update_cycles_per_hour(-5)  # N=4
    await step(pwm, clock, 1.0, allow_unrestricted=True)  # requested setpoint 50 at a flow of 50
    await step(pwm, clock, 1.0, 1000, allow_unrestricted=True)  # long past the smoothing of the pulse start
    await step(pwm, clock, 1.0, 0)  # the mode changes to MINIMUM: a new period starts at 1000 s
    assert pwm.status == PWMStatus.ON

    # The flow jumps from 50 to 80 within 180 s of the mode change: the duty formula sees 0.2 * 80 + 0.8 * 50 = 56,
    # so a duty of (50 - 20) / (56 - 20); without the restart it would see 80, a duty of (50 - 20) / (80 - 20)
    await step(pwm, clock, 1.0, 30, flow=80.0)
    await step(pwm, clock, 1.0, 30, flow=80.0)
    assert pwm.last_duty_cycle_percentage == pytest.approx(round(100 * 30 / 36, 2))
    assert pwm.status == PWMStatus.ON


async def test_a_mode_change_while_on_seeds_the_boiler_temperature_smoothing_with_the_current_flow(clock):
    pwm = make_pwm()  # N=4
    await step(pwm, clock, 0.5, allow_unrestricted=True, flow=30.0)  # an unrestricted pulse start at a flow of 30
    await step(pwm, clock, 0.5, 60, allow_unrestricted=True, flow=60.0)  # smoothed to 0.2 * 60 + 0.8 * 30 = 36
    await step(pwm, clock, 0.5, 30, flow=60.0)  # the mode changes to MINIMUM: a new period starts, seeded by the flow of 60

    # The duty formula sees 60, a duty of (35 - 20) / (60 - 20); unseeded it would see 0.2 * 60 + 0.8 * 36 = 40.8
    assert pwm.last_duty_cycle_percentage == 37.5


# (h) A mode change while OFF starts no period: only the duty changes

async def test_a_mode_change_while_off_changes_only_the_duty(clock):
    pwm = make_pwm()  # N=4: a duty of 0.5 at a flow of 50 gives 450 s on, 450 s off
    await start_pulse(pwm, clock, 0.5)
    await step(pwm, clock, 0.5, 450)
    assert pwm.status == PWMStatus.OFF

    await step(pwm, clock, 0.5, 60, allow_unrestricted=True, flow=40.0)  # UNRESTRICTED while OFF
    await step(pwm, clock, 0.5, 60, flow=40.0)  # MINIMUM again: the duty formula still sees the flow of 50 at the pulse end
    assert pwm.duty_cycle == (450, 450)

    await step(pwm, clock, 0.5, 329, flow=40.0)  # 449 s after the pulse end
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.5, 1, flow=40.0)  # 450 s after the pulse end
    assert pwm.status == PWMStatus.ON


# (i) MINIMUM initially, after a reset and when values are missing

async def test_the_mode_is_minimum_initially_and_after_a_reset(clock):
    pwm = make_pwm()
    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.state.mode == ModulationMode.MINIMUM

    await step(pwm, clock, 0.5, allow_unrestricted=True)
    assert pwm.mode == ModulationMode.UNRESTRICTED
    pwm.reset()
    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.state.mode == ModulationMode.MINIMUM


async def test_the_mode_is_computed_without_a_flow_temperature(clock):
    pwm = make_pwm()
    await step(pwm, clock, 0.5, flow=None)
    assert pwm.mode == ModulationMode.MINIMUM

    await step(pwm, clock, 0.5, 10, allow_unrestricted=True, flow=None)
    assert pwm.mode == ModulationMode.UNRESTRICTED


async def test_without_a_flow_temperature_unrestricted_gives_a_100_percent_pulse(clock, caplog):
    pwm = make_pwm()  # N=4: a 100% period is 1800 s on, 0 s off

    await step(pwm, clock, 0.5, allow_unrestricted=True, flow=None)
    assert pwm.status == PWMStatus.ON
    assert pwm.duty_cycle == (1800, 0)

    # It rolls over as usual
    await step(pwm, clock, 0.5, 1800, allow_unrestricted=True, flow=None)
    await step(pwm, clock, 0.5, 10, allow_unrestricted=True, flow=None)
    assert pwm.status == PWMStatus.ON
    assert pwm_lines(caplog) == ["pwm mode UNRESTRICTED", "pwm ON (4 cycles/h)"]


async def test_without_a_flow_temperature_minimum_starts_no_pulse_before_the_first_duty(clock):
    pwm = make_pwm()

    await step(pwm, clock, 0.5, flow=None)
    await step(pwm, clock, 0.5, 600, flow=None)
    assert pwm.status == PWMStatus.OFF
    assert pwm.duty_cycle is None

    # With the flow back the duty is computed; no pulse has ended, so the start is due at once
    await step(pwm, clock, 0.5, 1)
    assert pwm.status == PWMStatus.ON


async def test_without_a_flow_temperature_minimum_starts_no_pulse_after_a_reset(clock):
    pwm = make_pwm()  # N=4: a duty of 0.5 gives (450, 450)
    await start_pulse(pwm, clock, 0.5)
    assert pwm.duty_cycle == (450, 450)
    pwm.reset()

    # The reset cleared the duty: past the off phase the last duty would start a pulse, but there is none
    await step(pwm, clock, 0.5, flow=None)
    await step(pwm, clock, 0.5, 451, flow=None)
    assert pwm.status == PWMStatus.OFF
    assert pwm.duty_cycle is None

    # With the flow back the duty is computed, and the off phase is over
    await step(pwm, clock, 0.5, 1)
    assert pwm.status == PWMStatus.ON


async def test_a_missing_flow_through_unrestricted_then_minimum_then_back(clock):
    pwm = make_pwm()  # N=4
    await step(pwm, clock, 0.5, allow_unrestricted=True, flow=None)
    assert (pwm.status, pwm.mode, pwm.duty_cycle) == (PWMStatus.ON, ModulationMode.UNRESTRICTED, (1800, 0))

    # MINIMUM without a flow keeps the 100% duty and runs it on from the mode change at 100 s
    await step(pwm, clock, 0.5, 100, flow=None)
    assert pwm.mode == ModulationMode.MINIMUM
    assert pwm.duty_cycle == (1800, 0)
    assert pwm.status == PWMStatus.ON

    # The flow returns: the duty is computed again; the period started at the mode change, 10 s ago
    await step(pwm, clock, 0.5, 10)
    assert pwm.duty_cycle == (450, 450)
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 439)  # 549 s: 449 s from the mode change
    assert pwm.status == PWMStatus.ON
    await step(pwm, clock, 0.5, 1)  # 450 s from the mode change
    assert pwm.status == PWMStatus.OFF


async def test_the_duty_rising_to_100_percent_in_an_off_phase_from_a_reset_keeps_the_180_s_minimum(clock):
    pwm = make_pwm()  # N=4
    await start_pulse(pwm, clock, 0.5)
    clock.tick(10)
    pwm.reset("test")

    await step(pwm, clock, 0.5, 30)  # an off time of 450 s
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.5, 60, allow_unrestricted=True)  # 90 s: 100%, off time 0
    assert pwm.mode == ModulationMode.UNRESTRICTED
    assert pwm.duty_cycle == (1800, 0)
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.5, 89, allow_unrestricted=True)  # 179 s
    assert pwm.status == PWMStatus.OFF
    await step(pwm, clock, 0.5, 1, allow_unrestricted=True)  # 180 s
    assert pwm.status == PWMStatus.ON


# (j) Through the climate: with "Allow unrestricted modulation" off, the mode stays MINIMUM

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_the_climate_publishes_the_minimum_setpoint_while_pwm_is_on_across_period_ends(climate, boiler_data, published, freezer, caplog):
    cold_boiler = IDLE | {"selflowtemp": 25, "curflowtemp": 25, "rettemp": 25, "burnmaxpower": 0}
    await boiler_data(**cold_boiler)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # A cold boiler and a high target: 100% duty, so the period ends roll over and PWM stays ON from the first start
    started_at = None
    cycles_per_hour = 0
    for minute in range(80):  # 80 minutes: at least one period end (N is at least 2: a 100% period of 3600 s)
        freezer.tick(60)
        await boiler_data(**cold_boiler)
        await climate.async_control_heating_loop()
        assert climate.calculated_setpoint > climate.minimum_setpoint_value
        assert climate.pwm.mode == ModulationMode.MINIMUM

        if started_at is not None:
            assert climate.pwm.status == PWMStatus.ON
        elif climate.pwm.status == PWMStatus.ON:
            started_at = minute
            cycles_per_hour = climate.pwm.cycles_per_hour

        assert climate.setpoint == (30 if climate.pwm.status == PWMStatus.ON else MINIMUM_SETPOINT)

    # The run lasts beyond the end of the first 100% period (2 * 3600 / N after the start)
    assert started_at is not None
    assert (80 - 1 - started_at) * 60 > 2 * 3600 / cycles_per_hour
    assert len([line for line in pwm_lines(caplog) if line.startswith("pwm ON (")]) == 1

    commands = [json.loads(args[1]) for args in published() if args[0] == "ems-esp/boiler"]
    values = {command["value"] for command in commands if command["cmd"] == "selflowtemp"}
    assert values <= {30, MINIMUM_SETPOINT}
    assert 30 in values
