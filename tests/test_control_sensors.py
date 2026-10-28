"""Heating Curve, Error Value and the control-loop diagnostic sensors, and the climate's attribute set."""
import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import Event, callback
from homeassistant.helpers import entity_registry as er

from custom_components.sat.const import (
    CONF_AUTOMATIC_DUTY_CYCLE,
    CONF_DUTY_CYCLE,
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    MODE_FAKE,
)
from custom_components.sat.fake import SatFakeCoordinator
from tests.conftest import advance
from tests.const import IDLE, ems_entry, with_data

pytestmark = pytest.mark.usefixtures("loop_entry")

# name, entity_id, unique_id, unit, device class
CONTROL = [
    ("proportional", "sensor.proportional_test", "test-proportional", "°C", None),
    ("integral", "sensor.integral_test", "test-integral", "°C", None),
    ("derivative", "sensor.derivative_test", "test-derivative", "°C", None),
    ("kp", "sensor.kp_test", "test-kp", None, None),
    ("ki", "sensor.ki_test", "test-ki", None, None),
    ("kd", "sensor.kd_test", "test-kd", None, None),
    ("minimum_setpoint", "sensor.minimum_setpoint_test", "test-minimum-setpoint", "°C", SensorDeviceClass.TEMPERATURE),
    ("autotune", "sensor.autotune_coefficient_test", "test-autotune-coefficient", None, None),
    ("flame", "sensor.flame_average_on_time_test", "test-flame-average-on-time", "s", SensorDeviceClass.DURATION),
]
DUTY = ("sensor.pwm_duty_cycle_test", "test-pwm-duty-cycle")
HEATING_CURVE = "sensor.heating_curve_test"
ERROR_VALUE = "sensor.error_value_test"
DISABLED_BY_DEFAULT = [
    "sensor.pwm_status", "sensor.pwm_cycles_per_hour", "sensor.reliable_flow_temperature", DUTY[0],
] + [row[1] for row in CONTROL]

STANDARD_ATTRIBUTES = {
    "current_temperature", "temperature", "hvac_action", "hvac_modes", "min_temp", "max_temp", "target_temp_step", "preset_mode",
    "preset_modes", "supported_features", "friendly_name", "current_humidity",
}


async def heat(climate, boiler_data, target=21.0, **data):
    await boiler_data(**IDLE | {"heatingoff": "on"} | data)
    await climate.async_set_target_temperature(target)
    await climate.async_set_hvac_mode(HVACMode.HEAT)


def value(hass, entity_id):
    return hass.states.get(entity_id).state


# Registry, fresh setup

@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_diagnostic_entities_are_disabled_by_default(hass, entry):
    registry = er.async_get(hass)

    for entity_id in DISABLED_BY_DEFAULT:
        item = registry.async_get(entity_id)
        assert item is not None, entity_id
        assert item.disabled_by == er.RegistryEntryDisabler.INTEGRATION, entity_id
        assert item.entity_category == EntityCategory.DIAGNOSTIC, entity_id
        assert hass.states.get(entity_id) is None, entity_id


@pytest.mark.parametrize(*ems_entry())
async def test_synchro_sensors_are_disabled_by_default(hass, entry):
    items = [i for i in er.async_get(hass).entities.values() if i.entity_id.endswith("_synchro")]

    assert items
    assert all(i.disabled_by == er.RegistryEntryDisabler.INTEGRATION and i.entity_category == EntityCategory.DIAGNOSTIC for i in items)


@pytest.mark.parametrize(*ems_entry())
async def test_regular_sensors_are_enabled(hass, entry):
    registry = er.async_get(hass)

    for entity_id, unique_id in [(HEATING_CURVE, "test-heating-curve"), (ERROR_VALUE, "test-error-value"), ("sensor.calculated_setpoint_test", "test-calculated-setpoint")]:
        item = registry.async_get(entity_id)
        assert item is not None
        assert item.unique_id == unique_id
        assert item.disabled_by is None
        assert item.entity_category is None
        assert hass.states.get(entity_id) is not None


@pytest.mark.parametrize(*ems_entry())
async def test_control_sensor_registry_entries(hass, entry):
    registry = er.async_get(hass)

    for _key, entity_id, unique_id, _unit, _cls in CONTROL:
        item = registry.async_get(entity_id)
        assert item is not None, entity_id
        assert item.unique_id == unique_id
        assert item.domain == "sensor"


# States with entities enabled

@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_control_sensor_metadata(hass, entry):
    for _key, entity_id, _uid, unit, device_class in CONTROL:
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes.get("unit_of_measurement") == unit, entity_id
        assert state.attributes.get("device_class") == (device_class.value if device_class else None), entity_id
        assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
        assert set(state.attributes) <= {"friendly_name", "unit_of_measurement", "device_class", "state_class"}, entity_id


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_regular_sensor_metadata(hass, entry):
    for entity_id in (HEATING_CURVE, ERROR_VALUE):
        assert hass.states.get(entity_id).attributes["unit_of_measurement"] == "°C"

    # The error is an offset, so it has no temperature device class
    assert hass.states.get(HEATING_CURVE).attributes["device_class"] == "temperature"
    assert "device_class" not in hass.states.get(ERROR_VALUE).attributes


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_autotune_and_flame_are_unknown_before_first_value(hass, entry, climate, coordinator):
    assert climate.heating_curve.optimal_coefficient is None
    assert value(hass, "sensor.autotune_coefficient_test") == STATE_UNKNOWN
    assert coordinator.flame.average_on_time_seconds is None
    assert value(hass, "sensor.flame_average_on_time_test") == STATE_UNKNOWN


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_values_follow_sources_after_a_cycle(hass, entry, climate, coordinator, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    pid = climate.pid
    assert float(value(hass, "sensor.proportional_test")) == round(pid.proportional, 3)
    assert float(value(hass, "sensor.integral_test")) == round(pid.integral, 3)
    assert float(value(hass, "sensor.derivative_test")) == round(pid.derivative, 3)
    for key, entity_id in (("kp", "sensor.kp_test"), ("ki", "sensor.ki_test"), ("kd", "sensor.kd_test")):
        source = getattr(pid, key)
        if source is None:
            assert value(hass, entity_id) == STATE_UNKNOWN
        else:
            assert float(value(hass, entity_id)) == source
    assert float(value(hass, "sensor.minimum_setpoint_test")) == pytest.approx(climate.minimum_setpoint_value, abs=0.1)
    assert float(value(hass, HEATING_CURVE)) == pytest.approx(climate.heating_curve.value, abs=0.1)
    assert float(value(hass, ERROR_VALUE)) == pytest.approx(climate.max_error.value, abs=0.1)
    assert float(value(hass, "sensor.calculated_setpoint_test")) == pytest.approx(climate.calculated_setpoint, abs=0.1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_values_track_changes_without_standard_attribute_change(hass, entry, climate, boiler_data):
    """A cycle that changes no standard climate attribute still updates the sensors."""
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    attributes = dict(hass.states.get(climate.entity_id).attributes)
    first = (value(hass, HEATING_CURVE), value(hass, "sensor.calculated_setpoint_test"), value(hass, ERROR_VALUE))

    # The outside temperature changes the heating curve and setpoint but is not a climate attribute
    hass.states.async_set("sensor.test_outside_sensor", "-15")
    await hass.async_block_till_done()
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    assert dict(hass.states.get(climate.entity_id).attributes) == attributes
    assert (value(hass, HEATING_CURVE), value(hass, "sensor.calculated_setpoint_test")) != first[:2]
    assert float(value(hass, "sensor.calculated_setpoint_test")) == pytest.approx(climate.calculated_setpoint, abs=0.1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_error_value_follows_target_changes(hass, entry, climate, boiler_data):
    await heat(climate, boiler_data, target=21.0)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    low = float(value(hass, ERROR_VALUE))

    await climate.async_set_target_temperature(25.0)
    await hass.async_block_till_done()

    assert float(value(hass, ERROR_VALUE)) == pytest.approx(low + 4.0, abs=0.11)
    assert float(value(hass, ERROR_VALUE)) == pytest.approx(climate.max_error.value, abs=0.1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_pid_sensors_update_on_inside_temperature_events(hass, entry, climate, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    before = value(hass, ERROR_VALUE)

    hass.states.async_set("sensor.test_inside_sensor", "19.0")
    await hass.async_block_till_done()

    assert value(hass, ERROR_VALUE) != before
    assert float(value(hass, ERROR_VALUE)) == pytest.approx(climate.max_error.value, abs=0.1)
    assert float(value(hass, "sensor.proportional_test")) == round(climate.pid.proportional, 3)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_error_value_is_always_available(hass, entry, climate):
    assert value(hass, ERROR_VALUE) != STATE_UNAVAILABLE


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_heating_curve_unavailable_when_value_is_none(hass, entry, climate, coordinator):
    assert climate.heating_curve.value is not None
    assert value(hass, HEATING_CURVE) != STATE_UNAVAILABLE

    # Reset the curve (value None) and let a coordinator update write the sensors
    climate.heating_curve.reset()
    assert climate.heating_curve.value is None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert value(hass, HEATING_CURVE) == STATE_UNAVAILABLE


# PWM Duty Cycle

@pytest.fixture
def no_setpoint_management(monkeypatch):
    """The fake coordinator supports setpoint management; turn it off."""
    monkeypatch.setattr(SatFakeCoordinator, "supports_setpoint_management", property(lambda self: False))


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "no_setpoint_management")
@pytest.mark.parametrize("domains, data, options, config", [([], {CONF_MODE: MODE_FAKE}, {}, {})])
async def test_pwm_duty_cycle_not_created_without_setpoint_management(hass, entry, coordinator):
    assert not coordinator.supports_setpoint_management
    assert hass.states.get(DUTY[0]) is None
    assert hass.states.get("sensor.pwm_status") is None
    assert er.async_get(hass).async_get_entity_id("sensor", "sat", DUTY[1]) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_duty_cycle_unknown_then_known(hass, entry, climate, boiler_data):
    state = hass.states.get(DUTY[0])
    assert state.state == STATE_UNKNOWN
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["state_class"] == "measurement"
    assert "device_class" not in state.attributes
    assert climate.pwm.duty_cycle is None

    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    on, off = climate.pwm.duty_cycle
    assert float(value(hass, DUTY[0])) == round(on / (on + off) * 100, 1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_duty_cycle_updates_with_hvac_off(hass, entry, climate, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    assert value(hass, DUTY[0]) != STATE_UNKNOWN

    await climate.async_set_hvac_mode(HVACMode.OFF)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    duty = climate.pwm.duty_cycle
    expected = STATE_UNKNOWN if duty is None else str(round(duty[0] / (duty[0] + duty[1]) * 100, 1))
    assert value(hass, DUTY[0]) == expected


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_duty_cycle_follows_changed_target(hass, entry, climate, boiler_data):
    await heat(climate, boiler_data, target=21.0)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    on, off = climate.pwm.duty_cycle
    first = float(value(hass, DUTY[0]))
    assert first == round(on / (on + off) * 100, 1)

    await climate.async_set_target_temperature(30.0)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    on, off = climate.pwm.duty_cycle
    assert float(value(hass, DUTY[0])) == round(on / (on + off) * 100, 1)
    assert float(value(hass, DUTY[0])) != first


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_duty_cycle_unknown_after_reset(hass, entry, climate, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    assert value(hass, DUTY[0]) != STATE_UNKNOWN

    # A target change resets the PWM, which signals the sensor
    await climate.async_set_target_temperature(23.0)
    await hass.async_block_till_done()

    assert climate.pwm.duty_cycle is None
    assert value(hass, DUTY[0]) == STATE_UNKNOWN


# Climate attributes

@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_climate_has_only_standard_attributes(hass, entry, climate, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    attributes = hass.states.get(climate.entity_id).attributes

    assert set(attributes) <= STANDARD_ATTRIBUTES
    assert {"current_temperature", "temperature", "hvac_modes", "min_temp", "max_temp", "friendly_name", "supported_features"} <= set(attributes)
    assert "current_humidity" not in attributes


@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: "sensor.test_humidity"}))
async def test_climate_humidity_attribute_with_sensor(hass, entry, climate, boiler_data):
    hass.states.async_set("sensor.test_humidity", "55")
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    attributes = hass.states.get(climate.entity_id).attributes
    assert set(attributes) <= STANDARD_ATTRIBUTES
    assert attributes["current_humidity"] == 55.0


@pytest.mark.parametrize(*ems_entry({CONF_MINIMUM_SETPOINT: 20}))
async def test_climate_lacks_old_extra_keys(hass, entry, climate):
    attributes = hass.states.get(climate.entity_id).attributes

    for key in ("error", "integral", "setpoint", "heating_curve", "optimal_coefficient", "valves_open", "rooms", "pulse_width_modulation_state", "relative_modulation_state"):
        assert key not in attributes
    assert not [k for k in attributes if k.startswith(("pulse_width_modulation", "relative_modulation", "summer_simmer", "boiler_temperature"))]


# Duty (0, 0)

ZERO_DUTY = with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_AUTOMATIC_DUTY_CYCLE: False, CONF_DUTY_CYCLE: "00:00:00"})


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ZERO_DUTY)
async def test_pwm_duty_cycle_is_unknown_for_zero_on_and_off_time(hass, entry, climate, coordinator, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    assert climate.pwm.duty_cycle == (0, 0)
    assert value(hass, DUTY[0]) == STATE_UNKNOWN

    # A coordinator update doesn't raise and still reaches listeners registered after it
    notified = []
    coordinator.async_add_listener(lambda: notified.append(True))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert notified == [True]
    assert value(hass, DUTY[0]) == STATE_UNKNOWN


# One write per climate write

def writes(hass, entity_ids):
    events: list[Event] = []
    wanted = set(entity_ids)
    hass.bus.async_listen("state_changed", events.append)
    hass.bus.async_listen("state_reported", events.append, event_filter=callback(lambda data: data["entity_id"] in wanted))

    return events


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_one_climate_write_gives_one_write_per_value_sensor(hass, entry, climate, coordinator, boiler_data):
    await heat(climate, boiler_data)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    entity_ids = [HEATING_CURVE, ERROR_VALUE, "sensor.calculated_setpoint_test"] + [row[1] for row in CONTROL]
    events = writes(hass, [*entity_ids, climate.entity_id])

    # The climate writes its state on a coordinator update; each sensor is written once for it
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    for entity_id in entity_ids:
        count = len([e for e in events if e.data["entity_id"] == entity_id])
        assert count == 1, entity_id
    assert len([e for e in events if e.data["entity_id"] == climate.entity_id]) == 1

    events.clear()
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    for entity_id in entity_ids:
        assert len([e for e in events if e.data["entity_id"] == entity_id]) == 1, entity_id


# Flame Average On-Time

@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*ems_entry())
async def test_flame_average_on_time_follows_completed_burns(hass, entry, climate, coordinator, boiler_data, freezer):
    async def step(burngas: str, seconds: float) -> None:
        await advance(hass, freezer, seconds)
        await boiler_data(**IDLE | {"burngas": burngas})
        await climate.async_control_heating_loop()
        await hass.async_block_till_done()

    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    for burngas, seconds in (("off", 0), ("on", 0), ("on", 60), ("off", 10), ("on", 30)):
        await step(burngas, seconds)
    assert value(hass, "sensor.flame_average_on_time_test") == STATE_UNKNOWN

    await step("on", 60)

    assert coordinator.flame.average_on_time_seconds == 60.0
    assert float(value(hass, "sensor.flame_average_on_time_test")) == coordinator.flame.average_on_time_seconds
