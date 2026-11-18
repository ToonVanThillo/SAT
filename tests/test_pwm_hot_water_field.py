"""Independent boundary tests for PWM and hot water (plan step 14): EMS-ESP data in; PWM status, timeline and flame properties out."""
import logging
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_HOT_WATER_DELAY,
    CONF_MINIMUM_SETPOINT,
    DOMAIN,
    PWMStatus,
)
from custom_components.sat.pwm import PWM
from tests.conftest import (
    TIMELINE,
    current,
    cycle_records,
    flame_transitions,
    last_cycle,
)
from tests.const import STORE_KEY, with_data
from tests.test_burns import DRAW, HEATING, OFF, OPTIONS, burn, started
from tests.test_pwm_cycles import CURVE, Clock, boiler, config, setpoint_for
from tests.test_pwm_hot_water import flame

TAIL = DRAW | {"tapwateractive": "off"}


def entry_with(delay: float | None):
    options: dict = {CONF_ALLOW_UNRESTRICTED_MODULATION: False}
    if delay is not None:
        options[CONF_HOT_WATER_DELAY] = delay

    return with_data({CONF_MINIMUM_SETPOINT: 30}, options)


async def heat(climate) -> None:
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)


@pytest.fixture(autouse=True)
def debug_timeline(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(300))
async def test_a_hot_water_burn_with_a_tail_holds_pwm_until_300_s_after_flame_off(hass, entry, climate, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    await heat(climate)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["pwm"].hold == "hot water"

    # The tap closes, the flame is still on: still producing hot water
    freezer.tick(20)
    await boiler_data(**TAIL)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["pwm"].hold == "hot water"

    freezer.tick(10)
    await boiler_data(**OFF)
    await climate.async_control_heating_loop()
    assert last_cycle(caplog)["pwm"].hold == "hot water delay"

    freezer.tick(299)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["pwm"].hold == "hot water delay"

    freezer.tick(1)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON
    assert last_cycle(caplog)["pwm"].hold is None


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(None))
async def test_without_a_delay_a_pulse_starts_right_after_flame_off_but_not_in_the_tail(hass, entry, climate, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    await heat(climate)
    await climate.async_control_heating_loop()
    freezer.tick(20)
    await boiler_data(**TAIL)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF

    freezer.tick(10)
    await boiler_data(**OFF)
    await climate.async_control_heating_loop()

    assert climate.pwm.status == PWMStatus.ON


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(300))
async def test_hot_water_during_a_pulse_does_not_end_or_hold_it(hass, entry, climate, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await heat(climate)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON

    freezer.tick(5)
    await boiler_data(**DRAW)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON
    assert last_cycle(caplog)["pwm"].hold is None

    freezer.tick(60)
    await boiler_data(**OFF)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(300))
async def test_the_delay_counts_from_the_last_of_two_hot_water_burns(hass, entry, climate, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    freezer.tick(30)
    await boiler_data(**OFF)
    await heat(climate)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF

    freezer.tick(200)
    await boiler_data(**DRAW)
    freezer.tick(30)
    await boiler_data(**OFF)

    # 230 s after the first burn, 0 s after the second
    freezer.tick(299)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF
    assert last_cycle(caplog)["pwm"].hold == "hot water delay"

    freezer.tick(1)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(300))
async def test_a_heating_burn_does_not_restart_the_delay(hass, entry, climate, boiler_data, freezer):
    await boiler_data(**OFF)
    await burn(boiler_data, freezer, DRAW, 30)
    freezer.tick(250)
    await burn(boiler_data, freezer, HEATING, 10)
    await heat(climate)
    freezer.tick(40)
    await climate.async_control_heating_loop()

    # 300 s after the hot water burn ended, 40 s after a heating burn
    assert climate.pwm.status == PWMStatus.ON


# Last burn duration and partial burns

async def test_the_last_burn_duration_follows_heating_burns_only(hass, mqtt_mock, no_publish_delay, boiler_data, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await started(hass, OPTIONS)

    # A burn running at the first observation is partial
    await boiler_data(**HEATING)
    freezer.tick(50)
    await boiler_data(**OFF)
    assert coordinator.flame.last_cycle_duration_seconds is None

    await burn(boiler_data, freezer, HEATING, 120)
    assert coordinator.flame.last_cycle_duration_seconds == 120

    freezer.tick(10)
    await burn(boiler_data, freezer, DRAW, 30)
    assert coordinator.flame.last_cycle_duration_seconds == 120

    await burn(boiler_data, freezer, HEATING, 45)
    assert coordinator.flame.last_cycle_duration_seconds == 45
    await coordinator.async_will_remove_from_hass()


async def test_a_partial_hot_water_burn_starts_the_delay_clock(hass, mqtt_mock, no_publish_delay, boiler_data, freezer):
    coordinator = await started(hass, OPTIONS)

    await boiler_data(**DRAW)
    freezer.tick(30)
    await boiler_data(**OFF)
    freezer.tick(20)

    assert coordinator.flame.seconds_since_hot_water_burn == 20
    assert coordinator.flame.last_cycle_duration_seconds is None
    await coordinator.async_will_remove_from_hass()


async def test_a_stored_flame_on_does_not_seed_the_ems_flame(hass, mqtt_mock, no_publish_delay, hass_storage, boiler_data, freezer, caplog):
    caplog.set_level(logging.INFO, logger=TIMELINE)
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"burngas": "on"}}
    coordinator = await started(hass, OPTIONS)
    freezer.tick(60)
    await coordinator.async_control_heating_loop()
    freezer.tick(60)

    # Live flame on arrives late, then off
    await boiler_data(**HEATING)
    freezer.tick(60)
    await boiler_data(**OFF)

    assert flame_transitions(caplog) == ["flame off, heating (burn started before the first observation, not counted)"]
    await coordinator.async_will_remove_from_hass()


# PWM unit level: the hold below the minimum duty

async def test_a_hold_with_a_duty_below_the_minimum_never_starts_a_pulse():
    clock = Clock()
    with patch("custom_components.sat.pwm.monotonic", clock):
        CURVE.value = 30.0
        pwm = PWM(config(), CURVE, hot_water_delay=300)  # type: ignore[arg-type]
        pwm.update_cycles_per_hour(20)
        for duty in (0.01, 0.01):
            await pwm.update(boiler(hot_water=True), flame(), setpoint_for(duty), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]
            clock.tick(1000)

        # Hold ends, the duty is still too low: nothing starts
        await pwm.update(boiler(), flame(since=400), setpoint_for(0.01), minimum_setpoint=10.0, allow_unrestricted=False)  # type: ignore[arg-type]

    assert pwm.status != PWMStatus.ON


# Options flow

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(None))
async def test_the_hot_water_delay_round_trips_through_the_options_flow_and_takes_effect(hass, entry, boiler_data, freezer, caplog):

    assert entry.options.get(CONF_HOT_WATER_DELAY, 0) == 0
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "system_configuration"})
    schema = result["data_schema"].schema
    defaults = {str(k): k.default() for k in schema if hasattr(k, "default") and callable(k.default)}
    assert defaults[CONF_HOT_WATER_DELAY] == 0

    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults | {CONF_HOT_WATER_DELAY: 300})
    await hass.async_block_till_done()
    assert result["type"].value == "create_entry"
    assert entry.options[CONF_HOT_WATER_DELAY] == 300

    # Reopened, the stored value is shown
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "system_configuration"})
    assert next(k for k in result["data_schema"].schema if k == CONF_HOT_WATER_DELAY).default() == 300
    hass.config_entries.options.async_abort(result["flow_id"])

    # The reloaded entry holds PWM for the delay
    climate = hass.data[DOMAIN][entry.entry_id]["climate"]
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    freezer.tick(30)
    await boiler_data(**OFF)
    await heat(climate)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.OFF
    assert current(hass, entry) is not None
    freezer.tick(300)
    await climate.async_control_heating_loop()
    assert climate.pwm.status == PWMStatus.ON


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*entry_with(300))
async def test_the_cycle_records_show_the_hold_sequence_of_the_field_scenario(hass, entry, climate, boiler_data, freezer, caplog):
    await boiler_data(**OFF)
    await boiler_data(**DRAW)
    await heat(climate)
    await climate.async_control_heating_loop()
    await climate.async_control_heating_loop()
    freezer.tick(20)
    await boiler_data(**TAIL)
    await climate.async_control_heating_loop()
    freezer.tick(10)
    await boiler_data(**OFF)
    await climate.async_control_heating_loop()
    freezer.tick(299)
    await climate.async_control_heating_loop()
    freezer.tick(1)
    await climate.async_control_heating_loop()

    holds = [record.fields["pwm"].hold for record in cycle_records(caplog)]  # type: ignore[attr-defined]
    sequence = [hold for index, hold in enumerate(holds) if index == 0 or hold != holds[index - 1]]
    assert sequence == ["hot water", "hot water delay", None]
    assert climate.pwm.status == PWMStatus.ON
    assert flame_transitions(caplog) == ["flame on", "flame off after 30.0s, hot water (flag), not counted"]
