"""The timeline: boiler signal changes, commands and control edges at INFO, when they happen, and one line per control cycle at DEBUG.

All lines go through the logger `custom_components.sat.timeline`, so enabling it alone gives the whole timeline.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import fields
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec

if TYPE_CHECKING:
    from .channels import DesiredState, StatusChange, WriteRecord

LOGGER = logging.getLogger(__name__)

_P = ParamSpec("_P")

# Whether a failure to build a line was logged with its traceback; later ones are logged without
_failure_logged = False

# Short names of the `DesiredState` fields in the cycle line
_DESIRED_NAMES = {
    "control_setpoint": "setpoint",
    "heater_state": "heater",
    "maximum_relative_modulation": "relmod",
    "maximum_setpoint": "maxsp",
    "thermostat_setpoint": "thermostat",
}


def _never_raises(func: Callable[_P, None]) -> Callable[_P, None]:
    """Catch and log a failure to build a timeline line, so logging never stops the caller; only the first failure has a traceback."""
    @wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> None:
        global _failure_logged
        try:
            func(*args, **kwargs)
        except Exception as err:
            if _failure_logged:
                LOGGER.warning("Failed to log a timeline line: %s", err)
            else:
                _failure_logged = True
                LOGGER.exception("Failed to log a timeline line")

    return wrapper


def fmt(value: Any) -> str:
    """Format a value for the timeline: enums by name, booleans as on/off, None (unknown) as `?`."""
    if value is None:
        return "?"

    if isinstance(value, bool):
        return "on" if value else "off"

    if isinstance(value, Enum):
        return value.name

    return str(value)


def fmt_number(value: float | None, signed: bool = False) -> str:
    """Format a number for the timeline with one decimal, with an explicit sign if `signed`; None (unknown) as `?`.

    A value rounding to zero prints as `0.0`, or `+0.0` when signed, never with a minus sign.
    """
    if value is None:
        return "?"

    # Adding 0.0 turns a negative zero into a positive one
    rounded = round(value, 1) + 0.0
    return f"{rounded:+.1f}" if signed else f"{rounded:.1f}"


@_never_raises
def boiler_change(key: str, previous: Any, value: Any) -> None:
    """Log a change of a traced raw device value on arrival; `previous` is None when unknown."""
    if LOGGER.isEnabledFor(logging.INFO):
        LOGGER.info("boiler %s %s -> %s", key, fmt(previous), fmt(value))


@_never_raises
def send(name: str, value: Any, requested: Any) -> None:
    """Log a write of a new value, right before it is sent; the requested value is shown only when encoding changed it."""
    if not LOGGER.isEnabledFor(logging.INFO):
        return

    if requested == value:
        LOGGER.info("send %s=%s", name, fmt(value))
    else:
        LOGGER.info("send %s=%s (requested %s)", name, fmt(value), fmt(requested))


@_never_raises
def resend(name: str, value: Any, read_back: Any) -> None:
    """Log a rewrite of the same value because the device read back something else, right before it is sent."""
    if LOGGER.isEnabledFor(logging.INFO):
        LOGGER.info("resend %s=%s (read back %s)", name, fmt(value), fmt(read_back))


def release(name: str) -> None:
    """Log that a channel drops its target."""
    LOGGER.info("release %s", name)


@_never_raises
def channel_status(name: str, previous: Enum, current: Enum, sent: Any, read_back: Any) -> None:
    """Log a channel status transition into or out of a problem status."""
    if LOGGER.isEnabledFor(logging.INFO):
        LOGGER.info("channel %s %s -> %s (sent %s, read back %s)", name, previous.name, current.name, fmt(sent), fmt(read_back))


def desired(state: DesiredState) -> str:
    """Format the requested fields of a desired state; fields left at None are omitted."""
    return " ".join(f"{_DESIRED_NAMES.get(field.name, field.name)}={fmt(value)}" for field in fields(state) if (value := getattr(state, field.name)) is not None) or "-"


def writes(records: Iterable[WriteRecord]) -> str:
    """Format the writes of a cycle; a resend or rewrite of the same value is marked with its kind."""
    from .channels import WriteKind

    return " ".join(
        f"{record.quantity.value}={fmt(record.value)}" + ("" if record.kind == WriteKind.SEND else f"({record.kind.value})")
        for record in records
    ) or "-"


def verified(changes: Iterable[StatusChange]) -> str:
    """Format the cycle line segment of the channel status changes found by a verify; empty without changes."""
    formatted = " ".join(f"{change.quantity.value} {change.previous.name}->{change.current.name}" for change in changes)
    return f"verify {formatted}" if formatted else ""


@_never_raises
def cycle(number: int, build: Callable[[], Iterable[str]]) -> None:
    """Log the cycle line of cycle `number` at debug; `build` returns its segments and only runs when debug is enabled. Empty segments are left out."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("cycle %d | %s", number, " | ".join(segment for segment in build() if segment))
