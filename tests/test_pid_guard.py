"""The event-driven PID guard: while the inside or target temperature is missing, sensor, area, target and HVAC-mode events leave the PID,
the heating curve, the requested setpoint and the PWM alone, so the state before a sensor gap is what the PID continues from."""
import logging
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MINIMUM_SETPOINT,
    CONF_ROOMS,
    CONF_SENSOR_MAX_VALUE_AGE,
    CONF_THERMAL_COMFORT,
    PWMStatus,
)
from tests.conftest import TIMELINE, run_one_loop
from tests.const import IDLE, with_data
from tests.test_climate_inputs import ROOM

pytestmark = pytest.mark.usefixtures("loop_entry")

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
HUMIDITY = "sensor.test_humidity"
SKIP = "Skipping the PID update: the inside or target temperature is missing."
CLIMATE_LOGGER = "custom_components.sat.climate"

FORCED = {CONF_MINIMUM_SETPOINT: 30}
FORCE_PWM = {CONF_FORCE_PULSE_WIDTH_MODULATION: True}


@pytest.fixture(autouse=True)
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMELINE)
    caplog.set_level(logging.DEBUG, logger=CLIMATE_LOGGER)


def skips(caplog) -> int:
    return len([r for r in caplog.records if r.name == CLIMATE_LOGGER and r.getMessage() == SKIP])


DEFERRED = "Running the PID reset deferred while the inside or target temperature was missing"


def deferred(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == CLIMATE_LOGGER and r.getMessage().startswith(DEFERRED)]


def resets(caplog) -> int:
    """The normal reset branch's runs, from its info line."""
    return len([r for r in caplog.records if r.name == CLIMATE_LOGGER and r.getMessage().endswith("(Reset: True)")])


def timeline(caplog, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.getMessage().startswith(prefix)]


def kept(climate) -> dict[str, Any]:
    """What the guard must keep through a gap: the PID's terms, error and timestamp, the requested setpoint and the PWM state."""
    pid = climate.pid

    return {
        "proportional": pid.proportional, "integral": pid.integral, "derivative": pid.derivative, "raw_derivative": pid.raw_derivative,
        "last_error": pid.last_error, "previous_error": pid.previous_error, "last_updated": pid.last_updated,
        "requested_setpoint": climate.requested_setpoint, "pwm": climate.pwm.state,
    }


async def steady(hass, climate, boiler_data, freezer, caplog, target: float = 21.5, exact: bool = True) -> dict[str, Any]:
    """Heating with an error of 0.6 (inside 20.9) and a PID that has moved: an error of 1.0 first, then 0.6; a live control loop at the end."""
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(target)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await run_one_loop(hass, climate, caplog, len(caplog.records))

    for value in ("20.5", "20.9"):
        freezer.tick(600)
        hass.states.async_set(INSIDE, value)
        await hass.async_block_till_done()

    await run_one_loop(hass, climate, caplog, len(caplog.records))
    if exact:
        assert climate.pid.last_error == pytest.approx(0.6)
        assert climate.pid.previous_error == pytest.approx(1.0)
    assert climate.pid.last_error != climate.pid.previous_error
    assert climate.pid.derivative != 0
    assert climate.pwm.status != PWMStatus.IDLE

    return kept(climate)


async def gap(hass) -> None:
    hass.states.async_set(INSIDE, "unavailable")
    await hass.async_block_till_done()


# The reviewer's scenario: an outside change during an inside gap

@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_an_outside_change_during_an_inside_gap_leaves_the_pid_and_pwm_alone(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    skipped = skips(caplog)

    hass.states.async_set(OUTSIDE, "5.0")
    await hass.async_block_till_done()

    assert kept(climate) == before
    assert skips(caplog) == skipped + 1
    assert timeline(caplog, "pwm IDLE") == []


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_the_pid_continues_from_the_kept_state_when_the_sensor_returns(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    hass.states.async_set(OUTSIDE, "5.0")
    await hass.async_block_till_done()

    freezer.tick(600)
    hass.states.async_set(INSIDE, "21.0")
    await hass.async_block_till_done()

    pid = climate.pid
    assert pid.last_error == pytest.approx(0.5)
    # The previous error is the one from before the gap, not the 0 of a missing reading
    assert pid.previous_error == before["last_error"]
    # A step of 0.1 from the kept error: the derivative is of the size it was before the gap, not a jump from 0
    assert abs(pid.derivative) <= 2 * abs(before["derivative"])
    assert pid.derivative <= 0


@pytest.mark.parametrize(*with_data(FORCED | {CONF_HUMIDITY_SENSOR_ENTITY_ID: HUMIDITY}, FORCE_PWM | {CONF_THERMAL_COMFORT: True}))
async def test_a_humidity_change_during_an_inside_gap_leaves_the_pid_and_pwm_alone(hass, entry, climate, boiler_data, freezer, caplog):
    hass.states.async_set(HUMIDITY, "50")
    await hass.async_block_till_done()
    before = await steady(hass, climate, boiler_data, freezer, caplog, exact=False)
    await gap(hass)
    skipped = skips(caplog)

    hass.states.async_set(HUMIDITY, "55")
    await hass.async_block_till_done()

    assert kept(climate) == before
    assert skips(caplog) == skipped + 1
    assert timeline(caplog, "pwm IDLE") == []


@pytest.mark.parametrize(*with_data(FORCED | {CONF_ROOMS: [ROOM]}, FORCE_PWM))
async def test_an_area_climate_change_during_an_inside_gap_leaves_the_pid_and_pwm_alone(hass, entry, climate, boiler_data, freezer, caplog):
    hass.states.async_set(ROOM, "heat", {"temperature": 22, "current_temperature": 21.9, "hvac_modes": ["heat", "off"]})
    await hass.async_block_till_done()
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    skipped = skips(caplog)

    # A target change of the room is the reset branch's entry point
    hass.states.async_set(ROOM, "heat", {"temperature": 23, "current_temperature": 21.9, "hvac_modes": ["heat", "off"]})
    await hass.async_block_till_done()

    assert kept(climate) == before
    assert skips(caplog) >= skipped + 1
    assert timeline(caplog, "pwm IDLE") == []


# The entry points that still reset the control state afterwards

@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_a_target_change_during_an_inside_gap_skips_the_pid_reset_but_not_the_control_state_reset(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    skipped = skips(caplog)

    await climate.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()

    pid_before = {key: value for key, value in before.items() if key not in ("pwm", "requested_setpoint")}
    assert {key: value for key, value in kept(climate).items() if key in pid_before} == pid_before
    assert skips(caplog) == skipped + 1
    assert "pwm IDLE (error reset)" not in timeline(caplog, "pwm IDLE")
    # reset_control_state is outside the guard: PWM is disabled
    assert "pwm IDLE (control state reset)" in timeline(caplog, "pwm IDLE")


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_an_hvac_mode_set_during_an_inside_gap_skips_the_pid_reset_but_not_the_control_state_reset(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    skipped = skips(caplog)

    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()

    pid_before = {key: value for key, value in before.items() if key not in ("pwm", "requested_setpoint")}
    assert {key: value for key, value in kept(climate).items() if key in pid_before} == pid_before
    assert skips(caplog) == skipped + 1
    assert "pwm IDLE (error reset)" not in timeline(caplog, "pwm IDLE")
    assert "pwm IDLE (control state reset)" in timeline(caplog, "pwm IDLE")


# The target guard

@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_a_missing_target_with_a_live_inside_sensor_skips_the_pid_update(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    skipped = skips(caplog)
    # There is no public way to lose a target once set; this is the state a failed restore leaves
    climate._target_temperature = None
    assert climate.current_temperature is not None

    hass.states.async_set(OUTSIDE, "5.0")
    await hass.async_block_till_done()

    assert kept(climate) == before
    assert skips(caplog) == skipped + 1


# No gap: the guard is out of the way

@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_without_a_gap_an_inside_change_updates_the_pid_as_before(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    skipped = skips(caplog)

    freezer.tick(600)
    hass.states.async_set(INSIDE, "21.0")
    await hass.async_block_till_done()

    assert skips(caplog) == skipped
    assert climate.pid.last_error == pytest.approx(0.5)


# The reset deferred through a gap

ROOM_ATTRS = {"hvac_modes": ["heat", "off"], "current_temperature": 21.9}


@pytest.mark.parametrize("with_gap", [True, False], ids=["gap", "no gap"])
@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_a_target_change_resets_the_pid_without_a_derivative_kick_with_or_without_a_gap(hass, entry, climate, boiler_data, freezer, caplog, with_gap):
    await steady(hass, climate, boiler_data, freezer, caplog)
    if with_gap:
        await gap(hass)

    await climate.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()
    pwm_after_change = climate.pwm.state
    lines_before = len(timeline(caplog, "pwm "))

    if with_gap:
        assert deferred(caplog) == []
        freezer.tick(10)
        hass.states.async_set(INSIDE, "20.9")
        await hass.async_block_till_done()
        assert len(deferred(caplog)) == 1
        assert deferred(caplog)[0].endswith("(error 1.1).")

    pid = climate.pid
    assert pid.last_error == pytest.approx(1.1)
    assert pid.previous_error == pytest.approx(1.1)
    assert pid.integral == 0
    assert pid.derivative == pytest.approx(0, abs=1e-9)
    assert pid.raw_derivative == pytest.approx(0, abs=1e-9)
    # No PWM reset on return, and the pulse (disabled by the target change's control state reset) isn't touched again
    assert climate.pwm.state == pwm_after_change
    assert len(timeline(caplog, "pwm ")) == lines_before
    # Only the no-gap reset is the normal reset branch, which resets PWM
    assert ("pwm IDLE (error reset)" in timeline(caplog, "pwm IDLE")) is (not with_gap)


@pytest.mark.parametrize(*with_data(FORCED | {CONF_ROOMS: [ROOM]}, FORCE_PWM))
async def test_an_area_change_in_a_gap_defers_the_reset_and_leaves_a_running_pulse_alone(hass, entry, climate, boiler_data, freezer, caplog):
    hass.states.async_set(ROOM, "heat", ROOM_ATTRS | {"temperature": 22})
    await hass.async_block_till_done()
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    hass.states.async_set(ROOM, "heat", ROOM_ATTRS | {"temperature": 23})
    await hass.async_block_till_done()
    assert deferred(caplog) == []

    freezer.tick(10)
    hass.states.async_set(INSIDE, "20.9")
    await hass.async_block_till_done()

    assert len(deferred(caplog)) == 1
    assert climate.pid.derivative == pytest.approx(0, abs=1e-9)
    assert climate.pid.last_error == climate.pid.previous_error != before["last_error"]
    # The area change doesn't reset the control state: the pulse runs on, PWM is never reset
    assert climate.pwm.state == before["pwm"]
    assert timeline(caplog, "pwm IDLE") == []


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_an_hvac_mode_set_in_a_gap_defers_the_reset(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()
    assert deferred(caplog) == []

    # The sensor returns with another reading, so the error differs from the PID's last one
    freezer.tick(10)
    hass.states.async_set(INSIDE, "20.8")
    await hass.async_block_till_done()

    assert len(deferred(caplog)) == 1
    assert climate.pid.last_error == pytest.approx(0.7)
    assert climate.pid.previous_error == pytest.approx(0.7)
    assert "pwm IDLE (error reset)" not in timeline(caplog, "pwm IDLE")


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_the_deferred_reset_runs_once(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    freezer.tick(10)
    hass.states.async_set(INSIDE, "20.8")
    await hass.async_block_till_done()
    assert len(deferred(caplog)) == 1

    freezer.tick(600)
    hass.states.async_set(INSIDE, "20.6")
    await hass.async_block_till_done()
    freezer.tick(600)
    hass.states.async_set(INSIDE, "20.9")
    await hass.async_block_till_done()

    assert len(deferred(caplog)) == 1
    # The later updates are normal ones: the previous error is the one before, not reset to the new
    assert climate.pid.previous_error == pytest.approx(0.9)
    assert climate.pid.last_error == pytest.approx(0.6)


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM | {CONF_SENSOR_MAX_VALUE_AGE: "00:15:00"}))
async def test_the_age_based_reset_wins_over_the_deferred_one(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    await climate.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()

    freezer.tick(1000)
    hass.states.async_set(INSIDE, "20.9")
    await hass.async_block_till_done()

    assert deferred(caplog) == []
    # pid.reset() ran: the errors are 0 (update_reset would have set both to 1.1)
    assert climate.pid.previous_error == 0
    assert climate.pid.last_error == 0
    # And the flag is gone: a further update doesn't defer anything
    freezer.tick(600)
    hass.states.async_set(INSIDE, "20.8")
    await hass.async_block_till_done()
    assert deferred(caplog) == []


@pytest.mark.parametrize(*with_data(FORCED | {CONF_ROOMS: [ROOM]}, FORCE_PWM))
async def test_when_the_first_call_after_the_gap_is_a_reset_the_reset_branch_runs_once(hass, entry, climate, boiler_data, freezer, caplog):
    hass.states.async_set(ROOM, "heat", ROOM_ATTRS | {"temperature": 22})
    await hass.async_block_till_done()
    await steady(hass, climate, boiler_data, freezer, caplog)
    # The target is what's missing; the inside sensor is live
    climate._target_temperature = None
    hass.states.async_set(ROOM, "heat", ROOM_ATTRS | {"temperature": 23})
    await hass.async_block_till_done()
    assert deferred(caplog) == []
    resets_before = resets(caplog)

    # The target is set again: that call is itself a reset and the first one to pass the guard
    await climate.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()

    assert deferred(caplog) == []
    assert resets(caplog) == resets_before + 1
    assert timeline(caplog, "pwm IDLE").count("pwm IDLE (error reset)") == 1
    assert climate.pid.last_error == climate.pid.previous_error

    # The flag was cleared by that call
    freezer.tick(600)
    hass.states.async_set(INSIDE, "20.7")
    await hass.async_block_till_done()
    assert deferred(caplog) == []


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_a_plain_gap_leaves_nothing_to_defer(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    hass.states.async_set(OUTSIDE, "5.0")
    await hass.async_block_till_done()

    freezer.tick(10)
    hass.states.async_set(INSIDE, "20.8")
    await hass.async_block_till_done()

    assert deferred(caplog) == []


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_an_equal_error_defers_nothing_and_clears_the_flag(hass, entry, climate, boiler_data, freezer, caplog):
    before = await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # The sensor returns with the reading it had: the error equals the PID's last error
    freezer.tick(10)
    hass.states.async_set(INSIDE, "20.9")
    await hass.async_block_till_done()

    assert deferred(caplog) == []
    assert climate.pid.last_error == before["last_error"]
    assert climate.pid.previous_error == before["previous_error"]

    # Cleared: a later change is a normal update, not a deferred reset
    freezer.tick(600)
    hass.states.async_set(INSIDE, "20.6")
    await hass.async_block_till_done()

    assert deferred(caplog) == []
    assert climate.pid.last_error == pytest.approx(0.9)
    assert climate.pid.previous_error == pytest.approx(0.6)


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_a_missing_outside_temperature_does_not_set_the_flag(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    hass.states.async_set(OUTSIDE, "unavailable")
    await hass.async_block_till_done()
    skipped = skips(caplog)

    # A reset request that stops at the outside check: no PID work, no skip line, nothing pending
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()
    assert skips(caplog) == skipped

    hass.states.async_set(OUTSIDE, "5.0")
    await hass.async_block_till_done()
    freezer.tick(600)
    hass.states.async_set(INSIDE, "20.8")
    await hass.async_block_till_done()

    assert deferred(caplog) == []


@pytest.mark.parametrize(*with_data(FORCED, FORCE_PWM))
async def test_a_missing_outside_temperature_does_not_clear_the_flag(hass, entry, climate, boiler_data, freezer, caplog):
    await steady(hass, climate, boiler_data, freezer, caplog)
    await gap(hass)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # Calls that stop at the outside check, with the flag pending
    hass.states.async_set(OUTSIDE, "unavailable")
    await hass.async_block_till_done()
    await climate.async_set_temperature(temperature=21.5)
    hass.states.async_set(OUTSIDE, "5.0")
    await hass.async_block_till_done()
    assert deferred(caplog) == []

    freezer.tick(10)
    hass.states.async_set(INSIDE, "20.8")
    await hass.async_block_till_done()

    assert len(deferred(caplog)) == 1
