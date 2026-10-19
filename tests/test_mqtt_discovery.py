"""Tests for the logging of SAT's MQTT discovery flow, and characterization tests of HA's manifest-based MQTT discovery."""
import json
import logging
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_MQTT, ConfigFlowResult
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.sat.config_flow import SatFlowHandler
from custom_components.sat.const import (
    CONF_DEVICE,
    CONF_MODE,
    DOMAIN,
    MODE_MQTT_EMS,
)


@pytest.fixture
def ems_entry(hass) -> MockConfigEntry:
    """Return a SAT EMS entry that is added but not set up."""
    config_entry = MockConfigEntry(domain=DOMAIN, unique_id="ems-esp", data={CONF_MODE: MODE_MQTT_EMS, CONF_DEVICE: "ems-esp"})
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
def discovery_flows() -> Iterator[list[str]]:
    """Return the outcome of SAT's `async_step_mqtt`, one per discovery flow: the abort reason or the form step."""
    outcomes = []
    original = SatFlowHandler.async_step_mqtt

    async def spy(self, discovery_info):
        try:
            result = await original(self, discovery_info)
        except AbortFlow as error:
            outcomes.append(error.reason)
            raise

        outcomes.append(result["step_id"])
        return result

    with patch.object(SatFlowHandler, "async_step_mqtt", spy):
        yield outcomes


@pytest.fixture
async def running(hass, mqtt_mock) -> None:
    """Start HA, otherwise discovery flows stay pending."""
    await hass.async_start()
    await hass.async_block_till_done()


def sat_logs(caplog) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in caplog.records if record.name == "custom_components.sat.config_flow"]


async def discover(hass, topic: str, payload: str = "{}") -> ConfigFlowResult:
    info = MqttServiceInfo(topic=topic, payload=payload, qos=0, retain=False, subscribed_topic="#", timestamp=0.0)
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_MQTT}, data=info)


async def publish(hass, topic: str, payload: dict) -> None:
    async_fire_mqtt_message(hass, topic, json.dumps(payload))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_ems_topic_shows_form(hass, mqtt_mock, caplog):
    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.config_flow"):
        result = await discover(hass, "ems-esp/boiler_data")

    assert sat_logs(caplog) == [
        (logging.DEBUG, "Discovered MQTT at [mqtt://ems-esp/boiler_data]"),
        (logging.DEBUG, "Identified gateway type ems: ems-esp"),
    ]

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "mosquitto_ems"

    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["unique_id"] == "ems-esp"


async def test_ems_topic_aborts_when_configured(hass, mqtt_mock, ems_entry):
    result = await discover(hass, "ems-esp/boiler_data")

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_opentherm_topic_shows_form(hass, mqtt_mock, caplog):
    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.config_flow"):
        result = await discover(hass, "OTGW/value/otgw-0123456789ab")

    assert sat_logs(caplog) == [
        (logging.DEBUG, "Discovered MQTT at [mqtt://OTGW/value/otgw-0123456789ab]"),
        (logging.DEBUG, "Identified gateway type opentherm: otgw-0123456789ab"),
    ]

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "mosquitto_opentherm"

    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["unique_id"] == "otgw-0123456789ab"


async def test_unsupported_topic_aborts_and_logs_error(hass, mqtt_mock, caplog):
    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.config_flow"):
        result = await discover(hass, "other/topic")

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unsupported_gateway"
    assert sat_logs(caplog) == [(logging.ERROR, "Unsupported MQTT topic format: other/topic")]


async def test_already_configured_abort_logs_nothing(hass, mqtt_mock, ems_entry, caplog):
    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.config_flow"):
        await discover(hass, "ems-esp/boiler_data")

    assert not sat_logs(caplog)


async def test_already_in_progress_abort_logs_nothing(hass, mqtt_mock, caplog):
    first = await discover(hass, "ems-esp/boiler_data")
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.config_flow"):
        second = await discover(hass, "ems-esp/boiler")

    assert first["type"] == FlowResultType.FORM
    assert second["type"] == FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    assert not sat_logs(caplog)


async def test_changed_payloads_start_aborting_flows(hass, running, ems_entry, discovery_flows):
    """Current behaviour, known bug: every changed message on a matching topic starts a discovery flow."""
    await publish(hass, "ems-esp/boiler_data", {"a": 1})
    await publish(hass, "ems-esp/boiler_data", {"a": 2})
    await publish(hass, "ems-esp/boiler", {"cmd": "selflowtemp"})

    assert discovery_flows == ["already_configured"] * 3


async def test_identical_payload_starts_no_new_flow(hass, running, ems_entry, discovery_flows):
    await publish(hass, "ems-esp/boiler_data", {"a": 1})
    await publish(hass, "ems-esp/boiler_data", {"a": 1})

    assert len(discovery_flows) == 1


async def test_discovery_keys_hold_last_ten_topics(hass, running, ems_entry):
    """Current behaviour, known bug: topics beyond the first ten keep changing the entry's discovery keys."""
    for index in range(10):
        await publish(hass, f"ems-esp/topic{index}", {"a": 1})

    keys = ems_entry.discovery_keys["mqtt"]
    assert len(keys) == 10

    await publish(hass, "ems-esp/topic10", {"a": 1})

    assert ems_entry.discovery_keys["mqtt"] != keys
    assert len(ems_entry.discovery_keys["mqtt"]) == 10


async def test_opentherm_value_topic_with_key_starts_no_flow(hass, running, discovery_flows):
    """Current behaviour, known bug: the manifest filter `OTGW/value/+` does not match `OTGW/value/<device>/<key>`."""
    await publish(hass, "OTGW/value/otgw-0123456789ab/boiler_temperature", {"a": 1})

    assert not discovery_flows
