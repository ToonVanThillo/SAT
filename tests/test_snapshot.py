"""Tests for BoilerSnapshot, RelativeModulation on a given boiler state, and the one-snapshot-per-cycle control loop."""
import json
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.boiler import BoilerState
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_MODE,
    MINIMUM_SETPOINT,
    MODE_FAKE,
    BoilerStatus,
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
    is_active=False, is_inactive=True, status=BoilerStatus.IDLE, flame_active=False, hot_water_active=False,
    setpoint=40.0, flow_temperature=None, return_temperature=None, relative_modulation_level=None,
)


# Snapshot

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_snapshot_copies_the_coordinator_values_and_is_immutable(hass, entry, coordinator, boiler_data):
    await boiler_data(**IDLE | {"heatingactive": "on"})
    snapshot = coordinator.snapshot()

    assert snapshot == BoilerSnapshot(
        boiler=coordinator.boiler,
        flame=coordinator.flame,
        maximum_relative_modulation_value=coordinator.maximum_relative_modulation_value,
        manufacturer=coordinator.manufacturer,
        readings=coordinator.data.readings(),
    )
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

@pytest.mark.parametrize(("hot_water", "setpoint", "pwm", "expected"), [
    (True, 40.0, True, RelativeModulationState.HOT_WATER),
    (True, None, False, RelativeModulationState.HOT_WATER),
    (True, MINIMUM_SETPOINT, True, RelativeModulationState.HOT_WATER),
    (False, None, True, RelativeModulationState.COLD),
    (False, MINIMUM_SETPOINT, False, RelativeModulationState.COLD),
    (False, MINIMUM_SETPOINT - 1, True, RelativeModulationState.COLD),
    (False, MINIMUM_SETPOINT + 0.1, True, RelativeModulationState.OFF),
    (False, 40.0, True, RelativeModulationState.OFF),
    (False, MINIMUM_SETPOINT + 0.1, False, RelativeModulationState.PULSE_WIDTH_MODULATION_OFF),
    (False, 40.0, False, RelativeModulationState.PULSE_WIDTH_MODULATION_OFF),
])
async def test_relative_modulation_state_follows_the_given_boiler(hot_water, setpoint, pwm, expected):
    relative_modulation = RelativeModulation("radiators")
    await relative_modulation.update(pwm)
    boiler = replace(BOILER_STATE, hot_water_active=hot_water, setpoint=setpoint)

    assert relative_modulation.state(boiler) == expected
    assert relative_modulation.enabled(boiler) is (expected != RelativeModulationState.OFF)


async def test_relative_modulation_defaults_to_pwm_disabled():
    assert RelativeModulation("radiators").state(BOILER_STATE) == RelativeModulationState.PULSE_WIDTH_MODULATION_OFF


# Control cycle

def forced_pwm_entry(**data) -> tuple:
    """Return the parametrization of the EMS entry with forced PWM, with the given data overrides."""
    names, [(domains, entry_data, options, config)] = ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True})
    return names, [(domains, entry_data | data, options, config)]


@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry", "no_publish_error")
class TestEmsCycle:
    @staticmethod
    def inject_after_snapshot(coordinator, hass, **data) -> None:
        """Make a boiler_data message arrive right after the next snapshot, before any decision, as device data arriving mid-cycle."""
        take = coordinator.snapshot

        def take_then_inject() -> BoilerSnapshot:
            del coordinator.snapshot
            snapshot = take()
            async_fire_mqtt_message(hass, "ems-esp/boiler_data", json.dumps(IDLE | data))
            return snapshot

        coordinator.snapshot = take_then_inject

    @pytest.mark.parametrize(*ems_entry())
    async def test_heater_decision_ignores_heating_enabled_arriving_mid_cycle(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        self.inject_after_snapshot(coordinator, hass, heatingoff="off")

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
        self.inject_after_snapshot(coordinator, hass, heatingoff="on")

        # Cold target: heating was enabled when the cycle started, so the heater is still stopped
        assert (await run_loop(climate, mqtt_mock, published, target=15.0))[-1] == message('{"cmd": "heatingoff", "value": "on"}')
        assert coordinator.data["heatingoff"] == "on"

    @pytest.mark.parametrize(*forced_pwm_entry())
    async def test_modulation_ignores_hot_water_arriving_mid_cycle(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published):
        # heatingoff "on" hides a heater OFF write: with a minimum setpoint ≤ COLD_SETPOINT (28.2) the PWM ON phase requests heater OFF (known bug, see docs/known-issues.md)
        await boiler_data(**IDLE | {"heatingoff": "on"})
        self.inject_after_snapshot(coordinator, hass, tapwateractive="on", heatingoff="on")

        # Without hot water at the start, PWM turns modulation down to 0; hot water would keep it at 100
        assert await run_loop(climate, mqtt_mock, published) == [
            message('{"cmd": "selflowtemp", "value": 20}'),
            message('{"cmd": "burnmaxpower", "value": 0}'),
        ]
        assert coordinator.hot_water_active


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*forced_pwm_entry(**{CONF_MODE: MODE_FAKE}))
async def test_fake_steps_after_the_setpoint_write_use_the_setpoint_from_before(entry, climate, coordinator):
    coordinator.config = SatFakeConfig(True, supports_relative_modulation_management=True)
    written = []
    original = coordinator.async_set_control_max_relative_modulation

    async def record(value: int) -> None:
        written.append(value)
        await original(value)

    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    with patch.object(coordinator, "async_set_control_max_relative_modulation", record):
        await climate.async_control_heating_loop()

    # Intended behaviour change of the snapshot step: the fake applies the new setpoint immediately,
    # but modulation is decided from the setpoint before the write (none yet: COLD, so modulation stays at 100)
    assert coordinator.setpoint is not None and coordinator.setpoint > MINIMUM_SETPOINT
    assert written == [100]


@pytest.mark.parametrize(*otgw_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_manufacturer_resolved_by_the_update_step_applies_in_the_same_cycle(hass, entry, climate, mqtt_mock, published):
    for key, value in {"slave_memberid_code": "4", "TSet": "40", "Tboiler": "40.5", "Tret": "35", "flame": "OFF", "centralheating": "OFF"}.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()

    # The manufacturer is only resolved from the member id during the coordinator update of this cycle
    with patch.object(ManufacturerFactory, "resolve_by_member_id", return_value=[Geminox()]):
        await run_loop(climate, mqtt_mock, published)

    # Forced PWM without hot water would turn modulation down to 0, but a Geminox needs at least 10
    assert ("otgw/set/gw1/command", "MM=10", 1, False) in published()
    assert ("otgw/set/gw1/command", "MM=0", 1, False) not in published()


# Presentation stays live

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_presentation_reflects_changes_after_the_last_cycle(hass, entry, climate, boiler_data, freezer):
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await boiler_data(**IDLE)
    await climate.async_control_heating_loop()
    assert climate.hvac_action == HVACAction.IDLE
    assert climate.extra_state_attributes["relative_modulation_state"] == RelativeModulationState.PULSE_WIDTH_MODULATION_OFF.name

    await boiler_data(**IDLE | {"heatingactive": "on", "tapwateractive": "on"})

    assert climate.hvac_action == HVACAction.HEATING
    assert climate.extra_state_attributes["relative_modulation_state"] == RelativeModulationState.HOT_WATER.name
