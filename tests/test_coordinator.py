"""Characterization tests for SatDataUpdateCoordinator and the SatMqttCoordinator plumbing."""
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import callback

from custom_components.sat.const import CONF_MQTT_TOPIC, CONF_SIMULATION
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.mqtt.opentherm import SatOpenThermMqttCoordinator
from tests.conftest import advance
from tests.const import STORE_KEY


def subscribed(mqtt_mock, prefix: str) -> list[str]:
    """Return the subscribed topics under `prefix`, ignoring discovery and the manifest's wildcard subscription."""
    return [call.args[0] for call in mqtt_mock.async_subscribe.call_args_list if call.args[0].startswith(prefix) and not call.args[0].endswith("#")]


# Publishing

async def test_publish_waits_one_second_after_publish(hass, mqtt_mock):
    coordinator = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"}, config_entry=None)

    with patch("custom_components.sat.mqtt.asyncio.sleep", new=AsyncMock()) as sleep:
        await coordinator.async_set_control_max_setpoint(55)

    assert [call.args for call in sleep.call_args_list] == [(1.0,)]


async def test_publish_failure_is_swallowed_and_setter_completes(hass, mqtt_mock, no_publish_delay, caplog):
    coordinator = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"}, config_entry=None)
    mqtt_mock.async_publish.side_effect = Exception("broker down")

    # current behaviour, known bug: a failed publish is only logged, the caller is told nothing
    with caplog.at_level(logging.INFO):
        await coordinator.async_set_control_setpoint(55)

    assert "Failed to publish MQTT command. Error: broker down" in caplog.text
    assert "Set control boiler setpoint to 55" in caplog.text


async def test_publish_in_simulation_publishes_nothing(hass, mqtt_mock, no_publish_delay):
    coordinator = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"}, {CONF_SIMULATION: True}, config_entry=None)

    await coordinator.async_set_control_setpoint(55)
    await coordinator.async_set_heater_state(DeviceState.ON)

    mqtt_mock.async_publish.assert_not_called()


# Startup

async def test_startup_subscribes_each_key(hass, mqtt_mock, no_publish_delay):
    coordinator = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"}, config_entry=None)
    await coordinator.async_added_to_hass()

    # MaxTSet (the maximum control setpoint reported by the gateway) is not among the subscribed keys
    assert subscribed(mqtt_mock, "otgw/") == [f"otgw/value/gw1/{key}" for key in [
        "slave_memberid_code",
        "centralheating",
        "flame",
        "domestichotwater",
        "TdhwSet",
        "TSet",
        "RelModLevel",
        "Tboiler",
        "Tret",
        "MaxCapacityMinModLevel_hb_u8",
        "MaxCapacityMinModLevel_lb_u8",
        "MaxCapacityMinModLevell_lb_u8",
        "MaxRelModLevelSetting",
        "TdhwSetUBTdhwSetLB_value_lb",
        "TdhwSetUBTdhwSetLB_value_hb",
    ]]


async def test_startup_ems_subscribes_once(hass, mqtt_mock, no_publish_delay):
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, config_entry=None)
    await coordinator.async_added_to_hass()

    assert subscribed(mqtt_mock, "ems-esp/") == ["ems-esp/boiler_data"]


async def test_startup_opentherm_subscribes_before_boot_then_sends_max_setpoint(hass, mqtt_mock, published, no_publish_delay):
    coordinator = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"}, config_entry=None)
    mqtt_mock.reset_mock()
    await coordinator.async_added_to_hass()

    assert [call[0] for call in mqtt_mock.mock_calls if call[0] in ("async_subscribe", "async_publish")] == ["async_subscribe"] * 15 + ["async_publish"] * 4
    assert [call[1] for call in published()] == ["PM=3", "PM=15", "PM=48", "SH=55.0"]


# Store

async def test_restore_drops_none_and_empty_values(hass, hass_storage, ems):
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"selflowtemp": 50, "burngas": "", "rettemp": None, "heatingactive": "off"}}

    await ems.async_setup()

    assert dict(ems.data) == {"selflowtemp": 50, "heatingactive": "off"}


async def test_restore_without_stored_data(ems):
    await ems.async_setup()

    assert dict(ems.data) == {}


async def test_data_is_saved_only_on_removal(hass, hass_storage, ems, boiler_data, freezer):
    await boiler_data(selflowtemp=55)
    await advance(hass, freezer, 60)

    assert STORE_KEY not in hass_storage

    await ems.async_will_remove_from_hass()

    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 55}


# Listener debounce

@pytest.fixture
def listener(ems) -> MagicMock:
    notified = MagicMock()
    ems.async_add_listener(callback(notified))
    return notified


async def test_dirty_update_notifies_after_five_seconds(hass, listener, boiler_data, freezer):
    await boiler_data(selflowtemp=55)

    await advance(hass, freezer, 4)
    listener.assert_not_called()

    await advance(hass, freezer, 1)
    listener.assert_called_once()


async def test_non_dirty_update_schedules_nothing(hass, listener, boiler_data, freezer):
    await boiler_data(selflowtemp=55)
    await advance(hass, freezer, 5)
    listener.reset_mock()

    await boiler_data(selflowtemp=55)
    await advance(hass, freezer, 60)

    listener.assert_not_called()


async def test_empty_update_never_notifies(hass, listener, boiler_data, freezer):
    await boiler_data()
    await advance(hass, freezer, 60)

    listener.assert_not_called()


async def test_new_dirty_update_reschedules_the_notify(hass, listener, boiler_data, freezer):
    await boiler_data(selflowtemp=55)
    await advance(hass, freezer, 4)

    await boiler_data(selflowtemp=56)
    await advance(hass, freezer, 2)
    listener.assert_not_called()

    await advance(hass, freezer, 3)
    listener.assert_called_once()


async def test_changes_faster_than_five_seconds_never_notify(hass, listener, boiler_data, freezer):
    # current behaviour, known bug: every change cancels the pending notify, so entities starve
    for value in range(50, 60):
        await boiler_data(selflowtemp=value)
        await advance(hass, freezer, 4)

    listener.assert_not_called()


# async_control_heating_loop

async def test_loop_without_temperature_returns_early(ems, boiler_data, freezer):
    await boiler_data(heatingactive="on")

    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_cold is None
    assert ems.boiler_temperature_derivative is None
    # The device state is tracked before the early return
    assert ems.heater_on_since == time.monotonic()


async def test_loop_ignores_duplicate_samples(ems, boiler_data, freezer):
    await boiler_data(curflowtemp=30)
    await ems.async_control_heating_loop()
    await ems.async_control_heating_loop()

    freezer.tick(10)
    await boiler_data(curflowtemp=32)
    await ems.async_control_heating_loop()

    # Mean absolute difference of the samples (30, 32), a duplicate 30 would give 1.0
    assert ems.boiler_temperature_filtered == 2.0


async def test_loop_prunes_samples_older_than_max_age(ems, boiler_data, freezer):
    await boiler_data(curflowtemp=30)
    await ems.async_control_heating_loop()

    # A sample of exactly MAX_BOILER_TEMPERATURE_AGE seconds old is kept
    freezer.tick(60)
    await boiler_data(curflowtemp=40)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_filtered == 10.0

    # Now the first sample is pruned, otherwise the result would be 5.0
    freezer.tick(0.5)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_filtered == 0.0


async def test_loop_tracks_cold_temperature(ems, boiler_data, freezer):
    await boiler_data(curflowtemp=30)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_cold == 30

    # Device switches on: only samples from before that moment count as cold
    freezer.tick(10)
    await boiler_data(curflowtemp=35, heatingactive="on")
    await ems.async_control_heating_loop()
    freezer.tick(10)
    await boiler_data(curflowtemp=40)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_cold == 30

    # All earlier samples have expired, so the cold temperature can only decrease
    freezer.tick(70)
    await boiler_data(curflowtemp=25)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_cold == 25


async def test_loop_keeps_cold_temperature_when_boiler_warms_after_expiry(ems, boiler_data, freezer):
    await boiler_data(curflowtemp=30)
    await ems.async_control_heating_loop()
    freezer.tick(10)
    await boiler_data(curflowtemp=35, heatingactive="on")
    await ems.async_control_heating_loop()

    freezer.tick(80)
    await boiler_data(curflowtemp=45)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_cold == 30


async def test_loop_tracks_device_on_since(ems, boiler_data, freezer):
    assert ems.heater_on_since is None

    started = time.monotonic()
    await boiler_data(heatingactive="on")
    await ems.async_control_heating_loop()
    freezer.tick(30)
    await ems.async_control_heating_loop()

    assert ems.heater_on_since == started

    await boiler_data(heatingactive="off")
    await ems.async_control_heating_loop()

    assert ems.heater_on_since is None


# boiler_temperature_filtered

async def test_filtered_returns_raw_temperature_with_fewer_than_two_samples(ems, boiler_data):
    assert ems.boiler_temperature_filtered is None

    await boiler_data(curflowtemp=48.5)
    assert ems.boiler_temperature_filtered == 48.5

    await ems.async_control_heating_loop()
    assert ems.boiler_temperature_filtered == 48.5


async def test_filtered_is_mean_absolute_difference_of_samples(ems, boiler_data, freezer):
    # Observation: the filtered temperature is not a smoothed temperature but a noise metric
    for temperature in [30, 32, 31, 36]:
        await boiler_data(curflowtemp=temperature)
        await ems.async_control_heating_loop()
        freezer.tick(1)

    assert ems.boiler_temperature_filtered == 2.67
