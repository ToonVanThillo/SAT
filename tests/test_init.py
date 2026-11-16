"""Test setup process."""
import json
import logging
from collections.abc import Awaitable, Callable
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.discovery_flow import DiscoveryKey
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.sat.config_flow import SatFlowHandler
from custom_components.sat.const import (
    CLIMATE,
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_AWAY_TEMPERATURE,
    CONF_DEVICE,
    CONF_DYNAMIC_MINIMUM_SETPOINT,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_MINIMUM_SETPOINT_ADJUSTMENT_FACTOR,
    DOMAIN,
)
from custom_components.sat.fake import SatFakeCoordinator
from tests.conftest import current
from tests.const import DEFAULT_USER_DATA, IDLE, ems_entry, with_data


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


# The minimum setpoint check at setup

MINIMUM_ERROR = "to set a minimum setpoint."
MISSING = "Minimum setpoint is missing; using 10 °C. Reconfigure SAT to set a minimum setpoint."


def minimum_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the minimum setpoint errors the entry's setup logged."""
    return [r for r in caplog.get_records("setup") if r.name == "custom_components.sat" and r.levelno == logging.ERROR and MINIMUM_ERROR in r.getMessage()]


async def add_without_minimum(hass: HomeAssistant, stored: dict[str, Any], options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Set up an EMS entry without a minimum setpoint (or with `stored`) at the current version, so no migration fills one in."""
    data = {key: value for key, value in ems_entry()[1][0][1].items() if key != CONF_MINIMUM_SETPOINT}
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA | data | stored, options=options or {}, version=SatFlowHandler.VERSION)

    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()

    return entry


@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "base_components")
@pytest.mark.parametrize("stored", [{}, {CONF_MINIMUM_SETPOINT: None}], ids=["absent", "none"])
async def test_a_missing_minimum_counts_as_10_and_logs_an_error(hass, caplog, stored: dict[str, Any]):
    entry = await add_without_minimum(hass, stored)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data.get(CONF_MINIMUM_SETPOINT) is None
    assert current(hass, entry).minimum_setpoint == 10.0
    errors = [r for r in caplog.records if r.name == "custom_components.sat" and r.levelno == logging.ERROR and MINIMUM_ERROR in r.getMessage()]
    assert [r.getMessage() for r in errors] == [MISSING]


@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "base_components")
async def test_a_missing_minimum_is_10_for_the_dynamic_minimum_setpoint(hass, boiler_data, freezer):
    # Dynamic minimum setpoint v1 in MINIMUM mode adds (return - base return temperature) * factor to the configured minimum: 10 + (35 - 30) * 10
    entry = await add_without_minimum(hass, {}, {CONF_DYNAMIC_MINIMUM_SETPOINT: True, CONF_ALLOW_UNRESTRICTED_MODULATION: False, CONF_MINIMUM_SETPOINT_ADJUSTMENT_FACTOR: 10})
    climate = hass.data[DOMAIN][entry.entry_id][CLIMATE]
    hass.states.async_set("sensor.test_inside_sensor", "20.9")
    hass.states.async_set("sensor.test_outside_sensor", "9.9")
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    freezer.tick(60)
    await boiler_data(**IDLE | {"burngas": "on", "heatingactive": "on", "curflowtemp": 45})
    climate.minimum_setpoint.warming_up(SimpleNamespace(return_temperature=30.0))

    await climate.async_control_heating_loop()

    assert current(hass, entry).flame_active is True
    assert climate.minimum_setpoint.current == 60.0
    assert climate.minimum_setpoint_value == 60.0


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize("domains, data, options, config, logged", [
    (*with_data({CONF_MINIMUM_SETPOINT: minimum})[1][0], logged) for minimum, logged in [(20, True), (28.2, True), (28.3, False), (40, False)]
], ids=["20", "28.2", "28.3", "40"])
async def test_a_minimum_at_or_below_the_cold_threshold_logs_an_error(entry, caplog, logged: bool):
    assert entry.state is ConfigEntryState.LOADED
    assert [r.getMessage() for r in minimum_errors(caplog)] == ([f"Minimum setpoint {entry.data[CONF_MINIMUM_SETPOINT]} °C is at or below 28.2 °C; reconfigure SAT to set a minimum setpoint."] if logged else [])


async def test_a_backend_without_setpoint_management_does_not_check_the_minimum(hass, caplog):
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA, version=SatFlowHandler.VERSION)
    assert CONF_MINIMUM_SETPOINT not in entry.data

    with patch.object(SatFakeCoordinator, "supports_setpoint_management", new_callable=PropertyMock, return_value=False):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert not [r for r in caplog.records if MINIMUM_ERROR in r.getMessage()]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize("domains, data, options, config, logged", [
    (*with_data({CONF_MINIMUM_SETPOINT: minimum})[1][0], logged) for minimum, logged in [("30", False), ("28.2", True), ("20", True)]
], ids=["string-30", "string-28.2", "string-20"])
async def test_a_minimum_stored_as_a_string_is_compared_as_a_number(entry, caplog, logged: bool):
    assert entry.state is ConfigEntryState.LOADED
    assert len(minimum_errors(caplog)) == int(logged)
