"""Through the climate, the option "Allow unrestricted modulation" decides the PWM mode above the minimum; a stored legacy force PWM option is ignored."""
import pytest
from homeassistant.components.climate import HVACMode

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_ALLOW_UNRESTRICTED_MODULATION,
    CONF_MINIMUM_SETPOINT,
    ModulationMode,
    PWMStatus,
)
from tests.const import IDLE, with_data


@pytest.mark.usefixtures("loop_entry")
@pytest.mark.parametrize("domains, data, options, config, expected", [
    (*with_data({CONF_MINIMUM_SETPOINT: 30}, options)[1][0], expected)
    for options, expected in [
        ({CONF_ALLOW_UNRESTRICTED_MODULATION: False}, ModulationMode.MINIMUM),
        ({}, ModulationMode.UNRESTRICTED),
        ({CONF_ALLOW_UNRESTRICTED_MODULATION: True}, ModulationMode.UNRESTRICTED),
        ({"force_pulse_width_modulation": True}, ModulationMode.UNRESTRICTED),
    ]
], ids=["disallowed", "default", "allowed", "legacy-force-ignored"])
async def test_the_option_decides_the_mode_above_the_minimum(climate: SatClimate, boiler_data, freezer, expected: ModulationMode) -> None:
    cold_boiler = IDLE | {"selflowtemp": 25, "curflowtemp": 25, "rettemp": 25, "burnmaxpower": 0}
    await boiler_data(**cold_boiler)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    freezer.tick(60)
    await boiler_data(**cold_boiler)
    await climate.async_control_heating_loop()

    assert climate.pwm.status == PWMStatus.ON
    assert (calculated := climate.calculated_setpoint) is not None
    assert calculated > climate.minimum_setpoint_value
    assert climate.pwm.mode == expected
