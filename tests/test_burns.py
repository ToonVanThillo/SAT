"""Burns classified as hot water or heating, at the MQTT boundary (plan step 13).

A burn lasts from flame on to flame off. It is hot water if a hot-water signal is seen while it is on, else heating; flame
statistics count heating burns only. Burns are tracked from device data as it arrives, live values only.
"""
import asyncio
import json
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pyotgw.vars import BOILER, DATA_SLAVE_FLAME_ON
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.sat.channels import DesiredState, Quantity
from custom_components.sat.const import (
    CONF_HOT_WATER_BURNER_POWER,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MQTT_TOPIC,
    BoilerStatus,
)
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from custom_components.sat.serial import SatSerialCoordinator
from tests.conftest import TIMELINE, advance, flame_transitions, listen, settle
from tests.const import IDLE, STORE_KEY

OPTIONS: dict[str, Any] = {CONF_HOT_WATER_BURNER_POWER: 134, CONF_HOT_WATER_FLOW_TEMPERATURE: 45, CONF_MAXIMUM_RELATIVE_MODULATION: 100}

# Flame off, no hot-water signal
OFF: dict[str, Any] = IDLE | {"burngas": "off", "heatingactive": "off", "tapwateractive": "off", "selburnpow": 0}
# Heating: a setpoint other than the hot water flow temperature, a burner power below the hot water burner power
HEATING: dict[str, Any] = OFF | {"burngas": "on", "heatingactive": "on", "selflowtemp": 50, "selburnpow": 60}
# A tap draw as the boiler reports it at its start
DRAW: dict[str, Any] = OFF | {"burngas": "on", "tapwateractive": "on"}


async def started(hass: HomeAssistant, options: dict[str, Any]) -> SatEmsMqttCoordinator:
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, options, config_entry=None)
    await coordinator.async_added_to_hass()
    return coordinator


@pytest.fixture
async def hw(hass, mqtt_mock, no_publish_delay, caplog) -> SatEmsMqttCoordinator:
    """A started EMS coordinator with both hot water options set, logging the timeline."""
    caplog.set_level(logging.INFO, logger=TIMELINE)
    return await started(hass, OPTIONS)


async def burn(boiler_data, freezer: FrozenDateTimeFactory, on: dict[str, Any], seconds: float) -> None:
    """Report the flame on with `on`, then off `seconds` later, without firing the listener debounce."""
    await boiler_data(**on)
    freezer.tick(timedelta(seconds=seconds))
    await boiler_data(**OFF)


# Classification

async def test_a_hot_water_tail_keeps_the_whole_burn_hot_water(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    freezer.tick(timedelta(seconds=20))

    # The tap closes before the flame goes out
    await boiler_data(**DRAW | {"tapwateractive": "off"})

    assert hw.hot_water_active is False
    assert hw.boiler.hot_water_active is True
    assert hw.device_status == BoilerStatus.HOT_WATER
    assert hw.snapshot().boiler.status == BoilerStatus.HOT_WATER

    freezer.tick(timedelta(seconds=9))
    await boiler_data(**OFF)

    assert hw.boiler.hot_water_active is False
    assert hw.device_status != BoilerStatus.HOT_WATER
    assert flame_transitions(caplog) == ["flame on", "flame off after 29.0s, hot water (flag), not counted"]
    assert hw.flame.cycles_last_hour == 0
    assert hw.flame.sample_count_4h == 0


@pytest.mark.parametrize("draw, signals", [
    ({"tapwateractive": "on"}, "flag"),
    ({"selburnpow": 134}, "burner_power"),
    ({"selflowtemp": 45}, "flow_temperature"),
    ({"tapwateractive": "on", "selburnpow": 134, "selflowtemp": 45}, "flag, burner_power, flow_temperature"),
])
async def test_a_short_draw_seen_by_any_signal_is_hot_water(hw, boiler_data, freezer, caplog, draw, signals):
    await boiler_data(**OFF)
    await burn(boiler_data, freezer, OFF | {"burngas": "on"} | draw, 15)

    assert flame_transitions(caplog) == ["flame on", f"flame off after 15.0s, hot water ({signals}), not counted"]
    assert hw.flame.cycles_last_hour == 0


async def test_a_signal_seen_later_in_the_burn_makes_it_hot_water(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=10))
    await boiler_data(**HEATING | {"selburnpow": 134})
    freezer.tick(timedelta(seconds=10))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == "flame off after 20.0s, hot water (burner_power), not counted"
    assert hw.flame.cycles_last_hour == 0
    assert hw.flame.duty_ratio_last_15m == 0


async def test_signals_while_the_flame_is_off_do_not_classify_the_next_burn(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF | {"tapwateractive": "on", "selburnpow": 134, "selflowtemp": 45})
    await burn(boiler_data, freezer, HEATING, 60)

    assert flame_transitions(caplog)[-1] == "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)"


async def test_without_the_options_only_the_flag_is_a_signal(hass, mqtt_mock, no_publish_delay, boiler_data, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await started(hass, {})

    await boiler_data(**OFF)
    await burn(boiler_data, freezer, HEATING | {"selburnpow": 134, "selflowtemp": 45}, 60)

    assert flame_transitions(caplog)[-1] == "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)"
    assert coordinator.flame.cycles_last_hour == 1


async def test_the_flow_temperature_sat_sent_is_not_hot_water(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    hw.verify(hw.snapshot())
    await hw.async_submit(DesiredState(control_setpoint=45))
    assert hw.channel_states[Quantity.CONTROL_SETPOINT].sent == 45

    await burn(boiler_data, freezer, HEATING | {"selflowtemp": 45}, 60)

    assert flame_transitions(caplog)[-1] == "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)"


async def test_the_flow_temperature_sat_is_sending_is_not_hot_water(hw, boiler_data, mqtt_mock, freezer, caplog):
    await boiler_data(**OFF)
    hw.verify(hw.snapshot())
    await boiler_data(**HEATING)

    # The boiler echoes the new setpoint before the publish returns, so before the channel records it as sent
    mqtt_mock.async_publish.side_effect = lambda *_args, **_kwargs: hw.async_set_updated_data({"selflowtemp": 45})
    await hw.async_submit(DesiredState(control_setpoint=45))
    mqtt_mock.async_publish.side_effect = None

    freezer.tick(timedelta(seconds=60))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)"


async def test_the_flow_temperatures_of_overlapping_writes_are_not_hot_water(hass, hw, boiler_data, mqtt_mock, freezer, caplog):
    await boiler_data(**OFF)
    hw.verify(hw.snapshot())
    await boiler_data(**HEATING)

    # Each publish of a setpoint waits until its gate opens
    gates = {45: asyncio.Event(), 50: asyncio.Event()}

    async def gated(*args: Any, **_kwargs: Any) -> None:
        await gates[json.loads(args[1])["value"]].wait()

    mqtt_mock.async_publish.side_effect = gated
    first = asyncio.create_task(hw.async_submit(DesiredState(control_setpoint=45)))
    await settle()
    second = asyncio.create_task(hw.async_submit(DesiredState(control_setpoint=50)))
    await settle()

    # The later write completes first; the boiler then echoes the earlier one, still being published
    gates[50].set()
    await second
    await boiler_data(**HEATING | {"selflowtemp": 45})
    gates[45].set()
    await first
    mqtt_mock.async_publish.side_effect = None

    freezer.tick(timedelta(seconds=60))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)"


# Tracked on arrival

@pytest.mark.parametrize("draw, kind", [
    ({}, "heating (cycles_last_hour=1.0, samples_4h=1)"),
    ({"tapwateractive": "on"}, "hot water (flag), not counted"),
])
async def test_a_burn_shorter_than_the_debounce_is_seen_and_classified(hw, boiler_data, freezer, caplog, draw, kind):
    notified = listen(hw)
    await boiler_data(**OFF)
    await burn(boiler_data, freezer, HEATING | draw, 3)

    notified.assert_not_called()
    assert flame_transitions(caplog) == ["flame on", f"flame off after 3.0s, {kind}"]


async def test_restored_values_are_not_signals(hass_storage, hw, boiler_data, freezer, caplog):
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"burngas": "off", "tapwateractive": "on", "selburnpow": 134, "selflowtemp": 45}}
    await hw.async_setup()

    await boiler_data(burngas="off")
    await burn(boiler_data, freezer, {"burngas": "on"}, 60)

    assert flame_transitions(caplog)[-1] == "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)"


async def test_opentherm_restored_flag_is_not_a_signal_but_a_live_one_is(otgw, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    hass = otgw.hass

    async def message(key: str, value: str) -> None:
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
        await hass.async_block_till_done()

    otgw.async_set_updated_data({"domestichotwater": "ON"}, restored=True)
    await message("flame", "OFF")
    await message("flame", "ON")
    freezer.tick(timedelta(seconds=60))
    await message("flame", "OFF")

    await message("domestichotwater", "ON")
    await message("flame", "ON")
    freezer.tick(timedelta(seconds=30))
    await message("flame", "OFF")

    assert flame_transitions(caplog) == [
        "flame on",
        "flame off after 60.0s, heating (cycles_last_hour=1.0, samples_4h=1)",
        "flame on",
        "flame off after 30.0s, hot water (flag), not counted",
    ]


async def test_a_burn_running_at_startup_through_serial_is_partial(hass, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    gateway = AsyncMock(subscribe=MagicMock(), unsubscribe=MagicMock())

    # The coordinator stores the gateway defaults on creation, which have no flame value
    with patch("custom_components.sat.serial.OpenThermGateway", return_value=gateway):
        coordinator = SatSerialCoordinator(hass, "/dev/ttyUSB0", {}, config_entry=None)

    publish = gateway.subscribe.call_args.args[0]
    await publish({BOILER: {DATA_SLAVE_FLAME_ON: True}})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=60))
    await publish({BOILER: {DATA_SLAVE_FLAME_ON: False}})
    await hass.async_block_till_done()

    assert flame_transitions(caplog) == ["flame off, heating (burn started before the first observation, not counted)"]
    assert coordinator.flame.cycles_last_hour == 0
    await coordinator.async_will_remove_from_hass()


async def test_a_burn_running_at_startup_through_opentherm_mqtt_is_partial(otgw, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    hass = otgw.hass

    async def message(key: str, value: str) -> None:
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
        await hass.async_block_till_done()

    # Another value arrives before the flame
    await message("Tboiler", "45")
    await message("flame", "ON")
    freezer.tick(timedelta(seconds=60))
    await message("flame", "OFF")

    assert flame_transitions(caplog) == ["flame off, heating (burn started before the first observation, not counted)"]
    assert otgw.flame.cycles_last_hour == 0


# The burner power SAT may write itself

@pytest.mark.parametrize("maximum, floor, used", [(133, 0, True), (134, 0, False), (200, 0, False), (100, 134, False)])
async def test_a_hot_water_burner_power_sat_may_write_is_not_used(hass, mqtt_mock, no_publish_delay, boiler_data, freezer, caplog, maximum, floor, used):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await started(hass, {CONF_HOT_WATER_BURNER_POWER: 134, CONF_MAXIMUM_RELATIVE_MODULATION: maximum, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: floor})

    await boiler_data(**OFF)
    await burn(boiler_data, freezer, HEATING | {"selburnpow": 134}, 30)
    await burn(boiler_data, freezer, HEATING | {"selburnpow": 134}, 30)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING and "hot water burner power" in record.getMessage()]
    assert len(warnings) == (0 if used else 1)
    assert coordinator.flame.cycles_last_hour == (0 if used else 2)


# Statistics

async def test_flame_statistics_count_heating_burns_only(hw, boiler_data, freezer):
    await boiler_data(**OFF)
    await burn(boiler_data, freezer, HEATING, 120)
    freezer.tick(timedelta(seconds=60))
    await burn(boiler_data, freezer, DRAW, 300)
    freezer.tick(timedelta(seconds=60))
    await burn(boiler_data, freezer, HEATING | {"selflowtemp": 45}, 30)

    assert hw.flame.cycles_last_hour == 1
    assert hw.flame.sample_count_4h == 1
    assert hw.flame.median_on_duration_seconds_4h == 120
    assert hw.flame.duty_ratio_last_15m == pytest.approx(120 / 900)


async def test_the_average_on_time_skips_hot_water_burns(hass, hw, boiler_data, freezer):
    async def on_for_two_updates(on: dict[str, Any]) -> None:
        await boiler_data(**on)
        await advance(hass, freezer, 10)
        await boiler_data(**on | {"curflowtemp": 50})
        await advance(hass, freezer, 10)

    await boiler_data(**OFF)
    await advance(hass, freezer, 10)

    # A completed heating burn starts the average with the next heating burn
    await burn(boiler_data, freezer, HEATING, 60)
    await advance(hass, freezer, 10)

    await on_for_two_updates(DRAW)
    assert hw.flame.average_on_time_seconds is None
    await boiler_data(**OFF)
    await advance(hass, freezer, 10)

    await on_for_two_updates(HEATING)
    assert hw.flame.average_on_time_seconds == 20


async def test_a_heating_burn_that_turns_hot_water_takes_back_its_statistics(hass, hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await advance(hass, freezer, 10)
    await burn(boiler_data, freezer, HEATING, 60)
    await advance(hass, freezer, 10)

    await boiler_data(**HEATING)
    await advance(hass, freezer, 10)
    await boiler_data(**HEATING | {"curflowtemp": 50})
    await advance(hass, freezer, 10)
    assert hw.flame.average_on_time_seconds == 20
    assert hw.flame.duty_ratio_last_15m == pytest.approx(80 / 900)

    await boiler_data(**HEATING | {"curflowtemp": 50, "tapwateractive": "on"})

    assert hw.flame.average_on_time_seconds is None
    assert hw.flame.duty_ratio_last_15m == pytest.approx(60 / 900)
    assert hw.device_status == BoilerStatus.HOT_WATER
