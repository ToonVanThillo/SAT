import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components import climate, mqtt
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.config_flow import (
    DEFAULT_MINIMUM_SETPOINT,
    SatFlowHandler,
    SatOptionsFlowHandler,
)
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_AUTOMATIC_GAINS,
    CONF_DERIVATIVE,
    CONF_DEVICE,
    CONF_HEATING_SYSTEM,
    CONF_HOT_WATER_BURNER_POWER,
    CONF_HOT_WATER_DELAY,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_INSIDE_SENSOR_ENTITY_ID,
    CONF_INTEGRAL,
    CONF_MANUFACTURER,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    CONF_OUTSIDE_SENSOR_ENTITY_ID,
    CONF_PROPORTIONAL,
    CONF_RADIATORS,
    CONF_ROOMS,
    DOMAIN,
    HEATING_SYSTEM_RADIATORS,
    MAXIMUM_SETPOINT,
    MINIMUM_SETPOINT,
    MODE_ESPHOME,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
    MODE_SERIAL,
    MODE_SIMULATOR,
    MODE_SWITCH,
    OPTIONS_DEFAULTS,
)
from custom_components.sat.manufacturer import MANUFACTURERS
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


HOT_WATER_OPTIONS = (CONF_HOT_WATER_BURNER_POWER, CONF_HOT_WATER_FLOW_TEMPERATURE)


@pytest.mark.parametrize("mode, shown", [(MODE_MQTT_EMS, True), (MODE_MQTT_OPENTHERM, False)])
async def test_advanced_shows_the_hot_water_options_only_for_ems(hass, mode, shown):
    schema = (await options_flow(mode).async_step_advanced())["data_schema"].schema

    for key in HOT_WATER_OPTIONS:
        assert (key in schema) is shown


async def test_the_hot_water_options_are_optional_and_unset_by_default(hass):
    schema = (await options_flow(MODE_MQTT_EMS).async_step_advanced())["data_schema"].schema

    for key in HOT_WATER_OPTIONS:
        marker = next(marker for marker in schema if marker == key)
        assert isinstance(marker, vol.Optional)
        assert marker.description == {"suggested_value": None}


async def test_a_cleared_hot_water_option_is_stored_unset(hass):
    flow = SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_MQTT_EMS, CONF_NAME: "Test"}, options={CONF_HOT_WATER_BURNER_POWER: 134, CONF_HOT_WATER_FLOW_TEMPERATURE: 45}))
    assert {marker: marker.description for marker in (await flow.async_step_advanced())["data_schema"].schema if marker in HOT_WATER_OPTIONS} == {
        CONF_HOT_WATER_BURNER_POWER: {"suggested_value": 134},
        CONF_HOT_WATER_FLOW_TEMPERATURE: {"suggested_value": 45},
    }

    result = await flow.async_step_advanced({CONF_HOT_WATER_FLOW_TEMPERATURE: 50})

    assert result["data"][CONF_HOT_WATER_BURNER_POWER] is None
    assert result["data"][CONF_HOT_WATER_FLOW_TEMPERATURE] == 50


@pytest.mark.parametrize("mode", [MODE_MQTT_EMS, MODE_MQTT_OPENTHERM, MODE_FAKE])
async def test_system_configuration_has_the_hot_water_delay_for_every_backend(hass, mode):
    result = await options_flow(mode).async_step_system_configuration()
    marker, value = next((marker, value) for marker, value in result["data_schema"].schema.items() if marker == CONF_HOT_WATER_DELAY)

    assert marker.default() == 0
    assert value.config["min"] == 0
    assert value.config["unit_of_measurement"] == "s"


# "Allow unrestricted modulation" replaces "Force pulse width modulation" for every backend with setpoint management

LEGACY_FORCE = "force_pulse_width_modulation"


def test_unrestricted_modulation_is_allowed_by_default_and_force_pwm_is_gone() -> None:
    assert OPTIONS_DEFAULTS[CONF_ALLOW_UNRESTRICTED_MODULATION] is True
    assert LEGACY_FORCE not in OPTIONS_DEFAULTS


@pytest.mark.parametrize("mode", [MODE_MQTT_EMS, MODE_MQTT_OPENTHERM, MODE_SERIAL, MODE_ESPHOME, MODE_SIMULATOR])
@pytest.mark.parametrize("stored, expected", [({}, True), ({CONF_ALLOW_UNRESTRICTED_MODULATION: False}, False), ({LEGACY_FORCE: True}, True)])
async def test_advanced_offers_unrestricted_modulation_with_setpoint_management(hass: HomeAssistant, mode: str, stored: dict[str, Any], expected: bool) -> None:
    flow = SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: mode, CONF_NAME: "Test"}, options=stored))
    schema = (await flow.async_step_advanced())["data_schema"].schema

    assert next(marker for marker in schema if marker == CONF_ALLOW_UNRESTRICTED_MODULATION).default() is expected
    assert LEGACY_FORCE not in schema


async def test_advanced_does_not_offer_unrestricted_modulation_for_switch(hass: HomeAssistant) -> None:
    schema = (await options_flow(MODE_SWITCH).async_step_advanced())["data_schema"].schema

    assert CONF_ALLOW_UNRESTRICTED_MODULATION not in schema
    assert LEGACY_FORCE not in schema


@pytest.mark.parametrize("language", ["en", "de", "es", "fr", "it", "nl", "pt", "sk"])
async def test_translations_hot_water_delay_in_system_configuration(language):
    steps = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json").read_text(encoding="utf-8"))["options"]["step"]

    for section in ("data", "data_description"):
        assert "hot_water_delay" in steps["system_configuration"][section]


@pytest.mark.parametrize("language", ["en", "de", "es", "fr", "it", "nl", "pt", "sk"])
async def test_translations_hot_water_options_in_advanced(language):
    steps = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json").read_text(encoding="utf-8"))["options"]["step"]

    for section in ("data", "data_description"):
        for key in HOT_WATER_OPTIONS:
            assert key in steps["advanced"][section]


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
def flow_coordinator(events: list[tuple[str, str]]) -> Iterator[MagicMock]:
    """Patch a fake coordinator with setpoint management into the config flow; it records its teardown."""
    coordinator = MagicMock(maximum_setpoint_value=60, supports_setpoint_management=True)
    coordinator.async_setup = AsyncMock()
    coordinator.async_will_remove_from_hass = AsyncMock(side_effect=lambda: events.append(("coordinator", "removed")))

    with patch.object(SatFlowHandler, "async_create_coordinator", AsyncMock(return_value=coordinator)):
        yield coordinator


@pytest.fixture
def flow_id(hass: HomeAssistant, events: list[tuple[str, str]], flow_coordinator: MagicMock) -> Callable[[], Awaitable[str]]:
    """Return a coroutine function that opens a flow at the calibrate menu, with a fake coordinator patched in."""
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

    return open_flow


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
    with calculating(events, 40.0):
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
async def test_calibration_failure_restores_the_mode_and_shows_the_manual_step(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]],
        caplog: pytest.LogCaptureFixture, outcome: BaseException, level: int, message: str) -> None:
    with caplog.at_level(logging.WARNING), calculating(events, outcome):
        id_, _ = await start(hass, flow_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(id_)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {"base": "calibration_failed"}
    assert minimum_default(result) == DEFAULT_MINIMUM_SETPOINT
    assert [flow["flow_id"] for flow in hass.config_entries.flow.async_progress()] == [id_]
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


# Minimum setpoint: required, above the cold threshold, then automatic gains, manufacturer (or PID controller) and finish

def minimum_default(result: ConfigFlowResult) -> float:
    schema = result["data_schema"]
    assert schema is not None
    return next(marker for marker in schema.schema if marker == CONF_MINIMUM_SETPOINT).default()


async def test_the_calibrate_menu_offers_only_calibration_and_the_manual_minimum(hass: HomeAssistant) -> None:
    result = await SatFlowHandler().async_step_calibrate_system()

    assert result["menu_options"] == ["calibrate", "overshoot_protection"]


async def test_the_manual_minimum_must_be_above_the_cold_threshold(hass: HomeAssistant, flow_id: Callable[[], Awaitable[str]]) -> None:
    manager = hass.config_entries.flow
    id_ = await flow_id()

    result = await manager.async_configure(id_, {"next_step_id": "overshoot_protection"})
    assert result["step_id"] == "overshoot_protection"
    assert not result["errors"]
    assert minimum_default(result) == DEFAULT_MINIMUM_SETPOINT

    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: 28.2})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {CONF_MINIMUM_SETPOINT: "minimum_setpoint_too_low"}

    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: 28.3})
    assert result["step_id"] == "automatic_gains"
    assert not result.get("errors")


@pytest.mark.parametrize("value", [28.2, 15.0])
async def test_a_calibration_at_or_below_the_cold_threshold_shows_the_manual_step(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]], value: float) -> None:
    with calculating(events, value):
        id_, _ = await start(hass, flow_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(id_)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {"base": "calibration_too_low"}
    assert minimum_default(result) == value
    assert events.count((SAT, PREVIOUS)) == 1

    result = await hass.config_entries.flow.async_configure(id_, {CONF_MINIMUM_SETPOINT: 45})
    assert result["step_id"] == "automatic_gains"


async def finish(hass: HomeAssistant, id_: str, automatic_gains: bool) -> ConfigFlowResult:
    """Continue a flow at the automatic gains step to its end and return the last result."""
    manager = hass.config_entries.flow
    result = await manager.async_configure(id_, {CONF_AUTOMATIC_GAINS: automatic_gains})

    if not automatic_gains:
        assert result["step_id"] == "pid_controller"
        result = await manager.async_configure(id_, {CONF_PROPORTIONAL: "40", CONF_INTEGRAL: "0", CONF_DERIVATIVE: "5000"})

    assert result["step_id"] == "manufacturer"
    return await manager.async_configure(id_, {CONF_MANUFACTURER: next(iter(MANUFACTURERS))})


@pytest.mark.parametrize("automatic_gains", [True, False], ids=["automatic-gains", "pid-controller"])
async def test_after_the_manual_minimum_the_flow_continues_to_automatic_gains(
        hass: HomeAssistant, flow_id: Callable[[], Awaitable[str]], automatic_gains: bool) -> None:
    id_ = await flow_id()
    await hass.config_entries.flow.async_configure(id_, {"next_step_id": "overshoot_protection"})
    result = await hass.config_entries.flow.async_configure(id_, {CONF_MINIMUM_SETPOINT: 45})
    assert result["step_id"] == "automatic_gains"

    result = await finish(hass, id_, automatic_gains)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MINIMUM_SETPOINT] == 45
    assert result["data"][CONF_AUTOMATIC_GAINS] is automatic_gains
    assert "overshoot_protection" not in result["data"]


async def test_after_a_calibration_the_flow_continues_to_automatic_gains(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events, 40.0):
        id_, _ = await start(hass, flow_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(id_)

    assert result["menu_options"] == ["calibrate", "automatic_gains"]
    result = await hass.config_entries.flow.async_configure(id_, {"next_step_id": "automatic_gains"})
    assert result["step_id"] == "automatic_gains"

    result = await finish(hass, id_, True)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MINIMUM_SETPOINT] == 40.0
    assert "overshoot_protection" not in result["data"]


@pytest.mark.usefixtures("flow_coordinator")
async def test_reconfigure_requires_the_minimum_and_continues_to_automatic_gains(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={
        CONF_MODE: MODE_MQTT_OPENTHERM, CONF_NAME: "Test", CONF_DEVICE: "otgw-1", CONF_MQTT_TOPIC: "OTGW", CONF_MINIMUM_SETPOINT: 20,
        CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"], CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
    })
    entry.add_to_hass(hass)
    manager = hass.config_entries.flow

    result = await manager.async_init(DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    steps: list[dict[str, Any]] = [
        {CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"]},
        {CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS},
        {},
    ]
    for user_input in steps:
        result = await manager.async_configure(result["flow_id"], user_input)
    assert result["menu_options"] == ["calibrate", "overshoot_protection"]

    id_ = result["flow_id"]
    result = await manager.async_configure(id_, {"next_step_id": "overshoot_protection"})
    assert minimum_default(result) == 20
    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: 20})
    assert result["errors"] == {CONF_MINIMUM_SETPOINT: "minimum_setpoint_too_low"}
    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: 42})
    assert result["step_id"] == "automatic_gains"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await finish(hass, id_, True)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MINIMUM_SETPOINT] == 42
    assert "overshoot_protection" not in entry.data


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


# Step 9 (configuration): more calibration outcomes, the manual step, the paths after the minimum, the options flow and the translations

LANGUAGES = ["en", "de", "es", "fr", "it", "nl", "pt", "sk"]
TRANSLATIONS = Path(__file__).parent.parent / "custom_components/sat/translations"


def translation(language: str) -> dict[str, Any]:
    return json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))


def calculating_in_turn(events: list[tuple[str, str]], outcomes: list[float | BaseException | None]) -> AbstractContextManager[Any]:
    """Patch `calculate` to return or raise the outcomes one by one, one per calibration run."""
    remaining = list(outcomes)

    async def calculate(_self: OvershootProtection) -> float | None:
        events.append(("calculate", ""))
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return patch.object(OvershootProtection, "calculate", new=calculate)


async def settle(hass: HomeAssistant, id_: str) -> ConfigFlowResult:
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(id_)


async def test_a_calibration_without_a_result_shows_the_manual_step_with_an_error(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating_in_turn(events, [None]):
        id_, _ = await start(hass, flow_id)
        result = await settle(hass, id_)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {"base": "calibration_failed"}
    assert events.count((SAT, PREVIOUS)) == 1


async def test_a_failed_calibration_followed_by_a_manual_minimum_completes_and_restores_the_mode_once(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events, TimeoutError()):
        id_, _ = await start(hass, flow_id)
        result = await settle(hass, id_)

    assert result["errors"] == {"base": "calibration_failed"}
    result = await hass.config_entries.flow.async_configure(id_, {CONF_MINIMUM_SETPOINT: 35})
    assert result["step_id"] == "automatic_gains"
    result = await finish(hass, id_, True)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MINIMUM_SETPOINT] == 35
    assert events.count((SAT, PREVIOUS)) == 1
    assert events.count((SAT, "off")) == 1


async def test_retrying_the_calibration_and_failing_shows_the_manual_step_with_the_stored_minimum(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    manager = hass.config_entries.flow

    with calculating_in_turn(events, [40.0, None]):
        id_, _ = await start(hass, flow_id)
        result = await settle(hass, id_)
        assert result["step_id"] == "calibrated"

        await manager.async_configure(id_, {"next_step_id": "calibrate"})
        result = await settle(hass, id_)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {"base": "calibration_failed"}
    assert minimum_default(result) == 40.0
    assert events.count((SAT, "off")) == 2
    assert events.count((SAT, PREVIOUS)) == 2
    assert [flow["flow_id"] for flow in manager.async_progress()] == [id_]

    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: 50})
    assert result["step_id"] == "automatic_gains"
    result = await finish(hass, id_, True)
    assert result["data"][CONF_MINIMUM_SETPOINT] == 50


async def test_a_retried_calibration_that_raises_does_not_report_the_previous_result(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    manager = hass.config_entries.flow

    with calculating_in_turn(events, [40.0, TimeoutError()]):
        id_, _ = await start(hass, flow_id)
        assert (await settle(hass, id_))["step_id"] == "calibrated"

        await manager.async_configure(id_, {"next_step_id": "calibrate"})
        result = await settle(hass, id_)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {"base": "calibration_failed"}


@pytest.mark.usefixtures("flow_coordinator")
async def test_reconfigure_prefills_a_too_low_calibration_result_over_the_stored_minimum(
        hass: HomeAssistant, events: list[tuple[str, str]]) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=SatFlowHandler.VERSION, data={
        CONF_MODE: MODE_MQTT_OPENTHERM, CONF_NAME: "Test", CONF_DEVICE: "otgw-1", CONF_MQTT_TOPIC: "OTGW", CONF_MINIMUM_SETPOINT: 45,
        CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"], CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
    })
    entry.add_to_hass(hass)
    manager = hass.config_entries.flow

    result = await manager.async_init(DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    steps: list[dict[str, Any]] = [
        {CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"]},
        {CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS},
        {},
    ]
    for user_input in steps:
        result = await manager.async_configure(result["flow_id"], user_input)

    with calculating_in_turn(events, [25.0]):
        await manager.async_configure(result["flow_id"], {"next_step_id": "calibrate"})
        result = await settle(hass, result["flow_id"])

    assert result["step_id"] == "overshoot_protection"
    assert result["errors"] == {"base": "calibration_too_low"}
    assert minimum_default(result) == 25.0


async def test_retrying_the_calibration_with_a_result_replaces_the_minimum(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    manager = hass.config_entries.flow

    with calculating_in_turn(events, [40.0, 45.5]):
        id_, _ = await start(hass, flow_id)
        await settle(hass, id_)
        await manager.async_configure(id_, {"next_step_id": "calibrate"})
        result = await settle(hass, id_)

    assert result["step_id"] == "calibrated"
    result = await manager.async_configure(id_, {"next_step_id": "automatic_gains"})
    result = await finish(hass, id_, True)
    assert result["data"][CONF_MINIMUM_SETPOINT] == 45.5


async def test_the_selector_bounds_of_the_manual_minimum(hass: HomeAssistant, flow_id: Callable[[], Awaitable[str]]) -> None:
    manager = hass.config_entries.flow
    id_ = await flow_id()

    result = await manager.async_configure(id_, {"next_step_id": "overshoot_protection"})
    schema = result["data_schema"]
    assert schema is not None
    selector = next(value for marker, value in schema.schema.items() if marker == CONF_MINIMUM_SETPOINT)
    assert (selector.config["min"], selector.config["max"]) == (MINIMUM_SETPOINT, MAXIMUM_SETPOINT)

    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: selector.config["min"]})
    assert result["errors"] == {CONF_MINIMUM_SETPOINT: "minimum_setpoint_too_low"}

    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: selector.config["max"]})
    assert result["step_id"] == "automatic_gains"
    result = await finish(hass, id_, True)
    assert result["data"][CONF_MINIMUM_SETPOINT] == MAXIMUM_SETPOINT


async def test_a_rejected_manual_minimum_keeps_the_calibration_result_as_the_default(
        hass: HomeAssistant, events: list[tuple[str, str]], flow_id: Callable[[], Awaitable[str]]) -> None:
    with calculating(events, 20.0):
        id_, _ = await start(hass, flow_id)
        result = await settle(hass, id_)

    assert minimum_default(result) == 20.0
    result = await hass.config_entries.flow.async_configure(id_, {CONF_MINIMUM_SETPOINT: 25})
    assert result["errors"] == {CONF_MINIMUM_SETPOINT: "minimum_setpoint_too_low"}


async def test_a_backend_without_setpoint_management_goes_from_areas_to_automatic_gains(
        hass: HomeAssistant, flow_id: Callable[[], Awaitable[str]], flow_coordinator: MagicMock) -> None:
    flow_coordinator.supports_setpoint_management = False
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": "user"})
    steps: list[dict[str, Any]] = [
        {"next_step_id": "mosquitto"},
        {CONF_MODE: MODE_MQTT_OPENTHERM},
        {CONF_NAME: "Test", CONF_MQTT_TOPIC: "OTGW", CONF_DEVICE: "otgw-1"},
        {CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"]},
        {CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS},
        {},
    ]
    for user_input in steps:
        result = await manager.async_configure(result["flow_id"], user_input)

    assert result["step_id"] == "automatic_gains"
    result = await finish(hass, result["flow_id"], True)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert "overshoot_protection" not in result["data"]


async def test_the_simulator_skips_the_manufacturer(hass: HomeAssistant, flow_coordinator: MagicMock) -> None:
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": "user", "show_advanced_options": True})
    steps: list[dict[str, Any]] = [
        {"next_step_id": "simulator"},
        {CONF_NAME: "Sim", CONF_MINIMUM_SETPOINT: 50},
        {CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"]},
        {CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS},
        {},
    ]
    for user_input in steps:
        result = await manager.async_configure(result["flow_id"], user_input)

    assert result["step_id"] == "calibrate_system"
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "overshoot_protection"})
    result = await manager.async_configure(result["flow_id"], {CONF_MINIMUM_SETPOINT: 45})
    result = await manager.async_configure(result["flow_id"], {CONF_AUTOMATIC_GAINS: True})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MINIMUM_SETPOINT] == 45


@pytest.mark.usefixtures("flow_coordinator")
@pytest.mark.parametrize("stored, expected_default", [({CONF_MINIMUM_SETPOINT: 10}, 10), ({}, DEFAULT_MINIMUM_SETPOINT)], ids=["stored-10", "none-stored"])
async def test_reconfigure_prefills_the_stored_minimum_or_40_and_rejects_it_when_too_low(
        hass: HomeAssistant, stored: dict[str, Any], expected_default: float) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=SatFlowHandler.VERSION, data={
        CONF_MODE: MODE_MQTT_OPENTHERM, CONF_NAME: "Test", CONF_DEVICE: "otgw-1", CONF_MQTT_TOPIC: "OTGW",
        CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"], CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
    } | stored)
    entry.add_to_hass(hass)
    manager = hass.config_entries.flow

    result = await manager.async_init(DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    steps: list[dict[str, Any]] = [
        {CONF_INSIDE_SENSOR_ENTITY_ID: "sensor.inside", CONF_OUTSIDE_SENSOR_ENTITY_ID: ["sensor.outside"]},
        {CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS},
        {},
    ]
    for user_input in steps:
        result = await manager.async_configure(result["flow_id"], user_input)

    id_ = result["flow_id"]
    result = await manager.async_configure(id_, {"next_step_id": "overshoot_protection"})
    assert minimum_default(result) == expected_default

    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: minimum_default(result) if expected_default <= 28.2 else 28.2})
    assert result["errors"] == {CONF_MINIMUM_SETPOINT: "minimum_setpoint_too_low"}
    result = await manager.async_configure(id_, {CONF_MINIMUM_SETPOINT: 33})
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await finish(hass, id_, True)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MINIMUM_SETPOINT] == 33


@pytest.mark.parametrize("mode", [MODE_MQTT_EMS, MODE_MQTT_OPENTHERM, MODE_SWITCH])
async def test_the_options_flow_with_a_stored_legacy_force_option_opens_and_saves(hass: HomeAssistant, mode: str) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MODE: mode, CONF_NAME: "Test", CONF_DEVICE: "d"}, options={LEGACY_FORCE: True, "simulation": True})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id, context={"show_advanced_options": True})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "advanced"})
    assert result["type"] == FlowResultType.FORM
    schema = result["data_schema"]
    assert schema is not None
    assert LEGACY_FORCE not in schema.schema

    defaults = {str(marker.schema): marker.default() for marker in schema.schema if isinstance(marker, vol.Required)}  # type: ignore[operator]
    assert defaults.get(CONF_ALLOW_UNRESTRICTED_MODULATION, True) is True
    assert defaults["simulation"] is True

    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults | {CONF_ALLOW_UNRESTRICTED_MODULATION: False} if mode != MODE_SWITCH else defaults)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["simulation"] is True
    assert result["data"].get(CONF_ALLOW_UNRESTRICTED_MODULATION, True) is (mode == MODE_SWITCH)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_translations_have_the_new_keys_and_not_the_removed_ones(language: str) -> None:
    data = translation(language)
    config, options = data["config"], data["options"]["step"]["advanced"]

    for section in ("data", "data_description"):
        assert CONF_ALLOW_UNRESTRICTED_MODULATION in options[section]
        assert LEGACY_FORCE not in options[section]

    for error in ("minimum_setpoint_too_low", "calibration_failed", "calibration_too_low"):
        assert config["error"][error]

    assert "automatic_gains" in config["step"]["calibrated"]["menu_options"]
    assert "finish" not in config["step"]["calibrated"]["menu_options"]
    assert "pid_controller" not in config["step"]["calibrate_system"]["menu_options"]
    assert set(config["step"]["calibrate_system"]["menu_options"]) == {"calibrate", "overshoot_protection"}


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_error_the_flow_can_emit_is_translated(language: str) -> None:
    source = (Path(__file__).parent.parent / "custom_components/sat/config_flow.py").read_text(encoding="utf-8").split("class SatOptionsFlowHandler")[0]
    emitted = set(re.findall(r'(?:errors?\[[^\]]+\]|errors) = (?:\{[^:]+: )?"([a-z_]+)"', source))

    assert {"calibration_failed", "calibration_too_low", "minimum_setpoint_too_low"} <= emitted
    assert emitted <= set(translation("en")["config"]["error"])
    # Only the keys of the calibration and minimum steps; "invalid_device" of the serial step is missing from some languages already
    assert {"calibration_failed", "calibration_too_low", "minimum_setpoint_too_low"} <= set(translation(language)["config"]["error"])


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_translations_are_valid_json_with_the_same_menus_as_english(language: str) -> None:
    config, english = translation(language)["config"]["step"], translation("en")["config"]["step"]

    for step in ("calibrate_system", "calibrated"):
        assert set(config[step]["menu_options"]) == set(english[step]["menu_options"])
