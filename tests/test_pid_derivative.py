"""The PID derivative: the slope of the error is filtered twice, each time with `ema(value, previous, alpha)`."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.sat.errors import Error
from custom_components.sat.pid import PID


def pid_with_error_3() -> PID:
    pid = PID("radiators", 0, 1.0, 1.0, 1.0, 1.0, 1.0, sample_time_limit=None, version=3)
    pid.update_reset(Error("climate.room", 3.0), 30.0)
    return pid


def test_the_pid_derivative_is_filtered_twice_from_a_zero_start():
    clock = SimpleNamespace(now=1000.0)
    with patch("custom_components.sat.pid.monotonic", lambda: clock.now):
        pid = pid_with_error_3()
        clock.now += 10
        pid.update(Error("climate.room", 5.0), 30.0)

    # The slope 0.2 per second goes through the two filters (0.8, then 0.6) from a derivative of 0
    assert pid.raw_derivative == pytest.approx(0.6 * (0.8 * 0.2))


def test_the_pid_derivative_keeps_the_weight_of_its_history():
    clock = SimpleNamespace(now=1000.0)
    with patch("custom_components.sat.pid.monotonic", lambda: clock.now):
        pid = pid_with_error_3()
        clock.now += 10
        pid.update(Error("climate.room", 5.0), 30.0)
        first = pid.raw_derivative
        clock.now += 10
        pid.update(Error("climate.room", 8.0), 30.0)

    filtered = 0.8 * 0.3 + 0.2 * first
    assert pid.raw_derivative == pytest.approx(0.6 * filtered + 0.4 * first, abs=1e-3)
