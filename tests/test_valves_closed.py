"""Tests for HEAT with no open valve: the boiler is requested off (minimum setpoint, heater OFF), end-to-end from the climate to the MQTT messages."""
import logging
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import RELEASE, ChannelStatus, Quantity
from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_RADIATORS,
    CONF_ROOMS,
    MINIMUM_SETPOINT,
    PWMStatus,
)
from custom_components.sat.coordinator import DeviceState
from tests.conftest import TIMELINE, advance
from tests.const import IDLE, otgw_entry, with_data
from tests.test_climate_loop import message, run_loop
from tests.test_climate_loop_ems import INSIDE, RADIATOR
from tests.test_heater_off_release import (
    HEATER_OFF,
    RELEASED,
    logged,
    requested_setpoints,
    selflowtemp,
    setpoint_status,
    step,
)

pytestmark = pytest.mark.usefixtures("loop_entry")

ROOM = "climate.room"
HEATER_ON = message('{"cmd": "heatingoff", "value": "off"}')
REQUESTED_AGAIN = "control setpoint requested again"
WITH_RADIATOR = with_data({CONF_RADIATORS: [RADIATOR]})
# A minimum setpoint of 30 (above COLD_SETPOINT, 28.2, so the PWM ON phase requests heater ON) with forced PWM: the first open-valve cycle starts a pulse at once
FORCED_PWM = with_data({CONF_RADIATORS: [RADIATOR], CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True})


def radiator(hvac_action: str) -> dict:
    return {"hvac_action": hvac_action, "hvac_modes": ["heat", "off"]}


def close_valves(hass: HomeAssistant) -> None:
    hass.states.async_set(RADIATOR, "heat", radiator("idle"))


def open_valves(hass: HomeAssistant) -> None:
    hass.states.async_set(RADIATOR, "heat", radiator("heating"))


async def prepare(hass: HomeAssistant, climate: SatClimate, boiler_data, **data) -> None:
    await boiler_data(**IDLE | data)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)


def cycle_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno == logging.DEBUG]


def timeline_info(caplog, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno == logging.INFO and text in r.getMessage()]


@pytest.fixture(autouse=True)
def timeline_levels(caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


# No radiators configured: the valves count as open

@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
@pytest.mark.parametrize("room_action", [None, "idle", "off"])
async def test_without_radiators_the_valves_count_as_open(hass, entry, climate, boiler_data, mqtt_mock, published, caplog, room_action):
    # Also with area/room climates configured: only radiators decide
    if room_action is not None:
        hass.states.async_set(ROOM, "off" if room_action == "off" else "heat", {"hvac_action": room_action, "temperature": 21, "current_temperature": 21, "hvac_modes": ["heat", "off"]})
    await boiler_data(**IDLE | {"heatingoff": "on"})

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 35}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
        HEATER_ON,
    ]
    assert climate.setpoint == 35.0
    assert hass.states.get(climate.entity_id).attributes["valves_open"] is True
    assert "valves=open" in cycle_lines(caplog)[-1]
    assert not timeline_info(caplog, "valves closed")


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_without_radiators_pwm_is_updated_normally(entry, climate, boiler_data, mqtt_mock, published, caplog):
    await boiler_data(**IDLE)

    await run_loop(climate, mqtt_mock, published)

    assert climate.pwm.status == PWMStatus.ON
    assert timeline_info(caplog, "pwm ON")
    assert not timeline_info(caplog, "valves closed")
    assert climate.setpoint == 30


# All radiators closed

@pytest.mark.parametrize(*WITH_RADIATOR)
@pytest.mark.parametrize(("state", "attributes"), [
    ("heat", radiator("idle")),
    ("off", {"hvac_modes": ["heat", "off"]}),
    ("unavailable", {"hvac_modes": ["heat", "off"]}),
])
async def test_closed_valves_request_minimum_setpoint_and_heater_off(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, state, attributes):
    await prepare(hass, climate, boiler_data, heatingoff="off")
    hass.states.async_set(RADIATOR, state, attributes)
    with patch.object(climate, "_calculate_control_setpoint", return_value=50.0), patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        calls = await run_loop(climate, mqtt_mock, published)

    desired = submit.await_args_list[-1].args[0]
    assert (desired.control_setpoint, desired.heater_state) == (MINIMUM_SETPOINT, DeviceState.OFF)
    assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 0}')]
    assert HEATER_OFF in calls


def entry_variants(extra_data: list[dict], options: list[dict]) -> tuple[str, list[tuple]]:
    """Return the EMS entry parametrization for every combination of the given option sets and data sets."""
    return ("domains, data, options, config", [with_data(data, opts)[1][0] for data in extra_data for opts in options])


CLOSED_ENTRIES = entry_variants(
    [{CONF_RADIATORS: [RADIATOR]}, {CONF_RADIATORS: [RADIATOR], CONF_MINIMUM_SETPOINT: 30}],
    [{}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}],
)


@pytest.mark.parametrize(*CLOSED_ENTRIES)
@pytest.mark.parametrize("calculated", [50.0, 15.0])
async def test_closed_valves_ignore_the_calculated_setpoint_and_pwm_mode(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, caplog, calculated):
    await prepare(hass, climate, boiler_data)
    close_valves(hass)

    with patch.object(climate, "_calculate_control_setpoint", return_value=calculated), \
            patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await run_loop(climate, mqtt_mock, published)

    desired = submit.await_args_list[-1].args[0]
    assert desired.control_setpoint == MINIMUM_SETPOINT
    assert desired.heater_state == DeviceState.OFF
    attributes = hass.states.get(climate.entity_id).attributes
    assert attributes["setpoint"] == 10
    assert attributes["pulse_width_modulation_state"] == "IDLE"
    # Current behaviour, known bug: the "valves closed" PWM reset also disables PWM, even when forced (docs/known-issues.md, Control logic)
    assert attributes["pulse_width_modulation_enabled"] is False
    assert climate.calculated_setpoint == calculated
    assert hass.states.get("sensor.calculated_setpoint_test").state == str(calculated)
    assert f"setpoint calc={calculated} (valves closed)" in cycle_lines(caplog)[-1]


# PWM when the valves close

@pytest.mark.parametrize(*FORCED_PWM)
@pytest.mark.parametrize("phase", ["ON", "OFF"])
async def test_closing_valves_ends_a_running_pulse_at_once(hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog, phase):
    await prepare(hass, climate, boiler_data, heatingoff="on")
    open_valves(hass)
    await run_loop(climate, mqtt_mock, published)
    assert climate.pwm.status == PWMStatus.ON
    if phase == "OFF":
        freezer.tick(1)
        await boiler_data(heatingoff="off")
        await advance(hass, freezer, 1800)
        assert climate.pwm.status == PWMStatus.OFF

    # Within the 180 s minimum pulse (ON) or the off phase: the next cycle resets at once
    caplog.clear()
    close_valves(hass)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    assert climate.pwm.status == PWMStatus.IDLE
    assert timeline_info(caplog, "pwm IDLE") == ["pwm IDLE (valves closed)"]
    # Current behaviour, known bug: the reset also disables PWM (docs/known-issues.md, Control logic)
    assert climate.pwm.enabled is False

    # Current behaviour, known bug: the reset zeroes the hourly pulse count, so the next pulse is pulse 1 again, not 2
    caplog.clear()
    open_valves(hass)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    assert timeline_info(caplog, "pwm ON (pulse 1 this hour")


@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_closed_valves_with_idle_pwm_log_nothing(hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    await prepare(hass, climate, boiler_data, heatingoff="on")
    close_valves(hass)
    assert climate.pulse_width_modulation_enabled is False

    await run_loop(climate, mqtt_mock, published)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    # PWM was already IDLE: nothing is logged
    assert not timeline_info(caplog, "pwm IDLE")


@pytest.mark.parametrize(*FORCED_PWM)
async def test_valves_closed_is_the_reason_when_pwm_is_not_enabled_either(hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    await prepare(hass, climate, boiler_data, heatingoff="on")
    open_valves(hass)
    await run_loop(climate, mqtt_mock, published)
    assert climate.pwm.status == PWMStatus.ON

    # PWM is ON and, without the force, not enabled in the same cycle (calculated setpoint 35 above the minimum 30)
    caplog.clear()
    close_valves(hass)
    with patch.object(climate, "_force_pulse_width_modulation", False):
        assert climate.pulse_width_modulation_enabled is False
        await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    assert timeline_info(caplog, "pwm IDLE") == ["pwm IDLE (valves closed)"]


# The debug cycle line

@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_cycle_line_shows_closed_valves(hass, entry, climate, boiler_data, mqtt_mock, published, caplog):
    await prepare(hass, climate, boiler_data, heatingoff="off")
    close_valves(hass)

    with patch.object(climate, "_calculate_control_setpoint", return_value=50.0):
        await run_loop(climate, mqtt_mock, published)

    line = cycle_lines(caplog)[-1]
    assert "valves=closed" in line
    assert "setpoint calc=50.0 (valves closed)" in line
    assert "want setpoint=10 heater=OFF" in line


# EMS-ESP release timing

@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_closed_valves_release_the_setpoint_once_heater_off_is_confirmed(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "off"})
    close_valves(hass)

    # Cycle 1: heating still enabled, so both are requested
    calls = await run_loop(climate, mqtt_mock, published)
    assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 0}')]
    assert HEATER_OFF in calls
    assert setpoint_status(coordinator) == ChannelStatus.PENDING
    assert requested_setpoints(caplog) == ["10"]
    assert not logged(caplog, RELEASED)

    # Cycle 2: verify has confirmed heatingoff on
    caplog.clear()
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=0)
    assert coordinator.channel_states[Quantity.HEATER_STATE].status == ChannelStatus.IN_SYNC
    assert selflowtemp(calls) == []
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert timeline_info(caplog, RELEASED) == [RELEASED]
    assert requested_setpoints(caplog) == ["RELEASE"]
    assert "want setpoint=RELEASE heater=OFF" in cycle_lines(caplog)[-1]
    assert "valves=closed" in cycle_lines(caplog)[-1]


# A backend that does not release

_, [(OT_DOMAINS, OT_DATA, OT_OPTIONS, OT_CONFIG)] = otgw_entry()


@pytest.mark.parametrize("domains, data, options, config", [(OT_DOMAINS, OT_DATA | {CONF_RADIATORS: [RADIATOR]}, OT_OPTIONS, OT_CONFIG)])
async def test_opentherm_keeps_requesting_the_minimum_setpoint_with_closed_valves(hass, entry, climate, mqtt_mock, published, caplog):
    async_fire_mqtt_message(hass, "otgw/value/gw1/centralheating", "ON")
    await hass.async_block_till_done()
    close_valves(hass)

    for _ in range(4):
        calls = await run_loop(climate, mqtt_mock, published)
        assert "CS=10" in [c[1] for c in calls]

    assert requested_setpoints(caplog) == ["10"] * 4
    assert all("heater=OFF" in line for line in cycle_lines(caplog) if "want " in line)
    assert not timeline_info(caplog, "released")
    assert not timeline_info(caplog, REQUESTED_AGAIN)


# Valves reopen

@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_reopening_valves_requests_the_setpoint_again(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    close_valves(hass)
    await run_loop(climate, mqtt_mock, published)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=0)
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert logged(caplog, RELEASED)

    caplog.clear()
    open_valves(hass)
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=5)

    assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 35}')]
    assert HEATER_ON in calls
    assert timeline_info(caplog, REQUESTED_AGAIN) == [REQUESTED_AGAIN]
    assert "valves=open" in cycle_lines(caplog)[-1]


@pytest.mark.parametrize(*FORCED_PWM)
async def test_reopening_valves_starts_a_pulse_at_once(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    close_valves(hass)
    await run_loop(climate, mqtt_mock, published)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    assert climate.pwm.status == PWMStatus.IDLE

    caplog.clear()
    open_valves(hass)
    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    assert climate.pwm.status == PWMStatus.ON
    assert timeline_info(caplog, "pwm ON (")
    desired = submit.await_args_list[-1].args[0]
    assert desired.control_setpoint == 30
    assert desired.heater_state == DeviceState.ON
    assert timeline_info(caplog, REQUESTED_AGAIN) == [REQUESTED_AGAIN]


# Valves change mid-cycle

def after_update_step(coordinator, change) -> None:
    """Run the async `change` once, right after the coordinator update step of the next cycle: its inputs are captured, no decision is made yet."""
    update = coordinator.async_control_heating_loop

    async def update_then_change(*args, **kwargs) -> None:
        del coordinator.async_control_heating_loop
        await update(*args, **kwargs)
        await change()

    coordinator.async_control_heating_loop = update_then_change


@pytest.mark.usefixtures("no_publish_error")
@pytest.mark.parametrize(*WITH_RADIATOR)
@pytest.mark.parametrize(("before", "during", "setpoint", "heater"), [
    ("heating", "idle", 35, DeviceState.ON),
    ("idle", "heating", 10, DeviceState.OFF),
])
async def test_setpoint_and_heater_follow_the_captured_valves(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, before, during, setpoint, heater):
    await prepare(hass, climate, boiler_data, heatingoff="on")
    hass.states.async_set(RADIATOR, "heat", radiator(before))
    mqtt_mock.async_publish.reset_mock()

    async def change() -> None:
        hass.states.async_set(RADIATOR, "heat", radiator(during))

    after_update_step(coordinator, change)
    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_control_heating_loop()

    assert hass.states.get(RADIATOR).attributes["hvac_action"] == during
    desired = submit.await_args_list[-1].args[0]
    assert (desired.control_setpoint, desired.heater_state) == (setpoint, heater)


@pytest.mark.usefixtures("no_publish_error")
@pytest.mark.parametrize(*FORCED_PWM)
@pytest.mark.parametrize(("before", "during", "status", "setpoint"), [
    ("heating", "idle", PWMStatus.ON, 30),
    ("idle", "heating", PWMStatus.IDLE, 10),
])
async def test_pwm_follows_the_captured_valves(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, before, during, status, setpoint):
    await prepare(hass, climate, boiler_data, heatingoff="on")
    hass.states.async_set(RADIATOR, "heat", radiator(before))
    mqtt_mock.async_publish.reset_mock()

    async def change() -> None:
        hass.states.async_set(RADIATOR, "heat", radiator(during))

    after_update_step(coordinator, change)
    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_control_heating_loop()

    assert climate.pwm.status == status
    assert submit.await_args_list[-1].args[0].control_setpoint == setpoint


# HVAC OFF and a missing temperature are not affected

@pytest.mark.parametrize(*WITH_RADIATOR)
@pytest.mark.parametrize("hvac_action", ["idle", "heating"])
async def test_hvac_off_releases_and_stops_the_heater_whatever_the_valves(hass, entry, climate, coordinator, boiler_data, hvac_action):
    await boiler_data(**IDLE)
    hass.states.async_set(RADIATOR, "heat", radiator(hvac_action))
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.OFF)

    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_control_heating_loop()

    submit.assert_awaited_once()
    desired = submit.await_args.args[0]
    assert (desired.control_setpoint, desired.heater_state) == (RELEASE, DeviceState.OFF)


@pytest.mark.parametrize(*WITH_RADIATOR)
@pytest.mark.parametrize("hvac_action", ["idle", "heating"])
async def test_missing_temperature_submits_nothing_whatever_the_valves(hass, entry, climate, coordinator, boiler_data, hvac_action):
    await boiler_data(**IDLE)
    hass.states.async_set(RADIATOR, "heat", radiator(hvac_action))
    hass.states.async_set(INSIDE, "unavailable")
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_control_heating_loop()

    submit.assert_not_awaited()


