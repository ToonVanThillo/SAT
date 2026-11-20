"""The tests for the climate component."""

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.template import DOMAIN as TEMPLATE_DOMAIN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import *
from custom_components.sat.fake import SatFakeCoordinator


@pytest.mark.parametrize(*[
    "domains, data, options, config",
    [(
            [(TEMPLATE_DOMAIN, 1)],
            {
                CONF_MODE: MODE_FAKE,
                CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
                CONF_MINIMUM_SETPOINT: 57,
            },
            {
                CONF_MAXIMUM_SETPOINT: 75,
                CONF_HEATING_CURVE_COEFFICIENT: 1.8,
                CONF_ALLOW_UNRESTRICTED_MODULATION: False,
            },
            {
                TEMPLATE_DOMAIN: [
                    {
                        SENSOR_DOMAIN: [
                            {
                                "name": "test_inside_sensor",
                                "state": "{{ 20.9 | float }}",
                            },
                            {
                                "name": "test_outside_sensor",
                                "state": "{{ 9.9 | float }}",
                            }
                        ]
                    },
                ],
            },
    )],
])
async def test_scenario_1(hass: HomeAssistant, entry: MockConfigEntry, climate: SatClimate, coordinator: SatFakeCoordinator) -> None:
    await coordinator.async_set_boiler_temperature(57)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    climate.schedule_control_heating_loop(force=True)

    # A MINIMUM pulse at the configured minimum setpoint of 57, below the configured maximum of 75
    assert coordinator.setpoint == 57
    assert climate.heating_curve.value == 32.2

    assert climate.pwm.last_duty_cycle_percentage == 23.83
    # 9.9 °C outside gives 2 cycles per hour
    assert climate.pwm.cycles_per_hour == 2
    assert climate.pwm.duty_cycle == (428, 1372)


@pytest.mark.parametrize(*[
    "domains, data, options, config",
    [(
            [(TEMPLATE_DOMAIN, 1)],
            {
                CONF_MODE: MODE_FAKE,
                CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
                CONF_MINIMUM_SETPOINT: 58,
            },
            {
                CONF_MAXIMUM_SETPOINT: 75,
                CONF_HEATING_CURVE_COEFFICIENT: 1.3,
                CONF_ALLOW_UNRESTRICTED_MODULATION: False,
            },
            {
                TEMPLATE_DOMAIN: [
                    {
                        SENSOR_DOMAIN: [
                            {
                                "name": "test_inside_sensor",
                                "state": "{{ 18.99 | float }}",
                            },
                            {
                                "name": "test_outside_sensor",
                                "state": "{{ 11.1 | float }}",
                            }
                        ]
                    },
                ],
            },
    )],
])
async def test_scenario_2(hass: HomeAssistant, entry: MockConfigEntry, climate: SatClimate, coordinator: SatFakeCoordinator) -> None:
    await coordinator.async_set_boiler_temperature(58)
    await climate.async_set_target_temperature(20.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    climate.schedule_control_heating_loop(force=True)

    assert climate.heating_curve.value == 29.1
    assert climate.calculated_setpoint == 48.5

    # The smoothed setpoint of 48.5 is above COLD_SETPOINT (28.2), so PWM runs a MINIMUM pulse at the minimum of 58.
    # Duty: (48.5 - 27.2 base offset) / (58 boiler - 27.2) = 69.16%
    assert coordinator.setpoint == 58
    assert climate.pwm.last_duty_cycle_percentage == 69.16
    # 11.1 °C outside gives 2 cycles per hour, so a period of 1800 s: 1800 * 0.6916 = 1244 s on
    assert climate.pwm.cycles_per_hour == 2
    assert climate.pwm.duty_cycle == (1244, 556)


@pytest.mark.parametrize(*[
    "domains, data, options, config",
    [(
            [(TEMPLATE_DOMAIN, 1)],
            {
                CONF_MODE: MODE_FAKE,
                CONF_HEATING_SYSTEM: HEATING_SYSTEM_RADIATORS,
                CONF_MINIMUM_SETPOINT: 41,
            },
            {
                CONF_MAXIMUM_SETPOINT: 75,
                CONF_HEATING_CURVE_COEFFICIENT: 0.9,
                CONF_ALLOW_UNRESTRICTED_MODULATION: False,
            },
            {
                TEMPLATE_DOMAIN: [
                    {
                        SENSOR_DOMAIN: [
                            {
                                "name": "test_inside_sensor",
                                "state": "{{ 19.9 | float }}",
                            },
                            {
                                "name": "test_outside_sensor",
                                "state": "{{ -2.2 | float }}",
                            }
                        ]
                    },
                ],
            },
    )],
])
async def test_scenario_3(hass: HomeAssistant, entry: MockConfigEntry, climate: SatClimate, coordinator: SatFakeCoordinator) -> None:
    await coordinator.async_set_boiler_temperature(41)
    await climate.async_set_target_temperature(20.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    climate.schedule_control_heating_loop(force=True)

    assert coordinator.setpoint == 41.0
    assert climate.heating_curve.value == 32.5
    assert climate.calculated_setpoint == 34.6

    assert climate.pwm.last_duty_cycle_percentage == 53.62
    # -2.2 °C outside gives 4 cycles per hour
    assert climate.pwm.cycles_per_hour == 4
    assert climate.pwm.duty_cycle == (482, 418)
