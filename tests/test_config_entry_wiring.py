"""The coordinator's config entry is passed explicitly: the setup entry's coordinator has it, temporary ones do not."""
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.const import (
    CONF_MODE,
    CONF_MQTT_TOPIC,
    DOMAIN,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
    MODE_SERIAL,
    MODE_SWITCH,
)
from custom_components.sat.coordinator import SatDataUpdateCoordinatorFactory
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.const import ems_entry, otgw_entry

MODES = [(MODE_FAKE, "device"), (MODE_MQTT_EMS, "ems"), (MODE_MQTT_OPENTHERM, "gw1"), (MODE_SWITCH, "switch.boiler"), (MODE_SERIAL, "socket://127.0.0.1:1")]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_setup_entry_coordinator_has_its_config_entry_on_ems(entry, coordinator):
    assert coordinator.config_entry is entry


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_setup_entry_coordinator_has_its_config_entry_on_opentherm(entry, coordinator):
    assert coordinator.config_entry is entry


@pytest.mark.parametrize("mode, device", MODES)
@pytest.mark.parametrize("with_entry", [True, False])
async def test_factory_passes_the_config_entry_to_the_backend(hass, mqtt_mock, mode, device, with_entry):
    config_entry = MockConfigEntry(domain=DOMAIN) if with_entry else None

    coordinator = SatDataUpdateCoordinatorFactory.resolve(hass, mode, device, {CONF_MODE: mode, CONF_MQTT_TOPIC: "topic"}, config_entry=config_entry)

    assert coordinator.config_entry is config_entry


async def test_config_entry_cannot_be_omitted(hass, mqtt_mock):
    with pytest.raises(TypeError):
        SatDataUpdateCoordinatorFactory.resolve(hass, MODE_FAKE, "device", {})

    with pytest.raises(TypeError):
        SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "topic"})
