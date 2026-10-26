"""Hot water signals (burner power, flow temperature, tap flag) hold back the reliable flow reading also with the flame off, and a pump
that starts in an update with a signal is no pump start.

A real EMS-ESP log: about 10 s before a draw one payload carries the pump off to on, `selburnpow` 0 to 134 (the hot water burner
power) and `selflowtemp` 5 to 0, with the flame still off; only 10 s later do `tapwateractive` and `burngas` go on. After the draw
there is an after-phase of about a minute (`selflowtemp` 45, `selburnpow` 134, flame off). The stagnant 57-58 degrees must never
count as reliable.
"""
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode

from custom_components.sat.channels import DesiredState, Quantity
from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HOT_WATER_BURNER_POWER,
    CONF_HOT_WATER_FLOW_TEMPERATURE,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MINIMUM_SETPOINT,
    BoilerStatus,
    PWMStatus,
)
from custom_components.sat.mqtt.ems import SatEmsMqttCoordinator
from tests.conftest import TIMELINE, run_one_loop
from tests.const import IDLE, with_data
from tests.test_burns import started
from tests.test_mqtt_ems import cycle as channel_cycle

OPTIONS = {CONF_HOT_WATER_BURNER_POWER: 134, CONF_HOT_WATER_FLOW_TEMPERATURE: 45, CONF_MAXIMUM_RELATIVE_MODULATION: 100}

# Quiet: no flame, no tap, no signal; the pump on
QUIET = IDLE | {"heatingpump": "on", "burngas": "off", "tapwateractive": "off", "heatingactive": "off", "selburnpow": 0, "selflowtemp": 5}
PUMP_OFF = {"heatingpump": "off"}
PRE_PHASE = {"heatingpump": "on", "selburnpow": 134, "selflowtemp": 0}
DRAW = {"burngas": "on", "tapwateractive": "on"}
AFTER_PHASE = {"burngas": "off", "tapwateractive": "off", "selflowtemp": 45, "selburnpow": 134}
NO_PUMP_STATE = {key: value for key, value in QUIET.items() if key != "heatingpump"}


@pytest.fixture
async def hw(hass, mqtt_mock, no_publish_delay, caplog) -> AsyncIterator[SatEmsMqttCoordinator]:
    caplog.set_level(logging.INFO, logger=TIMELINE)
    coordinator = await started(hass, OPTIONS)
    yield coordinator
    await coordinator.async_will_remove_from_hass()


# The log scenario

async def test_the_log_scenario_never_makes_the_stagnant_water_reliable(hw, boiler_data, freezer: FrozenDateTimeFactory):
    await boiler_data(**QUIET | {"curflowtemp": 46.4})
    assert hw.reliable_boiler_temperature == 46.4

    await boiler_data(**QUIET | PUMP_OFF | {"curflowtemp": 57.5})
    assert hw.reliable_boiler_temperature == 46.4

    # The pre-phase: one payload with the pump start, the burner power and the flow temperature, the flame still off
    freezer.tick(10)
    await boiler_data(**QUIET | PRE_PHASE | {"curflowtemp": 57.5})
    assert hw.reliable_boiler_temperature == 46.4
    assert hw.device_status != BoilerStatus.PUMP_STARTING

    freezer.tick(10)
    await boiler_data(**QUIET | PRE_PHASE | DRAW | {"curflowtemp": 60.0})
    assert hw.reliable_boiler_temperature == 46.4

    freezer.tick(30)
    await boiler_data(**QUIET | AFTER_PHASE | {"curflowtemp": 58.0})
    assert hw.reliable_boiler_temperature == 46.4

    await boiler_data(**QUIET | AFTER_PHASE | PUMP_OFF | {"curflowtemp": 58.0})
    assert hw.reliable_boiler_temperature == 46.4

    # The after-phase ends with the pump still off
    freezer.tick(60)
    await boiler_data(**QUIET | PUMP_OFF | {"curflowtemp": 57.0})
    assert hw.reliable_boiler_temperature == 46.4

    # The pump runs for heating, no signal: reliable again, and a pump start
    await boiler_data(**QUIET | {"curflowtemp": 50.0, "heatingactive": "on", "selflowtemp": 40})
    assert hw.reliable_boiler_temperature == 50.0
    assert hw.device_status == BoilerStatus.PUMP_STARTING


async def test_the_pre_phase_pump_start_in_one_payload_is_no_pump_start(hw, boiler_data):
    heating = QUIET | {"heatingactive": "on", "selflowtemp": 40}
    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 57.5})
    await boiler_data(**heating | {"selburnpow": 134, "curflowtemp": 57.5})

    assert hw.pump_active is True
    assert hw.device_status != BoilerStatus.PUMP_STARTING


async def test_a_pump_start_without_a_signal_is_a_pump_start(hw, boiler_data):
    heating = QUIET | {"heatingactive": "on", "selflowtemp": 40}
    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 57.5})
    await boiler_data(**heating | {"curflowtemp": 57.5})

    assert hw.device_status == BoilerStatus.PUMP_STARTING


# A signal alone

@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}, {"tapwateractive": "on"}], ids=["burner power", "flow temperature", "tap"])
async def test_a_signal_alone_makes_the_reading_unreliable_while_it_holds(hw, boiler_data, signal):
    await boiler_data(**QUIET | {"curflowtemp": 45.0})
    assert hw.reliable_boiler_temperature == 45.0

    await boiler_data(**QUIET | signal | {"curflowtemp": 55.0})
    assert hw.reliable_boiler_temperature == 45.0
    await boiler_data(**QUIET | signal | {"curflowtemp": 56.0})
    assert hw.reliable_boiler_temperature == 45.0

    # The signal goes, the pump still runs: reliable again at once
    await boiler_data(**QUIET | {"curflowtemp": 57.0})
    assert hw.reliable_boiler_temperature == 57.0


# A pump that starts with a signal present

@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}], ids=["burner power", "flow temperature"])
async def test_a_pump_that_starts_with_a_signal_and_keeps_running_is_reliable_once_the_signal_is_gone(hw, boiler_data, signal):
    await boiler_data(**QUIET | {"curflowtemp": 46.4})
    await boiler_data(**QUIET | PUMP_OFF | {"curflowtemp": 57.5})

    # Off to on in the update that carries the signal: unreliable
    await boiler_data(**QUIET | signal | {"curflowtemp": 57.5})
    assert hw.pump_active is True
    assert hw.reliable_boiler_temperature == 46.4

    # The pump keeps running, the signal is gone: reliable at once, no pump restart needed
    await boiler_data(**QUIET | {"curflowtemp": 57.0})
    assert hw.reliable_boiler_temperature == 57.0


# PUMP_STARTING

@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}], ids=["burner power", "flow temperature"])
async def test_an_off_to_on_with_a_signal_is_no_pump_start_but_a_later_one_without_is(hw, boiler_data, signal):
    heating = QUIET | {"heatingactive": "on", "selflowtemp": 40}
    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 57.5})
    await boiler_data(**heating | signal | {"curflowtemp": 57.5})
    assert hw.device_status != BoilerStatus.PUMP_STARTING

    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 57.5})
    await boiler_data(**heating | {"curflowtemp": 57.5})
    assert hw.device_status == BoilerStatus.PUMP_STARTING


async def test_an_off_to_on_while_sat_produces_hot_water_is_no_pump_start(hw, boiler_data):
    heating = QUIET | {"heatingactive": "on", "selflowtemp": 40}
    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 57.5})
    await boiler_data(**heating | DRAW | {"curflowtemp": 58.0})
    assert hw.device_status == BoilerStatus.HOT_WATER

    # The draw is over, the pump still runs: nothing started it
    await boiler_data(**heating | {"curflowtemp": 58.0})
    assert hw.device_status != BoilerStatus.PUMP_STARTING

    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 58.0})
    await boiler_data(**heating | {"curflowtemp": 58.0})
    assert hw.device_status == BoilerStatus.PUMP_STARTING


async def test_an_unknown_pump_state_between_off_and_on_still_counts_as_a_transition(hw, boiler_data):
    heating = QUIET | {"heatingactive": "on", "selflowtemp": 40}
    await boiler_data(**heating | PUMP_OFF | {"curflowtemp": 57.5})
    await boiler_data(**heating | {"heatingpump": "unknown", "curflowtemp": 57.5})
    assert hw.device_status != BoilerStatus.PUMP_STARTING

    await boiler_data(**heating | {"curflowtemp": 57.5})
    assert hw.device_status == BoilerStatus.PUMP_STARTING


async def test_an_unknown_pump_state_between_two_on_states_is_no_transition(hw, boiler_data):
    heating = QUIET | {"heatingactive": "on", "selflowtemp": 40}
    await boiler_data(**heating | {"curflowtemp": 57.5})
    await boiler_data(**heating | {"heatingpump": "unknown", "curflowtemp": 57.5})
    assert hw.pump_active is None
    assert hw.device_status != BoilerStatus.PUMP_STARTING

    # On, unknown, on: the unknown state did not reset the last known state, so this is no off to on
    await boiler_data(**heating | {"curflowtemp": 57.5})
    assert hw.pump_active is True
    assert hw.device_status != BoilerStatus.PUMP_STARTING


# An unknown pump state

@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}, {"tapwateractive": "on"}], ids=["burner power", "flow temperature", "tap"])
async def test_without_a_pump_state_a_signal_makes_the_reading_unreliable(hw, boiler_data, signal):
    await boiler_data(**NO_PUMP_STATE | {"curflowtemp": 45.0})
    assert hw.pump_active is None

    await boiler_data(**NO_PUMP_STATE | signal | {"curflowtemp": 55.0})
    assert hw.reliable_boiler_temperature == 45.0

    await boiler_data(**NO_PUMP_STATE | {"curflowtemp": 56.0})
    assert hw.reliable_boiler_temperature == 56.0

    # The first live pump on is reliable at once
    await boiler_data(**QUIET | {"curflowtemp": 57.0})
    assert hw.reliable_boiler_temperature == 57.0


# What is no signal

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({}, OPTIONS))
@pytest.mark.parametrize(("sent", "signal"), [(45, False), (40, True)], ids=["sent 45", "sent 40"])
async def test_a_flow_temperature_is_a_signal_unless_it_is_what_sat_sent(entry, coordinator, boiler_data, sent, signal):
    """ACCEPTED LIMITATION: when SAT's last sent control setpoint equals the hot water flow temperature, `selflowtemp` at that value is
    not a signal, so a boiler preparing hot water at that value is not recognised by it."""
    await boiler_data(**QUIET | {"curflowtemp": 45.0})
    assert coordinator.reliable_boiler_temperature == 45.0

    await channel_cycle(coordinator, DesiredState(control_setpoint=sent))
    assert coordinator.channel_states[Quantity.CONTROL_SETPOINT].sent == sent
    await boiler_data(**QUIET | {"selflowtemp": 45, "curflowtemp": 55.0})

    # SAT itself asked for 45: it is the control setpoint, not the boiler preparing hot water
    assert coordinator.reliable_boiler_temperature == (45.0 if signal else 55.0)


@pytest.mark.parametrize("options", [
    {CONF_HOT_WATER_BURNER_POWER: 134, CONF_MAXIMUM_RELATIVE_MODULATION: 140},
    {CONF_HOT_WATER_BURNER_POWER: 134, CONF_MAXIMUM_RELATIVE_MODULATION: 134},
    {CONF_HOT_WATER_BURNER_POWER: 134, CONF_MAXIMUM_RELATIVE_MODULATION: 100, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 134},
    {CONF_MAXIMUM_RELATIVE_MODULATION: 100},
], ids=["max above", "max equal", "floor equal", "unset"])
async def test_the_burner_power_is_no_signal_when_it_cannot_tell_hot_water_apart(hass, mqtt_mock, no_publish_delay, boiler_data, options):
    coordinator = await started(hass, options)
    await boiler_data(**QUIET | {"curflowtemp": 45.0})

    await boiler_data(**QUIET | {"selburnpow": 134, "curflowtemp": 55.0})

    assert coordinator.reliable_boiler_temperature == 55.0
    await coordinator.async_will_remove_from_hass()


async def test_restored_values_are_no_signal(hw, boiler_data):
    await boiler_data(**QUIET | {"curflowtemp": 45.0})
    hw.async_set_updated_data(QUIET | {"selburnpow": 134, "selflowtemp": 45, "curflowtemp": 50.0}, restored=True)

    await boiler_data(heatingpump="on", curflowtemp=52.0)

    assert hw.reliable_boiler_temperature == 52.0


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({}, {}))
async def test_without_configured_signals_the_pump_overrun_after_a_draw_is_reliable_at_once(entry, coordinator, boiler_data):
    """ACCEPTED LIMITATION: without a configured hot water burner power or flow temperature, nothing but the tap flag and the flame tells a
    hot water burn; the pump overrun after a draw (flame and flag off, pump on) is reliable at once, stagnant water included."""
    await boiler_data(**QUIET | {"curflowtemp": 45.0})
    await boiler_data(**QUIET | DRAW | {"curflowtemp": 60.0})
    assert coordinator.reliable_boiler_temperature == 45.0

    await boiler_data(**QUIET | {"burngas": "off", "tapwateractive": "off", "curflowtemp": 58.0})

    assert coordinator.reliable_boiler_temperature == 58.0


# Unchanged: burns, flame statistics, hot water status

async def test_flame_off_signals_open_no_burn_and_change_no_statistics(hw, boiler_data, caplog, freezer: FrozenDateTimeFactory):
    await boiler_data(**QUIET | {"curflowtemp": 45.0})
    caplog.clear()

    for _ in range(3):
        freezer.tick(10)
        await boiler_data(**QUIET | PRE_PHASE | {"curflowtemp": 57.5})
    await boiler_data(**QUIET | AFTER_PHASE | {"curflowtemp": 57.5})

    assert not [r for r in caplog.records if r.name == TIMELINE and r.getMessage().startswith("flame ")]
    assert not hw.flame.is_active
    assert hw.flame.last_cycle_duration_seconds is None
    assert hw.flame.seconds_since_hot_water_burn is None


async def test_the_burn_after_the_pre_phase_is_still_classified_by_its_signals(hw, boiler_data, caplog, freezer: FrozenDateTimeFactory):
    await boiler_data(**QUIET | PRE_PHASE | {"curflowtemp": 57.5})
    freezer.tick(10)
    await boiler_data(**QUIET | PRE_PHASE | DRAW | {"curflowtemp": 60.0})
    freezer.tick(30)
    await boiler_data(**QUIET | AFTER_PHASE | {"curflowtemp": 58.0})

    messages = [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.getMessage().startswith("flame ")]
    assert messages[0] == "flame on"
    assert messages[1].startswith("flame off after 30.0s, hot water")
    assert hw.flame.seconds_since_hot_water_burn is not None


@pytest.mark.parametrize("signal", [{"selburnpow": 134}, {"selflowtemp": 45}], ids=["burner power", "flow temperature"])
async def test_a_flame_off_signal_does_not_make_the_boiler_produce_hot_water(hw, boiler_data, signal):
    await boiler_data(**QUIET | {"curflowtemp": 45.0, "heatingactive": "on"})
    await boiler_data(**QUIET | signal | {"curflowtemp": 55.0, "heatingactive": "on"})

    assert not hw.boiler.hot_water_active
    assert not hw.producing_hot_water
    assert hw.device_status != BoilerStatus.HOT_WATER


# At climate level

HW_ENTRY = with_data({CONF_MINIMUM_SETPOINT: 30}, OPTIONS | {CONF_FORCE_PULSE_WIDTH_MODULATION: True})


def cycle_line(new: list[str]) -> str:
    return next(m for m in reversed(new) if m.startswith("cycle "))


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*HW_ENTRY)
async def test_the_cycle_lines_show_the_last_reliable_reading_through_the_log_scenario(hass, entry, climate, boiler_data, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)

    async def cycle(seconds: float, **data: Any) -> str:
        mark = len(caplog.records)
        freezer.tick(seconds)
        await boiler_data(**data)
        return cycle_line(await run_one_loop(hass, climate, caplog, mark))

    await boiler_data(**QUIET | {"curflowtemp": 46.4, "heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await cycle(10, **QUIET | {"curflowtemp": 46.4})

    lines = [
        await cycle(10, **QUIET | PUMP_OFF | {"curflowtemp": 57.5}),
        await cycle(10, **QUIET | PRE_PHASE | {"curflowtemp": 57.5}),
        await cycle(10, **QUIET | PRE_PHASE | DRAW | {"curflowtemp": 60.0}),
        await cycle(30, **QUIET | AFTER_PHASE | {"curflowtemp": 58.0}),
        await cycle(10, **QUIET | AFTER_PHASE | PUMP_OFF | {"curflowtemp": 58.0}),
        await cycle(10, **QUIET | PUMP_OFF | {"curflowtemp": 57.0}),
    ]

    assert all("reliable=46.4" in line for line in lines)

    line = await cycle(10, **QUIET | {"curflowtemp": 50.0})
    assert "reliable=" not in line
    assert "flow=50.0" in line


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*HW_ENTRY)
async def test_a_flame_off_signal_does_not_hold_pwm(hass, entry, climate, boiler_data, freezer, caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    await boiler_data(**QUIET | {"curflowtemp": 40.0, "heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    mark = len(caplog.records)
    freezer.tick(10)
    await boiler_data(**QUIET | {"selburnpow": 134, "curflowtemp": 40.0, "heatingoff": "on"})
    line = cycle_line(await run_one_loop(hass, climate, caplog, mark))

    assert "hw=off" in line
    assert "hold=" not in line
    assert climate.pwm.status == PWMStatus.ON

