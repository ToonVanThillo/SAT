"""The pulse_width_modulation service notifies the PWM sensors right after it changes PWM."""
import logging

import pytest
import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    DOMAIN,
    SIGNAL_PWM_UPDATED,
    PWMStatus,
)
from tests.const import IDLE, with_data

STATUS = "sensor.pwm_status"
DUTY = "sensor.pwm_duty_cycle_test"

pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default", "loop_entry")


async def call(hass, entity_ids, enabled):
    await hass.services.async_call(DOMAIN, "pulse_width_modulation", {"entity_id": entity_ids, "enabled": enabled}, blocking=True)


def listen(hass, entry, received):
    return async_dispatcher_connect(hass, SIGNAL_PWM_UPDATED.format(entry.entry_id), lambda status: received.append(status))


async def active_pwm(hass, climate, boiler_data):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state in ("on", "off")
    assert hass.states.get(DUTY).state not in (STATE_UNKNOWN, "unavailable")
    assert climate.pwm.duty_cycle is not None


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_disable_resets_sensors_immediately(hass, entry, climate, boiler_data):
    await active_pwm(hass, climate, boiler_data)
    received = []
    unsub = listen(hass, entry, received)

    await call(hass, [climate.entity_id], False)
    await hass.async_block_till_done()
    unsub()

    assert climate.pwm.status == PWMStatus.IDLE
    assert climate.pwm.duty_cycle is None
    assert received == [PWMStatus.IDLE]
    assert hass.states.get(STATUS).state == "idle"
    assert hass.states.get(DUTY).state == STATE_UNKNOWN


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_enable_keeps_status_and_duty(hass, entry, climate, boiler_data, freezer):
    await active_pwm(hass, climate, boiler_data)
    status_before, duty_before = hass.states.get(STATUS).state, hass.states.get(DUTY).state
    reported_before = (hass.states.get(STATUS).last_reported, hass.states.get(DUTY).last_reported)
    duty_cycle_before = climate.pwm.duty_cycle
    received = []
    unsub = listen(hass, entry, received)

    freezer.tick(1)  # time is frozen; moves the clock only, no timers run
    await call(hass, [climate.entity_id], True)
    await hass.async_block_till_done()
    unsub()

    assert received == [climate.pwm.status]
    assert climate.pwm.duty_cycle == duty_cycle_before
    assert hass.states.get(STATUS).state == status_before
    assert hass.states.get(DUTY).state == duty_before
    # The sensors were re-written
    assert hass.states.get(STATUS).last_reported > reported_before[0]
    assert hass.states.get(DUTY).last_reported > reported_before[1]


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_enable_after_disable_keeps_reset_state(hass, entry, climate, boiler_data, freezer):
    await active_pwm(hass, climate, boiler_data)
    await call(hass, [climate.entity_id], False)
    await hass.async_block_till_done()
    reported_before = (hass.states.get(STATUS).last_reported, hass.states.get(DUTY).last_reported)
    received = []
    unsub = listen(hass, entry, received)

    freezer.tick(1)  # time is frozen; moves the clock only, no timers run
    await call(hass, [climate.entity_id], True)
    await hass.async_block_till_done()
    unsub()

    assert received == [climate.pwm.status]
    assert climate.pwm.status == PWMStatus.IDLE
    assert climate.pwm.duty_cycle is None
    assert hass.states.get(STATUS).state == "idle"
    assert hass.states.get(DUTY).state == STATE_UNKNOWN
    assert hass.states.get(STATUS).last_reported > reported_before[0]
    assert hass.states.get(DUTY).last_reported > reported_before[1]


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_disable_already_disabled_still_dispatches_once(hass, entry, climate, boiler_data):
    await active_pwm(hass, climate, boiler_data)
    await call(hass, [climate.entity_id], False)
    await hass.async_block_till_done()
    received = []
    unsub = listen(hass, entry, received)

    await call(hass, [climate.entity_id], False)
    await hass.async_block_till_done()
    unsub()

    assert received == [PWMStatus.IDLE]
    assert hass.states.get(STATUS).state == "idle"
    assert hass.states.get(DUTY).state == STATE_UNKNOWN


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_duplicate_ids_dispatch_per_resolved_climate(hass, entry, climate):
    """Each resolved id dispatches once; the same id listed twice in the call resolves twice."""
    received = []
    unsub = listen(hass, entry, received)

    await call(hass, [climate.entity_id, "climate.does_not_exist"], True)
    await hass.async_block_till_done()
    assert received == [climate.pwm.status]

    received.clear()
    await call(hass, [climate.entity_id, climate.entity_id], True)
    await hass.async_block_till_done()
    unsub()
    assert received == [climate.pwm.status] * 2


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
@pytest.mark.parametrize("enabled", [True, False])
async def test_unknown_entity_is_skipped(hass, entry, climate, enabled):
    received = []
    unsub = listen(hass, entry, received)

    await call(hass, ["climate.does_not_exist", "sensor.not_registered"], enabled)
    await hass.async_block_till_done()
    unsub()

    assert received == []


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_enabled_and_entity_id_are_required(hass, entry, climate):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "pulse_width_modulation", {"entity_id": [climate.entity_id]}, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "pulse_width_modulation", {"enabled": True}, blocking=True)


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_enabled_accepts_boolean_strings(hass, entry, climate, boiler_data):
    await active_pwm(hass, climate, boiler_data)

    await call(hass, [climate.entity_id], "off")
    await hass.async_block_till_done()

    assert hass.states.get(STATUS).state == "idle"


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
@pytest.mark.parametrize("enabled", [True, False])
async def test_non_climate_sat_entity_is_skipped(hass, entry, climate, boiler_data, enabled, caplog):
    await active_pwm(hass, climate, boiler_data)
    status, duty = hass.states.get(STATUS).state, hass.states.get(DUTY).state
    received = []
    unsub = listen(hass, entry, received)

    await call(hass, ["sensor.pwm_status", DUTY], enabled)
    await hass.async_block_till_done()
    unsub()

    assert received == []
    assert hass.states.get(STATUS).state == status
    assert hass.states.get(DUTY).state == duty
    assert not [r for r in caplog.get_records("call") if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_non_climate_id_next_to_a_climate_id_handles_only_the_climate(hass, entry, climate, boiler_data):
    await active_pwm(hass, climate, boiler_data)
    received = []
    unsub = listen(hass, entry, received)

    await call(hass, ["sensor.pwm_status", climate.entity_id, DUTY], False)
    await hass.async_block_till_done()
    unsub()

    assert received == [PWMStatus.IDLE]
    assert hass.states.get(STATUS).state == "idle"
    assert hass.states.get(DUTY).state == STATE_UNKNOWN


@pytest.mark.parametrize(*with_data({}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
async def test_climate_without_hass_is_skipped(hass, entry, climate, boiler_data, caplog):
    await active_pwm(hass, climate, boiler_data)
    status, duty = hass.states.get(STATUS).state, hass.states.get(DUTY).state
    received = []
    unsub = listen(hass, entry, received)
    original = climate.hass
    climate.hass = None
    try:
        await call(hass, [climate.entity_id, "climate.does_not_exist"], False)
    finally:
        climate.hass = original
    await hass.async_block_till_done()
    unsub()

    assert received == []
    assert climate.pwm.status != PWMStatus.IDLE or climate.pwm.duty_cycle is not None
    assert hass.states.get(STATUS).state == status
    assert hass.states.get(DUTY).state == duty
    assert not [r for r in caplog.get_records("call") if r.levelno >= logging.WARNING]
