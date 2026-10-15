"""Tests for ClimateInputs, the one-capture-per-cycle control loop, the error helpers and the heater state rules."""
from dataclasses import FrozenInstanceError
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.climate import ClimateInputs, SatClimate
from custom_components.sat.const import (
    CONF_HEATING_MODE,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_RADIATORS,
    CONF_ROOMS,
    CONF_THERMAL_COMFORT,
    HEATING_MODE_ECO,
)
from custom_components.sat.coordinator import DeviceState
from tests.conftest import advance
from tests.const import IDLE
from tests.test_climate_loop import message
from tests.test_climate_loop_ems import INSIDE, RADIATOR, with_data

ROOM = "climate.room"
HUMIDITY = "sensor.test_humidity"
HEATER_ON = message('{"cmd": "heatingoff", "value": "off"}')
HEATER_OFF = message('{"cmd": "heatingoff", "value": "on"}')


def radiator(hvac_action: str) -> dict:
    return {"hvac_action": hvac_action, "hvac_modes": ["heat", "off"]}


# ClimateInputs

def test_climate_inputs_is_frozen_and_keyword_only():
    inputs = ClimateInputs(target_temperature=21.0, current_temperature=None, outside_temperature=10.0, valves_open=True)

    with pytest.raises(FrozenInstanceError):
        inputs.valves_open = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        ClimateInputs(21.0, 20.0, 10.0, True)  # type: ignore[misc]


# Mid-cycle changes are not seen by later steps of the cycle (spec: accepted behaviour change)

@pytest.mark.usefixtures("loop_entry", "no_publish_error")
class TestMidCycleChanges:
    @staticmethod
    def after_update_step(coordinator, change) -> None:
        """Run the async `change` once, right after the coordinator update step of the next cycle: its inputs are captured, no decision is made yet."""
        update = coordinator.async_control_heating_loop

        async def update_then_change(*args, **kwargs) -> None:
            del coordinator.async_control_heating_loop
            await update(*args, **kwargs)
            await change()

        coordinator.async_control_heating_loop = update_then_change

    @pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR]}))
    @pytest.mark.parametrize(("before", "during", "expected"), [("heating", "idle", [HEATER_ON]), ("idle", "heating", [])])
    async def test_heater_decision_ignores_valves_changing_mid_cycle(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, before, during, expected):
        await boiler_data(**IDLE)
        await climate.async_set_target_temperature(21.0)
        await climate.async_set_hvac_mode(HVACMode.HEAT)
        hass.states.async_set(RADIATOR, "heat", radiator(before))
        mqtt_mock.async_publish.reset_mock()

        async def change() -> None:
            hass.states.async_set(RADIATOR, "heat", radiator(during))

        self.after_update_step(coordinator, change)
        await climate.async_control_heating_loop()

        assert hass.states.get(RADIATOR).attributes["hvac_action"] == during
        assert [m for m in published() if "heatingoff" in m[1]] == expected

    @pytest.fixture
    async def integral_cycle(self, hass, entry, climate, coordinator, boiler_data, freezer):
        """Return a coroutine function that runs one cycle during which `change` happens and returns the errors the cycle gave the integral; the loops debounced by the setup are run before it and its own rescheduling is suppressed."""
        await boiler_data(**IDLE)
        await climate.async_set_target_temperature(21.0)
        await climate.async_set_hvac_mode(HVACMode.HEAT)

        update_integral = climate.pid.update_integral

        async def run(change, setup=None) -> list[float]:
            if setup is not None:
                await setup()

            # Run the loops debounced by the setup
            await advance(hass, freezer, 10)

            errors: list[float] = []
            changed = False

            def record(error, heating_curve_value, force=False) -> None:
                # Sensor handlers update the PID with force; only the cycle's update is recorded
                if not force:
                    errors.append(error.value)
                update_integral(error, heating_curve_value, force)

            async def timed_change() -> None:
                nonlocal changed
                await change()
                freezer.tick(timedelta(seconds=400))
                changed = True

            self.after_update_step(coordinator, timed_change)
            with patch.object(climate.pid, "update_integral", record), patch.object(climate, "schedule_control_heating_loop"):
                await climate.async_control_heating_loop()

            # Drain what became due once the clock moved
            await advance(hass, freezer, 10)

            assert changed  # the mid-cycle change happened
            return errors

        return run

    @pytest.mark.parametrize(*with_data({}))
    @pytest.mark.parametrize("change_name", ["target", "inside"])
    async def test_integral_update_ignores_error_changing_mid_cycle(self, hass, entry, climate, integral_cycle, change_name):
        async def change() -> None:
            # Error 0.1 -> 0.05, both within the deadband
            if change_name == "target":
                await climate.async_set_target_temperature(20.95)
            else:
                hass.states.async_set(INSIDE, "20.95")
                await hass.async_block_till_done()

        # One cycle ran, with the error of 0.1 captured at its start, and the integral accumulated it over the 400+ seconds since the last update
        assert await integral_cycle(change) == [0.1]
        assert 400 <= climate.pid.integral / (climate.pid.ki * 0.1) <= 450

    @pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, {CONF_THERMAL_COMFORT: True}))
    async def test_integral_update_ignores_humidity_changing_mid_cycle(self, hass, entry, climate, integral_cycle, freezer):
        async def setup() -> None:
            # Let the PID accept updates, then pick the target so that the comfort error is within the deadband
            await advance(hass, freezer, 60)
            hass.states.async_set(HUMIDITY, "40")
            await advance(hass, freezer, 60)
            await climate.async_set_target_temperature(climate.current_temperature - 0.1)

        async def change() -> None:
            hass.states.async_set(HUMIDITY, "90")
            await hass.async_block_till_done()

        # One cycle ran, with the comfort error of -0.1 captured at its start, not the -3.6 that 90 % humidity gives
        assert await integral_cycle(change, setup) == [-0.1]
        assert climate.error == -3.6
        assert 400 <= climate.pid.integral / (climate.pid.ki * -0.1) <= 450


# Error helpers

@pytest.mark.parametrize(("target", "current", "expected"), [
    (21.0, 20.9, 0.1), (21.0, 22.5, -1.5), (20.0, 20.0, 0), (20.123, 19.0, 1.12), (None, 20.0, 0), (21.0, None, 0), (None, None, 0),
])
def test_get_error(target, current, expected):
    assert SatClimate.get_error(target, current) == expected


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_max_error_includes_the_areas(hass, entry, climate):
    await climate.async_set_target_temperature(21.0)
    hass.states.async_set(ROOM, "heat", {"temperature": 22, "current_temperature": 18})  # room error 4.0

    assert climate.error == 0.1
    assert (climate.get_max_error(1.0).entity_id, climate.get_max_error(1.0).value) == (ROOM, 4.0)
    assert (climate.get_max_error(5.0).entity_id, climate.get_max_error(5.0).value) == (climate.entity_id, 5.0)
    assert (climate.max_error.entity_id, climate.max_error.value) == (ROOM, 4.0)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_max_error_tie_goes_to_the_climate(hass, entry, climate):
    hass.states.async_set(ROOM, "heat", {"temperature": 22, "current_temperature": 18})  # room error 4.0

    assert climate.get_max_error(4.0).entity_id == climate.entity_id


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}, {CONF_HEATING_MODE: HEATING_MODE_ECO}))
async def test_eco_mode_max_error_ignores_the_areas(hass, entry, climate):
    await climate.async_set_target_temperature(21.0)
    hass.states.async_set(ROOM, "heat", {"temperature": 22, "current_temperature": 18})

    assert (climate.get_max_error(1.0).entity_id, climate.get_max_error(1.0).value) == (climate.entity_id, 1.0)
    assert (climate.max_error.entity_id, climate.max_error.value) == (climate.entity_id, 0.1)


# Heater state

@pytest.mark.parametrize(*with_data({}))
@pytest.mark.parametrize(("state", "active", "args", "expected"), [
    (DeviceState.ON, False, {"valves_open": True}, [HEATER_ON]),
    (DeviceState.ON, False, {"valves_open": False}, []),
    (DeviceState.ON, False, {}, []),
    (DeviceState.ON, True, {"valves_open": True}, []),
    (DeviceState.OFF, True, {}, [HEATER_OFF]),
    (DeviceState.OFF, True, {"valves_open": True}, [HEATER_OFF]),
    (DeviceState.OFF, False, {"valves_open": True}, []),
])
@pytest.mark.usefixtures("loop_entry")
async def test_set_heater_state_rules(entry, climate, coordinator, boiler_data, mqtt_mock, published, state, active, args, expected):
    await boiler_data(**IDLE | {"heatingactive": "on" if active else "off"})
    mqtt_mock.async_publish.reset_mock()

    await climate.async_set_heater_state(state, coordinator.snapshot(), **args)

    assert published() == expected


# Presentation

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({}))
@pytest.mark.parametrize(("boiler", "state"), [(IDLE, "PULSE_WIDTH_MODULATION_OFF"), (IDLE | {"tapwateractive": "on"}, "HOT_WATER")])
async def test_relative_modulation_attributes(entry, climate, boiler_data, boiler, state):
    await boiler_data(**boiler)
    attributes = climate.extra_state_attributes

    assert attributes["relative_modulation_state"] == state
    assert attributes["relative_modulation_enabled"] is True
    assert attributes["relative_modulation_value"] == 100
