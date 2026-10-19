"""Characterization tests for the synchro binary sensors.

The binary_sensor platform setup fails intermittently (`KeyError: 'climate'`): a platform setup race, it can run before the climate
platform has stored the climate entity. It is left unfixed for now, so the sensors are constructed directly and the base components are
set up first to make the entry setup deterministic. Their on-delay is driven through the `freezer` fixture.

They run on OpenTherm over MQTT: backends with channels, such as EMS-ESP, get channel synchro sensors instead.
"""
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.binary_sensor import (
    SatCentralHeatingSynchroSensor,
    SatControlSetpointSynchroSensor,
)
from custom_components.sat.const import (
    CONF_MAXIMUM_SETPOINT,
)
from tests.conftest import advance
from tests.const import otgw_entry

pytestmark = pytest.mark.usefixtures("loop_entry")

# The OpenTherm counterpart of `IDLE`: a warm, idle boiler
OTGW_IDLE = {"TSet": "40", "Tboiler": "40.5", "Tret": "35", "centralheating": "OFF", "flame": "OFF"}


async def otgw_values(hass: HomeAssistant, **values: str) -> None:
    """Publish OpenTherm gateway values on topic `otgw` (device `gw1`)."""
    for key, value in values.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()


def is_on_after_delay(sensor: BinarySensorEntity, freezer: FrozenDateTimeFactory) -> tuple[bool, bool]:
    """Return the sensor state (on at any point before the delay, on once the 60 second delay has passed)."""
    first = bool(sensor.is_on)
    freezer.tick(59)
    before = bool(sensor.is_on)
    freezer.tick(1)

    return first or before, bool(sensor.is_on)


@pytest.mark.parametrize(*otgw_entry({CONF_MAXIMUM_SETPOINT: 30}))
async def test_control_setpoint_synchro_turns_on_above_maximum_setpoint(hass, entry, climate, coordinator, freezer, published):
    sensor = SatControlSetpointSynchroSensor(coordinator, entry, climate)
    await otgw_values(hass, **OTGW_IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    # The clamped value is sent correctly, and the boiler reports it back
    assert ("otgw/set/gw1/command", "CS=30.0", 1, False) in published()
    await otgw_values(hass, TSet="30")

    # current behaviour, known bug: the unclamped climate setpoint is compared with the coordinator setpoint
    assert climate.setpoint == 35.0
    assert coordinator.setpoint == 30.0
    assert is_on_after_delay(sensor, freezer) == (False, True)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*otgw_entry())
async def test_control_setpoint_synchro_stays_off_when_values_match(hass, entry, climate, coordinator, freezer):
    sensor = SatControlSetpointSynchroSensor(coordinator, entry, climate)
    await otgw_values(hass, **OTGW_IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    await otgw_values(hass, TSet="35.0")

    assert is_on_after_delay(sensor, freezer) == (False, False)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*otgw_entry())
async def test_central_heating_synchro_never_turns_on_in_heat_mode(hass, entry, climate, coordinator, freezer):
    sensor = SatCentralHeatingSynchroSensor(coordinator, entry, climate)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # current behaviour, known bug: hvac_action is derived from device_active, so in HEAT mode the sensor can never detect a mismatch
    for central_heating in ("OFF", "ON", "OFF"):
        await otgw_values(hass, **OTGW_IDLE | {"centralheating": central_heating})
        assert is_on_after_delay(sensor, freezer) == (False, False)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*otgw_entry())
async def test_central_heating_synchro_turns_on_when_off_but_device_active(hass, entry, climate, coordinator, freezer):
    sensor = SatCentralHeatingSynchroSensor(coordinator, entry, climate)
    await otgw_values(hass, **OTGW_IDLE | {"centralheating": "ON"})
    await climate.async_set_hvac_mode(HVACMode.OFF)

    assert is_on_after_delay(sensor, freezer) == (False, True)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown
