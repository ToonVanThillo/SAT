"""Leaving a preset returns to the stored target; without one the current target stays and a warning is logged.

After a restart the preset is restored from the old state, but the targets to return to come only from the SAT
restore data, so a preset can be active without a target to return to.
"""
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.sat.const import (
    CONF_ACTIVITY_TEMPERATURE,
    CONF_AWAY_TEMPERATURE,
    CONF_COMFORT_TEMPERATURE,
    CONF_HOME_TEMPERATURE,
    CONF_PUSH_SETPOINT_TO_THERMOSTAT,
    CONF_RADIATORS,
    CONF_SLEEP_TEMPERATURE,
    CONF_WINDOW_SENSORS,
)
from tests.conftest import settle
from tests.const import with_data

CLIMATE_ID = "climate.test"
RADIATOR = "climate.radiator"
WINDOW = "binary_sensor.window"
LOGGER = "custom_components.sat.climate"
CURRENT = 16.0
RETURN = 19.0
# Distinct preset temperatures, none equal to CURRENT or RETURN
PRESETS = {"activity": 10.0, "away": 12.0, "home": 18.0, "sleep": 15.0, "comfort": 20.0}
OPTIONS = {
    CONF_ACTIVITY_TEMPERATURE: 10, CONF_AWAY_TEMPERATURE: 12, CONF_HOME_TEMPERATURE: 18,
    CONF_SLEEP_TEMPERATURE: 15, CONF_COMFORT_TEMPERATURE: 20, CONF_WINDOW_SENSORS: [WINDOW],
}
BOTH_RETURNS = {"rooms": {}, "pre_custom_temperature": RETURN, "pre_activity_temperature": RETURN}

pytestmark = [
    pytest.mark.usefixtures("restore", "window_sensor_registered", "base_components", "mqtt_mock"),
    pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR], CONF_PUSH_SETPOINT_TO_THERMOSTAT: True}, OPTIONS)),
]

# Old states carrying a preset and a target, with no, empty or malformed SAT restore data
NO_DATA: list[Any] = [None, {}, {"rooms": {}, "pre_custom_temperature": "warm", "pre_activity_temperature": "warm"}]


def restoring(preset: str, extra: dict | None) -> tuple[State, dict | None]:
    return State(CLIMATE_ID, "heat", {"temperature": CURRENT, "preset_mode": preset}), extra


@pytest.fixture
async def radiator_calls(hass: HomeAssistant, entry, climate) -> list:
    """Return the climate.set_temperature calls the radiators receive (radiators included in the entry)."""
    hass.states.async_set(RADIATOR, "heat", {"temperature": CURRENT, "hvac_modes": ["heat", "off"]})
    # Let the climate handle the radiator's state change now, so it doesn't count as a call of the test
    await hass.async_block_till_done()
    return async_mock_service(hass, "climate", "set_temperature")


@pytest.fixture
def thermostat_push(climate):
    with patch.object(climate._coordinator, "async_submit", new_callable=AsyncMock) as submit:
        yield submit


@pytest.fixture
def control_spies(climate, radiator_calls):
    """Spy on the PID reset, the control state reset and the control loop triggers; installed after setup."""
    with (
        patch.object(climate.pid, "reset", wraps=climate.pid.reset) as pid_reset,
        patch.object(climate, "_async_control_pid", wraps=climate._async_control_pid) as control_pid,
        patch.object(climate, "reset_control_state", wraps=climate.reset_control_state) as reset_state,
        patch.object(climate, "schedule_control_heating_loop", wraps=climate.schedule_control_heating_loop) as schedule_loop,
        patch.object(climate, "async_control_heating_loop", wraps=climate.async_control_heating_loop) as control_loop,
    ):
        yield [pid_reset, control_pid, reset_state, schedule_loop, control_loop]


def assert_no_control_calls(spies: list) -> None:
    for spy in spies:
        spy.assert_not_called()


def shown_preset(hass: HomeAssistant, climate) -> str | None:
    state = hass.states.get(climate.entity_id)
    assert state is not None
    return state.attributes["preset_mode"]


def return_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING and "No target to return to" in r.getMessage()]


def temperatures(calls: list) -> list:
    return [call.data["temperature"] for call in calls]


# A. Leaving a preset to "none" without a return target

@pytest.mark.parametrize("restore", [restoring("away", extra) for extra in NO_DATA], ids=["no-extra", "empty-extra", "malformed-extra"], indirect=True)
async def test_leaving_a_preset_without_a_return_target_keeps_the_target(hass, restore, climate, radiator_calls, thermostat_push, control_spies, caplog):
    assert (climate.preset_mode, climate.target_temperature) == ("away", CURRENT)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("none")

    assert climate.preset_mode == "none"
    assert climate.target_temperature == CURRENT
    assert return_warnings(caplog) == [f"No target to return to after preset away; keeping the current target of {CURRENT}"]
    assert radiator_calls == []
    thermostat_push.assert_not_called()
    assert_no_control_calls(control_spies)
    assert shown_preset(hass, climate) == "none"
    assert hass.states.get(climate.entity_id).attributes["temperature"] == CURRENT


@pytest.mark.parametrize("preset", sorted(PRESETS))
@pytest.mark.parametrize("restore", [restoring("away", None)], indirect=True)
async def test_the_warning_names_the_preset_being_left(hass, restore, climate, radiator_calls, caplog, preset):
    climate._attr_preset_mode = preset

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("none")

    assert return_warnings(caplog) == [f"No target to return to after preset {preset}; keeping the current target of {CURRENT}"]
    assert climate.target_temperature == CURRENT


@pytest.mark.parametrize("restore", [restoring("away", None)], indirect=True)
async def test_selecting_none_again_does_not_warn_twice(hass, restore, climate, radiator_calls, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("none")
        await climate.async_set_preset_mode("none")

    assert len(return_warnings(caplog)) == 1


@pytest.mark.parametrize("restore", [restoring("away", None)], indirect=True)
async def test_the_next_preset_cycle_records_a_return_target_again(hass, restore, climate, radiator_calls, caplog):
    await climate.async_set_preset_mode("none")
    await climate.async_set_preset_mode("sleep")
    assert climate.target_temperature == PRESETS["sleep"]
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("none")

    assert return_warnings(caplog) == []
    assert (climate.preset_mode, climate.target_temperature) == ("none", CURRENT)
    assert temperatures(radiator_calls)[-1] == CURRENT


# B. Leaving a preset to "none" with a return target

@pytest.mark.parametrize("restore", [restoring("away", BOTH_RETURNS)], indirect=True)
async def test_leaving_a_preset_returns_to_the_stored_target(hass, restore, climate, radiator_calls, thermostat_push, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("none")

    assert climate.preset_mode == "none"
    assert climate.target_temperature == RETURN
    assert temperatures(radiator_calls) == [RETURN]
    assert RETURN in [call.args[0].thermostat_setpoint for call in thermostat_push.await_args_list]
    assert return_warnings(caplog) == []
    assert shown_preset(hass, climate) == "none"
    assert hass.states.get(climate.entity_id).attributes["temperature"] == RETURN


@pytest.mark.parametrize("restore", [restoring("away", BOTH_RETURNS | {"pre_custom_temperature": CURRENT})], indirect=True)
async def test_a_return_target_equal_to_the_current_target_sends_nothing(hass, restore, climate, radiator_calls, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("none")

    assert climate.preset_mode == "none"
    assert climate.target_temperature == CURRENT
    assert radiator_calls == []
    assert return_warnings(caplog) == []


# C. Switching to another preset

@pytest.mark.parametrize("restore", [restoring("away", extra) for extra in (None, BOTH_RETURNS)], ids=["no-return-target", "return-target"], indirect=True)
async def test_switching_to_another_preset_uses_its_temperature(hass, restore, climate, radiator_calls, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await climate.async_set_preset_mode("sleep")

    assert climate.preset_mode == "sleep"
    assert climate.target_temperature == PRESETS["sleep"]
    assert temperatures(radiator_calls) == [PRESETS["sleep"]]
    assert return_warnings(caplog) == []
    assert shown_preset(hass, climate) == "sleep"


# D / E / F. The window

async def set_window(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(WINDOW, state)
    await settle()


@pytest.mark.parametrize("restore", [restoring("activity", extra) for extra in NO_DATA], ids=["no-extra", "empty-extra", "malformed-extra"], indirect=True)
async def test_a_closing_window_without_a_return_target_keeps_the_target(hass, restore, climate, radiator_calls, thermostat_push, control_spies, caplog):
    assert climate.preset_mode == "activity"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await set_window(hass, STATE_ON)
        await set_window(hass, STATE_OFF)

    assert climate.preset_mode == "none"
    assert climate.target_temperature == CURRENT
    assert return_warnings(caplog) == [f"No target to return to after preset activity; keeping the current target of {CURRENT}"]
    assert radiator_calls == []
    thermostat_push.assert_not_called()
    assert_no_control_calls(control_spies)
    assert shown_preset(hass, climate) == "none"


@pytest.mark.parametrize("restore", [restoring("activity", BOTH_RETURNS)], indirect=True)
async def test_a_closing_window_returns_to_the_stored_target(hass, restore, climate, radiator_calls, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await set_window(hass, STATE_ON)
        await set_window(hass, STATE_OFF)

    assert climate.target_temperature == RETURN
    assert climate.preset_mode == "none"
    assert temperatures(radiator_calls) == [RETURN]
    assert return_warnings(caplog) == []
    assert shown_preset(hass, climate) == "none"


@pytest.mark.parametrize("restore", [restoring("activity", BOTH_RETURNS | {"pre_activity_temperature": PRESETS["comfort"]})], indirect=True)
async def test_a_return_target_equal_to_a_preset_selects_that_preset(hass, restore, climate, radiator_calls, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await set_window(hass, STATE_ON)
        await set_window(hass, STATE_OFF)

    assert climate.target_temperature == PRESETS["comfort"]
    assert climate.preset_mode == "comfort"
    assert temperatures(radiator_calls) == [PRESETS["comfort"]]
    assert return_warnings(caplog) == []


@pytest.mark.parametrize("preset", ["none", "away"])
@pytest.mark.parametrize("restore", [restoring("away", None)], indirect=True)
async def test_a_closing_window_outside_the_activity_preset_changes_nothing(hass, restore, climate, radiator_calls, thermostat_push, caplog, preset):
    climate._attr_preset_mode = preset

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await set_window(hass, STATE_ON)
        await set_window(hass, STATE_OFF)

    assert climate.preset_mode == preset
    assert climate.target_temperature == CURRENT
    assert radiator_calls == []
    thermostat_push.assert_not_called()
    assert return_warnings(caplog) == []


# No path may produce a missing target

@pytest.mark.parametrize("restore", [restoring("activity", None)], indirect=True)
async def test_no_target_is_ever_none(hass, restore, climate, radiator_calls):
    await set_window(hass, STATE_ON)
    await set_window(hass, STATE_OFF)
    await climate.async_set_preset_mode("away")
    await climate.async_set_preset_mode("none")

    assert climate.target_temperature is not None
    assert None not in temperatures(radiator_calls)
