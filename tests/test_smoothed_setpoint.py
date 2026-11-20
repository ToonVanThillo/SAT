"""The smoothed setpoint (low-pass filter of the calculated setpoint) and `ema`, whose argument order is (value, previous).

The tests of the other callers of `ema` are with their components: test_flame.py, test_pid_derivative.py, test_overshoot_protection.py and test_pwm_cycles.py.
"""
import logging
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_MAXIMUM_SETPOINT,
    CONF_MINIMUM_SETPOINT,
    MINIMUM_SETPOINT,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.helpers import ema
from tests.conftest import TIMELINE, last_cycle
from tests.const import IDLE, ems_entry, with_data

OUTSIDE = "sensor.test_outside_sensor"
# Outside temperatures that give a calculated setpoint of about 38, 63 and 31 with the default inputs
MILD, COLD, WARM = 5, -20, 30

# ema

def test_ema_returns_the_value_without_a_previous():
    assert ema(7.5, None) == 7.5
    assert ema(7.5, None, 0.9) == 7.5


@pytest.mark.parametrize(("value", "previous", "alpha", "expected"), [
    (10.0, 0.0, 0.2, 2.0),
    (0.0, 10.0, 0.2, 8.0),
    (10.0, 20.0, 0.5, 15.0),
    (10.0, 20.0, 1.0, 10.0),
    (10.0, 20.0, 0.0, 20.0),
])
def test_ema_weights_the_value_by_alpha(value, previous, alpha, expected):
    assert ema(value, previous, alpha) == pytest.approx(expected)


# The calculated setpoint

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize(("curve", "output", "expected"), [
    (None, 5.0, MINIMUM_SETPOINT),
    (40.0, 5.04, 45.0),
    (40.0, -5.06, 34.9),
    (20.0, -50.0, MINIMUM_SETPOINT),
])
async def test_the_calculated_setpoint_is_the_curve_plus_the_pid_output(entry, climate: SatClimate, curve, output, expected):
    # The curve and PID output are patched: a missing curve and a floor-breaking output cannot be reached through the inputs
    with patch.object(type(climate.heating_curve), "value", new_callable=PropertyMock, return_value=curve), \
            patch.object(type(climate.pid), "output", new_callable=PropertyMock, return_value=output):
        assert climate.calculated_setpoint == expected


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_calculated_setpoint_follows_the_outside_temperature(entry, hass, heating):
    result = []
    for outside in (WARM, MILD, COLD):
        calculated, _ = await loop_at(hass, heating, outside)
        result.append(calculated)

    assert result == sorted(result)
    assert result[0] < result[-1]


# The smoothed setpoint

@pytest.fixture
async def heating(climate: SatClimate, boiler_data):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    return climate


async def loop_at(hass, climate: SatClimate, outside: float) -> tuple[float, float | None]:
    """Run one loop at the outside temperature; return the calculated setpoint and the smoothed setpoint after it."""
    hass.states.async_set(OUTSIDE, str(outside))
    await hass.async_block_till_done()
    await climate.async_control_heating_loop()
    return climate.calculated_setpoint, climate.smoothed_setpoint


async def smoothed_after(hass, climate: SatClimate, outsides: list[float]) -> list[float | None]:
    """Run one loop per outside temperature, and check each smoothed setpoint is a fifth of the way from the previous one to the calculated one."""
    result: list[float | None] = []
    previous = None
    for outside in outsides:
        calculated, smoothed = await loop_at(hass, climate, outside)
        assert smoothed == (calculated if previous is None else round(0.2 * calculated + 0.8 * previous, 1))
        result.append(smoothed)
        previous = smoothed

    return result


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_smoothed_setpoint_is_none_before_the_first_cycle(entry, climate: SatClimate):
    assert climate.smoothed_setpoint is None


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_smoothed_setpoint_starts_at_the_calculated_one_and_follows_it_by_a_fifth(entry, hass, heating, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)

    first, second, third = await smoothed_after(hass, heating, [MILD, COLD, WARM])

    # The three differ from one another and from their calculated setpoints, so the filter is at work
    assert len({first, second, third}) == 3
    assert second != heating.calculated_setpoint
    record = last_cycle(caplog)
    assert (record["smoothed"], record["calculated"]) == (third, heating.calculated_setpoint)
    assert record["smoothed"] != record["calculated"]
    assert not {"setpoint", "requested"} & record.keys()
    assert {"setpoint_rule", "curve", "p", "i", "d"} <= record.keys()


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_sensor_shows_the_smoothed_setpoint(entry, hass, heating):
    _, smoothed = (await smoothed_after(hass, heating, [MILD, COLD]))

    assert smoothed != heating.calculated_setpoint
    assert hass.states.get("sensor.calculated_setpoint_test").state == str(smoothed)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 40}))
@pytest.mark.parametrize(("outsides", "mode"), [
    # The smoothed setpoint (about 57) stays above the minimum setpoint of 40 while the calculated one (about 31) is below it
    ([COLD, WARM], ModulationMode.UNRESTRICTED),
    # The smoothed setpoint (about 37) stays below the minimum setpoint while the calculated one (about 63) is above it
    ([WARM, COLD], ModulationMode.MINIMUM),
], ids=["falling", "rising"])
async def test_pwm_is_driven_by_the_smoothed_setpoint(entry, hass, heating, outsides, mode):
    await smoothed_after(hass, heating, outsides)

    assert (heating.calculated_setpoint > 40) != (mode == ModulationMode.UNRESTRICTED)
    assert heating.pwm.mode == mode


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry({CONF_MAXIMUM_SETPOINT: 40}))
@pytest.mark.parametrize(("outside", "capped"), [(MILD, False), (COLD, True)])
async def test_an_unrestricted_pulse_requests_the_smoothed_setpoint_capped_at_the_maximum(entry, hass, heating, coordinator, freezer, boiler_data, outside, capped):
    cold = IDLE | {"selflowtemp": 25, "curflowtemp": 25, "rettemp": 25, "burnmaxpower": 0, "heatingoff": "on"}
    await boiler_data(**cold)
    freezer.tick(60)
    await boiler_data(**cold)

    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        _, smoothed = await loop_at(hass, heating, outside)

    assert (smoothed > coordinator.maximum_setpoint) == capped
    assert heating.pwm.status == PWMStatus.ON
    assert heating.pwm.mode == ModulationMode.UNRESTRICTED
    assert submit.await_args_list[-1].args[0].control_setpoint == min(smoothed, coordinator.maximum_setpoint)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_control_state_reset_clears_the_smoothed_setpoint(entry, hass, heating):
    await smoothed_after(hass, heating, [MILD])

    heating.reset_control_state()

    assert heating.smoothed_setpoint is None
    # The first cycle after it starts at the calculated setpoint again
    assert await smoothed_after(hass, heating, [COLD]) == [heating.calculated_setpoint]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_pid_reset_with_a_changed_error_clears_the_smoothed_setpoint(entry, hass, heating):
    await smoothed_after(hass, heating, [MILD])
    assert heating.smoothed_setpoint is not None

    # Keep the control state reset of a target change out of it, so the PID reset alone clears the smoothed setpoint
    with patch.object(heating, "reset_control_state"):
        await heating.async_set_target_temperature(24.0)

    assert heating.smoothed_setpoint is None


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry())
async def test_a_cycle_with_a_missing_temperature_keeps_the_smoothed_setpoint(entry, hass, heating, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    _, smoothed = await smoothed_after(hass, heating, [MILD, COLD])

    hass.states.async_set(OUTSIDE, "unavailable")
    await hass.async_block_till_done()
    await heating.async_control_heating_loop()

    assert last_cycle(caplog)["inputs"].outside_temperature is None
    assert heating.smoothed_setpoint == smoothed
    assert heating.pwm.status == PWMStatus.OFF
