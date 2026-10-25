"""PWM Cycles Per Hour diagnostic sensor and the cycles per hour options."""
import json
import logging
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.climate import SatClimate
from custom_components.sat.config_flow import SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_AUTOMATIC_DUTY_CYCLE,
    CONF_CYCLES_PER_HOUR_COLD,
    CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE,
    CONF_CYCLES_PER_HOUR_MILD,
    CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE,
    CONF_DUTY_CYCLE,
    CONF_MODE,
    CONF_NAME,
    DOMAIN,
    MODE_MQTT_EMS,
    OPTIONS_DEFAULTS,
    BoilerStatus,
)
from custom_components.sat.util import create_pwm_controller
from tests.const import IDLE, ems_entry

ENTITY_ID = "sensor.pwm_cycles_per_hour"


def options_flow() -> SatOptionsFlowHandler:
    return SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_MQTT_EMS, CONF_NAME: "Test"}))


# Options flow

async def test_options_flow_shows_the_four_fields_with_defaults(hass):
    result = await options_flow().async_step_system_configuration()

    defaults = {str(marker): marker.default() for marker in result["data_schema"].schema}
    assert defaults[CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE] == 0
    assert defaults[CONF_CYCLES_PER_HOUR_COLD] == 4
    assert defaults[CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE] == 12
    assert defaults[CONF_CYCLES_PER_HOUR_MILD] == 2
    assert "cycles_per_hour" not in defaults
    assert not result["errors"]


async def test_options_flow_rejects_equal_temperatures(hass):
    flow = options_flow()
    shown = await flow.async_step_system_configuration()
    values = {str(marker): marker.default() for marker in shown["data_schema"].schema}
    values[CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE] = 5
    values[CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE] = 5

    result = await flow.async_step_system_configuration(values)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cycles_per_hour_temperatures_equal"}


async def test_translations_have_the_error_and_fields():
    for language in ("en", "sk"):
        data = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json").read_text(encoding="utf-8"))
        step = data["options"]["step"]["system_configuration"]
        for key in (CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE, CONF_CYCLES_PER_HOUR_COLD, CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE, CONF_CYCLES_PER_HOUR_MILD):
            assert key in step["data"]
        assert "cycles_per_hour_temperatures_equal" in data["options"]["error"]
        assert "cycles_per_hour" not in step["data"]


# Sensor

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_sensor_metadata(hass: HomeAssistant, entry, climate: SatClimate) -> None:
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes["friendly_name"].endswith("PWM Cycles Per Hour")
    assert set(state.attributes) <= {"friendly_name", "state_class", "unit_of_measurement", "device_class", "icon"}
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["unit_of_measurement"] == "cycles/h"

    registry = er.async_get(hass)
    item = registry.async_get(ENTITY_ID)
    assert item is not None
    assert item.unique_id == "test-pwm-cycles-per-hour"
    assert item.entity_category == EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_created_alongside_the_pwm_status_sensor(hass: HomeAssistant, entry) -> None:
    assert hass.states.get("sensor.pwm_status") is not None
    assert hass.states.get(ENTITY_ID) is not None


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_sensor_reflects_n_in_use_with_hvac_off(hass: HomeAssistant, entry, climate: SatClimate, boiler_data) -> None:
    """Outside 9.9 degrees gives N=2; the sensor follows every control cycle, including HVAC OFF."""
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    assert climate.pwm.cycles_per_hour == 2
    assert hass.states.get(ENTITY_ID).state == "2"

    hass.states.async_set("sensor.test_outside_sensor", "-5")
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    assert climate.pwm.cycles_per_hour == 4
    assert hass.states.get(ENTITY_ID).state == "4"


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry({CONF_CYCLES_PER_HOUR_COLD: 3, CONF_CYCLES_PER_HOUR_MILD: 3}))
async def test_options_set_the_cycles(hass: HomeAssistant, entry, climate: SatClimate, boiler_data) -> None:
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "3"


# Options flow schema range

async def test_cycles_fields_accept_up_to_6_and_reject_7(hass):
    shown = await options_flow().async_step_system_configuration()
    schema = shown["data_schema"]
    values = {str(marker): marker.default() for marker in schema.schema}

    for key in (CONF_CYCLES_PER_HOUR_COLD, CONF_CYCLES_PER_HOUR_MILD):
        assert schema(values | {key: 6})[key] == 6
        assert schema(values | {key: 1})[key] == 1
        with pytest.raises(vol.Invalid):
            schema(values | {key: 7})
        with pytest.raises(vol.Invalid):
            schema(values | {key: 0})


# Translations

def test_translation_descriptions_use_neutral_wording():
    for language in ("en", "sk"):
        data = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations" / f"{language}.json").read_text(encoding="utf-8"))
        descriptions = data["options"]["step"]["system_configuration"]["data_description"]
        for key in (CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE, CONF_CYCLES_PER_HOUR_COLD, CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE, CONF_CYCLES_PER_HOUR_MILD):
            assert descriptions[key]
    en = json.loads((Path(__file__).parent.parent / "custom_components/sat/translations/en.json").read_text(encoding="utf-8"))
    text = " ".join(en["options"]["step"]["system_configuration"]["data_description"].values())
    assert "At or below this outside temperature" not in text
    assert "At or above this outside temperature" not in text


# Invalid options fall back to the defaults

CURVE = SimpleNamespace(value=30.0, base_offset=20.0)

INVALID = [
    {CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE: 5, CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE: 5},
    {CONF_CYCLES_PER_HOUR_COLD: 0},
    {CONF_CYCLES_PER_HOUR_MILD: 0},
    {CONF_CYCLES_PER_HOUR_COLD: "abc"},
    {CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE: "abc"},
    {CONF_CYCLES_PER_HOUR_COLD: None},
    {CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE: None},
    {CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE: "nan"},
    {CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE: "nan"},
    {CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE: "inf"},
    {CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE: "inf"},
    {CONF_CYCLES_PER_HOUR_COLD: 7},
    {CONF_CYCLES_PER_HOUR_MILD: 7},
]


@pytest.mark.parametrize("overrides", INVALID)
def test_create_pwm_controller_falls_back_to_defaults(overrides, caplog):
    options = MappingProxyType(OPTIONS_DEFAULTS | {CONF_AUTOMATIC_DUTY_CYCLE: False, CONF_DUTY_CYCLE: "00:10:00"} | overrides)

    with caplog.at_level(logging.ERROR, logger="custom_components.sat.util"):
        pwm = create_pwm_controller(CURVE, False, MappingProxyType({}), options)  # type: ignore[arg-type]

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Invalid cycles per hour options" in errors[0].getMessage()
    assert pwm is not None
    for temperature, expected in [(-5, 4), (0, 4), (6, 3), (12, 2), (20, 2)]:
        pwm.update_cycles_per_hour(temperature)
        assert pwm.cycles_per_hour == expected


async def test_fallback_keeps_the_duty_cycle_maximum_time():
    options = MappingProxyType(OPTIONS_DEFAULTS | {CONF_AUTOMATIC_DUTY_CYCLE: False, CONF_DUTY_CYCLE: "00:06:40", CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE: 5, CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE: 5})
    pwm = create_pwm_controller(CURVE, False, MappingProxyType({}), options)  # type: ignore[arg-type]
    assert pwm is not None

    boiler = SimpleNamespace(status=BoilerStatus.AT_SETPOINT, flame_active=False, hot_water_active=False, flow_temperature=50.0)
    flame = SimpleNamespace(is_active=False, is_inactive=True, latest_on_time_seconds=None, average_on_time_seconds=100, seconds_since_hot_water_burn=None)
    duty = 0.25
    await pwm.update(boiler, flame, 20.0 + duty * 30.0)  # type: ignore[arg-type]

    on_time, off_time = pwm.state.duty_cycle  # type: ignore[misc]
    assert abs(on_time - duty * 400) <= 1
    assert abs(off_time - (1 - duty) * 400) <= 1


def test_valid_options_log_no_error(caplog):
    with caplog.at_level(logging.ERROR, logger="custom_components.sat.util"):
        create_pwm_controller(CURVE, False, MappingProxyType({}), MappingProxyType(dict(OPTIONS_DEFAULTS)))  # type: ignore[arg-type]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry({CONF_CYCLES_PER_HOUR_COLD_TEMPERATURE: 5, CONF_CYCLES_PER_HOUR_MILD_TEMPERATURE: 5}))
async def test_equal_temperatures_still_set_up_with_defaults(hass: HomeAssistant, entry, climate: SatClimate, boiler_data, caplog) -> None:
    assert any(r.levelno == logging.ERROR and "Invalid cycles per hour options" in r.getMessage() for r in caplog.get_records("setup"))
    assert hass.states.get(ENTITY_ID) is not None

    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    # Outside 9.9 degrees on the default mapping
    assert climate.pwm.cycles_per_hour == 2
    assert hass.states.get(ENTITY_ID).state == "2"

    # Logged once per setup, not per control cycle
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    errors = [r for phase in ("setup", "call") for r in caplog.get_records(phase) if r.levelno == logging.ERROR and "Invalid cycles per hour options" in r.getMessage()]
    assert len(errors) == 1


# Updates during HEAT

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*ems_entry())
async def test_sensor_updates_during_heat_cycles(hass: HomeAssistant, entry, climate: SatClimate, boiler_data) -> None:
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "2"

    hass.states.async_set("sensor.test_outside_sensor", "-5")
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()

    assert climate.pwm.cycles_per_hour == 4
    assert hass.states.get(ENTITY_ID).state == "4"
