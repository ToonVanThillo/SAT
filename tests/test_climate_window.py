"""The climate's window handling.

An open window switches the preset after the minimum open time, and nothing of it outlives the entity.

Without `loop_entry`: its `no_publish_delay` makes every asyncio.sleep immediate, also the window's
minimum open time. Without boiler data the channels don't write, so nothing waits for a publish.
"""
import asyncio
import logging
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from custom_components.sat.const import CONF_WINDOW_SENSORS
from tests.conftest import advance, settle, tick
from tests.const import ems_entry

WINDOW = "binary_sensor.window"
# SAT's window group sensor, which the climate follows
WINDOW_GROUP = "binary_sensor.sat_test_window_sensor"
MINIMUM_OPEN_TIME = 15
WINDOW_CLOSED = "Window closed before minimum open time."

pytestmark = [
    pytest.mark.usefixtures("window_sensor_registered", "mqtt_mock", "base_components"),
    pytest.mark.parametrize(*ems_entry({CONF_WINDOW_SENSORS: [WINDOW]})),
]


async def set_window(hass: HomeAssistant, state: str) -> None:
    """Set the window and let SAT's window group sensor and the climate follow."""
    hass.states.async_set(WINDOW, state)
    await settle()


async def test_a_window_open_for_the_minimum_time_switches_the_preset(hass, freezer, entry, climate):
    preset = climate.preset_mode

    await set_window(hass, STATE_ON)
    await advance(hass, freezer, 20)

    assert climate.preset_mode != preset


async def test_a_newer_window_open_event_restarts_the_minimum_open_time(hass, freezer, entry, climate):
    preset = climate.preset_mode

    await set_window(hass, STATE_ON)
    await tick(hass, freezer, 10)
    await set_window(hass, STATE_UNAVAILABLE)
    await set_window(hass, STATE_ON)

    # The first wait would have ended by now, the second has not
    await tick(hass, freezer, 7)
    assert climate.preset_mode == preset

    with patch.object(climate, "async_set_preset_mode", wraps=climate.async_set_preset_mode) as switched:
        await tick(hass, freezer, 10)

    switched.assert_called_once()
    assert climate.preset_mode != preset


async def test_a_window_closed_before_the_minimum_time_logs_once_and_changes_nothing(hass, freezer, entry, climate, caplog):
    preset = climate.preset_mode

    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.climate"):
        for _ in range(3):
            await set_window(hass, STATE_ON)
            await set_window(hass, STATE_UNAVAILABLE)
        await set_window(hass, STATE_ON)
        await set_window(hass, STATE_OFF)

    assert caplog.messages.count(WINDOW_CLOSED) == 1

    with patch.object(climate, "async_set_preset_mode", wraps=climate.async_set_preset_mode) as switched:
        await advance(hass, freezer, 80)

    switched.assert_not_called()
    assert climate.preset_mode == preset


async def test_a_window_closed_as_the_wait_ends_does_not_switch_the_preset(hass, entry, climate):
    preset = climate.preset_mode
    real_sleep = asyncio.sleep

    async def wait_ending_with_the_window_closed(seconds: float) -> None:
        if seconds != MINIMUM_OPEN_TIME:
            await real_sleep(seconds)
            return

        # The climate handles the closing after this wait has ended, but before it resumes from it
        hass.states.async_set(WINDOW_GROUP, STATE_OFF)

    with patch.object(asyncio, "sleep", wait_ending_with_the_window_closed):
        await set_window(hass, STATE_ON)
        await settle()

    assert hass.states.get(WINDOW_GROUP).state == STATE_OFF
    assert climate.preset_mode == preset


async def test_a_window_opened_just_before_unload_changes_nothing_after_it(hass, freezer, entry, climate, caplog):
    preset = climate.preset_mode

    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.climate"):
        await set_window(hass, STATE_ON)
    assert "Window Sensor Changed to on." in caplog.messages

    assert await hass.config_entries.async_unload(entry.entry_id)

    with patch.object(climate, "async_control_heating_loop") as loop:
        await advance(hass, freezer, 80)

    loop.assert_not_called()
    assert climate.preset_mode == preset


async def test_repeated_window_open_events_before_unload_change_nothing_afterwards(hass, freezer, entry, climate, mqtt_mock, caplog):
    preset = climate.preset_mode

    # The window sensor going unavailable doesn't stop a wait; reopening starts a new one
    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.climate"):
        for _ in range(3):
            await set_window(hass, STATE_ON)
            await set_window(hass, STATE_UNAVAILABLE)
        await set_window(hass, STATE_ON)
        assert caplog.messages.count("Window Sensor Changed to on.") == 4

        assert await hass.config_entries.async_unload(entry.entry_id)
        mqtt_mock.async_publish.reset_mock()

        with patch.object(climate, "async_control_heating_loop") as loop:
            await advance(hass, freezer, 80)

    loop.assert_not_called()
    mqtt_mock.async_publish.assert_not_called()
    assert climate.preset_mode == preset
    assert WINDOW_CLOSED not in caplog.messages


async def test_cancelling_the_window_handler_task_cancels_the_task_and_not_the_climate(hass, freezer, entry, climate):
    preset = climate.preset_mode
    await set_window(hass, STATE_ON)

    handlers = [t for t in asyncio.all_tasks() if "_async_window_sensor_changed" in repr(t.get_coro())]
    if not handlers:
        pytest.skip("the window handler task is not reachable through the public task list")

    for task in handlers:
        task.cancel()
    await settle()

    assert all(t.cancelled() for t in handlers)

    # The climate still works: a later open window switches the preset
    await set_window(hass, STATE_UNAVAILABLE)
    await set_window(hass, STATE_ON)
    await advance(hass, freezer, 20)
    assert climate.preset_mode != preset
