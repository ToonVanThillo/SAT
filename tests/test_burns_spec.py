"""Independent spec checks of burns (plan step 13): field sequence, debounce windows, same-message signals, health, snapshot, options."""
import logging
from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components.climate import HVACMode
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.sat.channels import DesiredState
from custom_components.sat.config_flow import SatOptionsFlowHandler
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HOT_WATER_BURNER_POWER,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    CONF_MQTT_TOPIC,
    CONF_NAME,
    DOMAIN,
    MODE_MQTT_EMS,
    BoilerStatus,
    FlameStatus,
)
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.conftest import TIMELINE, advance, current, flame_transitions
from tests.const import IDLE, with_data

OPTIONS: dict[str, Any] = {CONF_HOT_WATER_BURNER_POWER: 134, CONF_HOT_WATER_FLOW_TEMPERATURE: 45, CONF_MAXIMUM_RELATIVE_MODULATION: 100}
OFF: dict[str, Any] = IDLE | {"burngas": "off", "heatingactive": "off", "tapwateractive": "off", "selburnpow": 0}
HEATING: dict[str, Any] = OFF | {"burngas": "on", "heatingactive": "on", "selflowtemp": 50, "selburnpow": 60}
DRAW: dict[str, Any] = OFF | {"burngas": "on", "tapwateractive": "on"}
HEAT_LINE = "heating (cycles_last_hour=1.0, samples_4h=1)"


@pytest.fixture
async def hw(hass, mqtt_mock, no_publish_delay, caplog) -> SatEmsMqttCoordinator:
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, OPTIONS, config_entry=None)
    await coordinator.async_added_to_hass()
    return coordinator


# Field case: the tap closes before the flame goes out, a control cycle runs in between

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_field_case_a_control_cycle_in_the_tail_sees_hot_water(hass, entry, climate, boiler_data, mqtt_mock, freezer):
    await boiler_data(**OFF)
    await advance(hass, freezer, 10)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # Hot water draw starts (13:05:57), the tap closes (13:06:17), a control cycle runs (13:06:22), the flame goes out (13:06:26)
    await boiler_data(**DRAW)
    await advance(hass, freezer, 20)
    await boiler_data(**DRAW | {"tapwateractive": "off"})
    await advance(hass, freezer, 5)
    await climate.async_control_heating_loop()

    # The cycle sees the burn as hot water, though the raw flag is off, and the burn is not in the heating statistics
    assert current(hass, entry).snapshot().boiler.status == BoilerStatus.HOT_WATER

    await advance(hass, freezer, 4)
    await boiler_data(**OFF)
    assert current(hass, entry).device_status != BoilerStatus.HOT_WATER
    assert current(hass, entry).flame.cycles_last_hour == 0


# Several messages in one debounce window

async def test_two_burns_within_one_debounce_window_are_two_burns(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=1))
    await boiler_data(**OFF)
    freezer.tick(timedelta(seconds=1))
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=1))
    await boiler_data(**OFF)

    assert flame_transitions(caplog) == [
        "flame on", "flame off after 1.0s, heating (cycles_last_hour=1.0, samples_4h=1)",
        "flame on", "flame off after 1.0s, heating (cycles_last_hour=2.0, samples_4h=2)",
    ]
    assert hw.flame.cycles_last_hour == 2


async def test_a_hot_water_burn_then_a_heating_burn_in_one_window_counts_only_the_second(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    freezer.tick(timedelta(seconds=2))
    await boiler_data(**OFF)
    freezer.tick(timedelta(seconds=1))
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=4))
    await boiler_data(**OFF)

    assert flame_transitions(caplog) == [
        "flame on", "flame off after 2.0s, hot water (flag), not counted",
        "flame on", f"flame off after 4.0s, {HEAT_LINE}",
    ]
    assert hw.flame.cycles_last_hour == 1
    assert hw.flame.sample_count_4h == 1
    assert hw.flame.median_on_duration_seconds_4h == 4


async def test_a_heating_burn_then_a_hot_water_burn_counts_only_the_first(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=40))
    await boiler_data(**OFF)
    freezer.tick(timedelta(seconds=3))
    await boiler_data(**DRAW)
    freezer.tick(timedelta(seconds=7))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == "flame off after 7.0s, hot water (flag), not counted"
    assert hw.flame.cycles_last_hour == 1
    assert hw.flame.median_on_duration_seconds_4h == 40
    assert hw.flame.duty_ratio_last_15m == pytest.approx(40 / 900)


# Signals in the same message as the flame transition

@pytest.mark.parametrize("extra, signals", [
    ({"tapwateractive": "on"}, "flag"),
    ({"selburnpow": 134}, "burner_power"),
    ({"selflowtemp": 45}, "flow_temperature"),
])
async def test_a_signal_in_the_message_that_switches_the_flame_on_counts(hw, boiler_data, freezer, caplog, extra, signals):
    await boiler_data(**OFF)
    await boiler_data(**HEATING | extra)
    freezer.tick(timedelta(seconds=1))
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=1))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == f"flame off after 2.0s, hot water ({signals}), not counted"


@pytest.mark.parametrize("extra", [{"tapwateractive": "on"}, {"selburnpow": 134}, {"selflowtemp": 45}])
async def test_a_signal_in_the_message_that_switches_the_flame_off_does_not_count(hw, boiler_data, freezer, caplog, extra):
    # Ambiguous in the spec: "while the flame is on" read as the state after the update
    await boiler_data(**OFF)
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=30))
    await boiler_data(**OFF | extra)

    assert flame_transitions(caplog)[-1] == f"flame off after 30.0s, {HEAT_LINE}"


async def test_repeated_signals_in_one_burn_are_listed_once_and_in_fixed_order(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**HEATING | {"selflowtemp": 45})
    await boiler_data(**HEATING | {"selburnpow": 134})
    await boiler_data(**HEATING | {"tapwateractive": "on"})
    await boiler_data(**HEATING | {"tapwateractive": "on"})
    freezer.tick(timedelta(seconds=5))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == "flame off after 5.0s, hot water (flag, burner_power, flow_temperature), not counted"


# Flow temperature versus the setpoint SAT sent

async def test_the_flow_temperature_signal_holds_when_sat_sent_another_setpoint(hw, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    hw.verify(hw.snapshot())
    await hw.async_submit(DesiredState(control_setpoint=50))

    await boiler_data(**HEATING | {"selflowtemp": 45})
    freezer.tick(timedelta(seconds=10))
    await boiler_data(**OFF)

    assert flame_transitions(caplog)[-1] == "flame off after 10.0s, hot water (flow_temperature), not counted"


# Status and snapshot while a hot-water burn is on

@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}])
async def test_hot_water_status_follows_the_burn_not_the_raw_flag_for_every_signal(hw, boiler_data, freezer, signal):
    await boiler_data(**OFF)
    await boiler_data(**HEATING | signal)
    await boiler_data(**HEATING)

    assert hw.hot_water_active is False
    assert hw.device_status == BoilerStatus.HOT_WATER
    snapshot = hw.snapshot()
    assert snapshot.boiler.hot_water_active is True
    assert snapshot.boiler.status == BoilerStatus.HOT_WATER

    freezer.tick(timedelta(seconds=5))
    await boiler_data(**OFF)

    assert hw.snapshot().boiler.hot_water_active is False
    assert hw.snapshot().boiler.status != BoilerStatus.HOT_WATER


async def test_a_heating_burn_is_not_hot_water(hw, boiler_data):
    await boiler_data(**OFF)
    await boiler_data(**HEATING)

    assert hw.snapshot().boiler.hot_water_active is False
    assert hw.device_status != BoilerStatus.HOT_WATER


async def test_hot_water_stays_until_flame_off_even_after_all_signals_are_gone(hw, boiler_data, freezer):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    await boiler_data(**HEATING)
    freezer.tick(timedelta(seconds=300))
    await boiler_data(**HEATING | {"selflowtemp": 55})

    assert hw.snapshot().boiler.hot_water_active is True


async def test_opentherm_hot_water_tail(otgw, freezer, caplog):
    hass = otgw.hass

    async def message(key: str, value: str) -> None:
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
        await hass.async_block_till_done()

    await message("Tboiler", "50")
    await message("flame", "OFF")
    await message("domestichotwater", "ON")
    await message("flame", "ON")
    await message("domestichotwater", "OFF")

    assert otgw.snapshot().boiler.hot_water_active is True
    assert otgw.device_status == BoilerStatus.HOT_WATER

    await message("flame", "OFF")

    assert otgw.snapshot().boiler.hot_water_active is False
    assert otgw.device_status != BoilerStatus.HOT_WATER


# Flame health

async def test_hot_water_burns_do_not_make_the_flame_unhealthy(hass, hw, boiler_data, freezer):
    await boiler_data(**OFF | {"heatingactive": "on"})
    for _ in range(8):
        await advance(hass, freezer, 60)
        await boiler_data(**DRAW | {"heatingactive": "on"})
        await advance(hass, freezer, 20)
        await boiler_data(**OFF | {"heatingactive": "on"})
        await advance(hass, freezer, 5)

    assert hw.flame.cycles_last_hour == 0
    assert hw.flame.health_status not in (FlameStatus.SHORT_CYCLING, FlameStatus.PWM_SHORT)


async def test_the_same_short_burns_as_heating_are_short_cycling(hass, hw, boiler_data, freezer):
    # Control for the test above: heating burns of the same shape do count
    await boiler_data(**OFF | {"heatingactive": "on"})
    for _ in range(8):
        await advance(hass, freezer, 60)
        await boiler_data(**HEATING)
        await advance(hass, freezer, 20)
        await boiler_data(**OFF | {"heatingactive": "on"})
        await advance(hass, freezer, 5)

    assert hw.flame.cycles_last_hour == 8
    assert hw.flame.health_status == FlameStatus.SHORT_CYCLING


# Options

async def test_the_hot_water_options_accept_the_users_values_and_reject_invalid_ones(hass):
    flow = SatOptionsFlowHandler(MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_MQTT_EMS, CONF_NAME: "Test"}))
    schema = (await flow.async_step_advanced())["data_schema"].schema
    selectors = {str(marker): value for marker, value in schema.items()}

    assert selectors[CONF_HOT_WATER_BURNER_POWER](134) == 134
    assert selectors[CONF_HOT_WATER_FLOW_TEMPERATURE](45) == 45

    for invalid in (0, -5, 1000):
        with pytest.raises(vol.Invalid):
            selectors[CONF_HOT_WATER_BURNER_POWER](invalid)

    for invalid in (0, 5, 500):
        with pytest.raises(vol.Invalid):
            selectors[CONF_HOT_WATER_FLOW_TEMPERATURE](invalid)


@pytest.mark.parametrize("options", [
    {CONF_HOT_WATER_FLOW_TEMPERATURE: 45, CONF_MAXIMUM_RELATIVE_MODULATION: 254},
    {CONF_MAXIMUM_RELATIVE_MODULATION: 254},
    {CONF_HOT_WATER_BURNER_POWER: None, CONF_MAXIMUM_RELATIVE_MODULATION: 254},
])
async def test_no_warning_without_a_burner_power_option(hass, mqtt_mock, no_publish_delay, boiler_data, caplog, options):
    caplog.set_level(logging.INFO)
    coordinator = SatEmsMqttCoordinator(hass, "ems", {CONF_MQTT_TOPIC: "ems-esp"}, options, config_entry=None)
    await coordinator.async_added_to_hass()
    await boiler_data(**OFF)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "burner power" in r.getMessage()]
