import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import climate, mqtt
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.config_flow import SatFlowHandler, SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_DEVICE,
    CONF_HEATING_SYSTEM,
    CONF_INSIDE_SENSOR_ENTITY_ID,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MODE,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_OUTSIDE_SENSOR_ENTITY_ID,
    CONF_RADIATORS,
    CONF_ROOMS,
    DOMAIN,
    HEATING_SYSTEM_RADIATORS,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
)
from custom_components.sat.overshoot_protection import OvershootProtection
from tests.conftest import current
from tests.const import IDLE, STORE_KEY, ems_entry


def options_flow(mode: str) -> SatOptionsFlowHandler:
    return SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: mode, CONF_NAME: "Test"}))


def selector_max(result: dict, key: str) -> float:
    return next(value for marker, value in result["data_schema"].schema.items() if marker == key).config["max"]


async def test_create_coordinator(hass):
    flow_handler = SatFlowHandler()
    flow_handler.data = {
        "name": "Test",
        "mode": MODE_FAKE,
        "device": "test_device",
    }

    assert (await flow_handler.async_create_coordinator()).config_entry is None


async def test_create_coordinator_in_reconfigure_has_no_config_entry(hass):
    config_entry = MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_FAKE, CONF_NAME: "Test", CONF_DEVICE: "test_device"})
    flow_handler = SatFlowHandler()
    flow_handler.config_entry = config_entry
    flow_handler.data = dict(config_entry.data)

    assert (await flow_handler.async_create_coordinator()).config_entry is None


@pytest.mark.parametrize("mode, shown", [(MODE_MQTT_EMS, True), (MODE_MQTT_OPENTHERM, False)])
async def test_advanced_shows_modulation_floor_only_for_ems(hass, mode, shown):
    flow = options_flow(mode)

    assert (CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR in (await flow.async_step_advanced())["data_schema"].schema) is shown
    assert CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR not in (await flow.async_step_system_configuration())["data_schema"].schema


async def test_modulation_floor_allows_up_to_254(hass):
    result = await options_flow(MODE_MQTT_EMS).async_step_advanced()

    assert selector_max(result, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR) == 254


@pytest.mark.parametrize("mode, maximum", [(MODE_MQTT_EMS, 254), (MODE_MQTT_OPENTHERM, 100)])
async def test_maximum_relative_modulation_allows_above_100_only_for_ems(hass, mode, maximum):
    result = await options_flow(mode).async_step_advanced()

    assert selector_max(result, CONF_MAXIMUM_RELATIVE_MODULATION) == maximum


@pytest.mark.parametrize("language", ["en", "de", "es", "fr", "it", "nl", "pt", "sk"])
async def test_translations_modulation_floor_in_advanced(language):
    steps = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json").read_text(encoding="utf-8"))["options"]["step"]

    for section in ("data", "data_description"):
        assert "maximum_relative_modulation_floor" in steps["advanced"][section]
        assert "maximum_relative_modulation_floor" not in steps["system_configuration"].get(section, {})


# Calibration step

PREVIOUS = "heat"
SAT = "climate.test"
VALVES = [("climate.radiator", "heat"), ("climate.room", "heat")]


@pytest.fixture
def events(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Record `climate.set_hvac_mode` calls as (entity, mode), plus calibration events."""
    calls: list[tuple[str, str]] = []

    async def record(call: ServiceCall) -> None:
        calls.append((call.data["entity_id"], call.data["hvac_mode"]))

    hass.services.async_register(climate.DOMAIN, climate.SERVICE_SET_HVAC_MODE, record)
    entry = entity_registry.async_get(hass).async_get_or_create(climate.DOMAIN, DOMAIN, "test", suggested_object_id="test")
    hass.states.async_set(entry.entity_id, PREVIOUS)
    return calls


@pytest.fixture
def flow_id(hass: HomeAssistant, events: list[tuple[str, str]]) -> Iterator[Callable[[], Awaitable[str]]]:
    """Yield a coroutine function that opens a flow at the calibrate menu, with a fake coordinator patched in."""
    coordinator = MagicMock(maximum_setpoint_value=60, supports_setpoint_management=True)
    coordinator.async_setup = AsyncMock()
    coordinator.async_will_remove_from_hass = AsyncMock(side_effect=lambda: events.append(("coordinator", "removed")))

    async def open_flow() -> str:
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
        steps: list[dict[str, Any]] = [
            {"next_step_id": "mosquitto"},
            {CONF_MODE: MODE_MQTT_OPENTHERM},
            {CONF_NAME: "Test", CONF_MQTT_TOPIC: "OTGW", CONF_DEVICE: "otgw-1"},
            {CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"]},
            {CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS},
            {CONF_RADIATORS: ["climate.radiator"], CONF_ROOMS: ["climate.room"]},
        ]
        for user_input in steps:
            result = await manager.async_configure(result["flow_id"], user_input)

        assert result["step_id"] == "calibrate_system"

        # The areas step tears down the coordinator it created to read its capabilities
        assert events == [("coordinator", "removed")]
        events.clear()

        return result["flow_id"]

    with patch.object(SatFlowHandler, "async_create_coordinator", AsyncMock(return_value=coordinator)):
        yield open_flow


def calculating(events: list[tuple[str, str]], outcome: float | BaseException | None = None) -> AbstractContextManager[Any]:
    """Patch `calculate` to record a start event, then return `outcome`, raise it, or wait until cancelled."""
    async def calculate(_self: OvershootProtection) -> float | None:
        events.append(("calculate", ""))
        if outcome is None:
            await asyncio.Event().wait()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return patch.object(OvershootProtection, "calculate", new=calculate)


async def start(hass: HomeAssistant, open_flow: Callable[[], Awaitable[str]]) -> tuple[str, ConfigFlowResult]:
    flow_id = await open_flow()
    return flow_id, await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "calibrate"})


async def test_calibration_starts_with_the_climate_off_and_the_valves_open(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events):
        _, first = await start(hass, flow_id)

    assert first["type"] == FlowResultType.SHOW_PROGRESS
    assert first["step_id"] == "calibrate"
    assert first["progress_action"] == "calibration"
    assert events.index((SAT, "off")) < events.index(("calculate", ""))
    assert all(valve in events for valve in VALVES)
    assert (SAT, PREVIOUS) not in events


async def test_configuring_while_calibrating_shows_progress_without_restarting(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events):
        id_, _ = await start(hass, flow_id)
        again = await hass.config_entries.flow.async_configure(id_)

    assert again["type"] == FlowResultType.SHOW_PROGRESS
    assert events.count((SAT, "off")) == 1
    assert events.count(("calculate", "")) == 1
    assert [e for e in events if e in VALVES] == VALVES


async def test_calibration_success_continues_and_restores_the_mode_once(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events, 40.0), patch.object(SatFlowHandler, "_enable_overshoot_protection"):
        id_, _ = await start(hass, flow_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(id_)

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "calibrated"
    assert events.count((SAT, PREVIOUS)) == 1
    assert events.count(("coordinator", "removed")) == 1


@pytest.mark.parametrize("outcome, level, message", [
    (TimeoutError(), logging.WARNING, "Timed out"),
    (asyncio.CancelledError(), logging.WARNING, "Cancelled"),
    (TypeError("boom"), logging.ERROR, "Unexpected error during overshoot protection calculation."),
])
async def test_calibration_failure_restores_the_mode_and_aborts(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]],
        caplog: pytest.LogCaptureFixture, outcome: BaseException, level: int, message: str) -> None:
    with caplog.at_level(logging.WARNING), calculating(events, outcome):
        id_, _ = await start(hass, flow_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(id_)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unable_to_calibrate"
    assert hass.config_entries.flow.async_progress() == []
    assert events.count((SAT, PREVIOUS)) == 1
    assert events.count(("coordinator", "removed")) == 1
    assert events.index(("coordinator", "removed")) < events.index((SAT, PREVIOUS))

    logged = [r for r in caplog.records if r.levelno == level and message in r.getMessage()]
    assert len(logged) == 1
    assert (logged[0].exc_info is not None) is (level == logging.ERROR)


async def test_closing_the_flow_cancels_calibration_and_restores_the_mode_once(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events):
        id_, _ = await start(hass, flow_id)
        hass.config_entries.flow.async_abort(id_)
        await hass.async_block_till_done()

    assert hass.config_entries.flow.async_progress() == []
    assert events.count((SAT, PREVIOUS)) == 1
    assert events.count(("coordinator", "removed")) == 1


# Temporary coordinators and the live entry

@pytest.mark.parametrize("data", [
    {CONF_MODE: MODE_MQTT_EMS, CONF_DEVICE: "ems", CONF_MQTT_TOPIC: "ems-esp"},
    {CONF_MODE: MODE_MQTT_OPENTHERM, CONF_DEVICE: "gw1", CONF_MQTT_TOPIC: "otgw"},
], ids=["ems", "opentherm"])
async def test_the_flow_coordinator_neither_subscribes_nor_publishes(hass, mqtt_mock, no_publish_delay, published, data: dict[str, str]) -> None:
    flow = SatFlowHandler()
    flow.hass = hass
    flow.data = data | {CONF_NAME: "Test"}

    with patch.object(mqtt, "async_subscribe", wraps=mqtt.async_subscribe) as subscribe:
        await flow.async_step_manufacturer()
        await hass.async_block_till_done()

    subscribe.assert_not_called()
    assert published() == []


async def test_the_manufacturer_step_without_stored_data_stores_nothing(hass, mqtt_mock, hass_storage) -> None:
    flow = SatFlowHandler()
    flow.hass = hass
    flow.data = {CONF_MODE: MODE_MQTT_EMS, CONF_NAME: "Test", CONF_DEVICE: "ems", CONF_MQTT_TOPIC: "ems-esp"}

    await flow.async_step_manufacturer()

    assert STORE_KEY not in hass_storage


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_manufacturer_step_leaves_the_live_ems_entry_untouched(hass, entry, coordinator, boiler_data, hass_storage) -> None:
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"selflowtemp": 50}}
    flow = SatFlowHandler()
    flow.hass = hass
    flow.data = dict(entry.data)

    result = await flow.async_step_manufacturer()

    assert result["step_id"] == "manufacturer"
    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 50}

    await boiler_data(**IDLE | {"burngas": "on"})
    assert coordinator.flame_active is True


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_the_reconfigure_flow_leaves_the_live_ems_entry_untouched(hass, entry, coordinator, boiler_data, hass_storage) -> None:
    await boiler_data(**IDLE | {"selflowtemp": 44})
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"selflowtemp": 50}}
    manager = hass.config_entries.flow

    result = await manager.async_init(DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    assert result["step_id"] == "sensors"
    result = await manager.async_configure(result["flow_id"], {
        "inside_sensor_entity_id": "sensor.test_inside_sensor", "outside_sensor_entity_id": ["sensor.test_outside_sensor"]})
    result = await manager.async_configure(result["flow_id"], {"heating_system": "radiators"})
    assert result["step_id"] == "areas"
    result = await manager.async_configure(result["flow_id"], {})

    assert result["step_id"] != "areas"
    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 50}
    assert current(hass, entry) is coordinator

    await boiler_data(**IDLE | {"burngas": "on"})
    assert coordinator.flame_active is True

    manager.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert hass_storage[STORE_KEY]["data"] == {"selflowtemp": 50}
