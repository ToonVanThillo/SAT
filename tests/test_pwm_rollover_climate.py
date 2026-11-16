"""A 100% PWM burn goes into the next period without interruption, end-to-end from the climate to the EMS MQTT messages."""
import json

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    PWMStatus,
)
from tests.const import IDLE, with_data

pytestmark = pytest.mark.usefixtures("loop_entry")

# A cold boiler, so the requested setpoint is above the boiler temperature and the duty is in the max branch
COLD_BOILER = IDLE | {"selflowtemp": 25, "curflowtemp": 25, "rettemp": 25, "burnmaxpower": 0}
PERIOD = 3600  # the outside temperature of the entry gives 2 cycles per hour, so a 100% period is 2 * 3600 / 2 s


def commands(published) -> list[dict]:
    return [json.loads(call[1]) for call in published()]


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_a_100_percent_burn_never_submits_heater_off_across_period_ends(entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**COLD_BOILER)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    elapsed = 0
    for _ in range(26):  # 7800 s: two period ends, at 3600 and 7200 s
        freezer.tick(300)
        elapsed += 300
        await boiler_data(**COLD_BOILER)
        mqtt_mock.async_publish.reset_mock()
        await climate.async_control_heating_loop()

        assert climate.pwm.duty_cycle == (PERIOD, 0)
        assert climate.pwm.status == PWMStatus.ON, f"PWM went off at {elapsed} s"
        sent = commands(published)
        assert {"cmd": "heatingoff", "value": "on"} not in sent, f"heater off at {elapsed} s"
        assert not [c for c in sent if c["cmd"] == "selflowtemp" and c["value"] != 30], f"setpoint dropped at {elapsed} s"


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_ALLOW_UNRESTRICTED_MODULATION: False}))
async def test_a_pulse_end_followed_by_100_percent_keeps_the_heater_off_for_180_s(hass, entry, climate, boiler_data, mqtt_mock, published, freezer):
    boiler = IDLE | {"selflowtemp": 34, "curflowtemp": 34, "rettemp": 30, "burnmaxpower": 0}

    async def cycle(seconds: int, outside: float) -> list[dict]:
        """Run one heating cycle `seconds` later, with the outside temperature, and return the commands it published."""
        freezer.tick(seconds)
        hass.states.async_set("sensor.test_outside_sensor", str(outside))
        await boiler_data(**boiler)
        mqtt_mock.async_publish.reset_mock()
        await climate.async_control_heating_loop()
        await hass.async_block_till_done()
        return commands(published)

    await boiler_data(**boiler)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    for _ in range(4):  # a 100% burn
        await cycle(300, 9.9)
    assert climate.pwm.status == PWMStatus.ON
    assert climate.pwm.duty_cycle is not None and climate.pwm.duty_cycle[1] == 0

    # A milder outside temperature lowers the duty, well into the mid branch, until the pulse ends
    sent: list[dict] = []
    for _ in range(40):
        sent = await cycle(30, 17.0)
        if climate.pwm.status == PWMStatus.OFF:
            break
    assert climate.pwm.status == PWMStatus.OFF
    assert {"cmd": "heatingoff", "value": "on"} in sent

    # The outside temperature returns and the duty is back at 100%, whose own off time is 0
    since_pulse_end = 0
    saw_full_duty_while_off = False
    while climate.pwm.status == PWMStatus.OFF and since_pulse_end < 600:
        sent = await cycle(30, 9.9)
        since_pulse_end += 30
        if climate.pwm.status == PWMStatus.OFF:
            saw_full_duty_while_off |= climate.pwm.duty_cycle is not None and climate.pwm.duty_cycle[1] == 0
            assert {"cmd": "heatingoff", "value": "off"} not in sent, f"heater on {since_pulse_end} s after the pulse end"

    assert saw_full_duty_while_off
    assert since_pulse_end == 180
    assert {"cmd": "heatingoff", "value": "off"} in sent
