"""The manual duty cycle and its two options are gone; PWM timing is always the N-based timing."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.sat.config_flow import SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MODE,
    CONF_NAME,
    DOMAIN,
    MODE_MQTT_EMS,
    OPTIONS_DEFAULTS,
    SIGNAL_PWM_UPDATED,
    BoilerStatus,
)
from tests.const import with_data

REMOVED = ("automatic_duty_cycle", "duty_cycle")
STORED = {"automatic_duty_cycle": False, "duty_cycle": "00:10:00"}
TRANSLATIONS = sorted((Path(__file__).parent.parent / "custom_components/sat/translations").glob("*.json"))
DUTY = "sensor.pwm_duty_cycle_test"


def field_names(result) -> set[str]:
    return {str(marker) for marker in result["data_schema"].schema}


def options_flow(options: dict) -> SatOptionsFlowHandler:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_MQTT_EMS, CONF_NAME: "Test"}, options=options)
    return SatOptionsFlowHandler(entry)


def test_the_removed_options_have_no_default():
    assert not set(REMOVED) & set(OPTIONS_DEFAULTS)


@pytest.mark.parametrize("options", [{}, STORED, {"automatic_duty_cycle": True}])
async def test_the_options_flow_offers_neither_removed_field(hass, options):
    for step in ("system_configuration", "general"):
        flow = options_flow(options)
        flow.hass = hass
        result = await getattr(flow, f"async_step_{step}")()
        assert result["type"] == "form"
        assert not set(REMOVED) & field_names(result), step


def test_the_translation_files_are_found():
    assert TRANSLATIONS


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda path: path.stem)
def test_translations_are_valid_json_without_the_removed_options(path):
    text = path.read_text(encoding="utf-8")
    json.loads(text)
    for key in REMOVED:
        assert f'"{key}"' not in text


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")
@pytest.mark.parametrize(*with_data({}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False} | STORED))
async def test_stored_manual_duty_cycle_options_are_ignored(hass, entry, climate):
    # The cold point's N=4 gives a normal period of 900 s at a mid duty (the stored 00:10:00 would have given 600 s)
    pwm = climate.pwm
    pwm.update_cycles_per_hour(-5)
    boiler = SimpleNamespace(status=BoilerStatus.AT_SETPOINT, flame_active=False, hot_water_active=False, flow_temperature=50.0, reliable_flow_temperature=50.0)
    flame = SimpleNamespace(is_active=False, is_inactive=True, latest_on_time_seconds=None, average_on_time_seconds=100, seconds_since_hot_water_burn=None)
    base = climate.heating_curve.base_offset

    await pwm.update(boiler, flame, base + 0.5 * (50.0 - base), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]
    async_dispatcher_send(hass, SIGNAL_PWM_UPDATED.format(entry.entry_id), pwm.status)
    await hass.async_block_till_done()

    assert pwm.cycles_per_hour == 4
    on, off = pwm.duty_cycle
    assert on + off == pytest.approx(900, abs=1)
    assert float(hass.states.get(DUTY).state) == round(on / (on + off) * 100, 1)


@pytest.mark.parametrize(*with_data({}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False} | STORED))
async def test_stored_manual_duty_cycle_options_stay_in_the_diagnostics(hass, hass_client, entry, loop_entry):
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"]["options"]["automatic_duty_cycle"] is False
    assert diagnostics["entry"]["options"]["duty_cycle"] == "00:10:00"
