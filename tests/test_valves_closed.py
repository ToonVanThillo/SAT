"""Tests for HEAT with no open valve: the boiler is requested off (releases the control setpoint, heater OFF), end-to-end from the climate to the MQTT messages."""
import logging
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant

from custom_components.sat.channels import RELEASE, ChannelStatus
from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_RADIATORS,
    CONF_ROOMS,
    MINIMUM_OFF_TIME,
    PWMStatus,
)
from custom_components.sat.coordinator import DeviceState
from tests.conftest import TIMELINE, advance, cycle_records, last_cycle
from tests.const import IDLE, with_data
from tests.test_climate_loop import message, run_loop
from tests.test_climate_loop_ems import INSIDE, RADIATOR
from tests.test_heater_off_release import (
    HEATER_OFF,
    released,
    requested_setpoints,
    selflowtemp,
    setpoint_status,
    step,
)

pytestmark = pytest.mark.usefixtures("loop_entry")

ROOM = "climate.room"
HEATER_ON = message('{"cmd": "heatingoff", "value": "off"}')
WITH_RADIATOR = with_data({CONF_RADIATORS: [RADIATOR]})
# A minimum setpoint of 30 with unrestricted modulation disallowed: the first open-valve cycle starts a pulse at once
UNRESTRICTED_DISALLOWED = with_data({CONF_RADIATORS: [RADIATOR], CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False})


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


@pytest.fixture(autouse=True)
def timeline_levels(caplog):
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
    assert last_cycle(caplog)["want"].control_setpoint == 35.0
    assert climate.valves_open is True
    assert last_cycle(caplog)["inputs"].valves_open is True


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_without_radiators_pwm_is_updated_normally(entry, climate, boiler_data, mqtt_mock, published, caplog):
    await boiler_data(**IDLE)

    await run_loop(climate, mqtt_mock, published)

    assert climate.pwm.status == PWMStatus.ON
    assert last_cycle(caplog)["want"].control_setpoint == 30


# All radiators closed

@pytest.mark.parametrize(*WITH_RADIATOR)
@pytest.mark.parametrize(("state", "attributes"), [
    ("heat", radiator("idle")),
    ("off", {"hvac_modes": ["heat", "off"]}),
    ("unavailable", {"hvac_modes": ["heat", "off"]}),
])
async def test_closed_valves_release_the_setpoint_and_request_heater_off(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, state, attributes):
    await prepare(hass, climate, boiler_data, heatingoff="off")
    hass.states.async_set(RADIATOR, state, attributes)
    with patch.object(SatClimate, "calculated_setpoint", new_callable=PropertyMock, return_value=50.0), patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        calls = await run_loop(climate, mqtt_mock, published)

    desired = submit.await_args_list[-1].args[0]
    assert (desired.control_setpoint, desired.heater_state) == (RELEASE, DeviceState.OFF)
    assert selflowtemp(calls) == []
    assert HEATER_OFF in calls


def entry_variants(extra_data: list[dict], options: list[dict]) -> tuple[str, list[tuple]]:
    """Return the EMS entry parametrization for every combination of the given option sets and data sets."""
    return ("domains, data, options, config", [with_data(data, opts)[1][0] for data in extra_data for opts in options])


CLOSED_ENTRIES = entry_variants(
    [{CONF_RADIATORS: [RADIATOR]}, {CONF_RADIATORS: [RADIATOR], CONF_MINIMUM_SETPOINT: 30}],
    [{}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}],
)


@pytest.mark.parametrize(*CLOSED_ENTRIES)
@pytest.mark.parametrize("calculated", [50.0, 15.0])
async def test_closed_valves_ignore_the_calculated_setpoint_and_pwm_mode(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, caplog, calculated):
    await prepare(hass, climate, boiler_data)
    close_valves(hass)

    with patch.object(SatClimate, "calculated_setpoint", new_callable=PropertyMock, return_value=calculated), \
            patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await run_loop(climate, mqtt_mock, published)

    desired = submit.await_args_list[-1].args[0]
    assert desired.control_setpoint == RELEASE
    assert desired.heater_state == DeviceState.OFF
    assert climate.pwm.status == PWMStatus.OFF
    assert climate.smoothed_setpoint == calculated
    assert hass.states.get("sensor.calculated_setpoint_test").state == str(calculated)
    assert last_cycle(caplog)["smoothed"] == calculated


# PWM when the valves close

@pytest.mark.parametrize(*UNRESTRICTED_DISALLOWED)
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

    # Every cycle with closed valves resets PWM
    assert climate.pwm.status == PWMStatus.OFF
    assert [r.fields["inputs"].valves_open for r in cycle_records(caplog)] == [False] * 2  # type: ignore[attr-defined]

    # The reopened valves wait for the off phase after the pulse that ended, by the reset (ON) or before it (OFF)
    caplog.clear()
    open_valves(hass)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["inputs"].valves_open is True

    assert climate.pwm.duty_cycle is not None
    freezer.tick(max(climate.pwm.duty_cycle[1], MINIMUM_OFF_TIME))
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    assert climate.pwm.status == PWMStatus.ON


@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_closed_valves_reset_pwm_every_cycle_also_without_a_pulse(hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
    await prepare(hass, climate, boiler_data, heatingoff="on")
    close_valves(hass)

    await run_loop(climate, mqtt_mock, published)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    # No pulse ran, and each cycle still reports its reset
    assert climate.pwm.status == PWMStatus.OFF
    assert [r.fields["inputs"].valves_open for r in cycle_records(caplog)] == [False] * 2  # type: ignore[attr-defined]


# The cycle record

@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_cycle_record_shows_closed_valves(hass, entry, climate, boiler_data, mqtt_mock, published, caplog):
    await prepare(hass, climate, boiler_data, heatingoff="off")
    close_valves(hass)

    with patch.object(SatClimate, "calculated_setpoint", new_callable=PropertyMock, return_value=50.0):
        await run_loop(climate, mqtt_mock, published)

    fields = last_cycle(caplog)
    assert fields["inputs"].valves_open is False
    assert fields["smoothed"] == 50.0
    assert (fields["want"].control_setpoint, fields["want"].heater_state) == (RELEASE, DeviceState.OFF)


# Valves reopen

@pytest.mark.parametrize(*WITH_RADIATOR)
async def test_reopening_valves_requests_the_setpoint_again(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    close_valves(hass)
    await run_loop(climate, mqtt_mock, published)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=0)
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert released(caplog)

    caplog.clear()
    open_valves(hass)
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=5)

    assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 35}')]
    assert HEATER_ON in calls
    assert requested_setpoints(caplog) == [35.0]
    assert last_cycle(caplog)["inputs"].valves_open is True


@pytest.mark.parametrize(*UNRESTRICTED_DISALLOWED)
async def test_reopening_valves_before_any_pulse_starts_a_pulse_at_once(hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    close_valves(hass)
    await run_loop(climate, mqtt_mock, published)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    # The resets ended no pulse, as none ran: there is no off phase
    assert climate.pwm.status == PWMStatus.OFF

    caplog.clear()
    open_valves(hass)
    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    assert climate.pwm.status == PWMStatus.ON
    desired = submit.await_args_list[-1].args[0]
    assert desired.control_setpoint == 30
    assert desired.heater_state == DeviceState.ON
    assert requested_setpoints(caplog) == [30]


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
    ("idle", "heating", RELEASE, DeviceState.OFF),
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
@pytest.mark.parametrize(*UNRESTRICTED_DISALLOWED)
@pytest.mark.parametrize(("before", "during", "status", "setpoint"), [
    ("heating", "idle", PWMStatus.ON, 30),
    ("idle", "heating", PWMStatus.OFF, RELEASE),
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
@pytest.mark.usefixtures("no_input_grace")
async def test_missing_temperature_submits_heater_off_whatever_the_valves(hass, entry, climate, coordinator, boiler_data, hvac_action):
    await boiler_data(**IDLE)
    hass.states.async_set(RADIATOR, "heat", radiator(hvac_action))
    hass.states.async_set(INSIDE, "unavailable")
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    with patch.object(coordinator, "async_submit", wraps=coordinator.async_submit) as submit:
        await climate.async_control_heating_loop()

    submit.assert_awaited_once()
    desired = submit.await_args.args[0]
    assert (desired.control_setpoint, desired.heater_state) == (RELEASE, DeviceState.OFF)


