"""Circulation after hot water was removed. Options a user saved while it existed stay in the entry; they must be ignored without a trace,
and the loop after a hot water burn must behave as it did before."""
import json
import logging
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.config_flow import SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HOT_WATER_DELAY,
    CONF_MINIMUM_SETPOINT,
    DOMAIN,
)
from tests.conftest import TIMELINE, run_one_loop, timeline_since
from tests.const import IDLE, with_data

STALE = {"circulate_after_hot_water": True, "circulation_setpoint": 20, "circulation_duration": 60}
PWM_HELD = {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_HOT_WATER_DELAY: 300}

PUMP = IDLE | {"heatingpump": "on", "burngas": "off", "tapwateractive": "off", "heatingactive": "off", "selburnpow": 0, "heatingoff": "on"}
DRAW = PUMP | {"burngas": "on", "tapwateractive": "on"}


def sat_problems(caplog) -> list[str]:
    records = caplog.get_records("setup") + caplog.get_records("call")

    # "No previously saved temperature" is logged for every fresh entry, circulation or not
    return [
        f"{r.name}: {r.getMessage()}" for r in records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components.sat")
        and not (r.name == "custom_components.sat.climate" and r.getMessage().startswith("No previously saved temperature"))
    ]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, PWM_HELD | STALE))
async def test_stale_circulation_options_load_cleanly(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    assert entry.state == ConfigEntryState.LOADED
    assert entry.options["circulate_after_hot_water"] is True

    await boiler_data(**PUMP)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    new = await run_one_loop(hass, climate, caplog, len(caplog.records))

    assert "(circulation)" not in new[-1]
    assert not [m for m in new if m.lower().startswith("circulation")]

    # The advanced step opens, without those fields
    flow = SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data=dict(entry.data), options=dict(entry.options)))
    schema = (await flow.async_step_advanced())["data_schema"].schema
    assert not {str(key) for key in schema} & set(STALE)

    assert sat_problems(caplog) == []


def writes(mqtt_mock) -> list[dict[str, Any]]:
    return [json.loads(call.args[1]) for call in mqtt_mock.async_publish.call_args_list]


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, PWM_HELD | STALE))
async def test_after_a_hot_water_burn_the_heater_stays_off_and_is_released(hass, entry, climate, boiler_data, freezer, caplog, mqtt_mock):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    readback: dict[str, Any] = {"heatingoff": "on", "selflowtemp": 5}
    all_writes: list[dict[str, Any]] = []

    async def cycle(seconds: float, **data: Any) -> str:
        mark = len(caplog.records)
        mqtt_mock.async_publish.reset_mock()
        freezer.tick(seconds)
        await boiler_data(**IDLE | data | readback)
        new = await run_one_loop(hass, climate, caplog, mark)
        await hass.async_block_till_done()
        for write in writes(mqtt_mock):
            readback[write["cmd"]] = write["value"]
            all_writes.append(write)
        assert timeline_since(caplog, mark)

        return next(m for m in reversed(new) if m.startswith("cycle "))

    await boiler_data(**PUMP | readback)
    await boiler_data(**DRAW | readback)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await cycle(10, **DRAW)
    # Flame off, the pump off with a hot flow, the room below target, PWM held off
    await cycle(5, **DRAW | {"burngas": "off", "tapwateractive": "off"})
    for _ in range(4):
        line = await cycle(5, **PUMP | {"heatingpump": "off", "curflowtemp": 55.0})

    assert "inside=20.9" in line and "target=21.0" in line
    assert "pwm OFF" in line
    assert "(circulation)" not in line
    assert "heater=OFF" in line
    assert {"cmd": "heatingoff", "value": "off"} not in all_writes
    assert {"cmd": "selflowtemp", "value": 20} not in all_writes
    assert "want setpoint=RELEASE heater=OFF" in line
    assert sat_problems(caplog) == []
