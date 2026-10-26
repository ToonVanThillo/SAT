"""The missing-temperature path of the HEAT loop: without the current, target or outside temperature the heater is turned off, PWM is reset
and, once heater OFF is confirmed, the control setpoint is released. Applies to every user."""
import json
import logging
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_SENSOR_MAX_VALUE_AGE,
    CONF_THERMAL_COMFORT,
    SIGNAL_PWM_UPDATED,
    PWMStatus,
)
from tests.conftest import TIMELINE, advance
from tests.const import IDLE, ems_entry, otgw_entry, with_data

# These pin the path once the grace period of a missing input has run out
pytestmark = pytest.mark.usefixtures("loop_entry", "no_input_grace")

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
HUMIDITY = "sensor.test_humidity"
RELEASED = "control setpoint released (missing temperature)"

OFF_WRITES = ['{"cmd": "selflowtemp", "value": 0}', '{"cmd": "heatingoff", "value": "on"}']


@pytest.fixture(autouse=True)
def timeline_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


def sent(published) -> list[str]:
    return [p for _t, p, *_ in published()]


def echo(published) -> dict[str, Any]:
    """The boiler data that reads back what was published: the last value of each command."""
    values: dict[str, Any] = {}
    for _t, payload, *_ in published():
        command = json.loads(payload)
        values[command["cmd"]] = command["value"]

    return values


def timeline(caplog, prefix: str) -> list[str]:
    return [m for r in caplog.records if r.name == TIMELINE and (m := r.getMessage()).startswith(prefix)]


async def heating(climate, boiler_data, freezer, mqtt_mock, published, **data: Any) -> dict[str, Any]:
    """Heating wanted with the heater ON requested and read back as such; the publishes are cleared."""
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5} | data)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    freezer.tick(1)
    readback = echo(published)
    await boiler_data(**IDLE | readback | data)
    mqtt_mock.async_publish.reset_mock()

    return readback


async def missing_cycle(hass, climate, mqtt_mock) -> None:
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()


async def assert_off_then_released(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog, which: str, readback: dict[str, Any], scheduled: bool = False) -> None:
    """`scheduled`: the missing cycle already ran as the loop the debounce timer started (after a time jump), with the publishes cleared before."""
    setpoint = climate.setpoint
    if not scheduled:
        await missing_cycle(hass, climate, mqtt_mock)

    # Heater ON to OFF: the setpoint is sent as 0 (the minimum), then the heater off; no modulation
    assert sent(published) == OFF_WRITES
    line = timeline(caplog, "cycle ")[-1]
    assert f"| missing temperature: {which} | want setpoint=10 heater=OFF | sent " in line
    assert climate.setpoint == setpoint
    assert not timeline(caplog, RELEASED)

    # Heater OFF is read back: the setpoint is released and nothing more is sent
    freezer.tick(1)
    await boiler_data(**IDLE | readback | echo(published))
    mqtt_mock.async_publish.reset_mock()
    if scheduled:
        await advance(hass, freezer, 10)
    else:
        await climate.async_control_heating_loop()

    assert timeline(caplog, RELEASED) == [RELEASED]
    assert sent(published) == []
    assert "want setpoint=RELEASE heater=OFF" in timeline(caplog, "cycle ")[-1]


# Each trigger kind

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize(("sensor", "which"), [(INSIDE, "inside"), (OUTSIDE, "outside")])
@pytest.mark.parametrize("state", ["unavailable", "unknown", "abc", "nan", "inf"])
async def test_an_unreadable_inside_or_outside_sensor_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog, sensor, which, state):
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(sensor, state)

    await assert_off_then_released(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog, which, readback)


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize(("sensor", "which"), [(INSIDE, "inside"), (OUTSIDE, "outside")])
async def test_a_removed_sensor_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog, sensor, which):
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_remove(sensor)

    await assert_off_then_released(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog, which, readback)


@pytest.mark.parametrize(*ems_entry({CONF_SENSOR_MAX_VALUE_AGE: "00:01:00"}))
async def test_a_stale_inside_sensor_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    hass.states.async_set(INSIDE, "20.9")
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)

    # The time jump runs the loop the debounce timer started: that is the missing cycle
    mqtt_mock.async_publish.reset_mock()
    await advance(hass, freezer, 120)

    await assert_off_then_released(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog, "inside", readback, scheduled=True)


@pytest.mark.parametrize(*ems_entry({CONF_SENSOR_MAX_VALUE_AGE: "00:00:00"}))
async def test_without_a_maximum_age_an_old_sensor_is_fine(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    hass.states.async_set(INSIDE, "20.9")
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)
    freezer.tick(86400)
    await boiler_data(**IDLE | readback)
    await missing_cycle(hass, climate, mqtt_mock)

    assert not [m for m in timeline(caplog, "cycle ") if "missing temperature" in m]


# Thermal comfort needs the humidity

COMFORT = {CONF_THERMAL_COMFORT: True}


@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, COMFORT))
@pytest.mark.parametrize("state", ["unavailable", "unknown", "abc", None])
async def test_thermal_comfort_without_a_readable_humidity_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog, state):
    hass.states.async_set(HUMIDITY, "50")
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)
    if state is None:
        hass.states.async_remove(HUMIDITY)
    else:
        hass.states.async_set(HUMIDITY, state)

    await assert_off_then_released(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog, "humidity", readback)


@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, COMFORT | {CONF_SENSOR_MAX_VALUE_AGE: "00:01:00"}))
async def test_thermal_comfort_with_a_stale_humidity_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    hass.states.async_set(HUMIDITY, "50")
    hass.states.async_set(INSIDE, "20.9")
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)

    freezer.tick(50)
    hass.states.async_set(INSIDE, "20.9")
    mqtt_mock.async_publish.reset_mock()
    await advance(hass, freezer, 50)

    await assert_off_then_released(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog, "humidity", readback, scheduled=True)


@pytest.mark.parametrize(*with_data({}, COMFORT))
async def test_thermal_comfort_without_a_humidity_sensor_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await boiler_data(**IDLE | {"heatingoff": "off", "selflowtemp": 35})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    mqtt_mock.async_publish.reset_mock()

    await climate.async_control_heating_loop()

    assert sent(published) == OFF_WRITES
    assert "| missing temperature: humidity | want setpoint=10 heater=OFF" in timeline(caplog, "cycle ")[-1]


@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}))
@pytest.mark.parametrize("state", ["unavailable", "abc"])
async def test_humidity_is_no_trigger_without_thermal_comfort(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog, state):
    await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(HUMIDITY, state)
    await missing_cycle(hass, climate, mqtt_mock)

    assert not [m for m in timeline(caplog, "cycle ") if "missing temperature" in m]
    assert not [p for p in sent(published) if json.loads(p)["cmd"] == "heatingoff"]


@pytest.mark.parametrize(*ems_entry())
async def test_a_missing_flow_temperature_is_no_trigger(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)
    await boiler_data(**{key: value for key, value in IDLE.items() if key != "curflowtemp"} | readback)
    await missing_cycle(hass, climate, mqtt_mock)

    assert not [m for m in timeline(caplog, "cycle ") if "missing temperature" in m]
    assert "heater=ON" in timeline(caplog, "cycle ")[-1]
    assert not [p for p in sent(published) if json.loads(p)["cmd"] == "heatingoff"]


# PWM

FORCED = with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True})


@pytest.mark.parametrize(*FORCED)
@pytest.mark.parametrize("sensor", [INSIDE, OUTSIDE])
async def test_a_missing_temperature_mid_pulse_resets_pwm_and_turns_the_heater_off(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog, sensor):
    signals: list[PWMStatus] = []
    async_dispatcher_connect(hass, SIGNAL_PWM_UPDATED.format(entry.entry_id), signals.append)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON
    freezer.tick(1)
    readback = echo(published)
    await boiler_data(**IDLE | readback)
    pulses = timeline(caplog, "pwm ON")
    signals.clear()

    hass.states.async_set(sensor, "unavailable")
    await missing_cycle(hass, climate, mqtt_mock)
    await hass.async_block_till_done()

    assert timeline(caplog, "pwm IDLE") == ["pwm IDLE (missing temperature)"]
    assert climate.pwm.status == PWMStatus.IDLE
    assert not climate.pwm.state.enabled
    assert signals == [PWMStatus.IDLE]
    assert sent(published) == OFF_WRITES

    # The temperature returns: the old pulse doesn't resume; as after closed valves, PWM may start a fresh one (pulse 1 again)
    hass.states.async_set(sensor, "20.9" if sensor == INSIDE else "9.9")
    freezer.tick(1)
    await boiler_data(**IDLE | echo(published))
    await climate.async_control_heating_loop()
    assert "missing temperature" not in timeline(caplog, "cycle ")[-1]
    fresh = timeline(caplog, "pwm ON")[len(pulses):]
    assert all(line.startswith("pwm ON (pulse 1 this hour") for line in fresh)
    assert len(timeline(caplog, "pwm OFF")) == 0


@pytest.mark.parametrize(*ems_entry())
async def test_a_missing_temperature_without_pwm_active_does_not_log_a_pwm_reset(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(INSIDE, "unavailable")
    await missing_cycle(hass, climate, mqtt_mock)

    assert timeline(caplog, "pwm IDLE") == []


# HVAC OFF is checked first

@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_wins_over_a_missing_temperature(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(INSIDE, "unavailable")
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await missing_cycle(hass, climate, mqtt_mock)

    assert not [m for m in timeline(caplog, "cycle ") if "missing temperature" in m]
    assert "hvac OFF" in timeline(caplog, "cycle ")[-1]


# A backend without a control setpoint channel

@pytest.mark.parametrize(*otgw_entry())
async def test_opentherm_receives_the_minimum_setpoint_on_the_missing_temperature_path(hass, entry, climate, mqtt_mock, published, caplog):
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    mqtt_mock.async_publish.reset_mock()

    hass.states.async_set(INSIDE, "unavailable")
    await missing_cycle(hass, climate, mqtt_mock)

    assert "CS=10" in sent(published)
    assert "| missing temperature: inside | want setpoint=10 heater=OFF" in timeline(caplog, "cycle ")[-1]
    assert not timeline(caplog, RELEASED)
