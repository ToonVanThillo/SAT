"""Characterization tests for the heating loop, end-to-end from the climate entity to the EMS MQTT messages."""
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
)
from tests.conftest import advance
from tests.const import ems_entry

pytestmark = pytest.mark.usefixtures("loop_entry")

BOILER = "ems-esp/boiler"
IDLE = {"selflowtemp": 40, "curflowtemp": 40.5, "rettemp": 35, "curburnpow": 0, "burnmaxpower": 31, "heatingactive": "off", "burngas": "off", "tapactivated": "off"}


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
    await boiler_data(**IDLE)

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 35.0}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
        message('{"cmd": "heatingoff", "value": "off"}'),
    ]


@pytest.mark.parametrize(*ems_entry())
async def test_heater_already_active_is_not_started_again(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingactive": "on", "burnmaxpower": 100})

    assert await run_loop(climate, mqtt_mock, published) == [message('{"cmd": "selflowtemp", "value": 35.0}')]


@pytest.mark.parametrize(*ems_entry())
async def test_cold_setpoint_sends_zero_and_stops_heater(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingactive": "on"})

    # EMS quirk: the absolute minimum setpoint of 10 is sent as 0
    assert await run_loop(climate, mqtt_mock, published, target=15.0) == [
        message('{"cmd": "selflowtemp", "value": 0}'),
        message('{"cmd": "burnmaxpower", "value": 0}'),
        message('{"cmd": "heatingoff", "value": "on"}'),
    ]
    assert climate.setpoint == 10


@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_publishes_nothing_in_the_loop(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingactive": "on"})

    # Switching off stops the heater once, the loop itself does nothing afterwards
    await climate.async_set_hvac_mode(HVACMode.OFF)
    assert published()[-1] == message('{"cmd": "heatingoff", "value": "on"}')

    assert await run_loop(climate, mqtt_mock, published, mode=HVACMode.OFF) == []


@pytest.mark.parametrize(*ems_entry())
async def test_hvac_off_with_inactive_heater_publishes_nothing(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)
    mqtt_mock.async_publish.reset_mock()

    await climate.async_set_hvac_mode(HVACMode.OFF)

    assert published() == []


@pytest.mark.parametrize(*ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_hot_water_keeps_modulation_at_maximum_with_pwm(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"tapactivated": "on"})

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 20.0}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
    ]


@pytest.mark.parametrize(*ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_pwm_on_sends_minimum_setpoint_and_zero_modulation_once(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 20.0}'),
        message('{"cmd": "burnmaxpower", "value": 0}'),
    ]

    # The read-back equals the requested 0, so only selflowtemp is sent again (on every loop by design, which loop_runs relies on)
    await boiler_data(burnmaxpower=0)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published() == [message('{"cmd": "selflowtemp", "value": 20.0}')]


@pytest.mark.parametrize(*ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 31}))
async def test_pwm_on_with_floor_option_resends_floor_modulation_every_loop(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 20.0}'),
        message('{"cmd": "burnmaxpower", "value": 31}'),
    ]

    # current behaviour, known bug: the read-back floor of 31 never equals the requested 0, so it is re-sent on every loop
    await boiler_data(burnmaxpower=31)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published() == [
        message('{"cmd": "selflowtemp", "value": 20.0}'),
        message('{"cmd": "burnmaxpower", "value": 31}'),
    ]


@pytest.mark.parametrize(*ems_entry())
async def test_loop_without_boiler_data_still_publishes(entry, climate, mqtt_mock, published):
    # Without boiler data the loop runs from the sensors alone
    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 35.0}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
        message('{"cmd": "heatingoff", "value": "off"}'),
    ]


# schedule_control_heating_loop

def loop_runs(published: Callable[[], list[tuple]]) -> int:
    """Count the heating loop runs, each of which publishes one selflowtemp message."""
    return sum('"selflowtemp"' in call[1] for call in published())


@pytest.fixture
async def heating(hass: HomeAssistant, climate: SatClimate, mqtt_mock: MagicMock, freezer: FrozenDateTimeFactory) -> SatClimate:
    """Return the climate in HEAT mode with no loop run pending and nothing published."""
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
