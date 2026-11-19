"""Tests for releasing the control setpoint when the heater is off, end-to-end from the climate to the MQTT messages."""

import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import RELEASE, ChannelStatus, Quantity
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_RADIATORS,
    PWMStatus,
)
from custom_components.sat.coordinator import SatDataUpdateCoordinator
from tests.conftest import TIMELINE, advance, cycle_records
from tests.const import IDLE, ems_entry, otgw_entry, with_data
from tests.test_climate_loop import message, run_loop

pytestmark = pytest.mark.usefixtures("loop_entry")

RADIATOR = "climate.radiator"
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


@pytest.fixture(autouse=True)
def timeline_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


def requested_setpoints(caplog) -> list:
    """The control setpoint requested by each cycle so far, from the `want` of the cycle records (RELEASE when released)."""
    wants = [r.fields["want"] for r in cycle_records(caplog)]  # type: ignore[attr-defined]

    return [want.control_setpoint for want in wants if want.control_setpoint is not None]


def released(caplog) -> bool:
    """Whether a cycle so far requested the control setpoint released."""
    return RELEASE in requested_setpoints(caplog)


# Every heater OFF request releases the control setpoint from the first cycle, without waiting for a read-back

@pytest.mark.parametrize(*ems_entry())
async def test_heater_off_in_heat_releases_the_setpoint_on_the_first_cycle(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE)

    # Cycle 1: heating still reads as enabled, yet no setpoint is published
    calls = await run_loop(climate, mqtt_mock, published, target=15.0)
    assert selflowtemp(calls) == []
    assert HEATER_OFF in calls
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert requested_setpoints(caplog) == [RELEASE]

    # Later cycles, whatever the boiler reads back
    for heatingoff, value in (("off", 35), ("on", 5), ("on", 0)):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff=heatingoff, selflowtemp=value)
        assert selflowtemp(calls) == []
        assert setpoint_status(coordinator) == ChannelStatus.IDLE


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
async def test_no_open_valve_releases_the_setpoint(hass: HomeAssistant, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "idle", "hvac_modes": ["heat", "off"]})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    for _ in range(2):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="on")
        assert selflowtemp(calls) == []
        assert not any('"heatingoff"' in c[1] for c in calls)
        assert setpoint_status(coordinator) == ChannelStatus.IDLE

    assert released(caplog)


@pytest.mark.parametrize(*ems_entry())
async def test_heater_on_publishes_the_setpoint_every_cycle(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "off", "burnmaxpower": 100})
    await run_loop(climate, mqtt_mock, published)

    for _ in range(3):
        calls = await step(climate, boiler_data, mqtt_mock, published, freezer, heatingoff="off")
        assert selflowtemp(calls) == [message('{"cmd": "selflowtemp", "value": 35}')]

    assert not released(caplog)
    assert 35.0 in requested_setpoints(caplog)


# Off phase via PWM

@pytest.mark.parametrize(*ems_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_pwm_on_with_cold_minimum_setpoint_is_released(entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    # With a minimum setpoint at or below COLD_SETPOINT (28.2) the PWM ON phase requests heater OFF, so it is released too
    await boiler_data(**IDLE)
    calls = await run_loop(climate, mqtt_mock, published)

    assert HEATER_OFF in calls
    assert selflowtemp(calls) == []
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert released(caplog)


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_pwm_off_phase_is_released(hass: HomeAssistant, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published)

    # The ON phase requests heater ON. Advancing the clock past its duty time runs the pending scheduled loop, in which PWM switches to its OFF phase.
    assert climate.pwm.status == PWMStatus.ON
    freezer.tick(1)
    await boiler_data(heatingoff="off")
    mqtt_mock.async_publish.reset_mock()
    await advance(hass, freezer, 1800)

    assert climate.pwm.status == PWMStatus.OFF
    assert selflowtemp(published()) == []
    assert HEATER_OFF in published()
    assert setpoint_status(coordinator) == ChannelStatus.IDLE
    assert released(caplog)


# HVAC OFF and other backends

@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_releases_unconditionally(entry, climate, coordinator, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "off"})
    await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF)

    assert setpoint_status(coordinator) == ChannelStatus.IDLE


@pytest.mark.parametrize(*otgw_entry())
async def test_a_backend_without_channels_publishes_no_setpoint_with_the_heater_off(hass, entry, climate, coordinator, mqtt_mock, published, caplog):
    async_fire_mqtt_message(hass, "otgw/value/gw1/centralheating", "ON")
    await hass.async_block_till_done()

    for _ in range(2):
        calls = [c[1] for c in await run_loop(climate, mqtt_mock, published, target=15.0)]
        assert "CH=0" in calls
        assert not any(c.startswith("CS=") for c in calls)

    assert released(caplog)
