"""The missing-temperature path of the HEAT loop: without the current, target or outside temperature the heater is turned off, PWM is reset
and the control setpoint is released. Applies to every user."""
import json
import logging
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.sat.channels import RELEASE, DesiredState
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_SENSOR_MAX_VALUE_AGE,
    CONF_THERMAL_COMFORT,
    MINIMUM_OFF_TIME,
    SIGNAL_PWM_UPDATED,
    PWMStatus,
)
from custom_components.sat.coordinator import DeviceState
from tests.conftest import TIMELINE, advance, last_cycle
from tests.const import IDLE, ems_entry, otgw_entry, with_data

# These pin the path once the grace period of a missing input has run out
pytestmark = pytest.mark.usefixtures("loop_entry", "no_input_grace")

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
HUMIDITY = "sensor.test_humidity"

OFF_WRITES = ['{"cmd": "heatingoff", "value": "on"}']


@pytest.fixture(autouse=True)
def timeline_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


INPUT = {"inside": "current_temperature", "outside": "outside_temperature", "humidity": "current_temperature"}


def complete(caplog) -> bool:
    """Whether the latest cycle had all its temperatures."""
    inputs = last_cycle(caplog)["inputs"]

    return None not in (inputs.target_temperature, inputs.current_temperature, inputs.outside_temperature)


def sent(published) -> list[str]:
    return [p for _t, p, *_ in published()]


def echo(published) -> dict[str, Any]:
    """The boiler data that reads back what was published: the last value of each command."""
    values: dict[str, Any] = {}
    for _t, payload, *_ in published():
        command = json.loads(payload)
        values[command["cmd"]] = command["value"]

    return values


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

    # Heater ON to OFF: only the heater off is sent, with the setpoint released; no modulation
    assert sent(published) == OFF_WRITES
    fields = last_cycle(caplog)
    assert getattr(fields["inputs"], INPUT[which]) is None
    assert fields["want"] == DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF)
    assert climate.setpoint == setpoint

    # Heater OFF is read back: nothing more is sent
    freezer.tick(1)
    await boiler_data(**IDLE | readback | echo(published))
    mqtt_mock.async_publish.reset_mock()
    if scheduled:
        await advance(hass, freezer, 10)
    else:
        await climate.async_control_heating_loop()

    assert sent(published) == []
    assert last_cycle(caplog)["want"] == DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF)


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

    assert complete(caplog)


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
    assert last_cycle(caplog)["inputs"].current_temperature is None
    assert last_cycle(caplog)["want"] == DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF)


@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}))
@pytest.mark.parametrize("state", ["unavailable", "abc"])
async def test_humidity_is_no_trigger_without_thermal_comfort(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog, state):
    await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(HUMIDITY, state)
    await missing_cycle(hass, climate, mqtt_mock)

    assert complete(caplog)
    assert not [p for p in sent(published) if json.loads(p)["cmd"] == "heatingoff"]


@pytest.mark.parametrize(*ems_entry())
async def test_a_missing_flow_temperature_is_no_trigger(hass, entry, climate, coordinator, boiler_data, freezer, mqtt_mock, published, caplog):
    readback = await heating(climate, boiler_data, freezer, mqtt_mock, published)
    await boiler_data(**IDLE | readback | {"curflowtemp": None})
    await missing_cycle(hass, climate, mqtt_mock)

    # The cycle ran without a flow temperature
    assert coordinator._snapshot.boiler.reliable_flow_temperature is None
    assert complete(caplog)
    assert last_cycle(caplog)["want"].heater_state == DeviceState.ON
    assert not [p for p in sent(published) if json.loads(p)["cmd"] == "heatingoff"]


# PWM

UNRESTRICTED_DISALLOWED = with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False})


@pytest.mark.parametrize(*UNRESTRICTED_DISALLOWED)
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
    signals.clear()

    hass.states.async_set(sensor, "unavailable")
    await missing_cycle(hass, climate, mqtt_mock)
    await hass.async_block_till_done()

    # The reset ends the pulse at once, before its minimum pulse
    assert not complete(caplog) and last_cycle(caplog)["pwm"].status == PWMStatus.OFF
    assert climate.pwm.status == PWMStatus.OFF
    assert signals == [PWMStatus.OFF]
    assert sent(published) == OFF_WRITES

    # The temperature returns: the old pulse doesn't resume, and the next pulse waits for the off phase after the pulse the reset ended
    hass.states.async_set(sensor, "20.9" if sensor == INSIDE else "9.9")
    freezer.tick(1)
    await boiler_data(**IDLE | echo(published))
    await climate.async_control_heating_loop()
    assert complete(caplog)
    assert climate.pwm.status == PWMStatus.OFF

    # The off phase lasts the duty's off time, at least 180 s, from the reset
    assert climate.pwm.duty_cycle is not None
    off_time = max(climate.pwm.duty_cycle[1], MINIMUM_OFF_TIME)
    freezer.tick(off_time - 2)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF
    freezer.tick(1)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON


@pytest.mark.parametrize(*ems_entry())
async def test_a_missing_temperature_resets_pwm_at_every_cycle(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(INSIDE, "unavailable")

    for _ in range(3):
        await missing_cycle(hass, climate, mqtt_mock)
        assert not complete(caplog) and last_cycle(caplog)["pwm"].status == PWMStatus.OFF
        assert climate.pwm.status == PWMStatus.OFF


# HVAC OFF is checked first

@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_wins_over_a_missing_temperature(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await heating(climate, boiler_data, freezer, mqtt_mock, published)
    hass.states.async_set(INSIDE, "unavailable")
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await missing_cycle(hass, climate, mqtt_mock)

    assert last_cycle(caplog)["hvac"] == HVACMode.OFF


# A backend without a control setpoint channel

@pytest.mark.parametrize(*otgw_entry())
async def test_opentherm_receives_no_setpoint_on_the_missing_temperature_path(hass, entry, climate, mqtt_mock, published, caplog):
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    mqtt_mock.async_publish.reset_mock()

    hass.states.async_set(INSIDE, "unavailable")
    await missing_cycle(hass, climate, mqtt_mock)

    assert not any(command.startswith("CS=") for command in sent(published))
    assert last_cycle(caplog)["inputs"].current_temperature is None
    assert last_cycle(caplog)["want"] == DesiredState(control_setpoint=RELEASE, heater_state=DeviceState.OFF)
