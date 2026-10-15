import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.config_flow import SatFlowHandler, SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MODE,
    CONF_NAME,
    DOMAIN,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
)


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
    flow = SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: mode, CONF_NAME: "Test"}))

    result = await flow.async_step_system_configuration()

    assert (CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR in result["data_schema"].schema) is shown
