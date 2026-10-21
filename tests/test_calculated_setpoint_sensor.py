"""Calculated Setpoint sensor — state tracks the climate's _calculated_setpoint."""

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import DOMAIN
from tests.const import IDLE, ems_entry

pytestmark = pytest.mark.usefixtures("loop_entry")

ENTITY_ID = "sensor.calculated_setpoint_test"


@pytest.mark.parametrize(*ems_entry())
async def test_sensor_metadata(hass: HomeAssistant, entry) -> None:
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes["device_class"] == SensorDeviceClass.TEMPERATURE
    assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert state.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS


@pytest.mark.parametrize(*ems_entry())
async def test_sensor_unknown_before_first_cycle(hass: HomeAssistant, entry) -> None:
    """Before any control loop has run the calculated setpoint is None → unknown."""
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN


@pytest.mark.parametrize(*ems_entry())
async def test_sensor_updates_after_control_loop(hass: HomeAssistant, entry, climate: SatClimate, boiler_data) -> None:
    """After a successful control loop the sensor matches climate.calculated_setpoint."""
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    calculated = climate.calculated_setpoint
    assert calculated is not None

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert float(state.state) == calculated


@pytest.mark.parametrize(*ems_entry())
async def test_sensor_reflects_low_pass_filter_on_second_cycle(hass: HomeAssistant, entry, climate: SatClimate, boiler_data) -> None:
    """The second cycle applies the 0.2 alpha low-pass filter; the sensor reflects the filtered value."""
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    await climate.async_control_heating_loop()
    first = climate.calculated_setpoint

    await climate.async_control_heating_loop()
    second = climate.calculated_setpoint

    assert second is not None
    # The low-pass filter smooths toward the raw value, so the second value may differ
    state = hass.states.get(ENTITY_ID)
    assert float(state.state) == second
    # Sanity: the filter keeps the value in a reasonable range
    assert abs(second - first) < 10
