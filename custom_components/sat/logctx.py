"""Structured log events on the timeline logger: fields annotated while a run is open, logged as one record when it ends."""
from __future__ import annotations

import contextvars
import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any, ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")

logger = logging.getLogger("custom_components.sat.timeline")
_event: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("log_event", default=None)


def logged_event(name: str) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Coroutine[Any, Any, _R]]]:
    """Run the decorated async function inside `log_event(name)`; it stays a coroutine function."""
    def decorator(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Coroutine[Any, Any, _R]]:
        @functools.wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with log_event(name):
                return await func(*args, **kwargs)
        return wrapper
    return decorator


@contextmanager
def log_event(name: str) -> Iterator[None]:
    """Open an event; when the block ends, also on failure or cancellation, log it at debug as `name key="value" ...`, with the fields as
    `fields`. A failure or cancellation adds `error` and is re-raised."""
    event: dict[str, Any] = {"event": name}
    token = _event.set(event)
    try:
        yield
    except BaseException as exc:
        event["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _event.reset(token)
        logger.debug("%s %s", name, _Fields(event), extra={"fields": event})


def annotate_event(**fields: Any) -> None:
    """Add fields to the current log line. No-op if no event is open."""
    event = _event.get()
    if event is not None:
        event.update(fields)


def fmt(value: Any) -> str:
    """Render a value: None as `?`, booleans as true/false, enums by name, floats with one decimal (never `-0.0`), dataclasses (by their
    fields) and dicts as `{key:value,...}`, lists and tuples as `[...]`, their items with the same rules; others with str()."""
    if value is None:
        return "?"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, Enum):
        return value.name

    if isinstance(value, float):
        # Adding 0.0 turns a negative zero into a positive one
        return f"{round(value, 1) + 0.0:.1f}"

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return "{" + ", ".join(f"{field.name}:{fmt(getattr(value, field.name))}" for field in dataclasses.fields(value)) + "}"

    if isinstance(value, dict):
        return "{" + ", ".join(f"{fmt(key)}:{fmt(item)}" for key, item in value.items()) + "}"

    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(fmt(item) for item in value) + "]"

    return str(value)


class _Fields:
    """The fields of an event as `key="value"` in insertion order, a double quote in a value escaped as `\\"`; rendered only when the log
    message is built."""

    def __init__(self, event: dict[str, Any]) -> None:
        self._event = event

    def __str__(self) -> str:
        return " ".join(key + '="' + fmt(value).replace('"', r'\"') + '"' for key, value in self._event.items() if key != "event")
