"""Circulation after hot water (EMS-ESP): the unit rules of `Circulation`, the options, and the EMS climate loop end to end.

Written from the feature spec: after a hot water draw a combi boiler may stop its pump; SAT then requests heater ON with a low control
setpoint (below the flow temperature), so the pump runs without firing.
"""
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.circulation import Circulation
from custom_components.sat.config_flow import SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_CIRCULATE_AFTER_HOT_WATER,
    CONF_CIRCULATION_DURATION,
    CONF_CIRCULATION_SETPOINT,
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HOT_WATER_DELAY,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    CONF_NAME,
    CONF_RADIATORS,
    DOMAIN,
    MODE_FAKE,
    MODE_MQTT_EMS,
    MODE_MQTT_OPENTHERM,
    OPTIONS_DEFAULTS,
    PWMStatus,
)
from custom_components.sat.util import create_circulation
from tests.conftest import TIMELINE, boiler
from tests.const import IDLE, otgw_entry, with_data

# The upper bound of the duration option; change it here if it is lowered
DURATION_MAXIMUM = 240

LANGUAGES = ["en", "de", "es", "fr", "it", "nl", "pt", "sk"]
OPTION_KEYS = (CONF_CIRCULATE_AFTER_HOT_WATER, CONF_CIRCULATION_SETPOINT, CONF_CIRCULATION_DURATION)

START = "circulation start"
END = "circulation end"
NO_PUMP = "circulation: the pump did not start during circulation"
FIRED = "circulation: the boiler fired during circulation"


def lines(caplog, prefix: str = "") -> list[str]:
    return [m for r in caplog.records if r.name == TIMELINE and (m := r.getMessage()).startswith(prefix)]


def warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.levelno == logging.WARNING and r.getMessage().startswith("circulation")]


# Options and the factory

def test_the_option_defaults():
    assert OPTIONS_DEFAULTS[CONF_CIRCULATE_AFTER_HOT_WATER] is False
    assert OPTIONS_DEFAULTS[CONF_CIRCULATION_SETPOINT] == 20
    assert OPTIONS_DEFAULTS[CONF_CIRCULATION_DURATION] == 60


@pytest.mark.parametrize(("mode", "options", "created"), [
    (MODE_MQTT_EMS, {CONF_CIRCULATE_AFTER_HOT_WATER: True}, True),
    (MODE_MQTT_EMS, {}, False),
    (MODE_MQTT_EMS, {CONF_CIRCULATE_AFTER_HOT_WATER: False}, False),
    (MODE_MQTT_OPENTHERM, {CONF_CIRCULATE_AFTER_HOT_WATER: True}, False),
    (MODE_FAKE, {CONF_CIRCULATE_AFTER_HOT_WATER: True}, False),
])
def test_create_circulation_needs_ems_and_the_option(mode, options, created):
    assert (create_circulation({CONF_MODE: mode}, options) is not None) is created


def test_create_circulation_uses_the_defaults_and_the_options():
    default = create_circulation({CONF_MODE: MODE_MQTT_EMS}, {CONF_CIRCULATE_AFTER_HOT_WATER: True})
    assert default is not None
    assert default.setpoint == 20
    assert not default.active

    custom = create_circulation({CONF_MODE: MODE_MQTT_EMS}, {CONF_CIRCULATE_AFTER_HOT_WATER: True, CONF_CIRCULATION_SETPOINT: 17, CONF_CIRCULATION_DURATION: 90})
    assert custom is not None
    assert custom.setpoint == 17


# Config flow

def options_flow(mode: str, options: dict | None = None) -> SatOptionsFlowHandler:
    return SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: mode, CONF_NAME: "Test"}, options=options or {}))


@pytest.mark.parametrize(("mode", "shown"), [(MODE_MQTT_EMS, True), (MODE_MQTT_OPENTHERM, False), (MODE_FAKE, False)])
async def test_advanced_shows_the_circulation_fields_only_for_ems(hass, mode, shown):
    schema = (await options_flow(mode).async_step_advanced())["data_schema"].schema

    for key in OPTION_KEYS:
        assert (key in schema) is shown


async def test_the_circulation_fields_follow_the_flow_temperature_option_in_the_advanced_step(hass):
    keys = [str(key) for key in (await options_flow(MODE_MQTT_EMS).async_step_advanced())["data_schema"].schema]
    start = keys.index(CONF_HOT_WATER_FLOW_TEMPERATURE)

    assert keys[start + 1:start + 4] == list(OPTION_KEYS)


async def test_the_circulation_fields_have_defaults_and_ranges(hass):
    schema = (await options_flow(MODE_MQTT_EMS).async_step_advanced())["data_schema"].schema
    markers = {str(marker): (marker, value) for marker, value in schema.items()}

    marker, _ = markers[CONF_CIRCULATE_AFTER_HOT_WATER]
    assert marker.default() is False

    marker, selector = markers[CONF_CIRCULATION_SETPOINT]
    assert marker.default() == 20
    assert (selector.config["min"], selector.config["max"], selector.config["step"]) == (15, 25, 1)

    marker, selector = markers[CONF_CIRCULATION_DURATION]
    assert marker.default() == 60
    assert (selector.config["min"], selector.config["max"], selector.config["step"]) == (10, DURATION_MAXIMUM, 1)


async def test_the_circulation_fields_show_the_stored_options(hass):
    schema = (await options_flow(MODE_MQTT_EMS, {CONF_CIRCULATE_AFTER_HOT_WATER: True, CONF_CIRCULATION_SETPOINT: 18, CONF_CIRCULATION_DURATION: 120}).async_step_advanced())["data_schema"].schema
    defaults = {str(marker): marker.default() for marker in schema if str(marker) in OPTION_KEYS}

    assert defaults == {CONF_CIRCULATE_AFTER_HOT_WATER: True, CONF_CIRCULATION_SETPOINT: 18, CONF_CIRCULATION_DURATION: 120}


@pytest.mark.parametrize("language", LANGUAGES)
def test_translations_have_the_circulation_options_in_advanced(language):
    steps = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json").read_text(encoding="utf-8"))["options"]["step"]

    for section in ("data", "data_description"):
        for key in OPTION_KEYS:
            assert steps["advanced"][section][key].strip()


# The counters and properties

async def test_hot_water_burns_count_ended_hot_water_burns_only(hass, mqtt_mock, no_publish_delay, boiler_data, freezer: FrozenDateTimeFactory):
    from tests.test_burns import DRAW, HEATING, OFF, burn, started

    coordinator = await started(hass, {})
    await boiler_data(**OFF)
    assert coordinator.flame.hot_water_burns == 0

    await boiler_data(**DRAW)
    assert coordinator.flame.hot_water_burns == 0, "counted only when the burn ends"
    freezer.tick(30)
    await boiler_data(**OFF)
    assert coordinator.flame.hot_water_burns == 1

    await burn(boiler_data, freezer, HEATING, 60)
    assert coordinator.flame.hot_water_burns == 1

    await burn(boiler_data, freezer, DRAW, 20)
    assert coordinator.flame.hot_water_burns == 2
    await coordinator.async_will_remove_from_hass()


async def test_a_partial_hot_water_burn_counts(hass, mqtt_mock, no_publish_delay, boiler_data, freezer: FrozenDateTimeFactory):
    from tests.test_burns import DRAW, OFF, started

    coordinator = await started(hass, {})
    await boiler_data(**DRAW)
    freezer.tick(30)
    await boiler_data(**OFF)

    assert coordinator.flame.hot_water_burns == 1
    await coordinator.async_will_remove_from_hass()


async def test_the_snapshot_carries_the_burn_count_and_the_wait(hass, mqtt_mock, no_publish_delay, boiler_data, freezer: FrozenDateTimeFactory):
    from tests.test_burns import DRAW, OFF, started

    coordinator = await started(hass, {})
    await boiler_data(**OFF | {"heatingpump": "on"})
    assert coordinator.snapshot().flame.hot_water_burns == 0
    assert coordinator.snapshot().boiler.awaiting_pump_restart is False

    await boiler_data(**DRAW | {"heatingpump": "on"})
    freezer.tick(30)
    await boiler_data(**OFF | {"heatingpump": "off"})
    assert coordinator.snapshot().flame.hot_water_burns == 1
    assert coordinator.snapshot().boiler.awaiting_pump_restart is True
    assert coordinator.awaiting_pump_restart is True
    await coordinator.async_will_remove_from_hass()


async def test_awaiting_pump_restart_lasts_until_the_pump_goes_off_then_on(hass, mqtt_mock, no_publish_delay, boiler_data, freezer: FrozenDateTimeFactory):
    from tests.test_burns import DRAW, OFF, started

    coordinator = await started(hass, {})
    await boiler_data(**OFF | {"heatingpump": "on"})
    await boiler_data(**DRAW | {"heatingpump": "on"})
    freezer.tick(30)
    await boiler_data(**OFF | {"heatingpump": "on"})
    assert coordinator.awaiting_pump_restart is True

    await boiler_data(**OFF | {"heatingpump": "off"})
    assert coordinator.awaiting_pump_restart is True
    await boiler_data(**OFF | {"heatingpump": "on"})
    assert coordinator.awaiting_pump_restart is False
    await coordinator.async_will_remove_from_hass()


async def test_awaiting_pump_restart_is_false_without_a_pump_state(hass, mqtt_mock, no_publish_delay, boiler_data, freezer: FrozenDateTimeFactory):
    from tests.test_burns import DRAW, OFF, started

    def without_pump(data: dict) -> dict:
        return {key: value for key, value in data.items() if key != "heatingpump"}

    coordinator = await started(hass, {})
    await boiler_data(**without_pump(OFF))
    await boiler_data(**without_pump(DRAW))
    freezer.tick(30)
    await boiler_data(**without_pump(OFF))

    assert coordinator.flame.hot_water_burns == 1
    assert coordinator.awaiting_pump_restart is False
    await coordinator.async_will_remove_from_hass()


async def test_other_backends_never_await_a_pump_restart(otgw):
    assert otgw.awaiting_pump_restart is False
    assert otgw.snapshot().boiler.awaiting_pump_restart is False


# The unit rules of Circulation

class Env:
    """A Circulation with controllable inputs; every `update` runs with the current values."""

    def __init__(self, freezer: FrozenDateTimeFactory, setpoint: float = 20, duration: float = 60) -> None:
        self.circulation = Circulation(setpoint, duration)
        self.freezer = freezer
        self.boiler = replace(boiler(False), pump_active=False, awaiting_pump_restart=True, flow_temperature=55.0, reliable_flow_temperature=40.0)
        self.burns = 1
        # Seconds since the hot water burn ended
        self.since = 10.0
        self.args: dict[str, Any] = {"valves_open": True, "error": 0.5, "pwm_status": PWMStatus.OFF, "heater_off": True}

    def set(self, **changes) -> "Env":
        for key, value in changes.items():
            if key in self.args:
                self.args[key] = value
            elif key == "burns":
                self.burns = value
            elif key == "since":
                self.since = value
            else:
                self.boiler = replace(self.boiler, **{key: value})

        return self

    def update(self, **changes) -> bool:
        self.set(**changes)
        return self.circulation.update(self.boiler, SimpleNamespace(hot_water_burns=self.burns, seconds_since_hot_water_burn=self.since), **self.args)  # type: ignore[arg-type]


@pytest.fixture
def env(freezer: FrozenDateTimeFactory, caplog) -> Env:
    caplog.set_level(logging.INFO, logger=TIMELINE)
    return Env(freezer)


def test_it_starts_when_all_conditions_hold(env, caplog):
    assert env.update() is True
    assert env.circulation.active
    assert lines(caplog, START)[0].startswith("circulation start (pump off after hot water, flow=55")
    assert lines(caplog, START)[0].endswith("setpoint 20 for 60s)")


@pytest.mark.parametrize("changes", [
    {"awaiting_pump_restart": False},
    {"pump_active": True},
    {"pump_active": None},
    {"hot_water_active": True},
    {"flame_active": True},
    {"pwm_status": PWMStatus.ON},
    {"valves_open": False},
    {"error": -0.1},
    {"flow_temperature": None},
    {"flow_temperature": 29.9},
    {"heater_off": False},
])
def test_each_condition_blocks_the_start(env, caplog, changes):
    assert env.update(**changes) is False
    assert not env.circulation.active
    assert lines(caplog, START) == []


@pytest.mark.parametrize("changes", [{"error": 0.0}, {"flow_temperature": 30.0}, {"pwm_status": PWMStatus.OFF}, {"pwm_status": PWMStatus.IDLE}])
def test_the_boundaries_still_start(env, changes):
    assert env.update(**changes) is True


def test_the_flow_margin_follows_the_setpoint(freezer, caplog):
    env = Env(freezer, setpoint=25)

    assert env.update(flow_temperature=34.9) is False
    assert env.update(flow_temperature=35.0) is True


def test_it_does_not_start_again_for_the_same_burn(env):
    assert env.update() is True
    env.freezer.tick(60)
    assert env.update() is False
    assert not env.circulation.active

    # Whatever the reason the previous run ended, there's no retry for this burn
    assert env.update() is False
    assert env.update(pump_active=False) is False


@pytest.mark.parametrize("stop", ["pwm", "valves", "demand", "flow", "heater", "hot_water_on", "flame"])
def test_no_retry_for_the_same_burn_whatever_ended_the_run(env, stop):
    assert env.update() is True
    blocking = {
        "pwm": {"pwm_status": PWMStatus.ON}, "valves": {"valves_open": False}, "demand": {"error": -1}, "flow": {"flow_temperature": 20},
        "heater": {"heater_off": False}, "hot_water_on": {"hot_water_active": True}, "flame": {"flame_active": True},
    }[stop]
    assert env.update(**blocking) is False
    assert not env.circulation.active
    original = {"pwm_status": PWMStatus.OFF, "valves_open": True, "error": 0.5, "flow_temperature": 55.0, "heater_off": True, "hot_water_active": False, "flame_active": False}
    env.set(**original)

    assert env.update() is False


def test_a_new_hot_water_burn_allows_another_start(env, caplog):
    assert env.update() is True
    env.freezer.tick(61)
    assert env.update() is False
    assert env.update(burns=2) is True
    assert len(lines(caplog, START)) == 2


def test_the_request_continues_until_the_duration_has_elapsed(env, caplog):
    assert env.update() is True
    env.freezer.tick(59)
    assert env.update() is True
    env.freezer.tick(1)
    assert env.update() is False

    assert lines(caplog, END) == ["circulation end (duration)"]
    assert not env.circulation.active


def test_the_pump_coming_on_does_not_end_it(env, caplog):
    assert env.update() is True
    assert env.update(pump_active=True, awaiting_pump_restart=False) is True
    assert env.update(pump_active=True) is True
    assert env.circulation.active
    assert lines(caplog, END) == []


def test_the_wait_clearing_does_not_end_it(env):
    assert env.update() is True
    assert env.update(awaiting_pump_restart=False) is True


@pytest.mark.parametrize(("changes", "reason"), [
    ({"hot_water_active": True}, "hot water"),
    ({"burns": 2}, "hot water"),
    ({"flame_active": True}, "flame on"),
    ({"pwm_status": PWMStatus.ON}, "pwm on"),
    ({"valves_open": False}, "valves closed"),
    ({"error": -0.01}, "no heat demand"),
    ({"flow_temperature": None}, "flow temperature"),
    ({"flow_temperature": 29.9}, "flow temperature"),
    ({"heater_off": False}, "heater on"),
])
def test_each_end_reason(env, caplog, changes, reason):
    env.update()
    env.freezer.tick(1)

    assert env.update(**changes) is False
    assert not env.circulation.active
    assert lines(caplog, END) == [f"circulation end ({reason})"]


# Each pair: (earlier reason's change, later reason's change); the earlier one wins when both hold
@pytest.mark.parametrize(("first", "second", "reason"), [
    ({"hot_water_active": True}, {"flame_active": True, "pwm_status": PWMStatus.ON, "valves_open": False}, "hot water"),
    ({"burns": 2}, {"flame_active": True, "error": -1}, "hot water"),
    ({"flame_active": True}, {"pwm_status": PWMStatus.ON, "valves_open": False, "error": -1, "heater_off": False}, "flame on"),
    ({"pwm_status": PWMStatus.ON}, {"valves_open": False, "error": -1, "flow_temperature": 1.0, "heater_off": False}, "pwm on"),
    ({"valves_open": False}, {"error": -1, "flow_temperature": 1.0, "heater_off": False}, "valves closed"),
    ({"error": -1}, {"flow_temperature": 1.0, "heater_off": False}, "no heat demand"),
    ({"flow_temperature": 1.0}, {"heater_off": False}, "flow temperature"),
])
def test_the_end_reasons_apply_in_order(env, caplog, first, second, reason):
    env.update()
    env.freezer.tick(1000)

    assert env.update(**first | second) is False
    assert lines(caplog, END) == [f"circulation end ({reason})"]


def test_the_heater_on_reason_wins_over_the_duration(env, caplog):
    env.update()
    env.freezer.tick(1000)

    env.update(heater_off=False)
    assert lines(caplog, END) == ["circulation end (heater on)"]


def test_a_flame_ends_it_with_a_warning_after_the_end_line(env, caplog):
    env.update()
    env.update(flame_active=True)

    messages = [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.getMessage().startswith("circulation")]
    end = messages.index("circulation end (flame on)")
    # Both warnings follow the end line; the spec doesn't fix their order
    assert sorted(messages[end + 1:]) == sorted([FIRED, NO_PUMP])


def test_a_hot_water_draw_that_fires_the_burner_is_no_warning(env, caplog):
    env.update()
    env.update(hot_water_active=True, flame_active=True)

    assert FIRED not in warnings(caplog)
    assert lines(caplog, END) == ["circulation end (hot water)"]


def test_other_end_reasons_have_no_fired_warning(env, caplog):
    env.update()
    env.update(valves_open=False)

    assert FIRED not in warnings(caplog)


def test_the_pump_warning_follows_the_end_line_when_the_pump_never_started(env, caplog):
    env.update()
    env.freezer.tick(30)
    env.update()
    env.freezer.tick(30)
    env.update()

    messages = [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.getMessage().startswith("circulation")]
    assert messages[-2:] == ["circulation end (duration)", NO_PUMP]


@pytest.mark.parametrize("seen", [{"pump_active": True}, {"awaiting_pump_restart": False}])
def test_no_pump_warning_when_a_cycle_saw_the_pump_on_or_the_wait_cleared(env, caplog, seen):
    env.update()
    env.update(**seen)
    env.freezer.tick(61)
    env.update()

    assert lines(caplog, END) == ["circulation end (duration)"]
    assert warnings(caplog) == []


def test_the_pump_state_of_the_ending_cycle_counts(env, caplog):
    env.update()
    env.update(valves_open=False, pump_active=True)

    assert warnings(caplog) == []


def test_the_pump_seen_in_an_earlier_run_does_not_count_for_the_next(env, caplog):
    env.update()
    env.update(pump_active=True)
    env.freezer.tick(61)
    env.update()
    caplog.clear()

    env.set(pump_active=False, awaiting_pump_restart=True)
    assert env.update(burns=2) is True
    env.update(valves_open=False)

    assert warnings(caplog) == [NO_PUMP]


def test_stop_ends_with_the_reason_and_is_inert_when_idle(env, caplog):
    env.circulation.stop("hvac off")
    assert lines(caplog, END) == []

    env.update()
    env.circulation.stop("hvac off")
    assert lines(caplog, END) == ["circulation end (hvac off)"]
    assert not env.circulation.active
    assert warnings(caplog) == [NO_PUMP]

    env.circulation.stop("hvac off")
    assert len(lines(caplog, END)) == 1


def test_stop_observes_the_boiler_given(env, caplog):
    env.update()
    env.circulation.stop("missing temperature", replace(env.boiler, pump_active=True))

    assert warnings(caplog) == []


def test_a_stopped_circulation_does_not_restart_for_the_same_burn(env):
    env.update()
    env.circulation.stop("hvac off")

    assert env.update() is False


# The EMS climate loop

PUMP = IDLE | {"heatingpump": "on", "burngas": "off", "tapwateractive": "off", "heatingactive": "off", "selburnpow": 0, "heatingoff": "on"}
DRAW = PUMP | {"burngas": "on", "tapwateractive": "on", "heatingoff": "off"}
CIRCULATING = PUMP | {"heatingpump": "off", "curflowtemp": 55.0}

DELAY = 300


def entry_with(extra: dict[str, Any] | None = None, duration: float = 60):
    """A forced PWM, held for DELAY s after a hot water burn (its off phase), with circulation on."""
    return with_data(
        {CONF_MINIMUM_SETPOINT: 30},
        {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_HOT_WATER_DELAY: DELAY, CONF_CIRCULATE_AFTER_HOT_WATER: True, CONF_CIRCULATION_DURATION: duration} | (extra or {}),
    )


def setpoints(published) -> list[str]:
    return [p for _t, p, *_ in published() if "selflowtemp" in p]


def heater_writes(published) -> list[str]:
    return [json.loads(p)["value"] for _t, p, *_ in published() if "heatingoff" in p]


async def step(climate, boiler_data, freezer, mqtt_mock, seconds: float = 10, **data) -> None:
    mqtt_mock.async_publish.reset_mock()
    freezer.tick(seconds)
    await boiler_data(**data)
    await climate.async_control_heating_loop()


async def after_hot_water(climate, boiler_data, freezer, mqtt_mock) -> None:
    """Heating wanted, a hot water draw with the pump on, then the flame off and the pump off with a hot flow: the first loop after it."""
    await boiler_data(**PUMP)
    await boiler_data(**DRAW)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    freezer.tick(20)
    await boiler_data(**DRAW | {"burngas": "off", "tapwateractive": "off"})
    await step(climate, boiler_data, freezer, mqtt_mock, 5, **CIRCULATING)


@pytest.fixture(autouse=True)
def timeline_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


def cycle_line(caplog) -> str:
    return lines(caplog, "cycle ")[-1]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_happy_path(hass, entry, climate, coordinator, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    assert coordinator.awaiting_pump_restart
    assert lines(caplog, START)[0].startswith("circulation start (pump off after hot water, flow=55")
    assert set(setpoints(published)) == {'{"cmd": "selflowtemp", "value": 20}'}
    assert heater_writes(published) == ["off"]
    assert "(circulation)" in cycle_line(caplog)

    # The pump comes on: it goes on
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on"})
    assert not coordinator.awaiting_pump_restart
    assert lines(caplog, END) == []
    assert "(circulation)" in cycle_line(caplog)
    assert set(setpoints(published)) <= {'{"cmd": "selflowtemp", "value": 20}'}
    assert "on" not in heater_writes(published)

    # After the duration the normal decision applies: the PWM off phase requests heater OFF
    await step(climate, boiler_data, freezer, mqtt_mock, 50, **CIRCULATING | {"heatingpump": "on"})
    assert lines(caplog, END) == ["circulation end (duration)"]
    assert set(heater_writes(published)) == {"on"}
    assert "(circulation)" not in cycle_line(caplog)
    assert warnings(caplog) == []

    # No restart for the same burn
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on"})
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING)
    assert len(lines(caplog, START)) == 1

    # A new hot water burn: it can start again
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **DRAW)
    freezer.tick(20)
    await boiler_data(**DRAW | {"burngas": "off", "tapwateractive": "off"})
    await step(climate, boiler_data, freezer, mqtt_mock, 5, **CIRCULATING)
    assert len(lines(caplog, START)) == 2
    assert set(setpoints(published)) == {'{"cmd": "selflowtemp", "value": 20}'}
    assert heater_writes(published) == ["off"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_the_modulation_is_the_normal_one_during_circulation(entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    assert lines(caplog, START)
    assert "want setpoint=20.0 heater=ON" in cycle_line(caplog)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_HOT_WATER_DELAY: DELAY, CONF_CIRCULATE_AFTER_HOT_WATER: True, CONF_CIRCULATION_SETPOINT: 24}))
async def test_the_configured_setpoint_is_requested(entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    assert set(setpoints(published)) == {'{"cmd": "selflowtemp", "value": 24}'}


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_the_ending_cycle_never_requests_off_by_circulation(entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on"})

    # The room is warm now: the cycle's own decision applies, which is the PWM off phase here: heater OFF at minimum
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on", "curflowtemp": 20.0})
    assert lines(caplog, END) == ["circulation end (flow temperature)"]
    assert "(circulation)" not in cycle_line(caplog)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(duration=DURATION_MAXIMUM))
async def test_a_pwm_pulse_ends_circulation_and_keeps_its_on_time_and_setpoint(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    assert lines(caplog, START)
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on"})

    # Right before the delay is over: still circulating
    await step(climate, boiler_data, freezer, mqtt_mock, 200, **CIRCULATING | {"heatingpump": "on"})
    assert climate.pwm.status == PWMStatus.OFF
    assert lines(caplog, END) == []

    await step(climate, boiler_data, freezer, mqtt_mock, 90, **CIRCULATING | {"heatingpump": "on"})
    assert climate.pwm.status == PWMStatus.ON
    assert lines(caplog, END) == ["circulation end (pwm on)"]
    assert len(lines(caplog, "pwm ON")) == 1
    assert lines(caplog, "pwm ON")[0].startswith("pwm ON (pulse 1 this hour")
    assert "(circulation)" not in cycle_line(caplog)
    # The pulse's own setpoint (the minimum setpoint), not the circulation setpoint
    assert set(setpoints(published)) == {'{"cmd": "selflowtemp", "value": 30}'}
    assert heater_writes(published) in ([], ["off"])

    # The pulse keeps its on-time: it doesn't end early
    pwm_off = lines(caplog, "pwm OFF")
    await step(climate, boiler_data, freezer, mqtt_mock, 60, **CIRCULATING | {"heatingpump": "on"})
    assert climate.pwm.status == PWMStatus.ON
    assert lines(caplog, "pwm OFF") == pwm_off
    assert len(lines(caplog, START)) == 1


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_circulation_ending_by_duration_leaves_the_pwm_untouched(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on"})
    pwm_lines = lines(caplog, "pwm ")
    state = climate.pwm.state

    await step(climate, boiler_data, freezer, mqtt_mock, 50, **CIRCULATING | {"heatingpump": "on"})

    assert lines(caplog, END) == ["circulation end (duration)"]
    assert climate.pwm.state == state
    assert climate.pwm.status == PWMStatus.OFF
    assert lines(caplog, "pwm ") == pwm_lines
    assert not [m for m in lines(caplog, "pwm ") if m.startswith("pwm ON")]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(duration=DURATION_MAXIMUM))
async def test_circulation_does_not_touch_the_pwm_while_it_runs(hass, entry, climate, boiler_data, freezer, mqtt_mock, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    state = climate.pwm.state
    before = lines(caplog, "pwm ")

    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING)

    assert lines(caplog, START)
    assert climate.pwm.state == state
    assert lines(caplog, "pwm ") == before


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_hvac_off_ends_circulation(entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    assert climate.hvac_mode == HVACMode.HEAT

    await climate.async_set_hvac_mode(HVACMode.OFF)
    await climate.async_control_heating_loop()

    assert lines(caplog, END) == ["circulation end (hvac off)"]
    assert warnings(caplog) == [NO_PUMP]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
@pytest.mark.usefixtures("no_input_grace")
async def test_a_missing_temperature_ends_circulation(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    hass.states.async_set("sensor.test_inside_sensor", "unavailable")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()

    assert lines(caplog, END) == ["circulation end (missing temperature)"]
    # Heater OFF at the minimum setpoint (sent as 0), until heater OFF is confirmed
    assert heater_writes(published) == ["on"]
    assert setpoints(published) == ['{"cmd": "selflowtemp", "value": 0}']


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_no_heat_demand_blocks_the_start(hass, entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    hass.states.async_set("sensor.test_inside_sensor", "22")
    await hass.async_block_till_done()
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    assert lines(caplog, START) == []
    assert "(circulation)" not in cycle_line(caplog)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_a_flow_temperature_too_close_to_the_setpoint_blocks_the_start(entry, climate, boiler_data, freezer, mqtt_mock, caplog):
    await boiler_data(**PUMP)
    await boiler_data(**DRAW)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    freezer.tick(20)
    await boiler_data(**DRAW | {"burngas": "off", "tapwateractive": "off"})
    await step(climate, boiler_data, freezer, mqtt_mock, 5, **CIRCULATING | {"curflowtemp": 29.0})

    assert lines(caplog, START) == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_hvac_heat_again_does_not_restart_for_the_same_burn(entry, climate, boiler_data, freezer, mqtt_mock, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await climate.async_control_heating_loop()
    assert len(lines(caplog, START)) == 1

    # Same burn: HEAT again does not restart
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING)
    assert len(lines(caplog, START)) == 1


RADIATOR = "climate.radiator"


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with({CONF_RADIATORS: [RADIATOR]}))
@pytest.mark.parametrize(("hvac_action", "starts"), [("heating", True), ("idle", False)])
async def test_a_configured_radiator_must_be_heating(hass, entry, climate, boiler_data, freezer, mqtt_mock, caplog, hvac_action, starts):
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": hvac_action, "hvac_modes": ["heat", "off"]})
    await hass.async_block_till_done()
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    assert (len(lines(caplog, START)) == 1) is starts


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with({CONF_RADIATORS: [RADIATOR]}))
async def test_closing_the_valves_ends_circulation(hass, entry, climate, boiler_data, freezer, mqtt_mock, caplog):
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "heating", "hvac_modes": ["heat", "off"]})
    await hass.async_block_till_done()
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    assert lines(caplog, START)

    hass.states.async_set(RADIATOR, "heat", {"hvac_action": "idle", "hvac_modes": ["heat", "off"]})
    await hass.async_block_till_done()
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"heatingpump": "on"})

    assert lines(caplog, END) == ["circulation end (valves closed)"]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_a_hot_water_draw_ends_circulation(entry, climate, boiler_data, freezer, mqtt_mock, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **DRAW | {"heatingpump": "on"})

    assert lines(caplog, END) == ["circulation end (hot water)"]
    assert FIRED not in warnings(caplog)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with())
async def test_a_flame_during_circulation_warns(entry, climate, boiler_data, freezer, mqtt_mock, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)
    await step(climate, boiler_data, freezer, mqtt_mock, 10, **CIRCULATING | {"burngas": "on", "heatingactive": "on"})

    assert lines(caplog, END) == ["circulation end (flame on)"]
    assert FIRED in warnings(caplog)
    assert NO_PUMP in warnings(caplog)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_HOT_WATER_DELAY: DELAY}))
async def test_the_option_off_changes_nothing(entry, climate, boiler_data, freezer, mqtt_mock, published, caplog):
    await after_hot_water(climate, boiler_data, freezer, mqtt_mock)

    assert lines(caplog, "circulation") == []
    assert "(circulation)" not in cycle_line(caplog)
    assert setpoints(published) == []
    assert heater_writes(published) == []


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*otgw_entry({CONF_CIRCULATE_AFTER_HOT_WATER: True}))
async def test_a_non_ems_backend_has_no_circulation(entry, climate, coordinator, caplog):
    assert coordinator.awaiting_pump_restart is False
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()

    assert lines(caplog, "circulation") == []
