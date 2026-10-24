"""Tests for `SatClimate._heater_state`, which decides the heater request from the control setpoint alone."""
import pytest

from custom_components.sat.climate import SatClimate
from custom_components.sat.const import COLD_SETPOINT, MINIMUM_SETPOINT
from custom_components.sat.coordinator import DeviceState


@pytest.mark.parametrize(("setpoint", "expected"), [
    (None, DeviceState.OFF),
    (MINIMUM_SETPOINT, DeviceState.OFF),
    (COLD_SETPOINT - 0.1, DeviceState.OFF),
    (COLD_SETPOINT, DeviceState.OFF),
    (COLD_SETPOINT + 0.1, DeviceState.ON),
    (50.0, DeviceState.ON),
])
def test_heater_state_decides_from_the_setpoint_alone(setpoint, expected):
    assert SatClimate._heater_state(setpoint) is expected
