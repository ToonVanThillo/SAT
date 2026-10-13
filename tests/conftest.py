"""Fixtures for testing."""
import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from unittest.mock import patch

import pytest
from _pytest.logging import LogCaptureFixture
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import CLIMATE, CONF_MQTT_TOPIC, COORDINATOR, DOMAIN
from custom_components.sat.fake import SatFakeCoordinator
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.const import DEFAULT_USER_DATA


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def entry(hass: HomeAssistant, domains: list, data: dict, options: dict, config: dict, caplog: LogCaptureFixture) -> MockConfigEntry:
    """Setup any given integration."""
    for domain, count in domains:
        with assert_setup_component(count, domain):
            assert await async_setup_component(hass, domain, config)

        await hass.async_block_till_done()

    await hass.async_start()
    await hass.async_block_till_done()

    user_data = DEFAULT_USER_DATA.copy()
    user_data.update(data)

    config_entry = MockConfigEntry(domain=DOMAIN, data=user_data, options=options)
    await hass.config_entries.async_add(config_entry)

    return config_entry


@pytest.fixture
async def climate(hass, entry: MockConfigEntry) -> SatClimate:
    return hass.data[DOMAIN][entry.entry_id][CLIMATE]


@pytest.fixture
async def coordinator(hass, entry: MockConfigEntry) -> SatFakeCoordinator:
    return hass.data[DOMAIN][entry.entry_id][COORDINATOR]


@pytest.fixture
def no_publish_delay() -> Iterator[None]:
    """Replace asyncio.sleep globally with a zero-delay yield, so the 1s wait after an MQTT publish does not slow the tests."""
    real_sleep = asyncio.sleep

    with patch("custom_components.sat.mqtt.asyncio.sleep", new=lambda *_a, **_k: real_sleep(0)):
        yield


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Advance the frozen clock and fire the timers that are due."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
def published(mqtt_mock) -> Callable[[], list[tuple]]:
    """Return the (topic, payload, qos, retain) tuples published through the MQTT mock so far."""
    return lambda: [call.args for call in mqtt_mock.async_publish.call_args_list]


@pytest.fixture
async def base_components(hass: HomeAssistant) -> None:
    """Set up the platforms' base components first, so that all SAT entities get created."""
    for domain in ("climate", "binary_sensor", "sensor"):
        assert await async_setup_component(hass, domain, {})


@pytest.fixture
async def ems(hass: HomeAssistant, mqtt_mock, no_publish_delay) -> SatEmsMqttCoordinator:
    """Return a started EMS MQTT coordinator on topic `ems-esp`, with the startup publishes cleared."""
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"})
    await coordinator.async_added_to_hass()
    mqtt_mock.async_publish.reset_mock()

    return coordinator


@pytest.fixture
def boiler_data(hass: HomeAssistant) -> Callable[..., Awaitable[None]]:
    """Return a coroutine function that publishes an EMS `boiler_data` payload on topic `ems-esp`."""
    async def publish(**data) -> None:
        async_fire_mqtt_message(hass, "ems-esp/boiler_data", json.dumps(data))
        await hass.async_block_till_done()

    return publish


@pytest.fixture
async def loop_entry(mqtt_mock, no_publish_delay, freezer: FrozenDateTimeFactory, base_components: None, entry: MockConfigEntry) -> MockConfigEntry:
    """Opt-in: set up MQTT without publish delays, freeze time and set up the base components before the entry."""
    return entry


@pytest.fixture
def no_publish_error(caplog: LogCaptureFixture) -> Iterator[None]:
    """Opt-in: fail the test if an MQTT publish failed during its call phase."""
    yield
    # After the yield only teardown logs are in caplog.text, so inspect the call phase
    assert not [r for r in caplog.get_records("call") if "Failed to publish MQTT command" in r.getMessage()]
