"""Characterization tests for the EMS MQTT coordinator."""
import logging

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.const import (
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MQTT_TOPIC,
)
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator

TOPIC = "ems-esp"
BOILER_DATA = (
    '{"selflowtemp": 55, "curflowtemp": 48.5, "rettemp": 41.2, "curburnpow": 37, "burnmaxpower": 80,'
    ' "burnminpower": 20, "nompower": 24, "heatingactive": "on", "burngas": "on", "tapactivated": "off",'
    ' "dhw/seltemp": 52}'
)


async def feed(hass: HomeAssistant, payload: str) -> None:
    async_fire_mqtt_message(hass, f"{TOPIC}/boiler_data", payload)
    await hass.async_block_till_done()


async def test_reads_map_boiler_data(hass, ems):
    await feed(hass, BOILER_DATA)

    assert ems.setpoint == 55
    assert ems.boiler_temperature == 48.5
    assert ems.return_temperature == 41.2
    assert ems.relative_modulation_value == 37
    assert ems.maximum_relative_modulation_value == 80
    assert ems.minimum_relative_modulation_value == 20
    assert ems.boiler_capacity == 24
    assert ems.hot_water_setpoint == 52
    assert ems.device_active is True
    assert ems.flame_active is True
    assert ems.hot_water_active is False


async def test_flags_off_and_missing(hass, ems):
    assert ems.device_active is False
    assert ems.flame_active is False
    assert ems.hot_water_active is False
    assert ems.setpoint is None

    await feed(hass, '{"heatingactive": "off", "burngas": "off", "tapactivated": "on"}')

    assert ems.device_active is False
    assert ems.flame_active is False
    assert ems.hot_water_active is True


async def test_boiler_data_is_merged(hass, ems):
    await feed(hass, '{"selflowtemp": 55}')
    await feed(hass, '{"rettemp": 40}')

    assert ems.setpoint == 55
    assert ems.return_temperature == 40


async def test_invalid_json_is_logged_and_ignored(hass, ems, caplog):
    await feed(hass, "{not json")

    assert "Failed to decode JSON payload" in caplog.text
    assert dict(ems.data) == {}


async def test_non_object_json_is_logged_by_handler(hass, ems, caplog):
    with caplog.at_level(logging.ERROR):
        await feed(hass, "[1, 2]")

    assert "Failed to process message for key 'boiler_data'" in caplog.text
    assert dict(ems.data) == {}


async def test_flags_and_member_id(ems):
    assert ems.device_type == "Energy Management System (via mqtt)"
    assert ems.supports_setpoint_management is True
    assert ems.supports_hot_water_setpoint_management is True
    assert ems.supports_maximum_setpoint_management is True
    assert ems.supports_relative_modulation is True
    assert ems.supports_relative_modulation_management is True
    assert ems.member_id is None


@pytest.mark.parametrize("method, value, payload", [
    ("async_set_control_setpoint", 55, '{"cmd": "selflowtemp", "value": 55}'),
    ("async_set_control_setpoint", 45.5, '{"cmd": "selflowtemp", "value": 45.5}'),
    # EMS quirk: the absolute minimum setpoint of 10 is sent as 0
    ("async_set_control_setpoint", 10, '{"cmd": "selflowtemp", "value": 0}'),
    ("async_set_control_hot_water_setpoint", 50, '{"cmd": "dhw/seltemp", "value": 50}'),
    ("async_set_control_max_setpoint", 70, '{"cmd": "heatingtemp", "value": 70}'),
    ("async_set_control_max_relative_modulation", 60, '{"cmd": "burnmaxpower", "value": 60}'),
    # Without the floor option there is no floor
    ("async_set_control_max_relative_modulation", 0, '{"cmd": "burnmaxpower", "value": 0}'),
])
async def test_writes(ems, published, method, value, payload):
    await getattr(ems, method)(value)

    assert published() == [(f"{TOPIC}/boiler", payload, 1, False)]


@pytest.mark.parametrize("value, sent", [(0, 31), (30, 31), (31, 31), (60, 60)])
async def test_max_relative_modulation_is_raised_to_the_floor_option(hass, mqtt_mock, published, no_publish_delay, value, sent):
    # A float floor, as the options flow stores it, is published as an integer
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: TOPIC}, {CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31.0})

    await coordinator.async_set_control_max_relative_modulation(value)

    assert published() == [(f"{TOPIC}/boiler", f'{{"cmd": "burnmaxpower", "value": {sent}}}', 1, False)]


async def test_thermostat_setpoint_publishes_nothing(ems, published):
    await ems.async_set_control_thermostat_setpoint(20)

    assert published() == []


@pytest.mark.parametrize("state, value", [(DeviceState.ON, "off"), (DeviceState.OFF, "on")])
async def test_heater_state_is_inverted(ems, published, state, value):
    await ems.async_set_heater_state(state)

    assert published() == [(f"{TOPIC}/boiler", f'{{"cmd": "heatingoff", "value": "{value}"}}', 1, False)]


async def test_startup_sends_max_setpoint(hass, mqtt_mock, published, no_publish_delay):
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: TOPIC})
    await coordinator.async_added_to_hass()

    assert published() == [(f"{TOPIC}/boiler", '{"cmd": "heatingtemp", "value": 55.0}', 1, False)]
