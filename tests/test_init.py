"""Test setup process."""
import json
from collections.abc import Awaitable, Callable
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.discovery_flow import DiscoveryKey
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.sat.const import (
    CONF_AWAY_TEMPERATURE,
    CONF_DEVICE,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MINIMUM_SETPOINT,
    DOMAIN,
)
from tests.conftest import current
from tests.const import DEFAULT_USER_DATA, ems_entry


async def test_setup_reload_unload_entry(hass):
    """Test entry setup, reload and unload."""
    # Create the switch entry
    switch_entry = MockConfigEntry(domain=SWITCH_DOMAIN, entry_id="switch.test")
    await hass.config_entries.async_add(switch_entry)

    # Create our entity
    sat_entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)
    await hass.config_entries.async_add(sat_entry)

    # Wait till there are no tasks and see if we have been configured
    await hass.async_block_till_done()
    assert DOMAIN in hass.data and sat_entry.entry_id in hass.data[DOMAIN]

    # Reload the entry without errors
    assert await hass.config_entries.async_reload(sat_entry.entry_id)
    assert sat_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(sat_entry.entry_id)
    assert sat_entry.entry_id not in hass.data[DOMAIN]


# Reload on entry updates

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize("update", [
    {"discovery_keys": MappingProxyType({"mqtt": (DiscoveryKey(domain="mqtt", key="ems-esp/boiler_data", version=1),)})},
    {"unique_id": "ems-esp"},
    {"title": "Renamed"},
])
async def test_an_update_of_neither_data_nor_options_does_not_reload(hass, entry, coordinator, update: dict[str, Any]):
    assert hass.config_entries.async_update_entry(entry, **update)
    await hass.async_block_till_done()

    assert current(hass, entry) is coordinator


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_data_change_reloads(hass, entry, coordinator):
    hass.config_entries.async_update_entry(entry, data=entry.data | {CONF_MINIMUM_SETPOINT: 25})
    await hass.async_block_till_done()

    assert current(hass, entry) is not coordinator
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_an_options_change_reloads(hass, entry, coordinator):
    hass.config_entries.async_update_entry(entry, options=entry.options | {CONF_MAXIMUM_RELATIVE_MODULATION: 50})
    await hass.async_block_till_done()

    assert current(hass, entry) is not coordinator


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_options_flow_reloads(hass, entry, coordinator):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "presets"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_AWAY_TEMPERATURE: 12.5})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AWAY_TEMPERATURE] == 12.5
    assert current(hass, entry) is not coordinator


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_mqtt_discovery_of_a_configured_gateway_does_not_reload(hass, entry):
    # As set up by the config flow: the EMS-ESP device is also the unique ID
    hass.config_entries.async_update_entry(entry, unique_id="ems-esp", data=entry.data | {CONF_DEVICE: "ems-esp"})
    await hass.async_block_till_done()
    coordinator = current(hass, entry)

    # More than ten topics, so the aborted discovery flows keep changing the entry's discovery keys
    for index in range(11):
        async_fire_mqtt_message(hass, f"ems-esp/topic{index}", json.dumps({"a": 1}))
        await hass.async_block_till_done(wait_background_tasks=True)

    assert [key.key for key in entry.discovery_keys["mqtt"]][-1] == "ems-esp/topic10"
    assert current(hass, entry) is coordinator


def counted_reloads(reloads: list[str]) -> Callable[[ConfigEntries, str], Awaitable[bool]]:
    """Return a replacement for `ConfigEntries.async_reload` that records the reloaded entry IDs."""
    real = ConfigEntries.async_reload

    async def counting(self: ConfigEntries, entry_id: str) -> bool:
        reloads.append(entry_id)
        return await real(self, entry_id)

    return counting


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_baseline_after_a_reload_is_the_new_data_and_options(hass, entry):
    reloads: list[str] = []

    with patch.object(ConfigEntries, "async_reload", counted_reloads(reloads)):
        hass.config_entries.async_update_entry(entry, data=entry.data | {CONF_MINIMUM_SETPOINT: 25})
        await hass.async_block_till_done()
        assert len(reloads) == 1
        after_first = current(hass, entry)

        # Same data again: no reload
        hass.config_entries.async_update_entry(entry, data=entry.data | {CONF_MINIMUM_SETPOINT: 25}, title="Other")
        await hass.async_block_till_done()
        assert len(reloads) == 1
        assert current(hass, entry) is after_first

        # Reverting is a change against the new baseline
        hass.config_entries.async_update_entry(entry, data=entry.data | {CONF_MINIMUM_SETPOINT: 20})
        await hass.async_block_till_done()
        assert len(reloads) == 2

        hass.config_entries.async_update_entry(entry, options=entry.options | {CONF_MAXIMUM_RELATIVE_MODULATION: 50})
        await hass.async_block_till_done()
        assert len(reloads) == 3

        hass.config_entries.async_update_entry(entry, options=entry.options | {CONF_MAXIMUM_RELATIVE_MODULATION: 50}, unique_id="x")
        await hass.async_block_till_done()
        assert len(reloads) == 3


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_unique_id_update_with_changed_data_reloads_once(hass, entry, coordinator):
    reloads: list[str] = []

    with patch.object(ConfigEntries, "async_reload", counted_reloads(reloads)):
        hass.config_entries.async_update_entry(entry, unique_id="ems-esp", data=entry.data | {CONF_MINIMUM_SETPOINT: 26})
        await hass.async_block_till_done()

    assert len(reloads) == 1
    assert current(hass, entry) is not coordinator
    assert entry.state is ConfigEntryState.LOADED
