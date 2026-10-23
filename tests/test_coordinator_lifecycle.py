"""Coordinator start and teardown.

A coordinator starts once, a teardown is final, and a torn-down coordinator receives no device data.
"""
import asyncio
from collections.abc import Iterator
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.sat.const import CONF_MQTT_TOPIC
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.mqtt.opentherm import SatOpenThermMqttCoordinator
from custom_components.sat.serial import SatSerialCoordinator
from tests.conftest import advance, listen, otgw_flame_on, pm_commands
from tests.const import STORE_KEY


def ems_coordinator(hass: HomeAssistant) -> SatEmsMqttCoordinator:
    return SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, config_entry=None)


def otgw_coordinator(hass: HomeAssistant) -> SatOpenThermMqttCoordinator:
    return SatOpenThermMqttCoordinator(hass, "gw1", {CONF_MQTT_TOPIC: "otgw"}, config_entry=None)


@pytest.fixture
def mqtt_client_wait() -> Iterator[asyncio.Event]:
    """Suspend the coordinators' wait for the MQTT client until the returned event is set."""
    released = asyncio.Event()

    async def wait(_hass: HomeAssistant) -> bool:
        await released.wait()
        return True

    with patch.object(mqtt, "async_wait_for_mqtt_client", wait):
        yield released


# Starting

@pytest.mark.parametrize("topics", ["tracked", "none"])
async def test_starting_twice_subscribes_and_boots_once(hass, mqtt_mock, no_publish_delay, published, topics: str):
    coordinator = otgw_coordinator(hass)

    # Without tracked topics the coordinator holds no subscription that could tell it was started
    with patch.object(coordinator, "get_tracked_entities", return_value=[]) if topics == "none" else nullcontext():
        await coordinator.async_added_to_hass()
        await coordinator.async_added_to_hass()

    assert pm_commands(published) == ["PM=3", "PM=15", "PM=48"]

    with patch.object(coordinator, "async_set_updated_data", wraps=coordinator.async_set_updated_data) as handled:
        await otgw_flame_on(hass)

    assert handled.call_count == (1 if topics == "tracked" else 0)


async def test_a_second_start_during_the_mqtt_client_wait_subscribes_once(hass, mqtt_mock, mqtt_client_wait, boiler_data):
    coordinator = ems_coordinator(hass)

    with patch.object(mqtt, "async_subscribe", wraps=mqtt.async_subscribe) as subscribe:
        starts = [hass.async_create_task(coordinator.async_added_to_hass()) for _ in range(2)]
        await asyncio.sleep(0)
        mqtt_client_wait.set()
        await asyncio.gather(*starts)

    # EMS tracks one topic
    subscribe.assert_called_once()

    with patch.object(coordinator, "async_set_updated_data", wraps=coordinator.async_set_updated_data) as handled:
        await boiler_data(selflowtemp=55)

    handled.assert_called_once()


async def test_a_failed_start_is_logged_and_not_retried(hass, mqtt_mock, no_publish_delay, published, caplog):
    coordinator = otgw_coordinator(hass)

    with patch.object(mqtt, "async_wait_for_mqtt_client", AsyncMock(return_value=False)):
        await coordinator.async_added_to_hass()
        await coordinator.async_added_to_hass()

    failures = [r for r in caplog.records if r.getMessage().startswith("Failed to start the")]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert published() == []

    await otgw_flame_on(hass)
    assert coordinator.flame_active is False


# Teardown is final

async def test_a_torn_down_ems_coordinator_ignores_device_data(ems, boiler_data):
    await ems.async_will_remove_from_hass()

    await boiler_data(burngas="on")

    assert ems.flame_active is False


async def test_a_torn_down_opentherm_coordinator_ignores_device_data(hass, otgw):
    await otgw.async_will_remove_from_hass()

    await otgw_flame_on(hass)

    assert otgw.flame_active is False


async def test_a_torn_down_coordinator_ignores_data_and_arms_no_notification(hass, ems, freezer):
    notified = listen(ems)
    await ems.async_will_remove_from_hass()

    # As serial data queued before the teardown would arrive
    ems.async_set_updated_data({"burngas": "on"})
    await advance(hass, freezer, 60)

    assert dict(ems.data) == {}
    notified.assert_not_called()


async def test_a_torn_down_coordinator_no_longer_updates_the_flame_on_notify(hass, ems):
    ems.async_set_updated_data({"burngas": "on"})
    await ems.async_will_remove_from_hass()

    ems.async_update_listeners()

    assert ems.flame_active is True
    assert ems.flame.is_active is False


async def test_a_double_teardown_is_final(hass, otgw, published):
    await otgw.async_will_remove_from_hass()
    await otgw.async_will_remove_from_hass()

    await otgw.async_added_to_hass()
    await otgw_flame_on(hass)

    assert pm_commands(published) == []
    assert otgw.flame_active is False


async def test_stopping_after_a_double_start_releases_every_subscription(hass, ems, boiler_data):
    await ems.async_added_to_hass()
    await ems.async_will_remove_from_hass()

    await boiler_data(burngas="on")

    assert ems.flame_active is False


# Teardown during the start

async def test_a_teardown_during_the_mqtt_client_wait_leaves_nothing_subscribed(hass, mqtt_mock, mqtt_client_wait, boiler_data):
    coordinator = ems_coordinator(hass)

    with patch.object(mqtt, "async_subscribe", wraps=mqtt.async_subscribe) as subscribe:
        start = hass.async_create_task(coordinator.async_added_to_hass())
        await asyncio.sleep(0)
        await coordinator.async_will_remove_from_hass()
        mqtt_client_wait.set()
        await start

    subscribe.assert_not_called()
    await boiler_data(burngas="on")
    assert coordinator.flame_active is False


async def test_a_teardown_during_the_mqtt_client_wait_writes_nothing(hass, mqtt_mock, mqtt_client_wait, no_publish_delay, published):
    coordinator = otgw_coordinator(hass)

    start = hass.async_create_task(coordinator.async_added_to_hass())
    await asyncio.sleep(0)
    await coordinator.async_will_remove_from_hass()
    mqtt_client_wait.set()
    await start

    # Neither the boot nor the maximum setpoint
    assert published() == []


async def test_a_start_released_while_the_teardown_saves_subscribes_nothing(hass, mqtt_mock, mqtt_client_wait):
    coordinator = ems_coordinator(hass)
    coordinator.async_set_updated_data({"selflowtemp": 55})
    real_save = Store.async_save

    async def save(store: Store, data: dict) -> None:
        mqtt_client_wait.set()
        for _ in range(10):
            await asyncio.sleep(0)
        await real_save(store, data)

    with patch.object(mqtt, "async_subscribe", wraps=mqtt.async_subscribe) as subscribe, patch.object(Store, "async_save", save):
        start = hass.async_create_task(coordinator.async_added_to_hass())
        await asyncio.sleep(0)
        await coordinator.async_will_remove_from_hass()
        await start

    subscribe.assert_not_called()


# Teardown save

async def test_teardown_is_safe_twice_and_saves_the_data(ems, boiler_data, hass_storage):
    await boiler_data(selflowtemp=55)

    await ems.async_will_remove_from_hass()
    await ems.async_will_remove_from_hass()
    await boiler_data(burngas="on")

    assert ems.flame_active is False
    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 55}


async def test_the_teardown_unsubscribes_before_it_saves(hass, ems, boiler_data):
    await boiler_data(selflowtemp=55)
    order: list[str] = []
    unsubscribe = ems._subscriptions[0]

    def unsubscribed() -> None:
        order.append("unsubscribe")
        unsubscribe()

    ems._subscriptions[0] = unsubscribed
    real_save = Store.async_save

    async def save(store: Store, data: dict) -> None:
        order.append("save")
        await real_save(store, data)

    with patch.object(Store, "async_save", save):
        await ems.async_will_remove_from_hass()

    assert order == ["unsubscribe", "save"]


async def test_a_message_arriving_during_the_teardown_save_is_not_handled(hass, ems, boiler_data, hass_storage):
    await boiler_data(selflowtemp=55)
    real_save = Store.async_save

    async def save(store: Store, data: dict) -> None:
        await boiler_data(burngas="on")
        await real_save(store, data)

    with patch.object(Store, "async_save", save):
        await ems.async_will_remove_from_hass()

    assert ems.flame_active is False
    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 55}


async def test_teardown_without_data_keeps_the_stored_data(hass, mqtt_mock, hass_storage):
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"selflowtemp": 50}}
    coordinator = ems_coordinator(hass)

    # Never set up nor started, and torn down twice
    await coordinator.async_will_remove_from_hass()
    await coordinator.async_will_remove_from_hass()

    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 50}


# Pending notification

async def test_teardown_cancels_a_pending_listener_notification(hass, ems, boiler_data, freezer):
    notified = listen(ems)
    await boiler_data(selflowtemp=55)

    await ems.async_will_remove_from_hass()
    await advance(hass, freezer, 60)

    notified.assert_not_called()


async def test_serial_teardown_cancels_a_pending_listener_notification(hass, freezer: FrozenDateTimeFactory):
    with patch("custom_components.sat.serial.OpenThermGateway", return_value=AsyncMock(subscribe=MagicMock(), unsubscribe=MagicMock())):
        # The coordinator stores the gateway defaults on creation, which schedules a notification
        coordinator = SatSerialCoordinator(hass, "/dev/ttyUSB0", {}, config_entry=None)

    notified = listen(coordinator)

    await coordinator.async_will_remove_from_hass()
    await advance(hass, freezer, 60)

    notified.assert_not_called()


async def test_serial_teardown_unsubscribes_and_disconnects_the_gateway_once(hass):
    gateway = AsyncMock(subscribe=MagicMock(), unsubscribe=MagicMock())

    with patch("custom_components.sat.serial.OpenThermGateway", return_value=gateway):
        coordinator = SatSerialCoordinator(hass, "/dev/ttyUSB0", {}, config_entry=None)

    await coordinator.async_will_remove_from_hass()
    await coordinator.async_will_remove_from_hass()

    gateway.unsubscribe.assert_called_once()
    assert gateway.disconnect.await_count >= 1
