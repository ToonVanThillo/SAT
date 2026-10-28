"""The control section of the diagnostics download."""
import json

import pytest
from homeassistant.components.climate import HVACMode
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.sat.const import CONF_MODE, MODE_FAKE
from custom_components.sat.diagnostics import async_get_config_entry_diagnostics
from custom_components.sat.errors import Error
from custom_components.sat.relative_modulation import RelativeModulationState
from tests.const import IDLE, ems_entry

pytestmark = pytest.mark.usefixtures("loop_entry")

KEYS = {
    "pid_error", "error_source", "control_setpoint", "relative_modulation_state", "relative_modulation_value", "valves_open",
    "boiler_temperature_cold", "boiler_temperature_tracking", "boiler_temperature_derivative", "coefficient_derivative",
}


async def run(climate, boiler_data):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@pytest.mark.parametrize(*ems_entry())
async def test_control_section_keys_and_types(hass, hass_client, entry, climate, coordinator, boiler_data):
    await run(climate, boiler_data)

    control = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

    assert set(control) == KEYS
    assert number(control["pid_error"])
    assert isinstance(control["error_source"], str)
    assert control["control_setpoint"] is None or number(control["control_setpoint"])
    assert control["relative_modulation_state"] in RelativeModulationState.__members__
    assert isinstance(control["relative_modulation_value"], int) and not isinstance(control["relative_modulation_value"], bool)
    assert isinstance(control["valves_open"], bool)
    assert control["boiler_temperature_cold"] is None or number(control["boiler_temperature_cold"])
    assert isinstance(control["boiler_temperature_tracking"], bool)
    assert control["boiler_temperature_derivative"] is None or number(control["boiler_temperature_derivative"])
    assert control["coefficient_derivative"] is None or number(control["coefficient_derivative"])


@pytest.mark.parametrize(*ems_entry())
async def test_control_values_match_their_sources(hass, hass_client, entry, climate, coordinator, boiler_data):
    await run(climate, boiler_data)

    control = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

    assert control["pid_error"] == climate.pid.last_error
    assert control["error_source"] == climate.max_error.entity_id == climate.entity_id
    assert control["control_setpoint"] == climate.setpoint
    assert control["relative_modulation_state"] == climate.relative_modulation_state.name
    assert control["relative_modulation_value"] == climate.get_relative_modulation_value(coordinator.boiler)
    assert control["valves_open"] == climate.valves_open
    assert control["boiler_temperature_cold"] == coordinator.boiler_temperature_cold
    assert control["boiler_temperature_tracking"] == coordinator.boiler_temperature_tracking
    assert control["boiler_temperature_derivative"] == coordinator.boiler_temperature_derivative
    assert control["coefficient_derivative"] == climate.heating_curve.coefficient_derivative


@pytest.mark.parametrize(*ems_entry())
async def test_control_values_are_computed_at_download_time(hass, hass_client, entry, climate, boiler_data):
    await run(climate, boiler_data)
    first = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

    climate.pid.update_reset(Error("climate.test", 3.0), 30.0)
    second = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

    assert second["pid_error"] == climate.pid.last_error
    assert second["pid_error"] != first["pid_error"]


@pytest.mark.parametrize(*ems_entry())
async def test_control_section_is_json_serializable(hass, hass_client, entry, climate, boiler_data):
    await run(climate, boiler_data)

    raw = await async_get_config_entry_diagnostics(hass, entry)

    assert json.loads(json.dumps(raw["control"])) == raw["control"]


@pytest.mark.parametrize(*ems_entry())
async def test_channel_sections_are_kept(hass, hass_client, entry, climate, boiler_data):
    await run(climate, boiler_data)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert {"entry", "channels", "readings", "control"} <= set(diagnostics)
    assert set(diagnostics["channels"]) == {"control_setpoint", "heater_state", "maximum_relative_modulation", "maximum_setpoint"}


@pytest.mark.parametrize("domains, data, options, config", [([], {CONF_MODE: MODE_FAKE}, {}, {})])
async def test_control_section_with_fake_coordinator(hass, hass_client, entry, climate):
    control = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

    assert set(control) == KEYS
