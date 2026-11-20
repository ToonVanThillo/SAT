"""Characterization tests for the heating loop, end-to-end from the climate entity to the EMS MQTT messages."""
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later

from custom_components.sat.channels import RELEASE
from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MINIMUM_SETPOINT,
    PWMStatus,
)
from custom_components.sat.coordinator import DeviceState
from tests.conftest import TIMELINE, advance, last_cycle
from tests.const import IDLE, ems_entry, with_data

pytestmark = pytest.mark.usefixtures("loop_entry")


@pytest.fixture(autouse=True)
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


BOILER = "ems-esp/boiler"


def message(payload: str) -> tuple:
    return BOILER, payload, 1, False


async def run_loop(climate: SatClimate, mqtt_mock: MagicMock, published: Callable[[], list[tuple]], target: float = 21.0, mode: HVACMode = HVACMode.HEAT) -> list[tuple]:
    await climate.async_set_target_temperature(target)
    await climate.async_set_hvac_mode(mode)
    mqtt_mock.async_publish.reset_mock()

    await climate.async_control_heating_loop()

    return published()


@pytest.mark.parametrize(*ems_entry())
async def test_heat_publishes_setpoint_modulation_then_heater(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "on"})

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 35}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
        message('{"cmd": "heatingoff", "value": "off"}'),
    ]


@pytest.mark.parametrize(*ems_entry())
async def test_heater_already_enabled_is_not_started_again(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "off", "burnmaxpower": 100})

    assert await run_loop(climate, mqtt_mock, published) == [message('{"cmd": "selflowtemp", "value": 35}')]


@pytest.mark.parametrize(*ems_entry())
async def test_cold_setpoint_stops_heater_without_a_setpoint(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)

    # The control setpoint is released with the heater OFF, so no selflowtemp is published
    assert await run_loop(climate, mqtt_mock, published, target=15.0) == [
        message('{"cmd": "burnmaxpower", "value": 0}'),
        message('{"cmd": "heatingoff", "value": "on"}'),
    ]


@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_stops_an_enabled_heater_until_it_reads_back_off(entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE | {"heatingoff": "off"})
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # Switching off stops the heater right away
    await climate.async_set_hvac_mode(HVACMode.OFF)
    assert published()[-1] == message('{"cmd": "heatingoff", "value": "on"}')

    # A read-back that still shows heating enabled is a mismatch, so the next cycle repeats it
    freezer.tick(1)
    await boiler_data(heatingoff="off")
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published() == [message('{"cmd": "heatingoff", "value": "on"}')]

    # Once it reads back as disabled, nothing more is sent
    freezer.tick(1)
    await boiler_data(heatingoff="on")
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published() == []


@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_with_disabled_heater_publishes_nothing(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    mqtt_mock.async_publish.reset_mock()

    await climate.async_set_hvac_mode(HVACMode.OFF)
    await climate.async_control_heating_loop()

    assert published() == []


# A minimum setpoint of 30, above the default of the entries
WARM_MINIMUM_SETPOINT = {CONF_MINIMUM_SETPOINT: 30}


@pytest.mark.parametrize(*ems_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_pwm_on_with_a_minimum_below_30_heats_at_the_minimum(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "on"})

    # With a minimum setpoint below the lowest allowed minimum (30), e.g. from an entry stored before that bound, the PWM ON phase still requests the minimum with the heater ON
    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 20}'),
        message('{"cmd": "burnmaxpower", "value": 0}'),
        message('{"cmd": "heatingoff", "value": "off"}'),
    ]


@pytest.mark.parametrize(*with_data(WARM_MINIMUM_SETPOINT, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_hot_water_does_not_raise_pwm_modulation(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"tapwateractive": "on"})

    # burnmaxpower does not limit hot water power on EMS-ESP, so hot water does not raise it to the maximum.
    # No pulse starts while the boiler produces hot water, so PWM holds in its off phase and switches heating off.
    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "burnmaxpower", "value": 0}'),
        message('{"cmd": "heatingoff", "value": "on"}'),
    ]


@pytest.mark.parametrize(*with_data(WARM_MINIMUM_SETPOINT, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_pwm_on_sends_minimum_setpoint_and_zero_modulation_once(entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE)

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 30}'),
        message('{"cmd": "burnmaxpower", "value": 0}'),
    ]

    # The read-back equals the sent 0, so only selflowtemp is sent again (on every loop by design, which loop_runs relies on)
    freezer.tick(1)
    await boiler_data(burnmaxpower=0)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published() == [message('{"cmd": "selflowtemp", "value": 30}')]


@pytest.mark.parametrize(*with_data(WARM_MINIMUM_SETPOINT, {CONF_ALLOW_UNRESTRICTED_MODULATION: False, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31}))
async def test_pwm_on_with_floor_option_sends_floor_modulation_once(entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE | {"burnmaxpower": 100})

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 30}'),
        message('{"cmd": "burnmaxpower", "value": 31}'),
    ]

    # The read-back floor of 31 equals the sent value, so it is not sent again
    freezer.tick(1)
    await boiler_data(burnmaxpower=31)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published() == [message('{"cmd": "selflowtemp", "value": 30}')]


@pytest.mark.parametrize(*ems_entry())
async def test_loop_without_boiler_data_publishes_nothing(entry, climate, mqtt_mock, published):
    # Without boiler data the loop runs from the sensors alone, but the channels wait for a live read-back before their first write
    assert await run_loop(climate, mqtt_mock, published) == []


# schedule_control_heating_loop

def loop_runs(published: Callable[[], list[tuple]]) -> int:
    """Count the heating loop runs, each of which publishes one selflowtemp message."""
    return sum('"selflowtemp"' in call[1] for call in published())


@pytest.fixture
async def heating(hass: HomeAssistant, climate: SatClimate, boiler_data: Callable[..., Awaitable[None]], mqtt_mock: MagicMock, freezer: FrozenDateTimeFactory) -> SatClimate:
    """Return the climate in HEAT mode with live boiler data, no loop run pending and nothing published."""
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await advance(hass, freezer, 10)
    mqtt_mock.async_publish.reset_mock()

    return climate


@pytest.mark.parametrize(*ems_entry())
async def test_force_without_scheduled_run_only_schedules_in_ten_seconds(hass, entry, heating, published, freezer):
    # current behaviour, known bug: force=True does not run now unless a run is already pending
    heating.schedule_control_heating_loop(force=True)
    await hass.async_block_till_done()
    assert loop_runs(published) == 0

    await advance(hass, freezer, 9)
    assert loop_runs(published) == 0

    await advance(hass, freezer, 1)
    assert loop_runs(published) == 1


@pytest.mark.parametrize(*ems_entry())
async def test_force_with_scheduled_run_cancels_it_and_runs_now(hass, entry, heating, published, freezer):
    heating.schedule_control_heating_loop()

    heating.schedule_control_heating_loop(force=True)
    await hass.async_block_till_done()
    assert loop_runs(published) == 1

    # The cancelled run does not fire later
    await advance(hass, freezer, 15)
    assert loop_runs(published) == 1


@pytest.mark.parametrize(*ems_entry())
async def test_schedule_without_force_keeps_the_first_run(hass, entry, heating, published, freezer):
    heating.schedule_control_heating_loop()
    await advance(hass, freezer, 5)

    heating.schedule_control_heating_loop()
    await advance(hass, freezer, 5)

    # The second call did not push the run out to 15 seconds
    assert loop_runs(published) == 1

    # Nor did it add a second run
    await advance(hass, freezer, 5)
    assert loop_runs(published) == 1


@pytest.mark.parametrize(*ems_entry())
async def test_interval_schedules_the_run_on_the_event_loop(hass: HomeAssistant, heating: SatClimate, freezer: FrozenDateTimeFactory) -> None:
    threads: list[int] = []

    def spy(*args: Any, **kwargs: Any) -> CALLBACK_TYPE:
        threads.append(threading.get_ident())
        return async_call_later(*args, **kwargs)

    with patch("custom_components.sat.climate.async_call_later", spy):
        await advance(hass, freezer, 30)

    assert threads == [hass.loop_thread_id]


@pytest.mark.parametrize(*ems_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_tap_draw_with_pwm_publishes_no_burnmaxpower(entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE | {"burnmaxpower": 0, "heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # Before, at the start of, during and at the end of a tap draw, the read-back equals the PWM value
    for tap in ("off", "on", "on", "off"):
        freezer.tick(1)
        await boiler_data(**IDLE | {"burnmaxpower": 0, "heatingoff": "on", "tapwateractive": tap})
        mqtt_mock.async_publish.reset_mock()
        await climate.async_control_heating_loop()
        assert not any("burnmaxpower" in call[1] for call in published())


# No demand: a smoothed setpoint at or below COLD_SETPOINT (28.2) resets PWM, releases the setpoint and requests heater OFF in every modulation mode

@pytest.mark.parametrize(*with_data(WARM_MINIMUM_SETPOINT, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_a_cold_smoothed_setpoint_releases_with_the_heater_off_in_minimum_mode_with_a_warm_minimum(entry, climate, boiler_data, mqtt_mock, published, caplog):
    await boiler_data(**IDLE | {"heatingoff": "off"})

    # The minimum of 30 is irrelevant: there is no demand, so no setpoint is requested
    calls = await run_loop(climate, mqtt_mock, published, target=15.0)
    assert message('{"cmd": "heatingoff", "value": "on"}') in calls
    assert not [call for call in calls if "selflowtemp" in call[1]]
    assert climate.smoothed_setpoint is not None and climate.smoothed_setpoint <= 28.2
    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["want"].control_setpoint is RELEASE
    assert last_cycle(caplog)["want"].heater_state == DeviceState.OFF


@pytest.mark.parametrize(*with_data(WARM_MINIMUM_SETPOINT, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_a_cold_smoothed_setpoint_ends_a_running_minimum_pulse(entry, climate, boiler_data, mqtt_mock, published, caplog):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await run_loop(climate, mqtt_mock, published, target=21.0)
    assert climate.pwm.status == PWMStatus.ON
    assert last_cycle(caplog)["want"].control_setpoint == 30

    await climate.async_set_target_temperature(15.0)
    # The smoothed setpoint follows the lower target step by step
    for _ in range(20):
        await climate.async_control_heating_loop()
        if climate.smoothed_setpoint <= 28.2:
            break

    assert climate.smoothed_setpoint <= 28.2
    assert climate.pwm.status == PWMStatus.OFF
    # A reset, not the natural end of the pulse, which keeps the duty cycle
    assert climate.pwm.duty_cycle is None
    assert last_cycle(caplog)["want"].control_setpoint is RELEASE
    assert last_cycle(caplog)["want"].heater_state == DeviceState.OFF
