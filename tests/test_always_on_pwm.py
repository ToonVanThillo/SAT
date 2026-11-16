"""Always-on PWM (overhaul plan step 6): in HEAT with a valve open the climate always runs PWM and decides from its status and mode.

PWM OFF requests heater OFF at the minimum setpoint of 10 °C, released once heater OFF is confirmed; ON
with MINIMUM requests the effective minimum setpoint and the minimum max relative modulation; ON with UNRESTRICTED requests the calculated
setpoint and the configured maximum. Unrestricted modulation needs setpoint management and the option allowing it. With the default inputs the
calculated setpoint is 35.
"""
import json
import logging
from collections.abc import Awaitable
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_HOT_WATER_DELAY,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MINIMUM_SETPOINT,
    CONF_MODE,
    MODE_FAKE,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.fake import SatFakeConfig
from tests.conftest import TIMELINE, run_one_loop
from tests.const import IDLE, otgw_entry, with_data
from tests.test_climate_loop import message, run_loop
from tests.test_climate_loop_ems import OUTSIDE
from tests.test_heater_off_release import HEATER_OFF, RELEASED, step
from tests.test_reliable_flow_temperature import boiler_state, warmed_minimum_setpoint

HEATER_ON = message('{"cmd": "heatingoff", "value": "off"}')
# The boiler burning with heating enabled, as after a pulse start
BURNING = {"heatingoff": "off", "burngas": "on", "heatingactive": "on"}


@pytest.fixture(autouse=True)
def timeline_levels(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


def entries(*specs: tuple[str, dict, dict]) -> tuple[str, list]:
    """The EMS entry parametrization with one row per (id, data, options)."""
    names = with_data({})[0]
    return names, [pytest.param(*with_data(data, options)[1][0], id=name) for name, data, options in specs]


def selflowtemp(value: float) -> tuple:
    return message(f'{{"cmd": "selflowtemp", "value": {round(value)}}}')


def burnmaxpower(value: int) -> tuple:
    return message(f'{{"cmd": "burnmaxpower", "value": {value}}}')


def heater_writes(calls: list[tuple]) -> list[tuple]:
    return [call for call in calls if '"heatingoff"' in call[1]]


def timeline_info(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno == logging.INFO]


def last_cycle_line(caplog) -> str:
    return [r.getMessage() for r in caplog.get_records("call") if r.name == TIMELINE and r.levelno == logging.DEBUG][-1]


def burnmaxpower_writes(calls: list[tuple]) -> list[tuple]:
    return [call for call in calls if '"burnmaxpower"' in call[1]]


def echo(published) -> dict[str, Any]:
    """The boiler data that reads back the setpoint, modulation and heating switch published last."""
    commands = [json.loads(payload) for _topic, payload, *_ in published()]
    return {command["cmd"]: command["value"] for command in commands if command["cmd"] in ("selflowtemp", "burnmaxpower", "heatingoff")}


async def one_cycle(hass: HomeAssistant, climate: SatClimate, mqtt_mock, published, freezer: FrozenDateTimeFactory, caplog, feed: Awaitable[None]) -> list[tuple]:
    """29 s later, feed the read-backs and make sure exactly one cycle runs (the debounced one if it ran); returns what it published."""
    freezer.tick(29)
    mqtt_mock.async_publish.reset_mock()
    mark = len(caplog.records)
    await feed
    await run_one_loop(hass, climate, caplog, mark)
    return published()


async def burn(hass: HomeAssistant, climate: SatClimate, boiler_data, mqtt_mock, published, freezer: FrozenDateTimeFactory, **data) -> list[tuple]:
    """One cycle with the boiler burning and reading back what the previous cycle published; returns what the cycle published."""
    readback = echo(published)
    await hass.async_block_till_done()
    return await step(climate, boiler_data, mqtt_mock, published, freezer, **BURNING | readback | data)


@pytest.mark.usefixtures("loop_entry")
class TestDecideAtTheEmsBoundary:

    # (a) ON with MINIMUM: the minimum setpoint, and the minimum max relative modulation raised to the configured floor

    @pytest.mark.parametrize(*entries(
        ("unrestricted-disallowed", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}),
        ("unrestricted-disallowed-floor", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 20}),
        ("calculated-below-minimum", {CONF_MINIMUM_SETPOINT: 42}, {}),
    ))
    async def test_on_minimum_requests_the_minimum_setpoint_and_minimum_modulation(self, entry, options, climate, boiler_data, mqtt_mock, published, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        minimum = entry.data[CONF_MINIMUM_SETPOINT]
        floor = options.get(CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR, 0)

        assert await run_loop(climate, mqtt_mock, published) == [selflowtemp(minimum), burnmaxpower(floor), HEATER_ON]
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.MINIMUM)
        assert "(pwm on minimum)" in last_cycle_line(caplog)

    # (b) ON with UNRESTRICTED: the calculated setpoint and the configured maximum

    @pytest.mark.parametrize(*entries(("unrestricted", {CONF_MINIMUM_SETPOINT: 30}, {CONF_MAXIMUM_RELATIVE_MODULATION: 80})))
    async def test_on_unrestricted_requests_the_calculated_setpoint_and_the_maximum(self, entry, climate, boiler_data, mqtt_mock, published, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})

        assert await run_loop(climate, mqtt_mock, published) == [selflowtemp(35), burnmaxpower(80), HEATER_ON]
        assert climate.setpoint == climate.calculated_setpoint == 35.0
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)
        assert "(pwm on unrestricted)" in last_cycle_line(caplog)

    # (c) OFF: heater OFF at the 10 °C minimum (sent as 0 on EMS-ESP), released once heater OFF is confirmed

    @pytest.mark.parametrize(*entries(("unrestricted-disallowed", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False})))
    async def test_the_off_phase_turns_the_heater_off_then_releases(self, hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)
        assert climate.pwm.status == PWMStatus.ON

        # The pulse ends within the period of 3600 / 2 s at 9.9 °C outside
        for _ in range(80):
            calls = await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**BURNING | echo(published)))
            if climate.pwm.status != PWMStatus.ON:
                break
        assert climate.pwm.status == PWMStatus.OFF
        assert calls == [selflowtemp(0), HEATER_OFF]
        assert "(pwm off)" in last_cycle_line(caplog)

        off = {"heatingoff": "on", "selflowtemp": 5, "burngas": "off", "heatingactive": "off"}
        calls = await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**off))
        assert [call for call in calls if '"selflowtemp"' in call[1]] == []
        assert heater_writes(calls) == []
        assert climate.setpoint == 10
        assert RELEASED in timeline_info(caplog)

    # (c) Without a flow temperature PWM keeps running: a running pulse continues and the heater stays on

    @pytest.mark.parametrize(*entries(("unrestricted", {CONF_MINIMUM_SETPOINT: 30}, {})))
    async def test_a_missing_flow_temperature_keeps_a_running_pulse(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)
        assert climate.pwm.status == PWMStatus.ON

        calls = await burn(hass, climate, boiler_data, mqtt_mock, published, freezer, curflowtemp=None)

        # The cycle ran without a flow temperature
        assert coordinator._snapshot.boiler.reliable_flow_temperature is None

        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)
        assert heater_writes(calls) == []
        assert climate.setpoint == 35.0
        assert "(pwm on unrestricted)" in last_cycle_line(caplog)

    # (d) MINIMUM -> UNRESTRICTED during a burn: the setpoint and modulation change in place, the heater stays on

    @pytest.mark.parametrize(*entries(("minimum-36", {CONF_MINIMUM_SETPOINT: 36}, {})))
    async def test_minimum_to_unrestricted_during_a_burn_changes_in_place(self, hass, entry, climate, boiler_data, mqtt_mock, published, freezer):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        assert await run_loop(climate, mqtt_mock, published) == [selflowtemp(36), burnmaxpower(0), HEATER_ON]
        assert climate.pwm.mode == ModulationMode.MINIMUM
        assert heater_writes(await burn(hass, climate, boiler_data, mqtt_mock, published, freezer)) == []

        # A colder outside raises the calculated setpoint from 35 to 37.2, above the minimum of 36
        hass.states.async_set(OUTSIDE, "-5.0")
        calls = await burn(hass, climate, boiler_data, mqtt_mock, published, freezer)

        assert climate.calculated_setpoint is not None and climate.calculated_setpoint > 36
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)
        assert calls == [selflowtemp(climate.calculated_setpoint), burnmaxpower(100)]

        # Afterwards the burn runs on: only the setpoint follows the low-pass filtered calculated setpoint, when its sent value changes
        for _ in range(4):
            sent = round(climate.calculated_setpoint)
            calls = await burn(hass, climate, boiler_data, mqtt_mock, published, freezer)
            assert climate.pwm.mode == ModulationMode.UNRESTRICTED
            assert calls == ([] if round(climate.calculated_setpoint) == sent else [selflowtemp(climate.calculated_setpoint)])

    # (e) UNRESTRICTED -> MINIMUM during a burn: the heater stays on

    @pytest.mark.parametrize(*entries(("minimum-44", {CONF_MINIMUM_SETPOINT: 44}, {})))
    async def test_unrestricted_to_minimum_during_a_burn_changes_in_place(self, hass, entry, climate, boiler_data, mqtt_mock, published, freezer):
        # At -5 °C outside the first calculated setpoint is about 46, above the minimum of 44
        hass.states.async_set(OUTSIDE, "-5.0")
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)
        assert climate.calculated_setpoint is not None and climate.calculated_setpoint > 44

        # A milder outside lowers the low-pass filtered calculated setpoint below the minimum within a few cycles, the pulse running on
        hass.states.async_set(OUTSIDE, "9.9")
        for _ in range(5):
            calls = await burn(hass, climate, boiler_data, mqtt_mock, published, freezer)
            assert climate.pwm.status == PWMStatus.ON
            if climate.pwm.mode == ModulationMode.MINIMUM:
                break
            assert heater_writes(calls) == []

        assert climate.calculated_setpoint < 44
        assert climate.pwm.mode == ModulationMode.MINIMUM
        assert calls == [selflowtemp(44), burnmaxpower(0)]

    # (b) A mode flip during a burn never publishes `heatingoff on`, in either direction

    @pytest.mark.parametrize(*entries(("minimum-36", {CONF_MINIMUM_SETPOINT: 36}, {})))
    async def test_mode_flips_during_a_burn_never_turn_the_heater_off(self, hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)

        assert climate.pwm.mode == ModulationMode.MINIMUM

        # The outside temperature drives each flip: the low-pass filtered calculated setpoint crosses the minimum of 36 within some cycles
        for outside, mode in [("-5.0", ModulationMode.UNRESTRICTED), ("9.9", ModulationMode.MINIMUM)] * 3:
            hass.states.async_set(OUTSIDE, outside)
            for _ in range(20):
                calls = await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**BURNING | echo(published)))
                assert HEATER_OFF not in calls
                assert climate.pwm.status == PWMStatus.ON
                if climate.pwm.mode == mode:
                    break
            assert climate.pwm.mode == mode

    # Hot water at the boundary: a draw does not interrupt an UNRESTRICTED burn, but delays a start from OFF until the hold ends

    @pytest.mark.parametrize(*entries(("unrestricted", {CONF_MINIMUM_SETPOINT: 30}, {CONF_HOT_WATER_DELAY: 300})))
    async def test_a_hot_water_draw_does_not_interrupt_an_unrestricted_burn(self, hass, entry, climate, boiler_data, mqtt_mock, published, freezer, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)

        for draw in ("on", "on", "off", "off"):
            calls = await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**BURNING | {"tapwateractive": draw} | echo(published)))
            assert HEATER_OFF not in calls
            assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)
            assert climate.setpoint == 35.0

    # A hot water draw during a MINIMUM pulse, on the climate side: on EMS-ESP no heater OFF is published, the setpoint stays the minimum of
    # 30 and the relative modulation state stays MINIMUM. The draws fall within the minimum pulse, so PWM's own pulse handling isn't tested

    @pytest.mark.parametrize(*entries(("unrestricted-disallowed", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False, CONF_HOT_WATER_DELAY: 300})))
    async def test_a_hot_water_draw_does_not_interrupt_a_minimum_pulse(self, hass, entry, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.MINIMUM)

        for draw in ("on", "on", "off", "off"):
            calls = await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**BURNING | {"tapwateractive": draw} | echo(published)))
            # The burn stays a hot water burn after the tap closes, until the flame goes off
            assert coordinator.boiler.hot_water_active
            assert HEATER_OFF not in calls
            assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.MINIMUM)
            assert climate.setpoint == 30.0
            # EMS-ESP's max relative modulation doesn't limit hot water: the state follows the mode
            assert climate.relative_modulation_state.name == "MINIMUM"

    @pytest.mark.parametrize(*entries(("unrestricted", {CONF_MINIMUM_SETPOINT: 30}, {CONF_HOT_WATER_DELAY: 300})))
    async def test_a_hot_water_draw_while_off_delays_the_start_until_the_hold_ends(self, entry, climate, boiler_data, mqtt_mock, published, freezer):
        await boiler_data(**IDLE | {"heatingoff": "on", "burngas": "on", "tapwateractive": "on"})
        calls = await run_loop(climate, mqtt_mock, published)
        assert climate.pwm.status == PWMStatus.OFF
        assert HEATER_ON not in calls and [c for c in calls if '"selflowtemp"' in c[1]] == [selflowtemp(0)]

        # The tap closes and the flame goes out: the delay of 300 s counts from then
        freezer.tick(10)
        await boiler_data(**IDLE | {"heatingoff": "on", "burngas": "off", "tapwateractive": "off"})
        await climate.async_control_heating_loop()
        freezer.tick(289)
        await climate.async_control_heating_loop()
        assert climate.pwm.status == PWMStatus.OFF

        freezer.tick(11)
        mqtt_mock.async_publish.reset_mock()
        await climate.async_control_heating_loop()
        assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.UNRESTRICTED)
        calls = published()
        assert HEATER_ON in calls
        assert {c for c in calls if '"selflowtemp"' in c[1]} == {selflowtemp(35)}

    # The PWM modulation mode reaches dynamic minimum setpoint v1: the cycle passes it to `MinimumSetpoint.calculate`

    @pytest.mark.parametrize(*entries(
        ("minimum", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}),
        ("unrestricted", {CONF_MINIMUM_SETPOINT: 30}, {}),
    ))
    async def test_the_pwm_mode_reaches_dynamic_minimum_v1(self, entry, options, climate, boiler_data, mqtt_mock, published):
        await boiler_data(**IDLE | BURNING)

        with patch.object(climate.minimum_setpoint, "calculate", wraps=climate.minimum_setpoint.calculate) as calculate:
            await run_loop(climate, mqtt_mock, published)

        expected = ModulationMode.UNRESTRICTED if options.get(CONF_ALLOW_UNRESTRICTED_MODULATION, True) else ModulationMode.MINIMUM
        assert climate.pwm.mode == expected
        calculate.assert_called_once()
        assert calculate.call_args.args[1] == expected

    # (h) The relative modulation state in the diagnostics download

    @pytest.mark.parametrize(*entries(
        ("minimum", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}),
        ("unrestricted", {CONF_MINIMUM_SETPOINT: 30}, {}),
    ))
    @pytest.mark.parametrize("read_back_5", [False, True], ids=["live", "read-back-5"])
    async def test_diagnostics_show_the_relative_modulation_state(self, hass, hass_client, entry, options, climate, boiler_data, mqtt_mock, published, read_back_5):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        await run_loop(climate, mqtt_mock, published)
        if read_back_5:
            # The boiler reads back a device setpoint at or below 10 °C after the cycle
            await boiler_data(selflowtemp=5)

        control = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

        # The diagnostics show the last cycle's decision, which a later read-back doesn't change
        disallowed = not options.get(CONF_ALLOW_UNRESTRICTED_MODULATION, True)
        expected = ("MINIMUM", 0) if disallowed else ("UNRESTRICTED", 100)
        assert (control["relative_modulation_state"], control["relative_modulation_value"]) == expected

    # A MINIMUM pulse through its off phase: EMS-ESP's `burnmaxpower` doesn't limit hot water, so the heater OFF request doesn't raise it

    @pytest.mark.parametrize(*entries(
        ("unrestricted-disallowed", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}),
        ("unrestricted-disallowed-floor", {CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 20}),
    ))
    async def test_a_minimum_pulse_never_publishes_the_maximum_modulation(self, hass, entry, options, climate, coordinator, boiler_data, mqtt_mock, published, freezer, caplog):
        await boiler_data(**IDLE | {"heatingoff": "on"})
        floor = options.get(CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR, 0)
        calls = await run_loop(climate, mqtt_mock, published)
        assert calls == [selflowtemp(30), burnmaxpower(floor), HEATER_ON]
        sent = list(calls)

        # ON, reading back what was published, until the pulse ends
        for _ in range(80):
            sent += (calls := await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**BURNING | echo(published))))
            if climate.pwm.status != PWMStatus.ON:
                break
        assert climate.pwm.status == PWMStatus.OFF
        assert calls == [selflowtemp(0), HEATER_OFF]

        # OFF: the boiler reads back heating off with a device setpoint of 5 until the next pulse starts
        off = {"heatingoff": "on", "selflowtemp": 5, "burngas": "off", "heatingactive": "off"}
        for _ in range(80):
            sent += (calls := await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, boiler_data(**off)))
            if climate.pwm.status == PWMStatus.ON:
                break
        assert climate.pwm.status == PWMStatus.ON
        assert coordinator.boiler.setpoint == 5

        # The next pulse starts at the minimum setpoint with the modulation still at the minimum (or the floor): no max is published
        assert calls == [selflowtemp(30), HEATER_ON]
        assert coordinator.maximum_relative_modulation_value == floor
        assert burnmaxpower_writes(sent) == [burnmaxpower(floor)]


# (h) HOT_WATER and HEATER_OFF_REQUESTED: EMS-ESP's max relative modulation doesn't limit hot water, so OpenTherm over MQTT shows them,
# and otherwise the mode. A minimum at or below COLD_SETPOINT (28.2) makes the MINIMUM pulse request heater OFF.

def otgw_disallowed(minimum: float, hot_water: str, expected: tuple[str, int], name: str) -> Any:
    return pytest.param(*otgw_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}, {CONF_MINIMUM_SETPOINT: minimum})[1][0], hot_water, expected, id=name)


@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
@pytest.mark.parametrize(otgw_entry()[0] + ", hot_water, expected", [
    otgw_disallowed(30, "ON", ("HOT_WATER", 100), "hot-water"),
    otgw_disallowed(20, "OFF", ("HEATER_OFF_REQUESTED", 100), "heater-off"),
    otgw_disallowed(30, "OFF", ("MINIMUM", 0), "minimum"),
])
async def test_diagnostics_show_the_hot_water_state(hass, hass_client, entry, climate, mqtt_mock, published, hot_water, expected):
    for key, value in {"TSet": "40", "Tboiler": "40.5", "Tret": "35", "flame": "OFF", "centralheating": "OFF", "domestichotwater": hot_water}.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()
    await run_loop(climate, mqtt_mock, published)

    control = (await get_diagnostics_for_config_entry(hass, hass_client, entry))["control"]

    assert (control["relative_modulation_state"], control["relative_modulation_value"]) == expected


# OpenTherm over MQTT through a MINIMUM pulse's off phase: the off phase requests the maximum, and the next pulse requests the minimum
# in the same submit as heater ON, written before it

OTGW_COMMAND = "otgw/set/gw1/command"


async def otgw_values(hass: HomeAssistant, **values: str) -> None:
    for key, value in values.items():
        async_fire_mqtt_message(hass, f"otgw/value/gw1/{key}", value)
    await hass.async_block_till_done()


async def otgw_cycle(hass: HomeAssistant, climate: SatClimate, mqtt_mock, published, freezer: FrozenDateTimeFactory, caplog, **values: str) -> list[str]:
    """29 s later, feed the read-backs and run one cycle; returns the gateway commands it published."""
    calls = await one_cycle(hass, climate, mqtt_mock, published, freezer, caplog, otgw_values(hass, **values))
    return [payload for topic, payload, *_ in calls if topic == OTGW_COMMAND]


@pytest.mark.parametrize(*otgw_entry({CONF_ALLOW_UNRESTRICTED_MODULATION: False}, {CONF_MINIMUM_SETPOINT: 30}))
@pytest.mark.usefixtures("mqtt_mock", "no_publish_delay", "loop_entry")
async def test_opentherm_requests_the_maximum_while_off_and_the_minimum_with_the_heater_on(hass, entry, climate, mqtt_mock, published, freezer, caplog):
    await otgw_values(hass, TSet="40", Tboiler="40.5", Tret="35", flame="OFF", centralheating="OFF", domestichotwater="OFF")
    calls = await run_loop(climate, mqtt_mock, published)
    assert [payload for topic, payload, *_ in calls if topic == OTGW_COMMAND] == ["CS=30.0", "PM=25", "MM=0", "CH=1"]

    # ON until the pulse ends, reading back the pulse
    for _ in range(80):
        commands = await otgw_cycle(hass, climate, mqtt_mock, published, freezer, caplog, TSet="30.0", flame="ON", centralheating="ON", MaxRelModLevelSetting="0")
        if climate.pwm.status != PWMStatus.ON:
            break
        assert climate.relative_modulation_state.name == "MINIMUM"
    assert climate.pwm.status == PWMStatus.OFF
    assert climate.relative_modulation_state.name == "HEATER_OFF_REQUESTED"
    assert commands == ["CS=10", "PM=25", "MM=100", "CH=0"]

    # OFF, reading back the off request, until the next pulse starts
    for _ in range(80):
        commands = await otgw_cycle(hass, climate, mqtt_mock, published, freezer, caplog, TSet="10", flame="OFF", centralheating="OFF", MaxRelModLevelSetting="100")
        if climate.pwm.status == PWMStatus.ON:
            break
        assert climate.relative_modulation_state.name == "HEATER_OFF_REQUESTED"
        assert not [command for command in commands if command.startswith(("MM=", "CH="))]
    assert climate.pwm.status == PWMStatus.ON

    # The first ON cycle requests the minimum with heater ON, though the boiler still reads back the off request
    assert climate.relative_modulation_state.name == "MINIMUM"
    assert commands == ["CS=30.0", "PM=25", "MM=0", "CH=1"]


# (f) A backend without setpoint management is always MINIMUM

@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize(*with_data({CONF_MODE: MODE_FAKE, CONF_MINIMUM_SETPOINT: 30}))
@pytest.mark.parametrize(("setpoint_management", "mode", "setpoint"), [(False, ModulationMode.MINIMUM, 30), (True, ModulationMode.UNRESTRICTED, 35)])
async def test_without_setpoint_management_the_mode_is_minimum(entry, climate, coordinator, setpoint_management, mode, setpoint):
    coordinator.config = SatFakeConfig(setpoint_management, supports_relative_modulation_management=True)
    await coordinator.async_set_boiler_temperature(40)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    await climate.async_control_heating_loop()

    assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, mode)
    assert climate.setpoint == setpoint


# Dynamic minimum setpoint v1: its normal mode requires UNRESTRICTED

@pytest.mark.parametrize(("mode", "expected"), [
    (ModulationMode.UNRESTRICTED, 37.0),  # normal mode: (1 - 0.5) * 80 - 3
    (ModulationMode.MINIMUM, 30.0),  # the adjusted configured minimum: 30 + (35 - 35) * 1
])
async def test_dynamic_minimum_v1_normal_mode_requires_unrestricted(freezer: FrozenDateTimeFactory, mode, expected):
    state = boiler_state(80.0, 80.0, setpoint=80.0)
    minimum = await warmed_minimum_setpoint(freezer, state)

    minimum.calculate(state, mode)  # type: ignore[arg-type]

    assert minimum.current == expected
