"""Config entry lifecycle.

Reload and unload, one receiving coordinator per entry, failed setup and retry, and climate renames.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat import async_setup_entry
from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CLIMATE,
    CONF_DEVICE,
    CONF_MODE,
    CONF_MQTT_TOPIC,
    CONF_ROOMS,
    DOMAIN,
    MODE_MQTT_EMS,
    MODE_SERIAL,
)
from custom_components.sat.coordinator import SatDataUpdateCoordinatorFactory
from custom_components.sat.fake import SatFakeCoordinator
from tests.conftest import (
    advance,
    current,
    flame_transitions,
    listen,
    otgw_flame_on,
    pm_commands,
)
from tests.const import (
    DEFAULT_USER_DATA,
    IDLE,
    STORE_KEY,
    ems_entry,
    otgw_entry,
    with_data,
)

SERIAL_DATA = DEFAULT_USER_DATA | {CONF_MODE: MODE_SERIAL, CONF_DEVICE: "/dev/ttyUSB0"}


# Reload and unload

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_after_a_reload_only_the_new_ems_coordinator_receives_device_data(hass, entry, coordinator, boiler_data):
    await hass.config_entries.async_reload(entry.entry_id)
    new = current(hass, entry)

    await boiler_data(**IDLE | {"burngas": "on"})

    assert new is not coordinator
    assert new.flame_active is True
    assert coordinator.flame_active is False


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_after_a_reload_only_the_new_opentherm_coordinator_receives_device_data(hass, entry, coordinator):
    await hass.config_entries.async_reload(entry.entry_id)
    new = current(hass, entry)

    await otgw_flame_on(hass)

    assert new.flame_active is True
    assert coordinator.flame_active is False


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_two_reloads_in_a_row_leave_one_receiving_coordinator(hass, entry, coordinator, boiler_data, freezer, caplog):
    await hass.config_entries.async_reload(entry.entry_id)
    first = current(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)
    last = current(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sat.flame")
    caplog.clear()

    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 10)

    assert last.flame_active is True
    assert first.flame_active is False
    assert coordinator.flame_active is False
    assert flame_transitions(caplog) == ["Flame transition OFF->ON"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_unload_then_setup_again_subscribes_once(hass, entry, coordinator, boiler_data, freezer, caplog):
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    caplog.set_level(logging.DEBUG, logger="custom_components.sat.flame")
    caplog.clear()

    await boiler_data(**IDLE | {"burngas": "on"})
    await advance(hass, freezer, 10)

    assert current(hass, entry).flame_active is True
    assert coordinator.flame_active is False
    assert flame_transitions(caplog) == ["Flame transition OFF->ON"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_unload_removes_the_platforms_before_the_coordinator_is_torn_down(hass, entry, coordinator, climate):
    calls: list[str] = []

    def recording(name: str, method: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        async def wrapper() -> None:
            calls.append(name)
            await method()

        return wrapper

    with (
        patch.object(climate, "async_will_remove_from_hass", recording("climate", climate.async_will_remove_from_hass)),
        patch.object(coordinator, "async_will_remove_from_hass", recording("coordinator", coordinator.async_will_remove_from_hass)),
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert calls == ["climate", "coordinator"]
    assert entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_failed_platform_unload_leaves_the_coordinator_running(hass, entry, coordinator, boiler_data):
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert not await hass.config_entries.async_unload(entry.entry_id)

    await boiler_data(**IDLE | {"burngas": "on"})

    assert current(hass, entry) is coordinator
    assert coordinator.flame_active is True


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_unload_saves_the_device_data(hass, entry, coordinator, boiler_data, hass_storage):
    await boiler_data(**IDLE | {"selflowtemp": 44})
    hass_storage.clear()

    assert await hass.config_entries.async_unload(entry.entry_id)

    saved = [v["data"] for v in hass_storage.values()]
    assert any(d.get("selflowtemp") == 44 for d in saved)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_unload_during_a_pending_notification_never_fires_it(hass, entry, coordinator, boiler_data, freezer):
    notified = listen(coordinator)
    await boiler_data(**IDLE | {"selflowtemp": 51})
    assert await hass.config_entries.async_unload(entry.entry_id)

    await advance(hass, freezer, 60)

    assert entry.state is ConfigEntryState.NOT_LOADED
    notified.assert_not_called()


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_a_pending_control_loop_does_not_run_after_unload(hass, entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE)
    climate.schedule_control_heating_loop()

    assert await hass.config_entries.async_unload(entry.entry_id)
    mqtt_mock.async_publish.reset_mock()
    await advance(hass, freezer, 60)

    assert published() == []


async def test_an_entry_unloaded_before_home_assistant_started_runs_no_control_loop(hass, mqtt_mock, base_components):
    hass.set_state(CoreState.not_running)
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)

    # dhcp would open sockets once HA has started
    with (
        patch("homeassistant.components.dhcp.async_setup", AsyncMock(return_value=True)),
        patch.object(SatClimate, "async_control_heating_loop", autospec=True) as loop,
    ):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)

        await hass.async_start()
        await hass.async_block_till_done()

    loop.assert_not_called()


# Failed setup

def failing_at(hass: HomeAssistant, point: str, error: BaseException) -> AbstractContextManager[Any]:
    """Make the setup fail with `error` at `point`: the coordinator's setup or the platform forwarding."""
    if point == "coordinator setup":
        return patch.object(SatFakeCoordinator, "async_setup", AsyncMock(side_effect=error))

    return patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=error))


FAILURE_POINTS = ["coordinator setup", "platform forwarding"]


@pytest.mark.parametrize("point", FAILURE_POINTS)
async def test_a_failed_setup_tears_down_the_coordinator_and_propagates(hass, mqtt_mock, caplog, point: str):
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)

    with (
        failing_at(hass, point, RuntimeError("boom")),
        patch.object(SatFakeCoordinator, "async_will_remove_from_hass", autospec=True) as teardown,
    ):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.entry_id not in hass.data[DOMAIN]
    teardown.assert_called_once()
    assert any(r.exc_info is not None and str(r.exc_info[1]) == "boom" for r in caplog.records)


@pytest.mark.parametrize("point", FAILURE_POINTS)
async def test_a_failing_teardown_after_a_failed_setup_keeps_the_original_error(hass, mqtt_mock, caplog, point: str):
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)

    with (
        failing_at(hass, point, RuntimeError("boom")),
        patch.object(SatFakeCoordinator, "async_will_remove_from_hass", AsyncMock(side_effect=ValueError("teardown"))),
    ):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.entry_id not in hass.data[DOMAIN]
    assert "Error tearing down the coordinator after a failed setup" in caplog.messages
    assert any(r.exc_info is not None and str(r.exc_info[1]) == "teardown" for r in caplog.records)
    assert any(r.exc_info is not None and str(r.exc_info[1]) == "boom" for r in caplog.records)


@pytest.mark.parametrize("point", FAILURE_POINTS)
async def test_a_cancelled_setup_tears_down_the_coordinator(hass, mqtt_mock, point: str):
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)
    entry.add_to_hass(hass)

    with (
        failing_at(hass, point, asyncio.CancelledError()),
        patch.object(SatFakeCoordinator, "async_will_remove_from_hass", autospec=True) as teardown,
        pytest.raises(asyncio.CancelledError),
    ):
        await async_setup_entry(hass, entry)

    teardown.assert_called_once()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_a_failing_coordinator_constructor_leaves_no_entry_data(hass, mqtt_mock):
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)

    with patch.object(SatDataUpdateCoordinatorFactory, "resolve", side_effect=RuntimeError("boom")):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


@pytest.mark.usefixtures("mqtt_mock")
async def test_a_failed_platform_forward_propagates_and_a_retry_works(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA)
    original = hass.config_entries.async_forward_entry_setups

    with patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("boom"))):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.entry_id not in hass.data[DOMAIN]

    with patch.object(hass.config_entries, "async_forward_entry_setups", original):
        assert await hass.config_entries.async_reload(entry.entry_id)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]


async def test_a_failed_ems_setup_keeps_the_stored_data(hass, mqtt_mock, hass_storage):
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"selflowtemp": 50}}
    entry = MockConfigEntry(domain=DOMAIN, data=DEFAULT_USER_DATA | {CONF_MODE: MODE_MQTT_EMS, CONF_DEVICE: "ems", CONF_MQTT_TOPIC: "ems-esp"})

    with patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("boom"))):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 50}


# Serial retry

def serial_gateway(connect_failures: int) -> MagicMock:
    gateway = MagicMock(subscribe=MagicMock(), unsubscribe=MagicMock())
    gateway.connect = AsyncMock(side_effect=[ConnectionError] * connect_failures + [None] * 5)
    gateway.disconnect = AsyncMock()
    gateway.set_control_setpoint = AsyncMock()
    gateway.set_max_relative_mod = AsyncMock()
    return gateway


async def test_a_failed_serial_setup_releases_the_gateway(hass, mqtt_mock):
    gateway = serial_gateway(connect_failures=1)
    entry = MockConfigEntry(domain=DOMAIN, data=SERIAL_DATA)

    with patch("custom_components.sat.serial.OpenThermGateway", return_value=gateway):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.entry_id not in hass.data[DOMAIN]
    gateway.unsubscribe.assert_called_once()
    gateway.disconnect.assert_awaited_once()

    # Cancel the retry
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("mqtt_mock")
async def test_repeated_serial_retries_do_not_accumulate_live_subscriptions(hass: HomeAssistant):
    gateway = serial_gateway(connect_failures=100)
    entry = MockConfigEntry(domain=DOMAIN, data=SERIAL_DATA)

    with patch("custom_components.sat.serial.OpenThermGateway", return_value=gateway):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()
        for _ in range(3):
            assert not await hass.config_entries.async_reload(entry.entry_id)

        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert gateway.connect.await_count >= 2
        assert gateway.subscribe.call_count == gateway.unsubscribe.call_count
        assert gateway.disconnect.await_count == gateway.connect.await_count
        assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]

        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("mqtt_mock", "base_components")
async def test_a_serial_retry_after_a_failed_connect_loads_with_one_live_subscription(hass: HomeAssistant):
    gateway = serial_gateway(connect_failures=1)
    entry = MockConfigEntry(domain=DOMAIN, data=SERIAL_DATA)

    with patch("custom_components.sat.serial.OpenThermGateway", return_value=gateway):
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY

        assert await hass.config_entries.async_reload(entry.entry_id)

        assert entry.state is ConfigEntryState.LOADED
        assert entry.entry_id in hass.data[DOMAIN]
        assert gateway.subscribe.call_count - gateway.unsubscribe.call_count == 1


# Climate renames (HA removes and re-adds an entity whose entity ID changes)

async def rename_climate(hass: HomeAssistant, climate: SatClimate) -> None:
    er.async_get(hass).async_update_entity(climate.entity_id, new_entity_id="climate.renamed")
    await hass.async_block_till_done()
    assert hass.states.get("climate.renamed") is not None


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_renaming_the_climate_neither_subscribes_nor_boots_again(hass, entry, coordinator, climate, mqtt_mock, published):
    mqtt_mock.async_publish.reset_mock()

    await rename_climate(hass, climate)

    with patch.object(coordinator, "async_set_updated_data", wraps=coordinator.async_set_updated_data) as handled:
        await otgw_flame_on(hass)

    handled.assert_called_once()
    assert pm_commands(published) == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_after_a_rename_and_a_reload_only_the_new_coordinator_receives_data(hass, entry, coordinator, climate, boiler_data):
    await rename_climate(hass, climate)

    await hass.config_entries.async_reload(entry.entry_id)
    new = current(hass, entry)

    await boiler_data(**IDLE | {"burngas": "on"})

    assert new is not coordinator
    assert new.flame_active is True
    assert coordinator.flame_active is False
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry())
async def test_a_rename_after_a_reload_acts_only_on_the_new_coordinator(hass, entry, coordinator, mqtt_mock, published):
    await hass.config_entries.async_reload(entry.entry_id)
    new = current(hass, entry)
    mqtt_mock.async_publish.reset_mock()

    await rename_climate(hass, hass.data[DOMAIN][entry.entry_id][CLIMATE])

    with (
        patch.object(new, "async_set_updated_data", wraps=new.async_set_updated_data) as handled,
        patch.object(coordinator, "async_set_updated_data", wraps=coordinator.async_set_updated_data) as stale,
    ):
        await otgw_flame_on(hass)

    handled.assert_called_once()
    stale.assert_not_called()
    assert new.flame_active is True
    assert coordinator.flame_active is False
    assert pm_commands(published) == []
    assert current(hass, entry) is new


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_after_a_rename_an_unload_releases_the_coordinator(hass, entry, coordinator, climate, boiler_data):
    await rename_climate(hass, climate)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await boiler_data(**IDLE | {"burngas": "on"})

    assert coordinator.flame_active is False


ROOM = "climate.room"
ROOM_SENSOR = "sensor.room_temperature"


@pytest.fixture
def room(hass: HomeAssistant) -> None:
    hass.states.async_set(ROOM, "heat", {"temperature": 20, "current_temperature": 19, "sensor_temperature_id": ROOM_SENSOR})
    hass.states.async_set(ROOM_SENSOR, "19")


async def room_sensor_changes_are_seen(hass: HomeAssistant, caplog: pytest.LogCaptureFixture, value: str) -> bool:
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="custom_components.sat.climate"):
        hass.states.async_set(ROOM_SENSOR, value)
        await hass.async_block_till_done()

    return f"Climate Sensor Changed ({ROOM_SENSOR})." in caplog.messages


@pytest.mark.usefixtures("room", "loop_entry")
@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_a_room_temperature_sensor_is_tracked(hass, entry, climate, caplog):
    assert await room_sensor_changes_are_seen(hass, caplog, "19.5")


@pytest.mark.xfail(strict=True, reason="docs/known-issues.md: room temperature sensors are not tracked after the climate is renamed")
@pytest.mark.usefixtures("room", "loop_entry")
@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_a_room_temperature_sensor_is_tracked_after_a_rename(hass, entry, climate, caplog):
    await rename_climate(hass, climate)

    assert await room_sensor_changes_are_seen(hass, caplog, "19.5")
