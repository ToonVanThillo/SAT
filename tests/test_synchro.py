"""Characterization tests for the synchro binary sensors.

The binary_sensor platform setup fails intermittently (`KeyError: 'climate'`): a platform setup race, it can run before the climate
platform has stored the climate entity. It is left unfixed for now, so the sensors are constructed directly and the base components are
set up first to make the entry setup deterministic. Their on-delay is driven through the `freezer` fixture.
"""
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.climate import HVACMode

from custom_components.sat.binary_sensor import (
    SatCentralHeatingSynchroSensor,
    SatControlSetpointSynchroSensor,
    SatRelativeModulationSynchroSensor,
)
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MAXIMUM_SETPOINT,
)
from tests.conftest import advance
from tests.const import IDLE, ems_entry

pytestmark = pytest.mark.usefixtures("loop_entry")


def is_on_after_delay(sensor: BinarySensorEntity, freezer: FrozenDateTimeFactory) -> tuple[bool, bool]:
    """Return the sensor state (on at any point before the delay, on once the 60 second delay has passed)."""
    first = bool(sensor.is_on)
    freezer.tick(59)
    before = bool(sensor.is_on)
    freezer.tick(1)

    return first or before, bool(sensor.is_on)


@pytest.mark.parametrize(*ems_entry({CONF_MAXIMUM_SETPOINT: 30}))
async def test_control_setpoint_synchro_turns_on_above_maximum_setpoint(hass, entry, climate, coordinator, boiler_data, freezer, published):
    sensor = SatControlSetpointSynchroSensor(coordinator, entry, climate)
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    # The clamped value is sent correctly, and the boiler reports it back
    assert ("ems-esp/boiler", '{"cmd": "selflowtemp", "value": 30.0}', 1, False) in published()
    await boiler_data(selflowtemp=30)

    # current behaviour, known bug: the unclamped climate setpoint is compared with the coordinator setpoint
    assert climate.setpoint == 35.0
    assert coordinator.setpoint == 30.0
    assert is_on_after_delay(sensor, freezer) == (False, True)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*ems_entry())
async def test_control_setpoint_synchro_stays_off_when_values_match(hass, entry, climate, coordinator, boiler_data, freezer):
    sensor = SatControlSetpointSynchroSensor(coordinator, entry, climate)
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    await boiler_data(selflowtemp=35.0)

    assert is_on_after_delay(sensor, freezer) == (False, False)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31}))
async def test_relative_modulation_synchro_stays_on_with_floor_option(hass, entry, climate, coordinator, boiler_data, freezer):
    sensor = SatRelativeModulationSynchroSensor(coordinator, entry, climate)
    await boiler_data(**IDLE | {"burnmaxpower": 100})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    await boiler_data(burnmaxpower=31)

    # current behaviour, known bug: the coordinator sends the floor 31 while the climate requests 0, and the boiler reads back 31
    assert climate.relative_modulation_value == 0
    assert coordinator.maximum_relative_modulation_value == 31
    assert is_on_after_delay(sensor, freezer) == (False, True)

    freezer.tick(3600)
    assert sensor.is_on is True
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*ems_entry())
async def test_central_heating_synchro_never_turns_on_in_heat_mode(hass, entry, climate, coordinator, boiler_data, freezer):
    sensor = SatCentralHeatingSynchroSensor(coordinator, entry, climate)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # current behaviour, known bug: hvac_action is derived from device_active, so in HEAT mode the sensor can never detect a mismatch
    for heating_active in ("off", "on", "off"):
        await boiler_data(**IDLE | {"heatingactive": heating_active})
        assert is_on_after_delay(sensor, freezer) == (False, False)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown


@pytest.mark.parametrize(*ems_entry())
async def test_central_heating_synchro_turns_on_when_off_but_device_active(hass, entry, climate, coordinator, boiler_data, freezer):
    sensor = SatCentralHeatingSynchroSensor(coordinator, entry, climate)
    await boiler_data(**IDLE | {"heatingactive": "on"})
    await climate.async_set_hvac_mode(HVACMode.OFF)

    assert is_on_after_delay(sensor, freezer) == (False, True)
    await advance(hass, freezer, 0)  # run the debounced loop before teardown
