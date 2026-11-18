"""Staleness by the last report, and the grace for missing climate inputs (held inputs).

A sensor that re-reports its value is fresh; one that falls silent goes stale. A missing target, inside, outside or humidity reading is
replaced by the last valid value for up to MISSING_INPUT_GRACE seconds after the last capture that read it as valid, so a flapping sensor
doesn't turn the boiler off.
"""
import json
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant, State

from custom_components.sat.channels import DesiredState
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_SENSOR_MAX_VALUE_AGE,
    CONF_THERMAL_COMFORT,
    DOMAIN,
    PWMStatus,
)
from custom_components.sat.coordinator import DeviceState
from custom_components.sat.held_input import MISSING_INPUT_GRACE, HeldInput
from custom_components.sat.helpers import is_state_stale, state_age_seconds
from tests.conftest import run_one_loop
from tests.const import IDLE, ems_entry, with_data

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
HUMIDITY = "sensor.test_humidity"

OFF_WRITES = ['{"cmd": "selflowtemp", "value": 0}', '{"cmd": "heatingoff", "value": "on"}']


# The grace constant

def test_the_grace_is_five_minutes():
    assert MISSING_INPUT_GRACE == 300


# HeldInput, unit

@pytest.fixture
def held(freezer: FrozenDateTimeFactory) -> HeldInput:
    return HeldInput("inside")


def test_a_valid_reading_is_returned_and_is_neither_held_nor_missing(held):
    assert held.read(20.5) == 20.5
    assert not held.held
    assert held.name == "inside"


def test_a_missing_reading_holds_the_last_valid_value(held, freezer):
    held.read(20.5)
    freezer.tick(10)

    for _ in range(3):
        assert held.read(None) == 20.5
        assert held.held
        freezer.tick(10)


def test_the_hold_is_strict_at_the_grace(held, freezer):
    held.read(20.5)
    freezer.tick(299)
    assert held.read(None) == 20.5

    freezer.tick(1)
    assert held.read(None) is None
    assert not held.held
    assert not held.held


def test_the_hold_counts_from_the_last_valid_capture_and_does_not_move(held, freezer):
    held.read(20.5)
    freezer.tick(100)
    assert held.read(None) == 20.5
    freezer.tick(199.9)
    assert held.read(None) == 20.5
    freezer.tick(0.1)
    assert held.read(None) is None


def test_a_valid_reading_restarts_the_clock(held, freezer):
    held.read(20.5)
    freezer.tick(200)
    assert held.read(None) == 20.5
    freezer.tick(50)
    assert held.read(21.0) == 21.0
    freezer.tick(290)
    assert held.read(None) == 21.0
    freezer.tick(10)
    assert held.read(None) is None


def test_a_valid_reading_after_a_hold_or_a_lapse_clears_the_flags(held, freezer):
    held.read(20.5)
    held.read(None)
    assert held.read(21.0) == 21.0
    assert not held.held

    freezer.tick(500)
    assert held.read(None) is None
    assert held.read(19.0) == 19.0
    assert not held.held


def test_an_expired_hold_stays_missing_until_a_valid_reading(held, freezer):
    held.read(20.5)
    freezer.tick(310)

    for _ in range(3):
        assert held.read(None) is None
        assert not held.held
        freezer.tick(10)


def test_a_reading_after_a_lapse_is_held_again_when_it_goes_missing(held, freezer):
    held.read(20.5)
    freezer.tick(500)
    held.read(None)
    held.read(19.0)

    assert held.read(None) == 19.0
    assert held.held


def test_an_input_never_valid_since_start_holds_nothing(held, freezer):
    for _ in range(3):
        assert held.read(None) is None
        assert not held.held
        assert not held.held
        freezer.tick(1000)

    assert held.read(20.0) == 20.0


# Staleness by the last report

def test_state_age_counts_from_the_last_report(freezer: FrozenDateTimeFactory):
    state = State("sensor.x", "1")
    freezer.tick(timedelta(seconds=42))

    assert state_age_seconds(state) == 42


@pytest.mark.parametrize(("age", "stale"), [(59.9, False), (60, False), (60.1, True), (3600, True)])
def test_a_state_is_stale_strictly_after_the_max_age(freezer: FrozenDateTimeFactory, age, stale):
    state = State("sensor.x", "1")
    freezer.tick(timedelta(seconds=age))

    assert is_state_stale(state, 60) is stale


@pytest.mark.parametrize("max_age", [0, -1])
def test_a_max_age_of_zero_or_less_is_never_stale(freezer: FrozenDateTimeFactory, max_age):
    state = State("sensor.x", "1")
    freezer.tick(timedelta(days=30))

    assert is_state_stale(state, max_age) is False


def test_a_missing_state_is_never_stale():
    assert is_state_stale(None, 60) is False


async def test_a_same_value_report_moves_the_age_but_not_the_update_time(hass: HomeAssistant, freezer: FrozenDateTimeFactory):
    hass.states.async_set("sensor.x", "1")
    first = hass.states.get("sensor.x")
    assert first is not None
    freezer.tick(timedelta(seconds=100))
    assert is_state_stale(first, 60)

    hass.states.async_set("sensor.x", "1")
    again = hass.states.get("sensor.x")
    assert again is not None

    assert again.last_updated == first.last_updated
    assert state_age_seconds(again) == 0
    assert not is_state_stale(again, 60)


# Staleness at the climate level (grace off, so the missing path shows at once)

def missing(fields: dict[str, Any]) -> list[str]:
    """The inputs the cycle read as missing (after the hold ran out)."""
    inputs = fields["inputs"]
    named = (("target", inputs.target_temperature), ("inside", inputs.current_temperature), ("outside", inputs.outside_temperature))

    return [name for name, value in named if value is None]


async def cycle(hass: HomeAssistant, climate, freezer: FrozenDateTimeFactory, caplog, mqtt_mock, seconds: float = 10) -> dict[str, Any]:
    """Advance the clock, run exactly one control loop (the one the debounce timer started, or an explicit one) and return its cycle fields and publishes."""
    mark = len(caplog.records)
    mqtt_mock.async_publish.reset_mock()
    freezer.tick(seconds)
    fields = await run_one_loop(hass, climate, caplog, mark)
    writes = [json.loads(call.args[1]) for call in mqtt_mock.async_publish.call_args_list]

    return {"fields": fields, "writes": [f'{{"cmd": "{w["cmd"]}", "value": {json.dumps(w["value"])}}}' for w in writes]}


async def heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock, target: float = 21.0) -> dict[str, Any]:
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5})
    await climate.async_set_target_temperature(target)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    return await cycle(hass, climate, freezer, caplog, mqtt_mock)


MAX_AGE = {CONF_SENSOR_MAX_VALUE_AGE: "00:01:00"}


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry(MAX_AGE))
async def test_an_inside_sensor_that_re_reports_the_same_value_stays_fresh(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    for _ in range(4):
        hass.states.async_set(INSIDE, "20.9")
        result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 40)
        assert not missing(result["fields"])
        assert result["fields"]["inputs"].current_temperature == 20.9


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry(MAX_AGE))
async def test_a_silent_inside_sensor_goes_stale_after_the_max_age(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    # The first cycle took 10 s: 60 s since the report is not stale yet, a hair more is
    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 50)
    assert not missing(result["fields"])

    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 0.5)
    assert missing(result["fields"]) == ["inside"]
    assert result["writes"] == OFF_WRITES


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry({CONF_SENSOR_MAX_VALUE_AGE: "00:00:00"}))
async def test_a_max_age_of_zero_never_goes_stale(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 100000)
    assert not missing(result["fields"])


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, {CONF_THERMAL_COMFORT: True} | MAX_AGE))
async def test_humidity_follows_the_last_report_too(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(HUMIDITY, "50")
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    for _ in range(3):
        hass.states.async_set(HUMIDITY, "50")
        hass.states.async_set(INSIDE, "20.9")
        result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 40)
        assert not missing(result["fields"])

    freezer.tick(61)
    hass.states.async_set(INSIDE, "20.9")
    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 0)
    assert missing(result["fields"]) == ["inside"]


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry(MAX_AGE))
async def test_outside_entities_have_no_staleness_check(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(OUTSIDE, "9.9")
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    freezer.tick(3600)
    hass.states.async_set(INSIDE, "20.9")
    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 0)

    assert not missing(result["fields"])
    assert result["fields"]["inputs"].outside_temperature == 9.9


# Grace at the climate level

@pytest.fixture
def start(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    async def go(target: float = 21.0) -> dict[str, Any]:
        return await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock, target)

    return go


@pytest.fixture
def run(hass, climate, freezer, caplog, mqtt_mock):
    async def go(seconds: float = 10) -> dict[str, Any]:
        return await cycle(hass, climate, freezer, caplog, mqtt_mock, seconds)

    return go


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize(("sensor", "name", "held_value"), [
    (INSIDE, "inside", "20.9"),
    (OUTSIDE, "outside", "9.9"),
])
async def test_a_missing_sensor_is_held_for_one_cycle(hass, start, run, sensor, name, held_value):
    await start()
    hass.states.async_set(sensor, "unavailable")

    result = await run()

    fields = result["fields"]
    assert not missing(fields)
    assert fields["inputs"].held == (name,)
    assert (fields["inputs"].target_temperature, fields["inputs"].current_temperature, fields["inputs"].outside_temperature) == (21.0, 20.9, 9.9)
    assert fields["want"].heater_state == DeviceState.ON
    assert not set(result["writes"]) & set(OFF_WRITES)

    hass.states.async_set(sensor, held_value)
    result = await run()

    assert not result["fields"]["inputs"].held


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_missing_target_is_held(climate, start, run):
    await start()
    # There is no public way to lose a target once set; this is the state a failed restore or reset leaves
    climate._target_temperature = None

    result = await run()

    fields = result["fields"]
    assert not missing(fields)
    assert fields["inputs"].target_temperature == 21.0
    assert fields["inputs"].held == ("target",)

    await climate.async_set_target_temperature(21.0)
    result = await run()
    assert not result["fields"]["inputs"].held


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, {CONF_THERMAL_COMFORT: True}))
async def test_a_missing_humidity_is_held_with_thermal_comfort(hass, start, run):
    hass.states.async_set(HUMIDITY, "50")
    first = await start()
    comfort = first["fields"]["inputs"].current_temperature

    hass.states.async_set(HUMIDITY, "unavailable")
    result = await run()

    assert not missing(result["fields"])
    assert result["fields"]["inputs"].held == ("humidity",)
    assert result["fields"]["inputs"].current_temperature == comfort

    hass.states.async_set(HUMIDITY, "50")
    result = await run()
    assert not result["fields"]["inputs"].held


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, {CONF_THERMAL_COMFORT: True}))
async def test_held_inputs_are_listed_in_capture_order(hass, start, run):
    hass.states.async_set(HUMIDITY, "50")
    await start()
    for entity in (HUMIDITY, OUTSIDE, INSIDE):
        hass.states.async_set(entity, "unavailable")

    result = await run()

    assert result["fields"]["inputs"].held == ("inside", "outside", "humidity")


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_humidity_is_not_read_without_thermal_comfort(hass, start, run):
    await start()
    hass.states.async_set(HUMIDITY, "unavailable")
    result = await run()

    assert not result["fields"]["inputs"].held
    assert not missing(result["fields"])


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_an_input_is_held_at_299_seconds_and_missing_at_300(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")

    result = await run(299)
    assert result["fields"]["inputs"].held == ("inside",)
    assert not missing(result["fields"])
    assert not set(result["writes"]) & set(OFF_WRITES)

    result = await run(1)
    assert missing(result["fields"]) == ["inside"]
    assert not result["fields"]["inputs"].held
    assert result["writes"] == OFF_WRITES


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_hold_counts_from_the_last_valid_capture_not_from_the_last_held_cycle(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")

    for _ in range(2):
        result = await run(100)
        assert result["fields"]["inputs"].held == ("inside",)

    result = await run(100)
    assert missing(result["fields"]) == ["inside"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_hold_running_out_takes_the_full_missing_path_and_a_recovery_resumes_heating(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    await run(150)
    result = await run(150)

    assert missing(result["fields"]) == ["inside"]
    assert result["fields"]["want"] == DesiredState(control_setpoint=10, heater_state=DeviceState.OFF)
    assert result["writes"] == OFF_WRITES

    await run(10)
    hass.states.async_set(INSIDE, "20.9")
    result = await run(10)

    assert not missing(result["fields"])
    assert result["fields"]["inputs"].current_temperature == 20.9


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_last_valid_capture_older_than_the_grace_holds_nothing(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")

    result = await run(400)

    assert missing(result["fields"]) == ["inside"]
    assert not result["fields"]["inputs"].held

    hass.states.async_set(INSIDE, "20.9")
    result = await run()
    assert not missing(result["fields"])


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_after_a_restart_a_missing_input_goes_straight_to_the_missing_path(hass, entry, start, caplog, mqtt_mock, boiler_data, freezer):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    climate = hass.data[DOMAIN][entry.entry_id]["climate"]
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    result = await cycle(hass, climate, freezer, caplog, mqtt_mock)

    assert missing(result["fields"]) == ["inside"]
    assert not result["fields"]["inputs"].held


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_each_input_is_held_on_its_own(hass, start, run):
    await start()
    hass.states.async_set(OUTSIDE, "unavailable")
    hass.states.async_set(INSIDE, "20.5")

    result = await run()

    assert result["fields"]["inputs"].current_temperature == 20.5
    assert result["fields"]["inputs"].outside_temperature == 9.9
    assert result["fields"]["inputs"].held == ("outside",)

    # The outside one runs out 300 s after its last valid capture
    await run(289)
    result = await run(1)
    assert missing(result["fields"]) == ["outside"]


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry())
async def test_the_missing_path_lists_every_input_that_returned_nothing(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    hass.states.async_set(OUTSIDE, "unavailable")

    result = await run()

    assert missing(result["fields"]) == ["inside", "outside"]
    assert result["fields"]["want"] == DesiredState(control_setpoint=10, heater_state=DeviceState.OFF)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_reads_the_inputs_too(hass, start, run, climate):
    await start()
    await climate.async_set_hvac_mode(HVACMode.OFF)
    result = await run(200)
    assert result["fields"]["hvac"] == HVACMode.OFF

    # The sensor reads valid at the OFF cycle, then goes missing: back in HEAT it is still within the grace of that capture
    hass.states.async_set(INSIDE, "unavailable")
    await run(10)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    result = await run(150)

    assert result["fields"]["inputs"].held == ("inside",)


# PWM through a hold

UNRESTRICTED_DISALLOWED = with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False})


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*UNRESTRICTED_DISALLOWED)
async def test_a_flapping_sensor_never_resets_pwm_or_cuts_a_pulse(hass, climate, start, run):
    await start(21.0)
    assert climate.pwm.status == PWMStatus.ON

    # Missing for a cycle or two every few cycles, while the pulse runs
    for good in (True, False, True, True, False, False, True, False, True, True, False, True):
        hass.states.async_set(INSIDE, "20.9" if good else "unavailable")
        result = await run(10)

        assert not missing(result["fields"])
        assert climate.pwm.status == PWMStatus.ON
        assert not set(result["writes"]) & set(OFF_WRITES)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*UNRESTRICTED_DISALLOWED)
async def test_a_hold_that_runs_out_resets_pwm_and_turns_the_heater_off(hass, climate, start, run):
    await start(21.0)
    assert climate.pwm.status == PWMStatus.ON
    hass.states.async_set(INSIDE, "unavailable")

    for _ in range(2):
        result = await run(100)
        assert result["fields"]["inputs"].held == ("inside",)
        assert climate.pwm.status == PWMStatus.ON
        assert not set(result["writes"]) & set(OFF_WRITES)

    result = await run(100)

    assert missing(result["fields"]) == ["inside"]
    assert climate.pwm.status == PWMStatus.OFF
    assert result["writes"] == OFF_WRITES
