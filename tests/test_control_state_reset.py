"""What resets PWM and the dynamic minimum v2 setpoint adjuster, and what doesn't.

A target change, of the thermostat, through a preset or of an area, doesn't reset PWM: a running pulse or off phase carries on. A thermostat
target change and an HVAC mode change reset the control state, which drops the adjuster and the minimum it gave; an HVAC mode change also
resets PWM, and the PWM sensors follow. Other PWM resets keep the adjuster. Of the services, only `reset_integral` is registered.
"""
import logging
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.sat.const import (
    CONF_DYNAMIC_MINIMUM_SETPOINT,
    CONF_DYNAMIC_MINIMUM_SETPOINT_VERSION,
    CONF_ROOMS,
    DOMAIN,
    MINIMUM_SETPOINT,
    SIGNAL_PWM_UPDATED,
    ModulationMode,
    PWMStatus,
)
from custom_components.sat.held_input import MISSING_INPUT_GRACE
from tests.conftest import TIMELINE, advance, run_one_loop
from tests.const import IDLE, with_data
from tests.test_climate_inputs import ROOM
from tests.test_pid_guard import (
    INSIDE,
    ROOM_ATTRS,
    UNRESTRICTED_DISALLOWED,
    WARM_MINIMUM,
    resets,
    steady,
)

pytestmark = pytest.mark.usefixtures("loop_entry")

STATUS = "sensor.pwm_status"
V2 = {CONF_DYNAMIC_MINIMUM_SETPOINT: True, CONF_DYNAMIC_MINIMUM_SETPOINT_VERSION: 2}


@pytest.fixture(autouse=True)
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_the_pwm_service_is_not_registered_but_reset_integral_is(hass, entry, climate):
    assert not hass.services.has_service(DOMAIN, "pulse_width_modulation")
    assert hass.services.has_service(DOMAIN, "reset_integral")


# Target changes never reset PWM

@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_target_changes_keep_a_running_pulse_and_reset_the_setpoint_adjuster(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    assert before["pwm"].status == PWMStatus.ON
    assert climate.pwm.setpoint != MINIMUM_SETPOINT

    await climate.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()
    assert climate.pwm.state == before["pwm"]
    assert climate.pwm.setpoint == MINIMUM_SETPOINT

    # Changes in a row; the pending loop runs at once on each, and the adjuster may learn again in it
    for target in (23.0, 19.0, 21.5):
        await climate.async_set_temperature(temperature=target)
        await hass.async_block_till_done()
        assert climate.pwm.state == before["pwm"]

    # The next control loop continues the pulse: no reset, no pulse end, no new pulse
    fields = await run_one_loop(hass, climate, caplog, len(caplog.records))
    assert climate.pwm.status == PWMStatus.ON
    assert "pwm_reset" not in fields


@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_a_preset_change_keeps_the_pulse(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    preset = next(p for p in climate.preset_modes if p != "none" and climate._presets[p] != climate.target_temperature)

    await climate.async_set_preset_mode(preset)
    await hass.async_block_till_done()

    assert climate.preset_mode == preset
    assert climate.pwm.state == before["pwm"]


@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_a_target_change_in_the_off_phase_keeps_the_off_phase(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    # End the pulse: a request at the bottom of the range, fed to PWM the way the control loop does
    for _ in range(100):
        if climate.pwm.status == PWMStatus.OFF:
            break
        freezer.tick(60)
        snapshot = climate._coordinator.snapshot()
        await climate.pwm.update(snapshot.boiler, snapshot.flame, 10.0, minimum_setpoint=30.0, allow_unrestricted=False)
    assert climate.pwm.status == PWMStatus.OFF
    off_state = climate.pwm.state

    await climate.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()

    assert climate.pwm.state == off_state

    # The next loop doesn't force a new pulse start either
    fields = await run_one_loop(hass, climate, caplog, len(caplog.records))
    assert climate.pwm.status == PWMStatus.OFF
    assert "pwm_reset" not in fields


@pytest.mark.parametrize(*with_data(WARM_MINIMUM | {CONF_ROOMS: [ROOM]}, UNRESTRICTED_DISALLOWED))
async def test_an_area_target_change_resets_neither_pwm_nor_the_setpoint_adjuster(hass, entry, climate, boiler_data, freezer, caplog):
    hass.states.async_set(ROOM, "heat", ROOM_ATTRS | {"temperature": 22})
    await hass.async_block_till_done()
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    setpoint = climate.pwm.setpoint
    assert setpoint != MINIMUM_SETPOINT
    resets_before = resets(caplog)

    hass.states.async_set(ROOM, "heat", ROOM_ATTRS | {"temperature": 23})
    await hass.async_block_till_done()

    # The PID reset branch ran, and left PWM and the adjuster alone
    assert resets(caplog) == resets_before + 1
    assert climate.pwm.state == before["pwm"]
    assert climate.pwm.setpoint == setpoint


# The dynamic minimum v2

@pytest.mark.parametrize(*with_data(WARM_MINIMUM, V2))
async def test_with_dynamic_minimum_v2_a_target_change_drops_the_learned_minimum_and_the_pulse_turns_unrestricted(hass, entry, climate, boiler_data, freezer, caplog):
    # A cold boiler (flow 35, flame off, no average on time) forces the adjuster to flow + 10 while in MINIMUM mode
    await boiler_data(**IDLE | {"heatingoff": "on", "curflowtemp": 35})
    await climate.async_set_target_temperature(20.5)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await run_one_loop(hass, climate, caplog, len(caplog.records))
    assert climate.minimum_setpoint_value == 45.0

    # The room cools: a MINIMUM pulse starts at a calculated setpoint below the learned minimum, which is the control setpoint
    for inside in ("20.6", "20.5", "20.4"):
        freezer.tick(600)
        hass.states.async_set(INSIDE, inside)
        await hass.async_block_till_done()
        await run_one_loop(hass, climate, caplog, len(caplog.records))
    # The filtered calculated setpoint catches up with the request
    for _ in range(3):
        freezer.tick(60)
        await run_one_loop(hass, climate, caplog, len(caplog.records))
    assert (climate.pwm.status, climate.pwm.mode) == (PWMStatus.ON, ModulationMode.MINIMUM)
    assert 30.0 < climate.calculated_setpoint < 45.0
    assert climate.setpoint == 45.0
    period_start = climate.pwm._period_start

    # The next loop, which the change may run at once: the minimum is the configured one again and the calculated setpoint is above it,
    # so the pulse turns UNRESTRICTED in a new period at 100% duty
    freezer.tick(60)
    mark = len(caplog.records)
    await climate.async_set_target_temperature(20.6)
    await run_one_loop(hass, climate, caplog, mark)
    assert climate.minimum_setpoint_value == 30.0
    assert (climate.pwm.status, climate.pwm.mode, climate.pwm.duty_cycle) == (PWMStatus.ON, ModulationMode.UNRESTRICTED, (3600, 0))
    assert climate.pwm._period_start > period_start
    assert climate.setpoint == climate.calculated_setpoint > 30.0

    # The adjuster doesn't learn while UNRESTRICTED, although the flame is still off
    for _ in range(3):
        freezer.tick(30)
        await run_one_loop(hass, climate, caplog, len(caplog.records))
        assert climate.pwm.mode == ModulationMode.UNRESTRICTED
        assert climate.pwm.setpoint == MINIMUM_SETPOINT
        assert climate.minimum_setpoint_value == 30.0


@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_a_pwm_reset_by_the_control_loop_keeps_the_setpoint_adjuster(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    adjusted = climate.pwm.setpoint
    assert adjusted != MINIMUM_SETPOINT

    # Missing temperature: PWM is reset by the control loop, the adjuster is not
    hass.states.async_set(INSIDE, "unavailable")
    await hass.async_block_till_done()
    # Past the grace period a missing input counts as missing
    freezer.tick(MISSING_INPUT_GRACE + 1)
    fields = await run_one_loop(hass, climate, caplog, len(caplog.records))

    assert fields["inputs"].current_temperature is None
    assert climate.pwm.status == PWMStatus.OFF
    assert climate.pwm.setpoint == adjusted


# An HVAC mode change

@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_an_hvac_mode_change_resets_pwm_and_the_setpoint_adjuster(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    assert climate.pwm.status == PWMStatus.ON
    assert climate.pwm.setpoint != MINIMUM_SETPOINT
    assert hass.states.get(STATUS).state == PWMStatus.ON.value
    received: list[PWMStatus] = []

    @callback
    def receive(status: PWMStatus) -> None:
        received.append(status)

    async_dispatcher_connect(hass, SIGNAL_PWM_UPDATED.format(entry.entry_id), receive)

    await climate.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()

    # HVAC mode OFF runs a control loop at once, which still signals the running pulse, before the reset ends it
    assert climate.pwm.status == PWMStatus.OFF
    assert climate.pwm.setpoint == MINIMUM_SETPOINT
    assert received == [PWMStatus.ON, PWMStatus.OFF]
    assert hass.states.get(STATUS).state == PWMStatus.OFF.value

    # The loop the change scheduled runs in HVAC mode OFF and leaves PWM alone
    await advance(hass, freezer, 11)
    assert climate.pwm.status == PWMStatus.OFF
    assert hass.states.get(STATUS).state == PWMStatus.OFF.value


async def heating_with_a_pulse(hass, climate, boiler_data, freezer, caplog) -> dict:
    """Heating with a running pulse and an adjusted setpoint; returns what setting the active HVAC mode again must leave as it is."""
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    assert before["pwm"].status == PWMStatus.ON
    assert climate.pwm.setpoint != MINIMUM_SETPOINT

    return before | {"setpoint": climate.pwm.setpoint}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_setting_the_active_hvac_mode_again_changes_nothing(hass, entry, climate, boiler_data, freezer, caplog):
    before = await heating_with_a_pulse(hass, climate, boiler_data, freezer, caplog)
    received: list[PWMStatus] = []
    async_dispatcher_connect(hass, SIGNAL_PWM_UPDATED.format(entry.entry_id), received.append)

    with patch.object(climate, "_async_control_pid") as pid, patch.object(climate, "reset_control_state") as control_state, \
            patch.object(climate, "async_write_ha_state") as write, patch.object(climate, "schedule_control_heating_loop") as schedule:
        await climate.async_set_hvac_mode(HVACMode.HEAT)
        await hass.async_block_till_done()

    for spy in (pid, control_state, write, schedule):
        spy.assert_not_called()
    assert climate.pwm.state == before["pwm"]
    assert climate.pwm.setpoint == before["setpoint"]
    assert received == []
    assert hass.states.get(STATUS).state == PWMStatus.ON.value


@pytest.mark.parametrize("service", ["set_hvac_mode", "turn_on"])
@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_the_climate_services_selecting_the_active_hvac_mode_change_nothing(hass, entry, climate, boiler_data, freezer, caplog, service):
    before = await heating_with_a_pulse(hass, climate, boiler_data, freezer, caplog)

    with patch.object(climate, "async_set_hvac_mode", wraps=climate.async_set_hvac_mode) as set_mode, \
            patch.object(climate, "_async_control_pid") as pid, patch.object(climate, "schedule_control_heating_loop") as schedule:
        data = {"entity_id": climate.entity_id} | ({"hvac_mode": HVACMode.HEAT} if service == "set_hvac_mode" else {})
        await hass.services.async_call("climate", service, data, blocking=True)
        await hass.async_block_till_done()

    # Home Assistant passes the call on; the climate ignores it
    set_mode.assert_awaited_once_with(HVACMode.HEAT)
    pid.assert_not_called()
    schedule.assert_not_called()
    assert climate.hvac_mode == HVACMode.HEAT
    assert climate.pwm.state == before["pwm"]
    assert climate.pwm.setpoint == before["setpoint"]


@pytest.mark.parametrize("service", ["turn_off", "set_hvac_mode"])
@pytest.mark.parametrize(*with_data(WARM_MINIMUM, UNRESTRICTED_DISALLOWED))
async def test_turning_off_while_already_off_changes_nothing(hass, entry, climate, boiler_data, caplog, service):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()
    assert climate.hvac_mode == HVACMode.OFF

    data = {"entity_id": climate.entity_id} | ({"hvac_mode": HVACMode.OFF} if service == "set_hvac_mode" else {})
    with patch.object(climate, "async_set_hvac_mode", wraps=climate.async_set_hvac_mode) as set_mode, \
            patch.object(climate, "_async_control_pid") as pid, patch.object(climate, "reset_control_state") as control_state, \
            patch.object(climate, "async_write_ha_state") as write, patch.object(climate, "schedule_control_heating_loop") as schedule, \
            patch.object(climate.pwm, "reset") as reset:
        await hass.services.async_call("climate", service, data, blocking=True)
        await hass.async_block_till_done()

    set_mode.assert_awaited_once_with(HVACMode.OFF)
    for spy in (pid, control_state, write, schedule, reset):
        spy.assert_not_called()
    assert climate.pwm.status == PWMStatus.OFF
