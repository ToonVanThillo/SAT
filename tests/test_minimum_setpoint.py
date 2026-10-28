"""Logging of the dynamic minimum setpoint (version 1)."""
import logging
from types import SimpleNamespace

import pytest

from custom_components.sat.minimum_setpoint import MinimumSetpoint

LOGGER = "custom_components.sat.minimum_setpoint"


def messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def state(return_temperature: float | None) -> SimpleNamespace:
    return SimpleNamespace(return_temperature=return_temperature)


def test_warming_up_none_then_value(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    minimum = MinimumSetpoint(adjustment_factor=1.0, configured_minimum_setpoint=30.0)

    # Without a return temperature no base is set and nothing is logged; pytest's log capture fails the test on a record that cannot be formatted
    minimum.warming_up(state(None))  # type: ignore[arg-type]
    assert messages(caplog) == []

    minimum.warming_up(state(41.25))  # type: ignore[arg-type]
    assert messages(caplog) == ["New temperature set to: 41.2°C."]


@pytest.mark.xfail(reason="current behaviour, known bug: update_state calls math.isclose with None", raises=TypeError, strict=True)
def test_warming_up_value_then_none_again(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    minimum = MinimumSetpoint(adjustment_factor=1.0, configured_minimum_setpoint=30.0)
    minimum.warming_up(state(40.0))  # type: ignore[arg-type]
    caplog.clear()

    minimum.warming_up(state(None))  # type: ignore[arg-type]
    assert messages(caplog) == []


def test_warming_up_unchanged_value_logs_once(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    minimum = MinimumSetpoint(adjustment_factor=1.0, configured_minimum_setpoint=30.0)
    minimum.warming_up(state(40.0))  # type: ignore[arg-type]
    minimum.warming_up(state(40.0))  # type: ignore[arg-type]
    assert messages(caplog) == ["New temperature set to: 40.0°C."]
