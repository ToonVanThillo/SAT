"""A 100% PWM burn goes into the next period without interruption, end-to-end from the climate to the EMS MQTT messages."""
import json

import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.const import (
    CONF_FORCE_PULSE_WIDTH_MODULATION,
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


@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True}))
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
