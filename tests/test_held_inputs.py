"""Staleness by the last report, and the grace for missing climate inputs (held inputs).

A sensor that re-reports its value is fresh; one that falls silent goes stale. A missing target, inside, outside or humidity reading is
replaced by the last valid value for up to MISSING_INPUT_GRACE seconds after the last capture that read it as valid, so a flapping sensor
doesn't turn the boiler off.
"""
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant, State

from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_SENSOR_MAX_VALUE_AGE,
    CONF_THERMAL_COMFORT,
    DOMAIN,
    PWMStatus,
)
from custom_components.sat.held_input import MISSING_INPUT_GRACE, HeldInput
from custom_components.sat.helpers import is_state_stale, state_age_seconds
from tests.conftest import TIMELINE, run_one_loop
from tests.const import IDLE, ems_entry, with_data

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
HUMIDITY = "sensor.test_humidity"

LABELS = ("target temperature", "inside temperature", "outside temperature", "humidity")
OFF_WRITES = ['{"cmd": "selflowtemp", "value": 0}', '{"cmd": "heatingoff", "value": "on"}']


def info(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.levelno == logging.INFO]


def about(caplog, label: str) -> list[str]:
    return [m for m in info(caplog) if m.startswith(label) and any(word in m for word in ("missing", "back", "held"))]


@pytest.fixture(autouse=True)
def timeline_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


# The grace constant

def test_the_grace_is_five_minutes():
    assert MISSING_INPUT_GRACE == 300


# HeldInput, unit

@pytest.fixture
def held(freezer: FrozenDateTimeFactory, caplog) -> HeldInput:
    caplog.set_level(logging.INFO, logger=TIMELINE)
    return HeldInput("inside", "inside temperature")


def test_a_valid_reading_is_returned_and_logs_nothing(held, caplog):
    assert held.read(20.5) == 20.5
    assert not held.held
    assert not held.missing
    assert held.name == "inside"
    assert info(caplog) == []


def test_a_missing_reading_holds_the_last_valid_value_and_logs_once(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(10)

    for _ in range(3):
        assert held.read(None) == 20.5
        freezer.tick(10)

    assert held.held
    assert not held.missing
    assert info(caplog) == ["inside temperature missing, holding 20.5 for up to 5 min"]


def test_the_hold_is_strict_at_the_grace(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(299)
    assert held.read(None) == 20.5
    assert info(caplog) == ["inside temperature missing, holding 20.5 for up to 5 min"]

    freezer.tick(1)
    assert held.read(None) is None
    assert held.missing
    assert not held.held
    assert info(caplog)[-1] == "inside temperature still missing after 5 min, no longer held"
    assert len(info(caplog)) == 2


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


def test_back_is_logged_once_after_a_hold(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(10)
    held.read(None)
    assert held.read(21.0) == 21.0
    assert held.read(21.5) == 21.5

    assert info(caplog) == ["inside temperature missing, holding 20.5 for up to 5 min", "inside temperature back"]
    assert not held.held


def test_nothing_after_expiry_until_the_value_is_back(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(10)
    held.read(None)
    freezer.tick(300)
    assert held.read(None) is None
    before = len(info(caplog))

    freezer.tick(10)
    assert held.read(None) is None
    assert held.read(None) is None
    assert len(info(caplog)) == before

    assert held.read(19.0) == 19.0
    assert info(caplog)[-1] == "inside temperature back"
    assert len(info(caplog)) == before + 1


def test_a_missing_reading_without_a_value_logs_nothing(held, caplog):
    assert held.read(None) is None
    assert held.read(None) is None
    assert held.missing
    assert held.read(20.0) == 20.0
    assert info(caplog) == []


def test_a_first_missing_reading_after_the_grace_has_nothing_to_hold(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(400)

    assert held.read(None) is None
    assert held.missing
    assert held.read(20.0) == 20.0
    # Logged once each, without a hold
    assert info(caplog) == ["inside temperature missing", "inside temperature back"]


def test_another_hold_after_a_recovery_logs_again(held, caplog, freezer):
    held.read(20.5)
    held.read(None)
    held.read(20.6)
    held.read(None)

    assert info(caplog) == [
        "inside temperature missing, holding 20.5 for up to 5 min", "inside temperature back", "inside temperature missing, holding 20.6 for up to 5 min",
    ]


# LAPSED: missing straight from valid, with the last valid value too old to hold

def test_the_lapse_boundary_is_strict_at_the_grace(freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    just_inside = HeldInput("inside", "inside temperature")
    just_inside.read(20.5)
    freezer.tick(299.9)

    assert just_inside.read(None) == 20.5
    assert just_inside.held
    assert info(caplog) == ["inside temperature missing, holding 20.5 for up to 5 min"]

    caplog.clear()
    at_grace = HeldInput("inside", "inside temperature")
    at_grace.read(20.5)
    freezer.tick(300)

    assert at_grace.read(None) is None
    assert at_grace.missing
    assert not at_grace.held
    assert info(caplog) == ["inside temperature missing"]


def test_a_lapse_is_logged_once(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(500)

    for _ in range(4):
        assert held.read(None) is None
        freezer.tick(10)

    assert info(caplog) == ["inside temperature missing"]


def test_back_is_logged_once_after_a_lapse(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(500)
    held.read(None)

    assert held.read(19.0) == 19.0
    assert held.read(19.5) == 19.5
    assert info(caplog) == ["inside temperature missing", "inside temperature back"]


def test_a_lapse_after_a_recovery_logs_again(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(500)
    held.read(None)
    held.read(19.0)
    freezer.tick(500)
    held.read(None)

    assert info(caplog) == ["inside temperature missing", "inside temperature back", "inside temperature missing"]


def test_an_input_never_valid_since_start_is_silent_when_missing_and_when_it_recovers(held, caplog, freezer):
    for _ in range(3):
        assert held.read(None) is None
        freezer.tick(1000)

    assert held.read(20.0) == 20.0
    assert info(caplog) == []


def test_back_is_logged_after_an_expired_hold(held, caplog, freezer):
    held.read(20.5)
    freezer.tick(10)
    held.read(None)
    freezer.tick(300)
    held.read(None)

    held.read(19.0)

    assert info(caplog)[-1] == "inside temperature back"


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

async def cycle(hass: HomeAssistant, climate, freezer: FrozenDateTimeFactory, caplog, mqtt_mock, seconds: float = 10) -> dict[str, Any]:
    """Advance the clock, run exactly one control loop (the one the debounce timer started, or an explicit one) and return its cycle line and publishes."""
    mark = len(caplog.records)
    mqtt_mock.async_publish.reset_mock()
    freezer.tick(seconds)
    new = await run_one_loop(hass, climate, caplog, mark)
    cycles = [m for m in new if m.startswith("cycle ")]
    writes = [json.loads(call.args[1]) for call in mqtt_mock.async_publish.call_args_list]

    return {"line": cycles[0], "new": new, "writes": [f'{{"cmd": "{w["cmd"]}", "value": {json.dumps(w["value"])}}}' for w in writes]}


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
        assert "missing temperature" not in result["line"]
        assert "inside=20.9" in result["line"]


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry(MAX_AGE))
async def test_a_silent_inside_sensor_goes_stale_after_the_max_age(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    # The first cycle took 10 s: 60 s since the report is not stale yet, a hair more is
    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 50)
    assert "missing temperature" not in result["line"]

    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 0.5)
    assert "missing temperature: inside" in result["line"]


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry({CONF_SENSOR_MAX_VALUE_AGE: "00:00:00"}))
async def test_a_max_age_of_zero_never_goes_stale(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 100000)
    assert "missing temperature" not in result["line"]


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
        assert "missing temperature" not in result["line"]

    freezer.tick(61)
    hass.states.async_set(INSIDE, "20.9")
    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 0)
    assert "missing temperature: humidity" in result["line"]


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry(MAX_AGE))
async def test_outside_entities_have_no_staleness_check(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    hass.states.async_set(OUTSIDE, "9.9")
    hass.states.async_set(INSIDE, "20.9")
    await heating(hass, climate, boiler_data, freezer, caplog, mqtt_mock)

    freezer.tick(3600)
    hass.states.async_set(INSIDE, "20.9")
    result = await cycle(hass, climate, freezer, caplog, mqtt_mock, 0)

    assert "missing temperature" not in result["line"]
    assert "out=9.9" in result["line"]


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
@pytest.mark.parametrize(("sensor", "name", "label", "held_value"), [
    (INSIDE, "inside", "inside temperature", "20.9"),
    (OUTSIDE, "outside", "outside temperature", "9.9"),
])
async def test_a_missing_sensor_is_held_for_one_cycle(hass, start, run, caplog, sensor, name, label, held_value):
    await start()
    hass.states.async_set(sensor, "unavailable")

    result = await run()

    assert "missing temperature" not in result["line"]
    assert f" held={name}" in result["line"]
    assert "in target=21.0 inside=20.9 out=9.9 valves=open held=" in result["line"]
    assert "heater=ON" in result["line"]
    assert not set(result["writes"]) & set(OFF_WRITES)
    assert about(caplog, label) == [f"{label} missing, holding {held_value} for up to 5 min"]

    hass.states.async_set(sensor, held_value)
    result = await run()

    assert "held=" not in result["line"]
    assert about(caplog, label) == [f"{label} missing, holding {held_value} for up to 5 min", f"{label} back"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_missing_target_is_held(hass, climate, start, run, caplog):
    await start()
    # There is no public way to lose a target once set; this is the state a failed restore or reset leaves
    climate._target_temperature = None

    result = await run()

    assert "missing temperature" not in result["line"]
    assert "in target=21.0 inside=20.9" in result["line"]
    assert " held=target" in result["line"]
    assert about(caplog, "target temperature") == ["target temperature missing, holding 21.0 for up to 5 min"]

    await climate.async_set_target_temperature(21.0)
    await run()
    assert about(caplog, "target temperature")[-1] == "target temperature back"


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, {CONF_THERMAL_COMFORT: True}))
async def test_a_missing_humidity_is_held_with_thermal_comfort(hass, start, run, caplog):
    hass.states.async_set(HUMIDITY, "50")
    first = await start()
    comfort = first["line"].split(" inside=")[1].split()[0]

    hass.states.async_set(HUMIDITY, "unavailable")
    result = await run()

    assert "missing temperature" not in result["line"]
    assert " held=humidity" in result["line"]
    assert f" inside={comfort} " in result["line"]
    assert about(caplog, "humidity") == ["humidity missing, holding 50.0 for up to 5 min"]

    hass.states.async_set(HUMIDITY, "50")
    await run()
    assert about(caplog, "humidity")[-1] == "humidity back"


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, {CONF_THERMAL_COMFORT: True}))
async def test_held_inputs_are_listed_in_capture_order(hass, start, run):
    hass.states.async_set(HUMIDITY, "50")
    await start()
    for entity in (HUMIDITY, OUTSIDE, INSIDE):
        hass.states.async_set(entity, "unavailable")

    result = await run()

    assert result["line"].split(" | ")[1].endswith("valves=open held=inside,outside,humidity")


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_humidity_is_not_read_without_thermal_comfort(hass, start, run, caplog):
    await start()
    hass.states.async_set(HUMIDITY, "unavailable")
    result = await run()

    assert "held=" not in result["line"]
    assert about(caplog, "humidity") == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_an_input_is_held_at_299_seconds_and_missing_at_300(hass, start, run, caplog):
    await start()
    hass.states.async_set(INSIDE, "unavailable")

    result = await run(299)
    assert " held=inside" in result["line"]
    assert "missing temperature" not in result["line"]
    assert not set(result["writes"]) & set(OFF_WRITES)

    result = await run(1)
    assert "missing temperature: inside" in result["line"]
    assert "held=" not in result["line"]
    assert result["writes"] == OFF_WRITES
    assert about(caplog, "inside temperature") == [
        "inside temperature missing, holding 20.9 for up to 5 min", "inside temperature still missing after 5 min, no longer held",
    ]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_hold_counts_from_the_last_valid_capture_not_from_the_last_held_cycle(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")

    for _ in range(2):
        result = await run(100)
        assert " held=inside" in result["line"]

    result = await run(100)
    assert "missing temperature: inside" in result["line"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_hold_running_out_takes_the_full_missing_path_and_a_recovery_logs_back(hass, start, run, caplog):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    await run(150)
    result = await run(150)

    assert "missing temperature: inside" in result["line"]
    assert "want setpoint=10 heater=OFF" in result["line"]
    assert result["writes"] == OFF_WRITES

    # More missing cycles log nothing at info
    before = len(about(caplog, "inside temperature"))
    await run(10)
    assert len(about(caplog, "inside temperature")) == before

    hass.states.async_set(INSIDE, "20.9")
    result = await run(10)

    assert "missing temperature" not in result["line"]
    assert about(caplog, "inside temperature")[-1] == "inside temperature back"
    assert len(about(caplog, "inside temperature")) == before + 1


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_missing_and_back_are_logged_once_when_there_is_nothing_to_hold(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock, run):
    # The first capture, at start-up, read the sensor as valid; once that is older than the grace there is nothing to hold
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set(INSIDE, "unavailable")

    result = await run(400)
    assert "missing temperature: inside" in result["line"]
    result = await run()
    assert "missing temperature: inside" in result["line"]

    hass.states.async_set(INSIDE, "20.9")
    result = await run()

    assert "missing temperature" not in result["line"]
    assert about(caplog, "inside temperature") == ["inside temperature missing", "inside temperature back"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_last_valid_capture_older_than_the_grace_holds_nothing(hass, start, run, caplog):
    await start()
    hass.states.async_set(INSIDE, "unavailable")

    result = await run(400)

    assert "missing temperature: inside" in result["line"]
    assert about(caplog, "inside temperature") == ["inside temperature missing"]

    hass.states.async_set(INSIDE, "20.9")
    await run()
    assert about(caplog, "inside temperature") == ["inside temperature missing", "inside temperature back"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_after_a_restart_a_missing_input_goes_straight_to_the_missing_path(hass, entry, start, caplog, mqtt_mock, boiler_data, freezer):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    climate = hass.data[DOMAIN][entry.entry_id]["climate"]
    caplog.clear()
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    result = await cycle(hass, climate, freezer, caplog, mqtt_mock)

    assert "missing temperature: inside" in result["line"]
    assert "held=" not in result["line"]
    assert about(caplog, "inside temperature") == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_each_input_is_held_on_its_own(hass, start, run, caplog):
    await start()
    hass.states.async_set(OUTSIDE, "unavailable")
    hass.states.async_set(INSIDE, "20.5")

    result = await run()

    assert "inside=20.5" in result["line"]
    assert "out=9.9" in result["line"]
    assert result["line"].split(" | ")[1].endswith("held=outside")
    assert about(caplog, "inside temperature") == []

    # Both missing, only the outside one is still held when the inside one has run out
    await run(299)
    result = await run(1)
    assert "missing temperature: outside" in result["line"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_each_hold_line_is_logged_once_per_transition_over_many_cycles(hass, start, run, caplog):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    hass.states.async_set(OUTSIDE, "unavailable")
    for _ in range(5):
        await run(10)
    hass.states.async_set(INSIDE, "20.9")
    for _ in range(3):
        await run(10)
    hass.states.async_set(OUTSIDE, "9.9")
    for _ in range(3):
        await run(10)

    assert about(caplog, "inside temperature") == ["inside temperature missing, holding 20.9 for up to 5 min", "inside temperature back"]
    assert about(caplog, "outside temperature") == ["outside temperature missing, holding 9.9 for up to 5 min", "outside temperature back"]


@pytest.mark.usefixtures("loop_entry", "no_input_grace")
@pytest.mark.parametrize(*ems_entry())
async def test_the_missing_path_lists_every_input_that_returned_nothing(hass, start, run):
    await start()
    hass.states.async_set(INSIDE, "unavailable")
    hass.states.async_set(OUTSIDE, "unavailable")

    result = await run()

    assert "| missing temperature: inside outside | want setpoint=10 heater=OFF" in result["line"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_reads_the_inputs_too(hass, start, run, climate, caplog):
    await start()
    await climate.async_set_hvac_mode(HVACMode.OFF)
    result = await run()
    assert "hvac OFF" in result["line"]
    hass.states.async_set(INSIDE, "unavailable")

    await run(10)

    assert about(caplog, "inside temperature") == ["inside temperature missing, holding 20.9 for up to 5 min"]


# PWM and circulation through a hold

FORCED = with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True})


def pwm_lines(caplog, prefix: str) -> list[str]:
    return [m for m in info(caplog) if m.startswith(prefix)]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*FORCED)
async def test_a_flapping_sensor_never_resets_pwm_or_cuts_a_pulse(hass, climate, start, run, caplog):
    await start(21.0)
    assert climate.pwm.status == PWMStatus.ON
    pulses = pwm_lines(caplog, "pwm ON")

    # Missing for a cycle or two every few cycles, while the pulse runs
    for good in (True, False, True, True, False, False, True, False, True, True, False, True):
        hass.states.async_set(INSIDE, "20.9" if good else "unavailable")
        result = await run(10)

        assert "missing temperature" not in result["line"]
        assert climate.pwm.status == PWMStatus.ON
        assert not set(result["writes"]) & set(OFF_WRITES)

    assert pwm_lines(caplog, "pwm IDLE") == []
    assert pwm_lines(caplog, "pwm OFF") == []
    assert pwm_lines(caplog, "pwm ON") == pulses


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*FORCED)
async def test_a_hold_that_runs_out_resets_pwm_and_turns_the_heater_off(hass, climate, start, run, caplog):
    await start(21.0)
    assert climate.pwm.status == PWMStatus.ON
    hass.states.async_set(INSIDE, "unavailable")

    for _ in range(2):
        result = await run(100)
        assert " held=inside" in result["line"]
        assert climate.pwm.status == PWMStatus.ON
        assert not set(result["writes"]) & set(OFF_WRITES)

    result = await run(100)

    assert "missing temperature: inside" in result["line"]
    assert pwm_lines(caplog, "pwm IDLE") == ["pwm IDLE (missing temperature)"]
    assert climate.pwm.status == PWMStatus.IDLE
    assert result["writes"] == OFF_WRITES
    assert "inside temperature still missing after 5 min, no longer held" in info(caplog)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry({"circulate_after_hot_water": True, "circulation_duration": 240}))
async def test_a_running_circulation_continues_through_a_hold(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    from tests.test_circulation_followups import PUMP_ON, Driver, circulate

    driver = Driver(hass, climate, boiler_data, freezer, mqtt_mock, published, caplog)
    await circulate(driver)
    assert driver.line and "(circulation)" in driver.line

    hass.states.async_set(INSIDE, "unavailable")
    for _ in range(3):
        await driver.cycle(10, **PUMP_ON)
        assert "(circulation)" in driver.line
        assert " held=inside" in driver.line
        assert "circulation end" not in " ".join(driver.new)
        assert driver.published_values("heatingoff") == []
        assert 0 not in driver.published_values("selflowtemp")


# Translations: the start window sentence

@pytest.mark.parametrize("language", ["en", "de", "es", "fr", "it", "nl", "pt", "sk"])
def test_translations_explain_the_start_window(language):
    from pathlib import Path

    path = Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json"
    descriptions = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]["advanced"]["data_description"]

    assert "5" in descriptions["circulate_after_hot_water"]
    assert all(number in descriptions["circulation_duration"] for number in ("5", "240", "60"))
    # The sentence is an addition: the older text about the control cycle is still there
    assert "30" in descriptions["circulation_duration"]
