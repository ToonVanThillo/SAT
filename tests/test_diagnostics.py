"""Tests for the diagnostics download, checked through the JSON as Home Assistant serves it."""
import json
from datetime import datetime
from types import MappingProxyType

import pytest
from homeassistant.helpers.discovery_flow import DiscoveryKey
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.sat.channels import DesiredState
from custom_components.sat.const import CONF_DEVICE
from tests.const import EMS_BOILER_DATA, IDLE, ems_entry, otgw_entry
from tests.test_channel_sensors import started, step
from tests.test_mqtt_ems import cycle

pytestmark = pytest.mark.usefixtures("loop_entry")

CHANNELS = {"control_setpoint", "heater_state", "maximum_relative_modulation", "maximum_setpoint"}
FIELDS = {"status", "target", "sent", "sent_at", "read_back", "read_back_at", "drift_count", "last_drift_at", "last_out_of_sync_at", "diagnostic_status"}


# EMS-ESP

@pytest.mark.parametrize(*ems_entry())
async def test_ems_lists_every_channel(hass, hass_client, entry):
    channels = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["channels"]

    assert set(channels) == CHANNELS
    assert all(set(channel) == FIELDS for channel in channels.values())


@pytest.mark.parametrize(*ems_entry())
async def test_confirmed_write_is_in_sync_with_iso_times(hass, hass_client, entry, coordinator, boiler_data, freezer):
    desired = DesiredState(control_setpoint=40)
    await boiler_data(**EMS_BOILER_DATA)
    await cycle(coordinator, desired)
    freezer.tick(1)
    await boiler_data(selflowtemp=40)
    await cycle(coordinator, desired)

    channel = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["channels"]["control_setpoint"]

    assert channel["status"] == channel["diagnostic_status"] == "in_sync"
    assert channel["target"] == channel["sent"] == channel["read_back"] == 40
    assert channel["drift_count"] == 0
    assert channel["last_drift_at"] is None
    assert channel["last_out_of_sync_at"] is None
    for key in ("sent_at", "read_back_at"):
        assert isinstance(datetime.fromisoformat(channel[key]), datetime)


@pytest.mark.parametrize(*ems_entry())
async def test_confirmed_drift_still_shows_as_drifted(hass, hass_client, entry, coordinator, climate, boiler_data, freezer):
    value = await started(coordinator, climate, boiler_data)
    await step(climate, boiler_data, freezer, selflowtemp=value)
    await step(climate, boiler_data, freezer, selflowtemp=value + 10)
    await step(climate, boiler_data, freezer, selflowtemp=value)

    channel = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["channels"]["control_setpoint"]

    assert channel["status"] == "in_sync"
    assert channel["diagnostic_status"] == "drifted"
    assert channel["drift_count"] == 1
    assert isinstance(datetime.fromisoformat(channel["last_drift_at"]), datetime)


@pytest.mark.parametrize(*ems_entry())
async def test_enum_values_are_strings(hass, hass_client, entry, coordinator, climate, boiler_data):
    await started(coordinator, climate, boiler_data)

    channels = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["channels"]

    assert channels["heater_state"]["target"] == "on"
    assert channels["heater_state"]["sent"] == "on"
    assert channels["heater_state"]["status"] == "pending"
    assert channels["heater_state"]["diagnostic_status"] == "in_sync"


@pytest.mark.parametrize(*ems_entry())
async def test_ems_readings_are_live(hass, hass_client, entry, boiler_data):
    await boiler_data(**IDLE)

    readings = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["readings"]

    assert readings["selflowtemp"]["value"] == 40
    assert readings["selflowtemp"]["restored"] is False
    assert isinstance(datetime.fromisoformat(readings["selflowtemp"]["received_at"]), datetime)
    assert set(IDLE) >= set(readings) >= {"selflowtemp", "rettemp"}


@pytest.mark.parametrize(*ems_entry())
async def test_restored_reading_has_no_time(hass, hass_client, entry, coordinator):
    coordinator.async_set_updated_data(EMS_BOILER_DATA, restored=True)

    reading = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["readings"]["selflowtemp"]

    assert reading == {"value": EMS_BOILER_DATA["selflowtemp"], "received_at": None, "restored": True}


# Backend without channels

@pytest.mark.parametrize(*otgw_entry())
async def test_backend_without_channels(hass, hass_client, entry):
    async_fire_mqtt_message(hass, "otgw/value/gw1/TSet", "55.5")
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["channels"] == {}
    assert diagnostics["entry"]["data"]["mqtt_topic"] == "otgw"
    reading = diagnostics["readings"]["TSet"]
    assert reading["value"] == "55.5"
    assert reading["restored"] is False
    assert isinstance(datetime.fromisoformat(reading["received_at"]), datetime)


# Redaction

@pytest.mark.parametrize(*ems_entry())
async def test_identifying_addresses_are_redacted(hass, hass_client, entry):
    mac = "aabbccddeeff"
    hass.config_entries.async_update_entry(
        entry,
        unique_id=mac,
        discovery_keys=MappingProxyType({"dhcp": (DiscoveryKey(domain="dhcp", key=mac, version=1),)}),
    )

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"]["unique_id"] == "**REDACTED**"
    assert diagnostics["entry"]["discovery_keys"] == "**REDACTED**"
    assert diagnostics["entry"]["data"][CONF_DEVICE] == "**REDACTED**"
    assert mac not in json.dumps(diagnostics)
    assert diagnostics["entry"]["data"]["mqtt_topic"] == "ems-esp"
    assert diagnostics["entry"]["data"]["inside_sensor_entity_id"] == "sensor.test_inside_sensor"
    assert diagnostics["entry"]["data"]["name"] == "Test"
