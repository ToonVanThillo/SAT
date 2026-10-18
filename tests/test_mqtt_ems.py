"""Characterization tests for the EMS MQTT coordinator."""
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import ChannelStatus, DesiredState, Quantity
from custom_components.sat.const import (
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MQTT_TOPIC,
    CONF_SIMULATION,
)
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.const import EMS_BOILER_DATA

TOPIC = "ems-esp"
BOILER_DATA = json.dumps(EMS_BOILER_DATA | {
    "selflowtemp": 55, "curflowtemp": 48.5, "rettemp": 41.2, "curburnpow": 37, "burnmaxpower": 80, "burnminpower": 20, "heatingactive": "on", "burngas": "on",
})


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
    assert ems.device_active is True
    assert ems.flame_active is True
    assert ems.hot_water_active is False


async def test_flags_off_and_missing(hass, ems):
    assert ems.device_active is False
    assert ems.flame_active is False
    assert ems.hot_water_active is False
    assert ems.setpoint is None

    await feed(hass, '{"heatingactive": "off", "burngas": "off", "tapwateractive": "on"}')

    assert ems.device_active is False
    assert ems.flame_active is False
    assert ems.hot_water_active is True


async def test_tap_water_setting_is_not_hot_water_active(hass, ems):
    await feed(hass, '{"tapwateractive": "off", "dhw": {"tapactivated": "on"}}')

    assert ems.hot_water_active is False


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
    assert ems.supports_hot_water_setpoint_management is False
    assert ems.supports_maximum_setpoint_management is True
    assert ems.supports_relative_modulation is True
    assert ems.supports_relative_modulation_management is True
    assert ems.member_id is None


# Channels

def command(payload: str) -> tuple:
    return f"{TOPIC}/boiler", payload, 1, False


def status(coordinator: SatEmsMqttCoordinator, quantity: Quantity) -> ChannelStatus:
    return coordinator._channels.states[quantity].status


async def start(hass: HomeAssistant, options: dict) -> SatEmsMqttCoordinator:
    """Return a started EMS coordinator with the given options."""
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: TOPIC}, options)
    await coordinator.async_added_to_hass()

    return coordinator


async def cycle(coordinator: SatEmsMqttCoordinator, desired: DesiredState) -> None:
    """Run the channel part of a control cycle: verify against a new snapshot, then submit."""
    coordinator.verify(coordinator.snapshot())
    await coordinator.async_submit(desired)


# The capture reads back selflowtemp 5, burnmaxpower 100, heatingoff "off" and heatingtemp 55 (the default maximum setpoint)
@pytest.mark.parametrize("desired, payload", [
    (DesiredState(control_setpoint=55), '{"cmd": "selflowtemp", "value": 55}'),
    # EMS-ESP takes whole degrees
    (DesiredState(control_setpoint=35.4), '{"cmd": "selflowtemp", "value": 35}'),
    (DesiredState(control_setpoint=45.6), '{"cmd": "selflowtemp", "value": 46}'),
    # EMS quirk: the absolute minimum setpoint of 10 is sent as 0
    (DesiredState(control_setpoint=10), '{"cmd": "selflowtemp", "value": 0}'),
    (DesiredState(maximum_setpoint=70.0), '{"cmd": "heatingtemp", "value": 70}'),
    (DesiredState(maximum_relative_modulation=60), '{"cmd": "burnmaxpower", "value": 60}'),
    # Without the floor option there is no floor
    (DesiredState(maximum_relative_modulation=0), '{"cmd": "burnmaxpower", "value": 0}'),
])
async def test_writes(ems, boiler_data, published, desired, payload):
    await boiler_data(**EMS_BOILER_DATA)

    await cycle(ems, desired)

    assert published() == [command(payload)]


@pytest.mark.parametrize("value, sent", [(0, 31), (30, 31), (31, 31), (60, 60)])
async def test_max_relative_modulation_is_raised_to_the_floor_option(hass, mqtt_mock, no_publish_delay, boiler_data, published, value, sent):
    # A float floor, as the options flow stores it, is published as an integer
    coordinator = await start(hass, {CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31.0})
    await boiler_data(**EMS_BOILER_DATA)

    await cycle(coordinator, DesiredState(maximum_relative_modulation=value))

    assert published() == [command(f'{{"cmd": "burnmaxpower", "value": {sent}}}')]


async def test_thermostat_setpoint_publishes_nothing(ems, boiler_data, published):
    await boiler_data(**EMS_BOILER_DATA)

    await cycle(ems, DesiredState(thermostat_setpoint=20))

    assert published() == []


@pytest.mark.parametrize("state, setting, value", [(DeviceState.ON, "on", "off"), (DeviceState.OFF, "off", "on")])
async def test_heater_state_is_inverted(ems, boiler_data, published, state, setting, value):
    await boiler_data(**EMS_BOILER_DATA | {"heatingoff": setting})

    await cycle(ems, DesiredState(heater_state=state))

    assert published() == [command(f'{{"cmd": "heatingoff", "value": "{value}"}}')]


@pytest.mark.parametrize("quantity, value, data", [
    (Quantity.CONTROL_SETPOINT, 35.4, {"selflowtemp": 35}),
    (Quantity.CONTROL_SETPOINT, 35, {"selflowtemp": 35.0}),
    (Quantity.CONTROL_SETPOINT, 10, {"selflowtemp": 0}),
    (Quantity.MAXIMUM_RELATIVE_MODULATION, 60, {"burnmaxpower": 60}),
    (Quantity.MAXIMUM_SETPOINT, 70.0, {"heatingtemp": 70}),
    (Quantity.HEATER_STATE, DeviceState.ON, {"heatingoff": "off"}),
    (Quantity.HEATER_STATE, DeviceState.OFF, {"heatingoff": "on"}),
])
async def test_matching_read_back_is_adopted_without_publishing(ems, boiler_data, published, quantity, value, data):
    await boiler_data(**EMS_BOILER_DATA | data)

    await cycle(ems, DesiredState(**{quantity.value: value}))

    assert published() == []
    assert status(ems, quantity) == ChannelStatus.IN_SYNC


@pytest.mark.parametrize("data", [{}, {"selflowtemp": "unavailable"}, {"selflowtemp": None}])
async def test_first_write_waits_for_a_readable_read_back(ems, boiler_data, published, data):
    await boiler_data(curflowtemp=40, **data)
    await cycle(ems, DesiredState(control_setpoint=40))
    assert published() == []

    await boiler_data(selflowtemp=5)
    await cycle(ems, DesiredState())
    assert published() == [command('{"cmd": "selflowtemp", "value": 40}')]


async def test_first_write_waits_for_a_live_read_back(ems, boiler_data, published):
    ems.async_set_updated_data(EMS_BOILER_DATA, restored=True)
    await cycle(ems, DesiredState(control_setpoint=40))
    assert published() == []

    await boiler_data(**EMS_BOILER_DATA)
    await cycle(ems, DesiredState())
    assert published() == [command('{"cmd": "selflowtemp", "value": 40}')]


# Startup

async def test_startup_adopts_a_matching_max_setpoint(hass, mqtt_mock, no_publish_delay, boiler_data, published):
    coordinator = await start(hass, {})
    assert published() == []

    # The capture's heatingtemp equals the default maximum setpoint of 55
    await boiler_data(**EMS_BOILER_DATA)
    await cycle(coordinator, DesiredState())

    assert published() == []
    assert status(coordinator, Quantity.MAXIMUM_SETPOINT) == ChannelStatus.IN_SYNC


async def test_startup_sends_a_differing_max_setpoint_after_the_first_boiler_data(hass, mqtt_mock, no_publish_delay, boiler_data, published):
    coordinator = await start(hass, {})
    assert published() == []

    await boiler_data(**EMS_BOILER_DATA | {"heatingtemp": 70})
    await cycle(coordinator, DesiredState())

    assert published() == [command('{"cmd": "heatingtemp", "value": 55}')]


# Publishing

async def test_channel_write_waits_one_second_after_publish(ems, boiler_data):
    await boiler_data(**EMS_BOILER_DATA)

    with patch("custom_components.sat.mqtt.asyncio.sleep", new=AsyncMock()) as sleep:
        await cycle(ems, DesiredState(control_setpoint=40))

    assert [call.args for call in sleep.call_args_list] == [(1.0,)]


async def test_publish_failure_leaves_the_channel_out_of_sync_and_is_retried(ems, boiler_data, mqtt_mock, published, caplog):
    async def fail_heater(topic: str, payload: str, qos: int, retain: bool) -> None:
        if "heatingoff" in payload:
            raise HomeAssistantError("broker down")

    await boiler_data(**EMS_BOILER_DATA | {"heatingoff": "on"})
    desired = DesiredState(control_setpoint=40, heater_state=DeviceState.ON, maximum_setpoint=70)
    mqtt_mock.async_publish.side_effect = fail_heater

    # The channels before and after the failing one are still written
    await cycle(ems, desired)
    assert published() == [
        command('{"cmd": "selflowtemp", "value": 40}'),
        command('{"cmd": "heatingoff", "value": "off"}'),
        command('{"cmd": "heatingtemp", "value": 70}'),
    ]
    failures = [record.getMessage() for record in caplog.records if record.getMessage().startswith("Failed to write")]
    assert len(failures) == 1 and failures[0].endswith("to channel heater_state")
    assert "Failed to publish MQTT command" not in caplog.text
    assert [status(ems, quantity) for quantity in (Quantity.CONTROL_SETPOINT, Quantity.HEATER_STATE, Quantity.MAXIMUM_SETPOINT)] == [
        ChannelStatus.PENDING, ChannelStatus.OUT_OF_SYNC, ChannelStatus.PENDING,
    ]

    # The next cycle retries it
    mqtt_mock.async_publish.side_effect = None
    mqtt_mock.async_publish.reset_mock()
    await cycle(ems, desired)
    assert published() == [
        command('{"cmd": "selflowtemp", "value": 40}'),
        command('{"cmd": "heatingoff", "value": "off"}'),
    ]


async def test_simulation_publishes_nothing(hass, mqtt_mock, no_publish_delay, boiler_data, caplog):
    coordinator = await start(hass, {CONF_SIMULATION: True})
    await boiler_data(**EMS_BOILER_DATA | {"heatingoff": "on"})

    await cycle(coordinator, DesiredState(control_setpoint=40, maximum_relative_modulation=60, heater_state=DeviceState.ON, maximum_setpoint=70))

    # The writes went through the channels, which take them as sent
    mqtt_mock.async_publish.assert_not_called()
    assert "Failed to write" not in caplog.text
    assert {state.status for state in coordinator._channels.states.values()} == {ChannelStatus.PENDING}
