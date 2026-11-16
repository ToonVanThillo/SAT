"""Tests for BoilerSnapshot, the RelativeModulation decision, and the one-snapshot-per-cycle control loop."""
import json
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.boiler import BoilerState
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    MINIMUM_SETPOINT,
    MODE_FAKE,
    BoilerStatus,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.coordinator import BoilerSnapshot
from custom_components.sat.fake import SatFakeConfig
from custom_components.sat.manufacturer import ManufacturerFactory
from custom_components.sat.manufacturers.geminox import Geminox
from custom_components.sat.relative_modulation import (
    RelativeModulation,
    RelativeModulationState,
)
from tests.const import IDLE, ems_entry, otgw_entry
from tests.test_climate_loop import message, run_loop

BOILER_STATE = BoilerState(
    is_active=False, is_inactive=True, status=BoilerStatus.IDLE, flame_active=False, hot_water_active=False, pump_active=None,
    setpoint=40.0, flow_temperature=None, reliable_flow_temperature=None, return_temperature=None, relative_modulation_level=None,
)


# Snapshot

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_snapshot_copies_the_coordinator_values_and_is_immutable(hass, entry, coordinator, boiler_data):
    await boiler_data(**IDLE | {"heatingactive": "on"})
    snapshot = coordinator.snapshot()

    assert snapshot == BoilerSnapshot(
        cycle=snapshot.cycle,
        boiler=coordinator.boiler,
        flame=coordinator.flame,
        maximum_relative_modulation_value=coordinator.maximum_relative_modulation_value,
        manufacturer=coordinator.manufacturer,
        readings=coordinator.data.readings(),
    )
    assert snapshot.cycle >= 1
    assert coordinator.snapshot().cycle == snapshot.cycle + 1
    assert snapshot.boiler.is_active
    with pytest.raises(FrozenInstanceError):
        snapshot.boiler = None  # type: ignore[misc]

    # Later device data does not change the existing snapshot
    await boiler_data(**IDLE | {"heatingactive": "off", "tapwateractive": "on", "selflowtemp": 50, "burnmaxpower": 60})
    assert coordinator.boiler != snapshot.boiler
    assert snapshot.boiler.is_active
    assert not snapshot.boiler.hot_water_active
    assert snapshot.boiler.setpoint == 40
    assert snapshot.maximum_relative_modulation_value == 31
    assert coordinator.snapshot().maximum_relative_modulation_value == 60


# RelativeModulation

MINIMUM = ModulationMode.MINIMUM
UNRESTRICTED = ModulationMode.UNRESTRICTED


@pytest.mark.parametrize(("hot_water", "heater_off", "mode", "limits_hot_water", "expected"), [
    # Hot water first, where the max relative modulation limits it
    (True, False, MINIMUM, True, RelativeModulationState.HOT_WATER),
    (True, True, MINIMUM, True, RelativeModulationState.HOT_WATER),
    (True, True, UNRESTRICTED, True, RelativeModulationState.HOT_WATER),
    # Then a heater OFF request, where the max relative modulation limits hot water
    (False, True, MINIMUM, True, RelativeModulationState.HEATER_OFF_REQUESTED),
    (False, True, UNRESTRICTED, True, RelativeModulationState.HEATER_OFF_REQUESTED),
    # Then the PWM mode
    (False, False, MINIMUM, True, RelativeModulationState.MINIMUM),
    (False, False, UNRESTRICTED, True, RelativeModulationState.UNRESTRICTED),
    # Where it doesn't limit hot water, neither hot water nor a heater OFF request matters
    (True, False, MINIMUM, False, RelativeModulationState.MINIMUM),
    (True, True, MINIMUM, False, RelativeModulationState.MINIMUM),
    (False, True, MINIMUM, False, RelativeModulationState.MINIMUM),
    (True, True, UNRESTRICTED, False, RelativeModulationState.UNRESTRICTED),
    (False, True, UNRESTRICTED, False, RelativeModulationState.UNRESTRICTED),
])
async def test_relative_modulation_state_precedence(hot_water, heater_off, mode, limits_hot_water, expected):
    relative_modulation = RelativeModulation("radiators", limits_hot_water)
    relative_modulation.update(replace(BOILER_STATE, hot_water_active=hot_water), mode, heater_off)

    assert relative_modulation.state == expected
    assert relative_modulation.enabled is (expected != RelativeModulationState.MINIMUM)


@pytest.mark.parametrize("setpoint", [None, 0.0, MINIMUM_SETPOINT, 40.0])
async def test_relative_modulation_ignores_the_read_back_setpoint(setpoint):
    relative_modulation = RelativeModulation("radiators", True)
    relative_modulation.update(replace(BOILER_STATE, setpoint=setpoint), MINIMUM, False)

    assert relative_modulation.state == RelativeModulationState.MINIMUM


async def test_relative_modulation_defaults_to_the_minimum_mode():
    assert RelativeModulation("radiators", True).state == RelativeModulationState.MINIMUM


# Control cycle

def unrestricted_disallowed_entry(**data) -> tuple:
    """Return the parametrization of the EMS entry with unrestricted modulation disallowed, with the given data overrides."""
    names, [(domains, entry_data, options, config)] = ems_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False})
    return names, [(domains, entry_data | data, options, config)]


def inject_after_snapshot(coordinator, hass, topic: str, payload: str) -> None:
    """Make an MQTT message arrive right after the next snapshot, before any decision, as device data arriving mid-cycle."""
    take = coordinator.snapshot

    def take_then_inject() -> BoilerSnapshot:
        del coordinator.snapshot
        snapshot = take()
        async_fire_mqtt_message(hass, topic, payload)
        return snapshot

    coordinator.snapshot = take_then_inject


@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry", "no_publish_error")
class TestEmsCycle:

    @pytest.mark.parametrize(*ems_entry())
    async def test_heater_decision_ignores_heating_enabled_arriving_mid_cycle(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        inject_after_snapshot(coordinator, hass, "ems-esp/boiler_data", json.dumps(IDLE | {"heatingoff": "off"}))

        # Heating was disabled when the cycle started, so the heater is still started
        assert await run_loop(climate, mqtt_mock, published) == [
            message('{"cmd": "selflowtemp", "value": 35}'),
            message('{"cmd": "burnmaxpower", "value": 100}'),
            message('{"cmd": "heatingoff", "value": "off"}'),
        ]
        assert coordinator.data["heatingoff"] == "off"

    @pytest.mark.parametrize(*ems_entry())
    async def test_heater_stop_ignores_heating_disabled_arriving_mid_cycle(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published):
        await boiler_data(**IDLE | {"heatingoff": "off"})
        inject_after_snapshot(coordinator, hass, "ems-esp/boiler_data", json.dumps(IDLE | {"heatingoff": "on"}))

        # Cold target: heating was enabled when the cycle started, so the heater is still stopped
        assert (await run_loop(climate, mqtt_mock, published, target=15.0))[-1] == message('{"cmd": "heatingoff", "value": "on"}')
        assert coordinator.data["heatingoff"] == "on"


# A minimum above COLD_SETPOINT (28.2) and a boiler temperature, so the first cycle starts a pulse at the minimum with heater ON
@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*unrestricted_disallowed_entry(**{CONF_MODE: MODE_FAKE, CONF_MINIMUM_SETPOINT: 30}))
async def test_fake_modulation_follows_the_cycles_heater_request_not_the_setpoint_read_back(entry, climate, coordinator):
    coordinator.config = SatFakeConfig(True, supports_relative_modulation_management=True)
    await coordinator.async_set_boiler_temperature(40)
    written = []
    original = coordinator.async_set_control_max_relative_modulation

    async def record(value: int) -> None:
        written.append(value)
        await original(value)

    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    with patch.object(coordinator, "async_set_control_max_relative_modulation", record):
        await climate.async_control_heating_loop()

    # The snapshot has no setpoint yet, but modulation follows this cycle's heater ON request: the PWM minimum 0, not the maximum
    assert climate.pwm.status == PWMStatus.ON
    assert coordinator.setpoint == 30
    assert climate.relative_modulation_state == RelativeModulationState.MINIMUM
    assert written == [0]


# A minimum above COLD_SETPOINT (28.2), so the MINIMUM pulse requests heater ON and the minimum max relative modulation
@pytest.mark.parametrize(*otgw_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}, {CONF_MINIMUM_SETPOINT: 30}))
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_manufacturer_resolved_by_the_update_step_applies_in_the_same_cycle(hass, entry, climate, mqtt_mock, published):
    for key, value in {"slave_memberid_code": "4", "TSet": "40", "Tboiler": "40.5", "Tret": "35", "flame": "OFF", "centralheating": "OFF"}.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()

    # The manufacturer is only resolved from the member id during the coordinator update of this cycle
    with patch.object(ManufacturerFactory, "resolve_by_member_id", return_value=[Geminox()]):
        await run_loop(climate, mqtt_mock, published)

    # MINIMUM mode without hot water would turn modulation down to 0, but a Geminox needs at least 10
    assert ("otgw/set/gw1/command", "MM=10", 1, False) in published()
    assert ("otgw/set/gw1/command", "MM=0", 1, False) not in published()


# A minimum above COLD_SETPOINT (28.2), so the MINIMUM pulse requests heater ON and the minimum max relative modulation
@pytest.mark.parametrize(*otgw_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}, {CONF_MINIMUM_SETPOINT: 30}))
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry", "no_publish_error")
async def test_modulation_ignores_hot_water_arriving_mid_cycle(hass, entry, climate, coordinator, mqtt_mock, published):
    for key, value in {"TSet": "40", "Tboiler": "40.5", "Tret": "35", "flame": "OFF", "centralheating": "OFF", "domestichotwater": "OFF"}.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()
    inject_after_snapshot(coordinator, hass, "otgw/value/gw1/domestichotwater", "ON")

    # Without hot water at the start, PWM turns modulation down to 0; hot water would keep it at 100
    commands = [c[1] for c in await run_loop(climate, mqtt_mock, published) if c[0] == "otgw/set/gw1/command"]
    assert "MM=0" in commands
    assert "MM=100" not in commands
    assert coordinator.hot_water_active


# Presentation stays live

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_presentation_reflects_changes_after_the_last_cycle(hass, entry, climate, boiler_data, freezer):
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await boiler_data(**IDLE)
    await climate.async_control_heating_loop()
    assert climate.hvac_action == HVACAction.IDLE
    assert climate.relative_modulation_state == RelativeModulationState.UNRESTRICTED

    await boiler_data(**IDLE | {"heatingactive": "on", "selflowtemp": 0})

    # The relative modulation state is the last cycle's decision, not derived from the later read-back
    assert climate.hvac_action == HVACAction.HEATING
    assert climate.relative_modulation_state == RelativeModulationState.UNRESTRICTED
