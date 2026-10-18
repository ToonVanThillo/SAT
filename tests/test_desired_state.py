"""Tests for `async_submit` and the places that write to the boiler through it."""
import asyncio
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.sat.channels import RELEASE, DesiredState
from custom_components.sat.const import (
    COLD_SETPOINT,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_PUSH_SETPOINT_TO_THERMOSTAT,
    CONF_RADIATORS,
    DOMAIN,
    MINIMUM_SETPOINT,
)
from custom_components.sat.coordinator import DeviceState, SatDataUpdateCoordinator
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.mqtt.opentherm import SatOpenThermMqttCoordinator
from custom_components.sat.number import SatHotWaterSetpointEntity
from custom_components.sat.overshoot_protection import OvershootProtection
from tests.const import EMS_BOILER_DATA, IDLE, ems_entry, otgw_entry
from tests.test_climate_loop import run_loop
from tests.test_climate_loop_ems import INSIDE, RADIATOR, with_data

FULL = DesiredState(
    control_setpoint=40.0,
    heater_state=DeviceState.ON,
    maximum_relative_modulation=80,
    maximum_setpoint=60.0,
    thermostat_setpoint=21.0,
)


async def otgw_values(hass: HomeAssistant, **values: str) -> None:
    for key, value in values.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()


def command(payload: str) -> tuple:
    return "otgw/set/gw1/command", payload, 1, False


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


async def test_submit_calls_setters_in_fixed_order(otgw):
    parent = mock_setters(otgw)

    await otgw.async_submit(FULL)

    assert [(c[0], c[1][0]) for c in parent.mock_calls] == [
        ("control_setpoint", 40.0),
        ("maximum_relative_modulation", 80),
        ("heater_state", DeviceState.ON),
        ("maximum_setpoint", 60.0),
        ("thermostat_setpoint", 21.0),
    ]


async def test_submit_skips_none_fields(otgw):
    parent = mock_setters(otgw)

    await otgw.async_submit(DesiredState(heater_state=DeviceState.OFF))

    assert [(c[0], c[1][0]) for c in parent.mock_calls] == [("heater_state", DeviceState.OFF)]


async def test_submit_releasing_the_control_setpoint_writes_nothing(otgw, published):
    await otgw.async_submit(DesiredState(control_setpoint=RELEASE))

    assert published() == []


async def test_ems_submit_publishes_in_fixed_order(ems, boiler_data, published):
    await boiler_data(**EMS_BOILER_DATA | {"heatingoff": "on"})
    ems.verify(ems.snapshot())

    await ems.async_submit(FULL)

    assert [c[1] for c in published()] == [
        '{"cmd": "selflowtemp", "value": 40}',
        '{"cmd": "burnmaxpower", "value": 80}',
        '{"cmd": "heatingoff", "value": "off"}',
        '{"cmd": "heatingtemp", "value": 60}',
    ]


async def test_ems_empty_submit_publishes_nothing(ems, published):
    await ems.async_submit(DesiredState())

    assert published() == []


@pytest.mark.parametrize(("state", "active", "expected"), [
    (DeviceState.ON, False, [command("CH=1")]),
    (DeviceState.ON, True, []),
    (DeviceState.OFF, True, [command("CH=0")]),
    (DeviceState.OFF, False, []),
])
async def test_submit_heater_state_rules(hass, otgw, published, state, active, expected):
    await otgw_values(hass, centralheating="ON" if active else "OFF")
    otgw.snapshot()

    await otgw.async_submit(DesiredState(heater_state=state))

    assert published() == expected


# Startup

async def test_startup_submits_only_maximum_setpoint(hass, mqtt_mock, no_publish_delay):
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"})

    with patch.object(coordinator, "async_submit", new=AsyncMock()) as submit:
        await coordinator.async_added_to_hass()

    submit.assert_awaited_once_with(DesiredState(maximum_setpoint=coordinator.maximum_setpoint))


# Climate loop

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_cycle_submits_once(entry, climate, coordinator, boiler_data, mqtt_mock, published, spy):
    await boiler_data(**IDLE)
    submit = spy(coordinator)

    await run_loop(climate, mqtt_mock, published)

    submit.assert_awaited_once_with(DesiredState(control_setpoint=35.0, maximum_relative_modulation=100, heater_state=DeviceState.ON))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_cycle_submits_the_full_state_when_nothing_changes(entry, climate, coordinator, boiler_data, mqtt_mock, published, spy):
    await boiler_data(**IDLE | {"burnmaxpower": 100})
    submit = spy(coordinator)

    await run_loop(climate, mqtt_mock, published)

    submit.assert_awaited_once_with(DesiredState(control_setpoint=35.0, maximum_relative_modulation=100, heater_state=DeviceState.ON))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_missing_temperature_submits_nothing(hass, entry, climate, coordinator, boiler_data):
    await boiler_data(**IDLE)
    hass.states.async_set(INSIDE, "unavailable")
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # Only the snapshot phase runs
    with patch.object(coordinator, "async_control_heating_loop", wraps=coordinator.async_control_heating_loop) as update, \
            patch.object(coordinator, "verify", wraps=coordinator.verify) as verify, \
            patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_control_heating_loop()

    update.assert_awaited_once()
    verify.assert_called_once_with(coordinator._snapshot)
    submit.assert_not_awaited()


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_loop_submits_heater_off(entry, climate, coordinator, boiler_data, spy):
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    submit = spy(coordinator)

    await climate.async_control_heating_loop()

    submit.assert_awaited_once_with(DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_PUSH_SETPOINT_TO_THERMOSTAT: True}))
async def test_thermostat_push_is_submitted(entry, climate, coordinator, spy):
    submit = spy(coordinator)

    await climate.async_set_target_temperature(23.0)

    submit.assert_awaited_once_with(DesiredState(thermostat_setpoint=23.0))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_no_thermostat_push_without_option(entry, climate, coordinator, spy):
    submit = spy(coordinator)

    await climate.async_set_target_temperature(23.0)

    submit.assert_not_awaited()


# Overshoot protection

@pytest.fixture
def overshoot(otgw) -> OvershootProtection:
    return OvershootProtection(otgw, "radiators")


async def test_overshoot_trigger_publish_order(overshoot, otgw, published):
    with patch("custom_components.sat.overshoot_protection.asyncio.sleep", new=AsyncMock()), patch.object(otgw, "async_control_heating_loop", new=AsyncMock()):
        await overshoot._trigger_heating_cycle(is_ready=False)

    assert [c[1] for c in published()] == [f"CS={overshoot._setpoint}", "PM=25", "MM=100", "CH=1"]


async def test_overshoot_cancel_publish_order(overshoot, otgw, published):
    with patch.object(overshoot, "_wait_for_flame", new=AsyncMock(side_effect=asyncio.CancelledError)), pytest.raises(asyncio.CancelledError):
        await overshoot.calculate()

    assert [c[1] for c in published()] == [f"CS={MINIMUM_SETPOINT}", "PM=25", "CH=0"]


async def test_overshoot_on_ems_publishes_nothing(ems, boiler_data, published):
    await boiler_data(**IDLE | {"heatingoff": "on"})

    with patch("custom_components.sat.overshoot_protection.asyncio.sleep", new=AsyncMock()), patch.object(ems, "async_control_heating_loop", new=AsyncMock()):
        await OvershootProtection(ems, "radiators")._trigger_heating_cycle(is_ready=False)

    # Current behaviour, known bug: calibration never verifies a snapshot, so the channels keep waiting for a live read-back
    assert published() == []


# Hot water

async def test_hot_water_number_writes_directly(hass, mqtt_mock, no_publish_delay, published):
    otgw = SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"})
    entity = SatHotWaterSetpointEntity(otgw, MockConfigEntry(domain=DOMAIN, data={CONF_NAME: "Test"}))

    with patch.object(otgw, "async_submit", new=AsyncMock()) as submit:
        await entity.async_set_native_value(52.0)

    submit.assert_not_awaited()
    assert [c[1] for c in published()] == ["SW=52.0"]


# Decide phase and verify

def record(order: list[str], name: str) -> Callable[..., object]:
    """Return a side effect that appends `name` to `order`; returning DEFAULT lets the wrapped method run."""
    def side_effect(*_: object, **__: object) -> object:
        order.append(name)
        return DEFAULT

    return side_effect


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_cycle_verifies_once_between_snapshot_and_submit(entry, climate, coordinator, boiler_data):
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    order: list[str] = []

    with patch.object(coordinator, "snapshot", wraps=coordinator.snapshot, side_effect=record(order, "snapshot")), \
            patch.object(coordinator, "verify", wraps=coordinator.verify, side_effect=record(order, "verify")) as verify, \
            patch.object(coordinator, "async_submit", wraps=coordinator.async_submit, side_effect=record(order, "async_submit")):
        await climate.async_control_heating_loop()

    assert order == ["snapshot", "verify", "async_submit"]
    verify.assert_called_once_with(coordinator._snapshot)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_cycle_runs_all_phases_without_temperatures(hass, entry, climate, coordinator, boiler_data):
    await boiler_data(**IDLE)
    hass.states.async_set(INSIDE, "unavailable")
    await climate.async_set_hvac_mode(HVACMode.OFF)
    order: list[str] = []

    # The snapshot phase is the same as in HEAT; missing temperatures do not stop it
    with patch.object(coordinator, "async_control_heating_loop", wraps=coordinator.async_control_heating_loop, side_effect=record(order, "update")), \
            patch.object(coordinator, "snapshot", wraps=coordinator.snapshot, side_effect=record(order, "snapshot")), \
            patch.object(coordinator, "verify", wraps=coordinator.verify, side_effect=record(order, "verify")) as verify, \
            patch.object(coordinator, "async_submit", wraps=coordinator.async_submit, side_effect=record(order, "async_submit")):
        await climate.async_control_heating_loop()

    assert order == ["update", "snapshot", "verify", "async_submit"]
    verify.assert_called_once_with(coordinator._snapshot)


async def test_submit_without_snapshot_writes_unconditionally(otgw, published):
    await otgw.async_submit(DesiredState(maximum_relative_modulation=100, heater_state=DeviceState.OFF))
    await otgw.async_submit(DesiredState(maximum_relative_modulation=100, heater_state=DeviceState.OFF))

    assert [c[1] for c in published()] == ["MM=100", "CH=0"] * 2


@pytest.mark.parametrize(("current", "requested", "expected"), [(31, 100, True), (100, 100, False), (100, 50, True)])
async def test_submit_modulation_only_when_it_differs_from_the_snapshot(hass, otgw, published, current, requested, expected):
    await otgw_values(hass, MaxRelModLevelSetting=f"{current}.0")
    otgw.snapshot()

    await otgw.async_submit(DesiredState(maximum_relative_modulation=requested))

    assert published() == ([command(f"MM={requested}")] if expected else [])


async def test_submit_uses_the_snapshot_not_later_data(hass, otgw, published):
    await otgw_values(hass, centralheating="OFF", MaxRelModLevelSetting="100.0")
    otgw.snapshot()
    await otgw_values(hass, centralheating="ON", MaxRelModLevelSetting="50.0")

    await otgw.async_submit(DesiredState(maximum_relative_modulation=50, heater_state=DeviceState.ON))

    assert [c[1] for c in published()] == ["MM=50", "CH=1"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR]}))
@pytest.mark.parametrize(("setpoint", "hvac_action", "expected"), [
    (COLD_SETPOINT, "heating", DeviceState.OFF),
    (COLD_SETPOINT - 5, "idle", DeviceState.OFF),
    (COLD_SETPOINT + 0.1, "heating", DeviceState.ON),
    # Current behaviour, known bug: closing all valves doesn't stop an active boiler
    (50.0, "idle", None),
])
async def test_cycle_heater_decision_table(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, spy, setpoint, hvac_action, expected):
    await boiler_data(**IDLE)
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": hvac_action, "hvac_modes": ["heat", "off"]})
    submit = spy(coordinator)

    with patch.object(climate, "_calculate_control_setpoint", return_value=setpoint):
        await run_loop(climate, mqtt_mock, published)

    assert submit.await_args_list[-1].args[0].heater_state == expected


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_opentherm_second_cycle_repeats_nothing_already_applied(hass, entry, climate, mqtt_mock, published):
    for key, payload in {"centralheating": "ON", "MaxRelModLevelSetting": "100.0", "Tboiler": "40.5", "Tret": "35.0", "TSet": "35.0"}.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", payload)
    await hass.async_block_till_done()

    first = await run_loop(climate, mqtt_mock, published)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    second = published()

    payloads = [c[1] for c in first]
    assert not [p for p in payloads if p.startswith(("CH=", "MM="))]
    assert any(p.startswith("CS=") for p in payloads)
    assert [c[1] for c in second] == payloads
