"""Tests for the heating loop in HVAC mode OFF: only a heater OFF request, and the normal cycle again after HEAT."""

from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import DesiredState
from custom_components.sat.const import (
    CONF_MAXIMUM_SETPOINT,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    CONF_SIMULATED_COOLING,
    CONF_SIMULATED_HEATING,
    CONF_SIMULATED_WARMING_UP,
    MODE_SIMULATOR,
)
from custom_components.sat.coordinator import DeviceState
from tests.conftest import advance
from tests.const import IDLE, ems_entry, otgw_entry
from tests.test_climate_loop import message, run_loop
from tests.test_climate_loop_ems import INSIDE, OUTSIDE, with_data

pytestmark = pytest.mark.usefixtures("loop_entry")

HEATER_OFF = message('{"cmd": "heatingoff", "value": "on"}')


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize("active", ["on", "off"])
async def test_off_never_publishes_setpoint_or_modulation(entry, climate, boiler_data, mqtt_mock, published, active):
    await boiler_data(**IDLE | {"heatingactive": active})
    await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF)

    for _ in range(4):
        await climate.async_control_heating_loop()

    assert all("selflowtemp" not in call[1] and "burnmaxpower" not in call[1] for call in published())


@pytest.mark.parametrize(*ems_entry())
async def test_first_heat_cycle_after_off_publishes_the_normal_sequence(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF)
    await climate.async_control_heating_loop()

    assert await run_loop(climate, mqtt_mock, published, mode=HVACMode.HEAT) == [
        message('{"cmd": "selflowtemp", "value": 35.0}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
        message('{"cmd": "heatingoff", "value": "off"}'),
    ]


@pytest.mark.parametrize(*otgw_entry())
@pytest.mark.parametrize(("central_heating", "expected"), [("ON", ["CH=0"]), ("OFF", [])])
async def test_opentherm_off_requests_heater_off_only_when_active(hass, entry, climate, mqtt_mock, published, central_heating, expected):
    async_fire_mqtt_message(hass, "otgw/value/gw1/centralheating", central_heating)
    await hass.async_block_till_done()

    await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()

    assert [(call[0], call[1]) for call in published()] == [("otgw/set/gw1/command", command) for command in expected]


@pytest.mark.parametrize(*ems_entry())
async def test_switching_off_runs_one_full_cycle(hass, entry, climate, boiler_data):
    await boiler_data(**IDLE | {"heatingactive": "on"})
    coordinator = climate._coordinator

    with patch.object(coordinator, "async_control_heating_loop", wraps=coordinator.async_control_heating_loop) as update, \
            patch.object(coordinator, "snapshot", wraps=coordinator.snapshot) as snapshot, \
            patch.object(coordinator, "verify", wraps=coordinator.verify) as verify, \
            patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_set_hvac_mode(HVACMode.OFF)

    update.assert_awaited_once()
    snapshot.assert_called_once()
    verify.assert_called_once_with(coordinator._snapshot)
    submit.assert_awaited_once_with(DesiredState(heater_state=DeviceState.OFF))


@pytest.mark.parametrize(*ems_entry())
async def test_off_cycles_keep_the_coordinator_state_updating(hass, entry, climate, boiler_data, freezer: FrozenDateTimeFactory):
    coordinator = climate._coordinator
    await climate.async_set_hvac_mode(HVACMode.OFF)

    # Each cycle appends the boiler temperature to the history that feeds the derivative; `heater_on_since` follows `heatingactive`
    await boiler_data(**IDLE | {"curflowtemp": 40.0, "burngas": "off", "heatingactive": "off"})
    freezer.tick(10)
    await climate.async_control_heating_loop()
    await boiler_data(**IDLE | {"curflowtemp": 50.0, "burngas": "on", "heatingactive": "on"})
    freezer.tick(10)
    await climate.async_control_heating_loop()

    assert coordinator.boiler_temperature_derivative == pytest.approx(1.0)
    assert coordinator.heater_on_since is not None

    await boiler_data(**IDLE | {"curflowtemp": 45.0, "burngas": "off", "heatingactive": "off"})
    freezer.tick(10)
    await climate.async_control_heating_loop()
    assert coordinator.heater_on_since is None


@pytest.mark.parametrize(*ems_entry())
async def test_switching_off_cancels_a_scheduled_run(hass, entry, climate, freezer: FrozenDateTimeFactory):
    climate.schedule_control_heating_loop()
    coordinator = climate._coordinator

    with patch.object(coordinator, "async_control_heating_loop", wraps=coordinator.async_control_heating_loop) as update:
        await climate.async_set_hvac_mode(HVACMode.OFF)
        await advance(hass, freezer, 15)

    # The direct cycle and the run scheduled at the end of the switch; the run scheduled before it is cancelled
    assert update.await_count == 2


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize("sensor", [INSIDE, OUTSIDE])
async def test_switching_off_with_a_non_numeric_temperature_stops_the_heater(hass, entry, climate, boiler_data, mqtt_mock, published, sensor):
    await boiler_data(**IDLE | {"heatingactive": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set(sensor, "abc")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    await climate.async_set_hvac_mode(HVACMode.OFF)

    assert published() == [HEATER_OFF]


@pytest.mark.parametrize(*with_data({
    CONF_MODE: MODE_SIMULATOR, CONF_MINIMUM_SETPOINT: 40, CONF_MAXIMUM_SETPOINT: 75,
    CONF_SIMULATED_HEATING: 5, CONF_SIMULATED_COOLING: 5, CONF_SIMULATED_WARMING_UP: "00:00:15",
}))
async def test_simulator_boiler_moves_toward_its_target_in_off(hass, entry, climate):
    await climate.async_set_hvac_mode(HVACMode.OFF)
    temperatures = [climate._coordinator.boiler_temperature]

    for _ in range(5):
        await climate.async_control_heating_loop()
        temperatures.append(climate._coordinator.boiler_temperature)

    # Current behaviour, known bug (simulator update step, see known-issues): from 10 °C, the startup and switch-off cycles heat by 5 °C each, up to the target: the minimum setpoint, as the setpoint stays below it
    assert temperatures == [20, 25, 30, 35, 40, 40]
