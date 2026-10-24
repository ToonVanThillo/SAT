"""Tests for releasing the control setpoint when the heater is off, end-to-end from the climate to the MQTT messages."""

import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import ChannelStatus, Quantity
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_RADIATORS,
)
from custom_components.sat.coordinator import DeviceState, SatDataUpdateCoordinator
from tests.conftest import TIMELINE, advance
from tests.const import IDLE, ems_entry, otgw_entry, with_data
from tests.test_climate_loop import message, run_loop

pytestmark = pytest.mark.usefixtures("loop_entry")

RADIATOR = "climate.radiator"
RELEASED = "control setpoint released (heater off)"
HEATER_OFF = message('{"cmd": "heatingoff", "value": "on"}')


def selflowtemp(calls: list[tuple]) -> list[tuple]:
    return [call for call in calls if '"selflowtemp"' in call[1]]


def setpoint_status(coordinator: SatDataUpdateCoordinator) -> ChannelStatus:
    return coordinator.channel_states[Quantity.CONTROL_SETPOINT].status


async def step(climate, boiler_data, mqtt_mock, published, freezer: FrozenDateTimeFactory, **data) -> list[tuple]:
    """Feed live boiler data, run one cycle and return what it published."""
    freezer.tick(1)
    await boiler_data(**data)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()

    return published()


def logged(caplog, text: str) -> bool:
    return any(text in r.getMessage() for r in caplog.get_records("call") if r.levelno == logging.INFO)


def requested_setpoints(caplog) -> list[str]:
    """The control setpoint requested by each cycle so far in the call phase, from the `want` segment of the timeline's cycle lines."""
    wants = [segment for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno == logging.DEBUG
             for segment in r.getMessage().split(" | ") if segment.startswith("want ")]
    return [field.removeprefix("setpoint=") for want in wants for field in want.split() if field.startswith("setpoint=")]


# Off phase on EMS-ESP, via the too-cold path

@pytest.mark.parametrize(*ems_entry())
async def test_off_phase_is_released_once_the_heater_setting_reads_back(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE)

    # Cycle 1: heating still reads as enabled
    assert await run_loop(climate, mqtt_mock, published, target=15.0) == [
        message('{"cmd": "selflowtemp", "value": 0}'),
        message('{"cmd": "burnmaxpower", "value": 0}'),
        HEATER_OFF,
    ]
    assert setpoint_status(coordinator) == ChannelStatus.PENDING
    assert requested_setpoints(caplog) == ["10"]
    assert not logged(caplog, RELEASED)

    # Cycle 2: heating reads back as disabled
    caplog.clear()
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=0)
    assert selflowtemp(calls) == []
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert logged(caplog, RELEASED)
    assert requested_setpoints(caplog) == ["RELEASE"]

    # Later off cycles, with the boiler reading selflowtemp 5 or 0
    for value in (5, 0, 5):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=value)
        assert selflowtemp(calls) == []
        assert setpoint_status(coordinator) == ChannelStatus.IDLE


@pytest.mark.parametrize(*ems_entry())
async def test_unconfirmed_heatingoff_write_keeps_publishing_the_setpoint(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published, target=15.0)

    for _ in range(3):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="off")
        assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 0}')]
        assert setpoint_status(coordinator) != ChannelStatus.IDLE

    assert not logged(caplog, RELEASED)


@pytest.mark.parametrize(*ems_entry())
async def test_restored_heatingoff_is_not_confirmation(entry, climate, coordinator, boiler_data, mqtt_mock, published, caplog):
    caplog.set_level(logging.INFO)
    coordinator.async_set_updated_data(IDLE | {"heatingoff": "on"}, restored=True)
    await climate.async_set_target_temperature(15.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    mqtt_mock.async_publish.reset_mock()

    await climate.async_control_heating_loop()

    assert not logged(caplog, RELEASED)
    assert setpoint_status(coordinator) != ChannelStatus.IDLE
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None


@pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR]}))
@pytest.mark.parametrize(("readback", "expected"), [
    (5, [message('{"cmd": "selflowtemp", "value": 35}'), message('{"cmd": "heatingoff", "value": "off"}')]),
    (35, [message('{"cmd": "heatingoff", "value": "off"}')]),
])
async def test_heater_on_after_release_is_a_first_write(hass: HomeAssistant, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, readback, expected):
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published, target=15.0)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=5)
    assert setpoint_status(coordinator) == ChannelStatus.IDLE

    await climate.async_set_target_temperature(21.0)
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "heating", "hvac_modes": ["heat", "off"]})
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=readback)

    assert [c for c in calls if '"burnmaxpower"' not in c[1]] == expected
    assert setpoint_status(coordinator) == (ChannelStatus.PENDING if readback == 5 else ChannelStatus.IN_SYNC)


@pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR]}))
async def test_no_open_valve_keeps_publishing_the_setpoint(hass: HomeAssistant, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "idle", "hvac_modes": ["heat", "off"]})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    for _ in range(3):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
        assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 35}')]
        assert not any('"heatingoff"' in c[1] for c in calls)

    assert not logged(caplog, RELEASED)


@pytest.mark.parametrize(*ems_entry())
async def test_heater_on_publishes_the_setpoint_every_cycle(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "off", "burnmaxpower": 100})
    await run_loop(climate, mqtt_mock, published)

    for _ in range(3):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="off")
        assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 35}')]

    assert not logged(caplog, RELEASED)
    assert "35.0" in requested_setpoints(caplog)


# Off phase via PWM

@pytest.mark.parametrize(*ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_on_with_cold_minimum_setpoint_is_released(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    # current behaviour, known bug: with a minimum setpoint ≤ COLD_SETPOINT (28.2) the PWM ON phase requests heater OFF, so it is released too
    caplog.set_level(logging.INFO)
    await boiler_data(**IDLE)
    assert HEATER_OFF in await run_loop(climate, mqtt_mock, published)
    assert setpoint_status(coordinator) == ChannelStatus.PENDING

    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    assert selflowtemp(calls) == []
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert logged(caplog, RELEASED)


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_off_phase_is_released(hass: HomeAssistant, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)

    # The ON phase requests heater ON. Advancing the clock past its duty time runs the pending scheduled loop, in which PWM switches to its OFF phase.
    assert "pwm ON (" in caplog.text
    freezer.tick(1)
    await boiler_data(heatingoff="off")
    mqtt_mock.async_publish.reset_mock()
    await advance(hass, freezer, 1800)
    assert "pwm OFF" in caplog.text
    assert selflowtemp(published()) == [message('{"cmd": "selflowtemp", "value": 0}')]
    assert HEATER_OFF in published()
    assert setpoint_status(coordinator) == ChannelStatus.PENDING

    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")

    assert selflowtemp(calls) == []
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert logged(caplog, RELEASED)


# HVAC OFF and other backends

@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_releases_unconditionally(entry, climate, coordinator, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "off"})
    await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF)

    assert setpoint_status(coordinator) == ChannelStatus.IDLE


@pytest.mark.parametrize(*otgw_entry())
async def test_opentherm_still_receives_the_minimum_setpoint_when_heater_off(hass, entry, climate, coordinator, mqtt_mock, published, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    async_fire_mqtt_message(hass, "otgw/value/gw1/centralheating", "ON")
    await hass.async_block_till_done()

    for _ in range(2):
        calls = await run_loop(climate, mqtt_mock, published, target=15.0)
        assert "CS=10" in [c[1] for c in calls]

    assert not logged(caplog, RELEASED)
    assert "10" in requested_setpoints(caplog)


# Confirmation must come from a read-back received after the send

def heater_status(coordinator: SatDataUpdateCoordinator) -> ChannelStatus:
    return coordinator.channel_states[Quantity.HEATER_STATE].status


@pytest.mark.parametrize(*ems_entry())
async def test_stale_readback_does_not_confirm_heater_off(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5})

    # Cycle N: heater ON requested
    assert selflowtemp(await run_loop(climate, mqtt_mock, published, target=21.0)) == [message('{"cmd": "selflowtemp", "value": 35}')]
    assert message('{"cmd": "heatingoff", "value": "off"}') in published()
    assert (setpoint_status(coordinator), heater_status(coordinator)) == (ChannelStatus.PENDING, ChannelStatus.PENDING)
    assert "35.0" in requested_setpoints(caplog)

    # Cycle N+1: heater OFF requested, the only read-back is the stale one from before cycle N
    caplog.clear()
    freezer.tick(1)
    await climate.async_set_target_temperature(15.0)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    calls = published()
    assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 0}')]
    assert HEATER_OFF in calls
    assert (setpoint_status(coordinator), heater_status(coordinator)) == (ChannelStatus.PENDING, ChannelStatus.PENDING)
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None
    assert "10" in requested_setpoints(caplog)
    assert not logged(caplog, RELEASED)

    # Cycle N+2: the read-back after the OFF send arrives
    caplog.clear()
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=0)
    assert selflowtemp(calls) == []
    assert (setpoint_status(coordinator), heater_status(coordinator)) == (ChannelStatus.IDLE, ChannelStatus.IN_SYNC)
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) == DeviceState.OFF
    assert logged(caplog, RELEASED)


@pytest.mark.parametrize(*ems_entry())
async def test_heater_off_answered_with_off_is_never_confirmed(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    await boiler_data(**IDLE | {"heatingoff": "off"})
    await run_loop(climate, mqtt_mock, published, target=15.0)

    for _ in range(3):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="off")
        assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 0}')]
        assert HEATER_OFF in calls
        assert heater_status(coordinator) == ChannelStatus.OUT_OF_SYNC
        assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None

    assert not logged(caplog, RELEASED)


@pytest.mark.parametrize(*ems_entry())
async def test_drift_after_release_requests_the_setpoint_again(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published, target=15.0)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=5)
    assert (setpoint_status(coordinator), heater_status(coordinator)) == (ChannelStatus.IDLE, ChannelStatus.IN_SYNC)
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) == DeviceState.OFF

    # Someone re-enables heating at the panel
    caplog.clear()
    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="off", selflowtemp=35)
    assert heater_status(coordinator) == ChannelStatus.DRIFTED
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None
    assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 0}')]
    assert HEATER_OFF in calls
    assert not logged(caplog, RELEASED)
    assert "10" in requested_setpoints(caplog)


@pytest.mark.parametrize(*ems_entry())
async def test_adopted_first_write_observed(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    caplog.set_level(logging.INFO)
    await boiler_data(**IDLE | {"heatingoff": "on", "selflowtemp": 5})

    calls = await run_loop(climate, mqtt_mock, published, target=15.0)
    assert HEATER_OFF not in calls
    assert heater_status(coordinator) == ChannelStatus.IN_SYNC
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) == DeviceState.OFF

    calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on", selflowtemp=5)
    assert selflowtemp(calls) == []
    assert logged(caplog, RELEASED)


# confirmed_value accessor

@pytest.mark.parametrize(*otgw_entry())
async def test_confirmed_value_is_none_without_channels(entry, coordinator):
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None
    assert coordinator.confirmed_value(Quantity.CONTROL_SETPOINT) is None


@pytest.mark.parametrize(*ems_entry())
async def test_confirmed_value_follows_the_channel_status(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer):
    # No live read-back yet
    await run_loop(climate, mqtt_mock, published, target=15.0)
    assert heater_status(coordinator) == ChannelStatus.WAITING
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None

    await step(climate, boiler_data, mqtt_mock, published, freezer, **IDLE | {"heatingoff": "on"})
    assert heater_status(coordinator) == ChannelStatus.IN_SYNC
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) == DeviceState.OFF

    # Persists while nothing new is sent
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) == DeviceState.OFF

    # A new send is pending
    await climate.async_set_target_temperature(21.0)
    await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
    assert heater_status(coordinator) == ChannelStatus.PENDING
    assert coordinator.confirmed_value(Quantity.HEATER_STATE) is None
