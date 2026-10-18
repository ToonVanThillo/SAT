import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.config_flow import SatFlowHandler, SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MODE,
    CONF_NAME,
    DOMAIN,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
)


def options_flow(mode: str) -> SatOptionsFlowHandler:
    return SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: mode, CONF_NAME: "Test"}))


def selector_max(result: dict, key: str) -> float:
    return next(value for marker, value in result["data_schema"].schema.items() if marker == key).config["max"]


async def test_create_coordinator(hass):
    flow_handler = SatFlowHandler()
    flow_handler.data = {
        "name": "Test",
        "mode": MODE_FAKE,
        "device": "test_device",
    }

    await flow_handler.async_create_coordinator()


@pytest.mark.parametrize("mode, shown", [(MODE_MQTT_EMS, True), (MODE_MQTT_OPENTHERM, False)])
async def test_system_configuration_shows_modulation_floor_only_for_ems(hass, mode, shown):
    result = await options_flow(mode).async_step_system_configuration()

    assert (CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR in result["data_schema"].schema) is shown


async def test_modulation_floor_allows_up_to_254(hass):
    result = await options_flow(MODE_MQTT_EMS).async_step_system_configuration()

    assert selector_max(result, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR) == 254


@pytest.mark.parametrize("mode, maximum", [(MODE_MQTT_EMS, 254), (MODE_MQTT_OPENTHERM, 100)])
async def test_maximum_relative_modulation_allows_above_100_only_for_ems(hass, mode, maximum):
    result = await options_flow(mode).async_step_advanced()

    assert selector_max(result, CONF_MAXIMUM_RELATIVE_MODULATION) == maximum

