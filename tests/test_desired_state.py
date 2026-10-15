"""Tests for `async_submit` and the places that write to the boiler through it."""
import asyncio
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.channels import DesiredState
from custom_components.sat.const import (
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_PUSH_SETPOINT_TO_THERMOSTAT,
    DOMAIN,
    MINIMUM_SETPOINT,
)
from custom_components.sat.coordinator import DeviceState, SatDataUpdateCoordinator
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.mqtt.opentherm import SatOpenThermMqttCoordinator
from custom_components.sat.number import SatHotWaterSetpointEntity
from custom_components.sat.overshoot_protection import OvershootProtection
from tests.const import IDLE, ems_entry
from tests.test_climate_loop import run_loop
from tests.test_climate_loop_ems import INSIDE, with_data

FULL = DesiredState(
    control_setpoint=40.0,
    heater_state=DeviceState.ON,
    maximum_relative_modulation=80,
    maximum_setpoint=60.0,
    thermostat_setpoint=21.0,
)


@pytest.fixture
def spy() -> Iterator[Callable[[SatDataUpdateCoordinator], MagicMock]]:
    """Return a function that wraps `async_submit` on a coordinator instance, keeping its behaviour; the patches are stopped after the test."""
    with ExitStack() as stack:
        yield lambda coordinator: stack.enter_context(patch.object(coordinator, "async_submit", wraps=coordinator.async_submit))


# async_submit

SETTERS = {
    "control_setpoint": "async_set_control_setpoint",
    "maximum_relative_modulation": "async_set_control_max_relative_modulation",
    "heater_state": "async_set_heater_state",
    "maximum_setpoint": "async_set_control_max_setpoint",
    "thermostat_setpoint": "async_set_control_thermostat_setpoint",
}


def mock_setters(coordinator) -> MagicMock:
    """Replace the setters by mocks attached to one parent, to observe the call order."""
    parent = MagicMock()
    for field, setter in SETTERS.items():
        mock = AsyncMock()
        setattr(coordinator, setter, mock)
        parent.attach_mock(mock, field)

    return parent


async def test_submit_calls_setters_in_fixed_order(ems):
    parent = mock_setters(ems)

    await ems.async_submit(FULL)

    assert [(c[0], c[1][0]) for c in parent.mock_calls] == [
        ("control_setpoint", 40.0),
        ("maximum_relative_modulation", 80),
        ("heater_state", DeviceState.ON),
        ("maximum_setpoint", 60.0),
        ("thermostat_setpoint", 21.0),
    ]


async def test_submit_skips_none_fields(ems):
    parent = mock_setters(ems)

    await ems.async_submit(DesiredState(heater_state=DeviceState.OFF))

    assert [(c[0], c[1][0]) for c in parent.mock_calls] == [("heater_state", DeviceState.OFF)]


async def test_ems_submit_publishes_in_fixed_order(ems, published):
    await ems.async_submit(FULL)

    assert [c[1] for c in published()] == [
        '{"cmd": "selflowtemp", "value": 40.0}',
        '{"cmd": "burnmaxpower", "value": 80}',
        '{"cmd": "heatingoff", "value": "off"}',
        '{"cmd": "heatingtemp", "value": 60.0}',
    ]


async def test_ems_empty_submit_publishes_nothing(ems, published):
    await ems.async_submit(DesiredState())

    assert published() == []


# Startup

async def test_startup_submits_only_maximum_setpoint(hass, mqtt_mock, no_publish_delay):
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"})

    with patch.object(coordinator, "async_submit", new=AsyncMock()) as submit:
        await coordinator.async_added_to_hass()

    submit.assert_awaited_once_with(DesiredState(maximum_setpoint=coordinator.maximum_setpoint))


# Climate loop

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_cycle_submits_once(entry, climate, boiler_data, mqtt_mock, published, spy):
    await boiler_data(**IDLE)
    submit = spy(climate._coordinator)

    await run_loop(climate, mqtt_mock, published)

    submit.assert_awaited_once_with(DesiredState(control_setpoint=35.0, maximum_relative_modulation=100, heater_state=DeviceState.ON))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_cycle_submits_only_the_setpoint_when_nothing_else_changes(entry, climate, boiler_data, mqtt_mock, published, spy):
    await boiler_data(**IDLE | {"heatingactive": "on", "burnmaxpower": 100})
    submit = spy(climate._coordinator)

    await run_loop(climate, mqtt_mock, published)

    submit.assert_awaited_once_with(DesiredState(control_setpoint=35.0))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_missing_temperature_submits_nothing(hass, entry, climate, boiler_data, mqtt_mock, published, spy):
    await boiler_data(**IDLE)
    hass.states.async_set(INSIDE, "unavailable")
    submit = spy(climate._coordinator)

    await run_loop(climate, mqtt_mock, published)

    submit.assert_not_awaited()


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_loop_submits_nothing(entry, climate, boiler_data, mqtt_mock, published, spy):
    await boiler_data(**IDLE)
    submit = spy(climate._coordinator)

    await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF)

    submit.assert_not_awaited()


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize(("active", "expected"), [("on", [DesiredState(heater_state=DeviceState.OFF)]), ("off", [])])
async def test_hvac_off_submits_heater_off_only_if_active(entry, climate, boiler_data, active, expected, spy):
    await boiler_data(**IDLE | {"heatingactive": active})
    submit = spy(climate._coordinator)

    await climate.async_set_hvac_mode(HVACMode.OFF)

    assert [c.args[0] for c in submit.await_args_list] == expected


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_PUSH_SETPOINT_TO_THERMOSTAT: True}))
async def test_thermostat_push_is_submitted(entry, climate, spy):
    submit = spy(climate._coordinator)

    await climate.async_set_target_temperature(23.0)

    submit.assert_awaited_once_with(DesiredState(thermostat_setpoint=23.0))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_no_thermostat_push_without_option(entry, climate, spy):
    submit = spy(climate._coordinator)

    await climate.async_set_target_temperature(23.0)

    submit.assert_not_awaited()


# Overshoot protection

@pytest.fixture
def overshoot(ems) -> OvershootProtection:
    return OvershootProtection(ems, "radiators")


async def test_overshoot_trigger_publish_order(overshoot, ems, published):
    with patch("custom_components.sat.overshoot_protection.asyncio.sleep", new=AsyncMock()), patch.object(ems, "async_control_heating_loop", new=AsyncMock()):
        await overshoot._trigger_heating_cycle(is_ready=False)

    assert [c[1] for c in published()] == [
        f'{{"cmd": "selflowtemp", "value": {overshoot._setpoint}}}',
        '{"cmd": "burnmaxpower", "value": 100}',
        '{"cmd": "heatingoff", "value": "off"}',
    ]


async def test_overshoot_cancel_publish_order(overshoot, ems, published):
    with patch.object(overshoot, "_wait_for_flame", new=AsyncMock(side_effect=asyncio.CancelledError)), pytest.raises(asyncio.CancelledError):
        await overshoot.calculate()

    # EMS sends the absolute minimum setpoint as 0
    assert MINIMUM_SETPOINT == 10
    assert [c[1] for c in published()] == [
        '{"cmd": "selflowtemp", "value": 0}',
        '{"cmd": "heatingoff", "value": "on"}',
    ]


# Hot water

async def test_hot_water_number_writes_directly(hass, mqtt_mock, no_publish_delay, published):
    otgw = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"})
    entity = SatHotWaterSetpointEntity(otgw, MockConfigEntry(domain=DOMAIN, data={CONF_NAME: "Test"}))

    with patch.object(otgw, "async_submit", new=AsyncMock()) as submit:
        await entity.async_set_native_value(52.0)

    submit.assert_not_awaited()
    assert [c[1] for c in published()] == ["SW=52.0"]
