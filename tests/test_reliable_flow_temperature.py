"""The pump state and the reliable flow temperature: what makes a reading reliable, what reads it, and the diagnostic sensor.

A combi boiler may stop its pump straight after a hot water draw; the water left in the boiler then reads stale and too high while the
pump is off. A reading is reliable when it is live, SAT is not producing hot water, the pump is not off and no hot water signal shows;
until then the control logic reads the last reliable reading instead of the raw one.
"""
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory

from custom_components.sat.const import (
    CONF_HOT_WATER_BURNER_POWER,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MQTT_TOPIC,
    DOMAIN,
    MINIMUM_SETPOINT,
    BoilerStatus,
    PWMStatus,
)
from custom_components.sat.fake import SatFakeCoordinator
from custom_components.sat.minimum_setpoint import MinimumSetpoint
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.pwm import PWM
from tests.conftest import TIMELINE, advance
from tests.const import EMS_BOILER_DATA, IDLE, ems_entry, otgw_entry
from tests.test_pwm_cycles import BASE, CURVE, Clock, config, setpoint_for

PUMP_ON = IDLE | {"heatingpump": "on"}
NO_PUMP_KEY = {key: value for key, value in IDLE.items() if key != "heatingpump"}


def boiler_state(flow: float | None, reliable: float | None, **overrides) -> SimpleNamespace:
    """A boiler state (as far as the PWM and the minimum setpoint read it) with distinct raw and reliable flow temperatures."""
    values = {
        "status": BoilerStatus.AT_SETPOINT, "flame_active": False, "hot_water_active": False, "pump_active": None, "setpoint": None,
        "flow_temperature": flow, "reliable_flow_temperature": reliable, "return_temperature": 35.0, "relative_modulation_level": 50.0,
    }

    return SimpleNamespace(**values | overrides)


def flame_state(active: bool = False, latest: float | None = None, average: float | None = 100) -> SimpleNamespace:
    return SimpleNamespace(is_active=active, is_inactive=not active, latest_on_time_seconds=latest, average_on_time_seconds=average, seconds_since_hot_water_burn=None)


# The pump state

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(("payload", "expected"), [
    (PUMP_ON, True),
    (IDLE | {"heatingpump": "off"}, False),
    (IDLE | {"heatingpump": "unknown"}, None),
    (IDLE | {"heatingpump": ""}, None),
    (NO_PUMP_KEY, None),
])
async def test_ems_pump_active_follows_the_live_heating_pump(entry, coordinator, boiler_data, payload, expected):
    await boiler_data(**payload)

    assert coordinator.pump_active is expected


async def test_ems_pump_active_is_none_before_any_data(ems):
    assert ems.pump_active is None


async def test_ems_pump_active_is_none_for_restored_data(ems):
    ems.async_set_updated_data(PUMP_ON, restored=True)

    assert ems.pump_active is None


async def test_other_backends_have_no_pump_state(otgw):
    assert otgw.pump_active is None


# The reliable flow temperature: scenario 1, a hot water draw and a pump that stops

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_a_hot_water_draw_and_a_stopped_pump_do_not_move_the_reliable_temperature(hass, entry, coordinator, boiler_data, freezer: FrozenDateTimeFactory):
    async def cycle(**data) -> None:
        await boiler_data(**PUMP_ON | data)
        await coordinator.async_control_heating_loop()
        await advance(hass, freezer, 10)

    await cycle(curflowtemp=44.0)
    await cycle(curflowtemp=45.0)
    assert coordinator.reliable_boiler_temperature == 45.0
    derivative = coordinator.boiler_temperature_derivative
    cold = coordinator.boiler_temperature_cold
    tracking = coordinator.boiler_temperature_tracking
    assert derivative is not None

    def assert_no_samples(reliable: float = 45.0) -> None:
        # The history feeds the derivative and the cold temperature; the tracker takes the same samples
        assert coordinator.reliable_boiler_temperature == reliable
        assert coordinator.boiler_temperature_derivative == derivative
        assert coordinator.boiler_temperature_cold == cold
        assert coordinator.boiler_temperature_tracking == tracking

    # The hot water burn: flag on with the flame, then the flag off while the flame stays on
    await cycle(curflowtemp=55.0, burngas="on", tapwateractive="on")
    assert_no_samples()
    await cycle(curflowtemp=58.0, burngas="on", tapwateractive="off")
    assert coordinator.boiler.hot_water_active
    assert_no_samples()

    # The flame goes off as the pump stops, and the water left in the boiler reads high
    await cycle(curflowtemp=60.0, burngas="off", tapwateractive="off", heatingpump="off")
    assert not coordinator.boiler.hot_water_active
    assert_no_samples()
    assert coordinator.boiler_temperature == 60.0
    assert coordinator.boiler.flow_temperature == 60.0
    assert coordinator.boiler.reliable_flow_temperature == 45.0
    assert coordinator.boiler.pump_active is False

    # The pump runs again, no hot water: the new reading is reliable and feeds the history again
    await cycle(curflowtemp=50.0)
    assert coordinator.reliable_boiler_temperature == 50.0
    await cycle(curflowtemp=52.0)
    assert coordinator.reliable_boiler_temperature == 52.0


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_a_hot_water_burn_starting_in_the_payload_makes_the_reading_unreliable(entry, coordinator, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})

    # No control loop in between
    await boiler_data(**PUMP_ON | {"curflowtemp": 55.0, "burngas": "on", "tapwateractive": "on"})

    assert coordinator.reliable_boiler_temperature == 45.0


@pytest.fixture
async def hw(hass, mqtt_mock, no_publish_delay) -> SatEmsMqttCoordinator:
    """A started EMS coordinator with a hot water burner power and flow temperature configured, so burns are classified without the tap flag."""
    options = {CONF_HOT_WATER_BURNER_POWER: 134, CONF_HOT_WATER_FLOW_TEMPERATURE: 45, CONF_MAXIMUM_RELATIVE_MODULATION: 100}
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, options, config_entry=None)
    await coordinator.async_added_to_hass()

    return coordinator


@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}])
async def test_a_burn_classified_by_a_non_flag_signal_in_the_payload_is_unreliable_at_once(hw, boiler_data, signal):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    assert hw.reliable_boiler_temperature == 45.0

    # The flag is off: only the burner power or flow temperature says this burn is hot water, in the payload that starts the flame
    await boiler_data(**PUMP_ON | {"curflowtemp": 55.0, "burngas": "on", "tapwateractive": "off"} | signal)

    assert hw.boiler.hot_water_active
    assert hw.reliable_boiler_temperature == 45.0


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_a_hot_water_burn_is_unreliable_until_the_flame_goes_off(entry, coordinator, boiler_data):
    """ACCEPTED LIMITATION 1: without a configured hot water burner power or flow temperature, nothing but the tap flag and the flame tells a
    hot water burn; the pump overrun after a draw (flame and flag off, pump on) is reliable at once, stagnant water included."""
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    await boiler_data(**PUMP_ON | {"curflowtemp": 55.0, "burngas": "on", "tapwateractive": "on"})

    await boiler_data(**PUMP_ON | {"curflowtemp": 58.0, "burngas": "on", "tapwateractive": "off"})
    assert coordinator.reliable_boiler_temperature == 45.0

    # The flame is off and no hot-water signal is left: the pump overrun is reliable (without the burner power or flow temperature
    # signals configured, nothing tells it apart)
    await boiler_data(**PUMP_ON | {"curflowtemp": 52.0, "burngas": "off", "tapwateractive": "off"})
    assert coordinator.reliable_boiler_temperature == 52.0
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 52.0})
    assert coordinator.reliable_boiler_temperature == 52.0

    await boiler_data(**PUMP_ON | {"curflowtemp": 48.0})
    assert coordinator.reliable_boiler_temperature == 48.0


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_a_pump_that_is_off_makes_the_reading_unreliable(entry, coordinator, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})

    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 60.0})
    assert coordinator.reliable_boiler_temperature == 45.0

    await boiler_data(**IDLE | {"heatingpump": "on", "curflowtemp": 47.0})
    assert coordinator.reliable_boiler_temperature == 47.0


# Scenario 3: no pump state

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_without_a_pump_state_readings_are_reliable_except_during_hot_water(entry, coordinator, boiler_data):
    await boiler_data(**NO_PUMP_KEY | {"curflowtemp": 45.0})
    assert coordinator.pump_active is None
    assert coordinator.reliable_boiler_temperature == 45.0

    await boiler_data(**NO_PUMP_KEY | {"curflowtemp": 48.0})
    assert coordinator.reliable_boiler_temperature == 48.0

    await boiler_data(**NO_PUMP_KEY | {"curflowtemp": 60.0, "burngas": "on", "tapwateractive": "on"})
    assert coordinator.reliable_boiler_temperature == 48.0

    await boiler_data(**NO_PUMP_KEY | {"curflowtemp": 61.0, "burngas": "off", "tapwateractive": "off"})
    assert coordinator.reliable_boiler_temperature == 61.0


# Scenario 2: restored and first live readings

async def test_restored_data_gives_the_raw_value_and_is_never_locked_in(ems):
    ems.async_set_updated_data(PUMP_ON | {"curflowtemp": 33.0}, restored=True)
    assert ems.boiler_temperature == 33.0
    assert ems.reliable_boiler_temperature == 33.0

    # The first live reading replaces it, pump on or not
    ems.async_set_updated_data(PUMP_ON | {"curflowtemp": 41.0})
    assert ems.reliable_boiler_temperature == 41.0

    ems.async_set_updated_data(IDLE | {"heatingpump": "off", "curflowtemp": 60.0})
    assert ems.reliable_boiler_temperature == 41.0


async def test_the_raw_value_stands_in_until_a_reliable_reading_has_happened(ems, boiler_data):
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 44.0})
    assert ems.reliable_boiler_temperature == 44.0

    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 50.0})
    assert ems.reliable_boiler_temperature == 50.0

    await boiler_data(**PUMP_ON | {"curflowtemp": 42.0})
    assert ems.reliable_boiler_temperature == 42.0


async def test_the_raw_value_stands_in_during_hot_water_at_a_fresh_start(ems, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 58.0, "burngas": "on", "tapwateractive": "on"})

    assert ems.reliable_boiler_temperature == 58.0


async def test_a_restored_reading_is_not_reliable_once_live_data_arrived(ems, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})

    ems.async_set_updated_data(PUMP_ON | {"curflowtemp": 70.0}, restored=True)

    assert ems.reliable_boiler_temperature == 45.0


async def test_the_reliable_value_is_none_without_a_raw_value(ems):
    assert ems.boiler_temperature is None
    assert ems.reliable_boiler_temperature is None


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_the_reliable_value_is_kept_in_memory_only(hass, entry, coordinator, boiler_data, freezer: FrozenDateTimeFactory):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    assert coordinator.reliable_boiler_temperature == 45.0

    # After a reload the coordinator is new: nothing is locked in, so the raw value stands in
    await hass.config_entries.async_reload(entry.entry_id)
    await advance(hass, freezer, 10)
    reloaded = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 60.0})

    assert reloaded is not coordinator
    assert reloaded.reliable_boiler_temperature == 60.0


# Snapshot and device status

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_the_snapshot_carries_the_pump_state_and_the_reliable_temperature(entry, coordinator, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 60.0})

    state = coordinator.snapshot().boiler

    assert state.pump_active is False
    assert state.pump_active == coordinator.pump_active
    assert state.flow_temperature == 60.0
    assert state.reliable_flow_temperature == 45.0
    assert state.reliable_flow_temperature == coordinator.reliable_boiler_temperature


async def test_the_status_is_insufficient_data_without_a_raw_temperature(ems, boiler_data):
    await boiler_data(**{key: value for key, value in PUMP_ON.items() if key != "curflowtemp"})

    assert ems.boiler_temperature is None
    assert ems.device_status == BoilerStatus.INSUFFICIENT_DATA


async def test_the_status_compares_the_reliable_temperature(ems, boiler_data):
    # The setpoint is 40
    await boiler_data(**PUMP_ON | {"curflowtemp": 40.5})
    assert ems.device_status == BoilerStatus.AT_SETPOINT

    # The raw 60 would be far above the setpoint
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 60.0})
    assert ems.device_status == BoilerStatus.AT_SETPOINT


# PWM

@pytest.fixture
def clock() -> Iterator[Clock]:
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        yield clock


def make_pwm(force: bool = False) -> PWM:
    CURVE.value = 30.0
    pwm = PWM(config(), CURVE, False, True, force)  # type: ignore[arg-type]
    pwm.update_cycles_per_hour(20)  # 2 cycles per hour

    return pwm


async def test_the_duty_cycle_uses_the_reliable_flow_temperature(clock):
    pwm = make_pwm()

    # A requested setpoint of 35 over a base of 20: 50% of the way to a reliable 50, 30% of the way to a raw 70
    await pwm.update(boiler_state(70.0, 50.0), flame_state(), setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.last_duty_cycle_percentage == 50.0
    assert pwm.duty_cycle == (450 * 2, 450 * 2)
    assert pwm.status == PWMStatus.ON


async def test_the_stored_last_boiler_temperature_is_the_reliable_one(clock):
    pwm = make_pwm()
    await pwm.update(boiler_state(70.0, 50.0), flame_state(), setpoint_for(0.5))  # type: ignore[arg-type]

    # The pulse is on; a later raw 90 doesn't change the reference temperature of the duty cycle either
    clock.tick(10)
    await pwm.update(boiler_state(90.0, 50.0), flame_state(), setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.last_duty_cycle_percentage == 50.0


async def test_a_missing_reliable_temperature_idles_the_pwm_whatever_the_raw_one(clock):
    pwm = make_pwm()

    await pwm.update(boiler_state(50.0, None), flame_state(), setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.status == PWMStatus.IDLE


async def test_the_setpoint_adjuster_follows_the_reliable_temperature_with_the_flame_on(clock):
    pwm = make_pwm()

    await pwm.update(boiler_state(70.0, 50.0, flame_active=True), flame_state(True, latest=30), setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.setpoint == 47.0


async def test_the_setpoint_adjuster_forces_the_reliable_temperature_plus_ten_with_the_flame_off(clock):
    pwm = make_pwm()

    await pwm.update(boiler_state(80.0, 50.0), flame_state(average=30), setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.setpoint == 60.0


# Scenario 4: the flame-off force step does not run while hot water is active

async def test_the_force_step_is_skipped_while_hot_water_is_active_and_runs_after(clock):
    pwm = make_pwm()
    short_burns = flame_state(average=30)

    await pwm.update(boiler_state(60.0, 45.0, hot_water_active=True), short_burns, setpoint_for(0.5))  # type: ignore[arg-type]
    assert pwm.setpoint == MINIMUM_SETPOINT

    await pwm.update(boiler_state(60.0, 45.0), short_burns, setpoint_for(0.5))  # type: ignore[arg-type]
    assert pwm.setpoint == 55.0


async def test_the_force_step_keeps_an_existing_adjusted_value_during_hot_water(clock):
    pwm = make_pwm()
    short_burns = flame_state(average=30)
    await pwm.update(boiler_state(60.0, 45.0), short_burns, setpoint_for(0.5))  # type: ignore[arg-type]
    assert pwm.setpoint == 55.0

    await pwm.update(boiler_state(80.0, 70.0, hot_water_active=True), short_burns, setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.setpoint == 55.0


# Scenario 1 through the PWM: no force to 70 from the stale raw 60

async def test_a_stale_raw_flow_temperature_does_not_force_the_setpoint(clock):
    pwm = make_pwm()
    short_burns = flame_state(average=30)

    # The reliable temperature stays at 45 while the raw one reads 60 after the hot water draw
    await pwm.update(boiler_state(60.0, 45.0), short_burns, setpoint_for(0.5))  # type: ignore[arg-type]

    assert pwm.setpoint == 55.0
    assert pwm.setpoint != 70.0
    assert BASE < pwm.setpoint


# Dynamic minimum setpoint v1

async def warmed_minimum_setpoint(freezer: FrozenDateTimeFactory, first: SimpleNamespace) -> MinimumSetpoint:
    minimum = MinimumSetpoint(adjustment_factor=1.0, configured_minimum_setpoint=30.0)
    minimum.warming_up(first)  # type: ignore[arg-type]
    minimum.calculate(first, PWMStatus.IDLE)  # type: ignore[arg-type]
    freezer.tick(200)

    return minimum


async def test_the_normal_mode_check_and_the_candidate_use_the_reliable_temperature(freezer: FrozenDateTimeFactory):
    state = boiler_state(100.0, 80.0, setpoint=80.0)
    minimum = await warmed_minimum_setpoint(freezer, state)

    minimum.calculate(state, PWMStatus.IDLE)  # type: ignore[arg-type]

    # Normal mode (the reliable 80 is at the setpoint, the raw 100 is not): (1 - 0.5) * 80 - 3
    assert minimum.current == 37.0


async def test_the_normal_mode_is_not_entered_from_a_raw_temperature_at_the_setpoint(freezer: FrozenDateTimeFactory):
    state = boiler_state(80.0, 60.0, setpoint=80.0)
    minimum = await warmed_minimum_setpoint(freezer, state)

    minimum.calculate(state, PWMStatus.IDLE)  # type: ignore[arg-type]

    # The adjusted configured minimum: 30 + (35 - 35) * 1
    assert minimum.current == 30.0


# The diagnostic sensor

def sensor_entry(hass: HomeAssistant, entry):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "test-reliable-flow-temperature")

    return registry, entity_id


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_reliable_flow_temperature_sensor_shows_the_reliable_value(hass, entry, boiler_data, freezer: FrozenDateTimeFactory):
    registry, entity_id = sensor_entry(hass, entry)
    assert entity_id is not None

    registered = registry.async_get(entity_id)
    assert registered.entity_category == EntityCategory.DIAGNOSTIC
    assert registered.original_device_class == "temperature"
    assert registered.unit_of_measurement == "°C"

    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    await advance(hass, freezer, 10)
    state = hass.states.get(entity_id)
    assert float(state.state) == 45.0
    assert state.attributes["device_class"] == "temperature"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["unit_of_measurement"] == "°C"
    assert state.attributes["friendly_name"].endswith("Reliable Flow Temperature")

    # The pump stops and the raw reading rises: the sensor keeps the reliable value
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 60.0})
    await advance(hass, freezer, 10)
    assert float(hass.states.get(entity_id).state) == 45.0


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_every_backend_has_the_reliable_flow_temperature_sensor(hass, entry):
    registry, entity_id = sensor_entry(hass, entry)

    assert entity_id is not None
    assert registry.async_get(entity_id).entity_category == EntityCategory.DIAGNOSTIC


def test_the_ems_fixture_has_a_pump_that_is_off():
    # The tests above rely on it: the capture's pump is off, so tests that need a reliable reading send `heatingpump: on`
    assert EMS_BOILER_DATA["heatingpump"] == "off"


# C and D: the pump transition

HEATING_ON = PUMP_ON | {"heatingactive": "on"}
DRAW = {"burngas": "on", "tapwateractive": "on"}
OVERRUN = {"burngas": "off", "tapwateractive": "off"}


async def test_a_pump_that_is_off_shows_the_lower_of_the_raw_and_the_last_reliable_value(ems, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})

    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 38.0})
    assert ems.reliable_boiler_temperature == 38.0

    # The last reliable value was not lowered by the cooled reading
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 44.0})
    assert ems.reliable_boiler_temperature == 44.0
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 50.0})
    assert ems.reliable_boiler_temperature == 45.0


async def test_restored_data_takes_no_part_in_the_pump_transition(ems, boiler_data):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 55.0})

    ems.async_set_updated_data(PUMP_ON | {"curflowtemp": 50.0}, restored=True)

    assert ems.reliable_boiler_temperature == 45.0
    assert ems.pump_active is not True


# The update step on a backend that has no data arrival

async def test_the_update_step_keeps_the_reliable_value_on_a_backend_without_arrivals(hass):
    coordinator = SatFakeCoordinator(hass, {}, config_entry=None)
    hot_water = PropertyMock(return_value=False)

    with patch.object(SatFakeCoordinator, "hot_water_active", hot_water):
        await coordinator.async_set_boiler_temperature(45.0)
        await coordinator.async_control_heating_loop()
        assert coordinator.reliable_boiler_temperature == 45.0

        # Hot water: the raw value rises, the reliable value holds
        hot_water.return_value = True
        await coordinator.async_set_boiler_temperature(60.0)
        await coordinator.async_control_heating_loop()
        assert coordinator.boiler_temperature == 60.0
        assert coordinator.reliable_boiler_temperature == 45.0

        # After hot water the reading follows the raw value again
        hot_water.return_value = False
        await coordinator.async_set_boiler_temperature(50.0)
        await coordinator.async_control_heating_loop()
        assert coordinator.reliable_boiler_temperature == 50.0

        await coordinator.async_set_boiler_temperature(53.0)
        assert coordinator.reliable_boiler_temperature == 53.0


async def test_the_history_resumes_with_reliable_readings_after_the_pump_restarts(ems, boiler_data, freezer):
    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    await ems.async_control_heating_loop()
    freezer.tick(5)
    await boiler_data(**IDLE | {"heatingpump": "off", "curflowtemp": 60.0})
    await ems.async_control_heating_loop()
    assert ems.boiler_temperature_derivative is None

    freezer.tick(5)
    await boiler_data(**PUMP_ON | {"curflowtemp": 47.0})
    await ems.async_control_heating_loop()
    freezer.tick(5)
    await boiler_data(**PUMP_ON | {"curflowtemp": 49.0})
    await ems.async_control_heating_loop()

    # The last two samples are 47 and 49, 5 s apart; the skipped 60 is no sample
    assert ems.boiler_temperature_derivative == pytest.approx(0.4)


# The tracker takes reliable samples only

async def tracked(ems, boiler_data, freezer: FrozenDateTimeFactory) -> None:
    """A tracker that is active: the flame is off with a known setpoint (40) and reliable samples."""
    await boiler_data(**PUMP_ON | {"curflowtemp": 40.0})
    for _ in range(3):
        await ems.async_control_heating_loop()
        freezer.tick(5)
    assert ems.boiler_temperature_derivative is not None
    assert ems.boiler_temperature_tracking


@pytest.mark.parametrize("data", [
    pytest.param(DRAW, id="hot-water"),
    pytest.param({"burngas": "on", "heatingpump": "off"}, id="pump-off"),
])
async def test_the_tracker_ignores_an_unreliable_reading_above_the_setpoint(ems, boiler_data, freezer, data):
    await tracked(ems, boiler_data, freezer)

    # With the flame on a raw 60 over a setpoint of 40 would stop the tracking ("exceeds setpoint")
    freezer.tick(5)
    await boiler_data(**PUMP_ON | {"curflowtemp": 60.0} | data)
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature == 60.0
    assert ems.boiler_temperature_tracking


async def test_the_tracker_takes_a_reliable_reading_above_the_setpoint(ems, boiler_data, freezer):
    await tracked(ems, boiler_data, freezer)
    freezer.tick(5)

    await boiler_data(**PUMP_ON | {"curflowtemp": 60.0, "burngas": "on"})
    await ems.async_control_heating_loop()

    assert not ems.boiler_temperature_tracking


# PUMP_STARTING with a known pump state

OFF_THEN_ON = [IDLE | {"heatingpump": "off", "heatingactive": "on"}, HEATING_ON]


async def start_the_pump(ems, boiler_data, **data) -> None:
    await boiler_data(**OFF_THEN_ON[0] | data)
    await boiler_data(**OFF_THEN_ON[1] | data)


async def test_the_pump_starting_holds_from_an_off_to_on_transition_for_under_a_minute(ems, boiler_data, freezer):
    await boiler_data(**HEATING_ON)
    assert ems.device_status != BoilerStatus.PUMP_STARTING

    await start_the_pump(ems, boiler_data)
    assert ems.device_status == BoilerStatus.PUMP_STARTING

    freezer.tick(59)
    assert ems.device_status == BoilerStatus.PUMP_STARTING

    freezer.tick(1)
    assert ems.device_status != BoilerStatus.PUMP_STARTING


async def test_the_pump_starting_ends_at_the_first_update_with_a_rising_reliable_sample(ems, boiler_data, freezer):
    await start_the_pump(ems, boiler_data)
    await ems.async_control_heating_loop()
    assert ems.device_status == BoilerStatus.PUMP_STARTING

    # A flat reading does not end it
    freezer.tick(5)
    await ems.async_control_heating_loop()
    assert ems.boiler_temperature_derivative == 0
    assert ems.device_status == BoilerStatus.PUMP_STARTING

    freezer.tick(5)
    await boiler_data(**HEATING_ON | {"curflowtemp": 43.0})
    await ems.async_control_heating_loop()
    assert ems.boiler_temperature_derivative > 0
    assert ems.device_status != BoilerStatus.PUMP_STARTING

    # It does not return without a new transition, whatever the next updates show
    freezer.tick(5)
    await boiler_data(**HEATING_ON | {"curflowtemp": 41.0})
    await ems.async_control_heating_loop()
    assert ems.device_status != BoilerStatus.PUMP_STARTING


async def test_the_pump_starting_ends_when_the_pump_goes_off_and_returns_with_a_new_transition(ems, boiler_data):
    await start_the_pump(ems, boiler_data)
    assert ems.device_status == BoilerStatus.PUMP_STARTING

    await boiler_data(**IDLE | {"heatingpump": "off", "heatingactive": "on"})
    assert ems.device_status != BoilerStatus.PUMP_STARTING

    await boiler_data(**HEATING_ON)
    assert ems.device_status == BoilerStatus.PUMP_STARTING


async def test_the_pump_starting_is_not_held_by_the_first_live_pump_on(ems, boiler_data):
    # A first live on has no known off before it
    await boiler_data(**HEATING_ON)
    await boiler_data(**HEATING_ON)

    assert ems.device_status != BoilerStatus.PUMP_STARTING


async def test_a_transition_is_recorded_while_the_device_is_inactive(ems, boiler_data):
    await boiler_data(**IDLE | {"heatingpump": "off", "heatingactive": "off"})
    await boiler_data(**PUMP_ON | {"heatingactive": "off"})
    assert ems.device_status != BoilerStatus.PUMP_STARTING

    await boiler_data(**HEATING_ON)

    assert ems.device_status == BoilerStatus.PUMP_STARTING


async def test_without_a_pump_state_the_pump_starting_is_guessed_from_the_cold_temperature(ems, boiler_data, freezer):
    no_pump = NO_PUMP_KEY | {"heatingactive": "off"}

    # A cold reference of 45, then the device starts and the flow drops below it
    await boiler_data(**no_pump | {"curflowtemp": 45.0})
    await ems.async_control_heating_loop()
    freezer.tick(5)
    await ems.async_control_heating_loop()
    freezer.tick(5)
    await boiler_data(**no_pump | {"heatingactive": "on", "curflowtemp": 40.0})
    await ems.async_control_heating_loop()

    assert ems.pump_active is None
    assert ems.boiler_temperature_cold == 45.0
    assert ems.boiler_temperature_derivative < 0
    assert ems.device_status == BoilerStatus.PUMP_STARTING


async def test_with_a_known_pump_state_the_cold_guess_does_not_give_the_pump_starting(ems, boiler_data, freezer):
    pump = PUMP_ON | {"heatingactive": "off"}

    await boiler_data(**pump | {"curflowtemp": 45.0})
    await ems.async_control_heating_loop()
    freezer.tick(5)
    await ems.async_control_heating_loop()
    freezer.tick(5)
    await boiler_data(**pump | {"heatingactive": "on", "curflowtemp": 40.0})
    await ems.async_control_heating_loop()

    assert ems.boiler_temperature_derivative < 0
    assert ems.device_status != BoilerStatus.PUMP_STARTING


# The debug cycle line

def cycle_boiler_lines(caplog) -> list[str]:
    return [message for record in caplog.records if record.name == TIMELINE and (message := record.getMessage()).startswith("cycle ") and " boiler flow=" in message]


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.usefixtures("loop_entry")
async def test_the_cycle_line_shows_the_reliable_temperature_only_when_it_differs(entry, climate, boiler_data, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    await boiler_data(**PUMP_ON | {"curflowtemp": 45.0})
    await climate.async_control_heating_loop()
    assert " flow=45" in cycle_boiler_lines(caplog)[-1]
    assert "reliable=" not in cycle_boiler_lines(caplog)[-1]

    await boiler_data(**PUMP_ON | {"curflowtemp": 60.0} | DRAW)
    await climate.async_control_heating_loop()
    line = cycle_boiler_lines(caplog)[-1]
    assert " flow=60 reliable=45 " in line or " flow=60.0 reliable=45.0 " in line


# A pump that starts with a draw is no pump start, and a hot water burn ends a running one

async def test_a_pump_start_during_a_draw_is_no_pump_start_after_it(ems, boiler_data, freezer):
    active = IDLE | {"selflowtemp": 55, "heatingactive": "on"}
    statuses = []

    async def step(**data) -> None:
        await boiler_data(**active | data)
        statuses.append(ems.device_status)
        freezer.tick(10)

    await step(heatingpump="off", curflowtemp=40.0)
    # The pump starts in the payload of a short draw
    await step(heatingpump="on", curflowtemp=55.0, burngas="on", tapwateractive="on")
    await step(heatingpump="on", curflowtemp=55.0, burngas="off", tapwateractive="off")
    await step(heatingpump="off", curflowtemp=55.0)
    # The real restart after the draw
    await step(heatingpump="on", curflowtemp=55.0)

    assert statuses == [
        BoilerStatus.WAITING_FOR_FLAME,
        BoilerStatus.HOT_WATER,
        BoilerStatus.AT_SETPOINT,
        BoilerStatus.AT_SETPOINT,
        BoilerStatus.PUMP_STARTING,
    ]


async def test_a_pump_start_in_the_middle_of_a_draw_ends_with_the_draw(ems, boiler_data):
    active = IDLE | {"selflowtemp": 55, "heatingactive": "on", "curflowtemp": 55.0}

    await boiler_data(**active | {"heatingpump": "off"} | DRAW)
    await boiler_data(**active | {"heatingpump": "on"} | DRAW)
    assert ems.device_status == BoilerStatus.HOT_WATER

    await boiler_data(**active | {"heatingpump": "on"} | OVERRUN)
    assert ems.device_status != BoilerStatus.PUMP_STARTING


async def test_without_a_pump_state_no_pump_start_is_recorded(ems, boiler_data):
    no_pump = NO_PUMP_KEY | {"selflowtemp": 55, "heatingactive": "on"}

    await boiler_data(**no_pump | {"curflowtemp": 45.0})
    await boiler_data(**no_pump | {"curflowtemp": 58.0} | DRAW)
    await boiler_data(**no_pump | {"curflowtemp": 60.0} | OVERRUN)

    assert ems.pump_active is None
    assert ems.reliable_boiler_temperature == 60.0
    assert ems.device_status != BoilerStatus.PUMP_STARTING


async def test_a_pump_state_that_appears_after_a_draw_is_no_restart(ems, boiler_data):
    no_pump = NO_PUMP_KEY | {"selflowtemp": 55, "heatingactive": "on"}
    await boiler_data(**no_pump | {"curflowtemp": 45.0})
    await boiler_data(**no_pump | {"curflowtemp": 58.0} | DRAW)
    await boiler_data(**no_pump | {"curflowtemp": 50.0} | OVERRUN)
    assert ems.reliable_boiler_temperature == 50.0

    # The pump state becomes known: the first known state is no restart
    await boiler_data(**IDLE | {"selflowtemp": 55, "heatingactive": "on", "heatingpump": "on", "curflowtemp": 62.0})

    assert ems.pump_active is True
    assert ems.reliable_boiler_temperature == 62.0
    assert ems.device_status != BoilerStatus.PUMP_STARTING
