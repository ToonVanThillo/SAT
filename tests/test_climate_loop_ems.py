"""Characterization tests for the climate loop against the EMS MQTT backend, beyond the basic write sequences."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import (
    CONF_DYNAMIC_MINIMUM_SETPOINT,
    CONF_DYNAMIC_MINIMUM_SETPOINT_VERSION,
    CONF_FORCE_PULSE_WIDTH_MODULATION,
    CONF_HUMIDITY_SENSOR_ENTITY_ID,
    CONF_MAXIMUM_RELATIVE_MODULATION,
    CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR,
    CONF_MAXIMUM_SETPOINT,
    CONF_MINIMUM_SETPOINT,
    CONF_RADIATORS,
)
from tests.conftest import advance
from tests.const import IDLE, ems_entry, with_data
from tests.test_climate_loop import message, run_loop

pytestmark = pytest.mark.usefixtures("loop_entry")

INSIDE = "sensor.test_inside_sensor"
OUTSIDE = "sensor.test_outside_sensor"
RADIATOR = "climate.radiator"


async def cycle(hass: HomeAssistant, climate: SatClimate, boiler_data, freezer: FrozenDateTimeFactory, **data) -> None:
    """Feed boiler data, advance past the debounces and run one loop."""
    await boiler_data(**data)
    await advance(hass, freezer, 10)
    await climate.async_control_heating_loop()


# Control setpoint

@pytest.mark.parametrize(*ems_entry({CONF_MAXIMUM_SETPOINT: 30}))
async def test_setpoint_is_clamped_to_the_maximum_setpoint(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "on"})

    # The climate keeps the unclamped setpoint, only the written value is clamped
    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 30}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
        message('{"cmd": "heatingoff", "value": "off"}'),
        message('{"cmd": "heatingtemp", "value": 30}'),
    ]
    assert climate.setpoint == 35.0


# The minimum setpoint is above COLD_SETPOINT (28.2), so the PWM ON phase requests heater ON and doesn't release the setpoint
@pytest.mark.parametrize(*with_data({CONF_MINIMUM_SETPOINT: 30}, {CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_MAXIMUM_SETPOINT: 29}))
async def test_pwm_setpoint_is_clamped_to_the_maximum_setpoint(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)

    assert (await run_loop(climate, mqtt_mock, published))[0] == message('{"cmd": "selflowtemp", "value": 29}')


@pytest.mark.parametrize(*ems_entry())
async def test_outside_temperature_changes_the_setpoint(hass, entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)
    assert (await run_loop(climate, mqtt_mock, published))[0] == message('{"cmd": "selflowtemp", "value": 35}')

    hass.states.async_set(OUTSIDE, "-5.0")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    # The setpoint is low-pass filtered (alpha 0.2) from the previous 35.0 to 37.2; the unchanged modulation is not sent again
    await climate.async_control_heating_loop()

    assert published() == [message('{"cmd": "selflowtemp", "value": 37}')]
    assert hass.states.get(climate.entity_id).attributes["outside_temperature"] == -5.0


@pytest.mark.parametrize(*with_data({CONF_HUMIDITY_SENSOR_ENTITY_ID: "sensor.test_humidity"}))
async def test_humidity_is_exposed_as_attribute(hass, entry, climate, boiler_data, mqtt_mock, published):
    hass.states.async_set("sensor.test_humidity", "55")
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published)

    assert climate.current_humidity == 55.0
    assert hass.states.get(climate.entity_id).attributes["current_humidity"] == 55.0


# Loop guard

@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize("sensor", [INSIDE, OUTSIDE])
async def test_loop_publishes_nothing_without_a_temperature(hass, entry, climate, boiler_data, mqtt_mock, published, sensor):
    await boiler_data(**IDLE)
    hass.states.async_set(sensor, "unavailable")

    assert await run_loop(climate, mqtt_mock, published) == []


@pytest.mark.parametrize(*ems_entry())
@pytest.mark.parametrize("sensor", [INSIDE, OUTSIDE])
async def test_loop_publishes_nothing_with_a_non_numeric_temperature(hass, entry, climate, boiler_data, mqtt_mock, published, sensor):
    await boiler_data(**IDLE)
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()
    hass.states.async_set(sensor, "abc")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    await climate.async_control_heating_loop()

    assert published() == []


# Heater state

@pytest.mark.parametrize(*with_data({CONF_RADIATORS: [RADIATOR]}))
@pytest.mark.parametrize(("hvac_action", "expected"), [("idle", []), ("heating", [message('{"cmd": "heatingoff", "value": "off"}')])])
async def test_heater_start_depends_on_open_valves(hass, entry, climate, boiler_data, mqtt_mock, published, hvac_action, expected):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set(RADIATOR, "heat", {"hvac_action": hvac_action, "hvac_modes": ["heat", "off"]})
    mqtt_mock.async_publish.reset_mock()

    await climate.async_control_heating_loop()

    assert published()[2:] == expected
    assert hass.states.get(climate.entity_id).attributes["valves_open"] is (hvac_action == "heating")


@pytest.mark.parametrize(*ems_entry())
async def test_heater_state_follows_the_setpoint_on_consecutive_loops(entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE | {"heatingoff": "on"})
    assert message('{"cmd": "heatingoff", "value": "off"}') in await run_loop(climate, mqtt_mock, published)

    # Heating now reads back as enabled: no further start
    freezer.tick(1)
    await boiler_data(heatingoff="off")
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert [call for call in published() if "heatingoff" in call[1]] == []

    # Cold target: the enabled heater is stopped
    await climate.async_set_target_temperature(15.0)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_control_heating_loop()
    assert published()[-1] == message('{"cmd": "heatingoff", "value": "on"}')


# Max relative modulation

@pytest.mark.parametrize(ems_entry()[0], [row for maximum in (80, 134) for row in ems_entry({CONF_MAXIMUM_RELATIVE_MODULATION: maximum})[1]])
@pytest.mark.parametrize("matches", [True, False])
async def test_modulation_is_written_only_when_it_differs(entry, options, climate, boiler_data, mqtt_mock, published, matches):
    maximum = options[CONF_MAXIMUM_RELATIVE_MODULATION]
    await boiler_data(**IDLE | {"burnmaxpower": maximum if matches else 100})

    expected = [] if matches else [message(f'{{"cmd": "burnmaxpower", "value": {maximum}}}')]
    assert (await run_loop(climate, mqtt_mock, published))[1:] == expected


@pytest.mark.parametrize(*ems_entry({CONF_FORCE_PULSE_WIDTH_MODULATION: True, CONF_MAXIMUM_RELATIVE_MODULATION: 134, CONF_MAXIMUM_RELATIVE_MODULATION_FLOOR: 120}))
async def test_modulation_floor_above_100_is_written_with_pwm(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"burnmaxpower": 100})

    assert message('{"cmd": "burnmaxpower", "value": 120}') in await run_loop(climate, mqtt_mock, published)


@pytest.mark.parametrize(*ems_entry())
async def test_modulation_stays_maximum_without_pwm(entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE | {"heatingoff": "off"})

    assert await run_loop(climate, mqtt_mock, published) == [
        message('{"cmd": "selflowtemp", "value": 35}'),
        message('{"cmd": "burnmaxpower", "value": 100}'),
    ]


# HVAC mode OFF

@pytest.mark.parametrize(*ems_entry())
async def test_only_heater_off_after_hvac_off_until_heat_again(hass, entry, climate, boiler_data, mqtt_mock, published, freezer):
    await boiler_data(**IDLE | {"heatingoff": "off"})
    await climate.async_set_target_temperature(21.0)
    mqtt_mock.async_publish.reset_mock()
    await climate.async_set_hvac_mode(HVACMode.OFF)

    # The OFF cycle, the scheduled ones and a direct one only stop the heater, once
    await advance(hass, freezer, 40)
    await climate.async_control_heating_loop()
    assert published() == [message('{"cmd": "heatingoff", "value": "on"}')]
    mqtt_mock.async_publish.reset_mock()

    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_control_heating_loop()
    assert published()[0] == message('{"cmd": "selflowtemp", "value": 35}')


# Overshoot handling

OVERSHOOT = IDLE | {"curflowtemp": 50, "selflowtemp": 40, "burngas": "on", "heatingactive": "on"}


@pytest.mark.parametrize(*ems_entry())
async def test_overshoot_does_not_enable_pwm_by_default(hass, entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**OVERSHOOT)
    await run_loop(climate, mqtt_mock, published)

    # current behaviour, known bug: the loop enables PWM on overshoot, then resets (and so disables) it again
    # because the PWM-enabled condition is not met
    assert hass.states.get(climate.entity_id).attributes["pulse_width_modulation_enabled"] is False


@pytest.mark.parametrize(*ems_entry({CONF_DYNAMIC_MINIMUM_SETPOINT: True, CONF_DYNAMIC_MINIMUM_SETPOINT_VERSION: 2}))
async def test_overshoot_handling_enables_pwm(hass, entry, climate, boiler_data, mqtt_mock, published):
    await boiler_data(**IDLE)
    await run_loop(climate, mqtt_mock, published)
    assert hass.states.get(climate.entity_id).attributes["pulse_width_modulation_enabled"] is False

    await boiler_data(**OVERSHOOT)
    await climate.async_control_heating_loop()

    assert hass.states.get(climate.entity_id).attributes["pulse_width_modulation_enabled"] is True
    assert climate.pulse_width_modulation_enabled


# Coordinator attributes

@pytest.mark.parametrize(*ems_entry())
async def test_boiler_attributes_follow_the_boiler_temperature(hass, entry, climate, boiler_data, freezer):
    def attributes() -> dict:
        return hass.states.get(climate.entity_id).attributes

    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    await cycle(hass, climate, boiler_data, freezer, **IDLE | {"curflowtemp": 40})
    assert attributes()["boiler_temperature_cold"] == 40
    assert attributes()["boiler_temperature_derivative"] is None
    assert attributes()["boiler_temperature_tracking"] is False

    await cycle(hass, climate, boiler_data, freezer, **IDLE | {"curflowtemp": 50, "burngas": "on", "heatingactive": "on"})
    assert attributes()["boiler_temperature_derivative"] > 0
    assert attributes()["boiler_temperature_cold"] == 40

    await cycle(hass, climate, boiler_data, freezer, **IDLE | {"curflowtemp": 45, "burngas": "on", "heatingactive": "on"})
    assert attributes()["boiler_temperature_derivative"] < 0
    assert attributes()["boiler_temperature_cold"] == 40


@pytest.mark.parametrize(*ems_entry())
async def test_flame_timing_attribute_is_the_average_on_time(hass, entry, climate, boiler_data, freezer):
    async def step(burngas: str, seconds: float) -> object:
        await advance(hass, freezer, seconds)
        await boiler_data(**IDLE | {"burngas": burngas})
        await climate.async_control_heating_loop()

        return hass.states.get(climate.entity_id).attributes["boiler_flame_timing"]

    await climate.async_set_target_temperature(21.0)
    await climate.async_set_hvac_mode(HVACMode.HEAT)

    # No average until the first flame cycle has completed; the first live flame value is off, so the first burn is a full one
    assert await step("off", 0) is None
    assert await step("on", 0) is None
    assert await step("on", 60) is None
    assert await step("off", 10) is None
    assert await step("on", 30) is None

    # The first average is the on time of the second flame cycle
    assert await step("on", 60) == 60.0
