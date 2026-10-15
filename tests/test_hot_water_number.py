"""The hot water setpoint number entity exists only for backends that support it."""
import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.sat.const import CONF_MODE, CONF_MQTT_TOPIC, MODE_MQTT_OPENTHERM
from tests.const import ems_entry


def opentherm_entry() -> tuple[str, list[tuple]]:
    names, [(domains, data, options, config)] = ems_entry()
    return names, [(domains, data | {CONF_MODE: MODE_MQTT_OPENTHERM, CONF_MQTT_TOPIC: "otgw"}, options, config)]


def numbers(hass) -> list[str]:
    return [e.entity_id for e in er.async_get(hass).entities.values() if e.domain == "number" and e.platform == "sat"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_no_hot_water_number_for_ems(hass, entry, coordinator):
    assert coordinator.supports_hot_water_setpoint_management is False
    assert numbers(hass) == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*opentherm_entry())
async def test_hot_water_number_for_opentherm(hass, entry, coordinator):
    assert coordinator.supports_hot_water_setpoint_management is True
    assert len(numbers(hass)) == 1
