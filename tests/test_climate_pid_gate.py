"""Tests for the boiler-temperature gate on the PID updates in the climate's sensor-event control path."""
from unittest.mock import PropertyMock, patch

import pytest

from custom_components.sat.const import CONF_ROOMS
from tests.conftest import advance
from tests.const import with_data
from tests.test_climate_inputs import ROOM
from tests.test_climate_loop_ems import INSIDE, OUTSIDE

pytestmark = pytest.mark.usefixtures("loop_entry")


async def sensor_change(hass, value: str = "19.0") -> None:
    """Change the inside sensor, which runs the non-reset PID control."""
    hass.states.async_set(INSIDE, value)
    await hass.async_block_till_done()


async def prepare(hass, climate, freezer) -> None:
    """Give the climate a target and a valid outside temperature, and let the PID accept updates."""
    await climate.async_set_target_temperature(21.0)
    hass.states.async_set(OUTSIDE, "5.0")
    hass.states.async_set(ROOM, "heat", {"temperature": 22, "current_temperature": 18})
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)


@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_sensor_change_updates_climate_and_area_pids_with_a_boiler_temperature(hass, entry, climate, boiler_data, freezer):
    await prepare(hass, climate, freezer)
    await boiler_data(curflowtemp=40)
    assert climate._coordinator.boiler_temperature == 40
    area = climate.areas._areas[0]
    before = (climate.pid.last_error, area.pid.last_error)

    # Inside 10.0 gives a climate error of 11.0, above the room error of 4.0
    await sensor_change(hass, "10.0")

    assert before[1] == 0
    assert (climate.pid.last_error, area.pid.last_error) == (11.0, 4.0)


@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_sensor_change_does_not_update_pids_without_any_boiler_temperature(hass, entry, climate, freezer):
    assert climate._coordinator.boiler_temperature is None
    await prepare(hass, climate, freezer)
    area = climate.areas._areas[0]
    before = (climate.pid.last_error, area.pid.last_error)

    await sensor_change(hass, "10.0")

    assert (climate.pid.last_error, area.pid.last_error) == before


@pytest.mark.parametrize(*with_data({CONF_ROOMS: [ROOM]}))
async def test_sensor_change_does_not_update_pids_when_boiler_temperature_is_gone_but_samples_remain(hass, entry, climate, boiler_data, freezer):
    """Test for the behaviour change: with 2+ samples in the 60 s buffer but no current boiler temperature, the PIDs no longer update."""
    coordinator = climate._coordinator
    await prepare(hass, climate, freezer)
    # Two samples within 60 s
    await boiler_data(curflowtemp=42)
    await coordinator.async_control_heating_loop()
    freezer.tick(5)
    await boiler_data(curflowtemp=43)
    await coordinator.async_control_heating_loop()
    assert len(coordinator._boiler_temperatures) == 2
    area = climate.areas._areas[0]
    before = (climate.pid.last_error, area.pid.last_error)

    with patch.object(type(coordinator), "boiler_temperature", new_callable=PropertyMock, return_value=None):
        await sensor_change(hass, "10.0")

    assert (climate.pid.last_error, area.pid.last_error) == before
