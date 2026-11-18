"""Structured log events (`logged_event`, `annotate_event`): one debug record per run with its fields, rendered lazily, failures never raised by logging."""
import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

import pytest

from custom_components.sat.logctx import annotate_event, logged_event

LOGGER = "custom_components.sat.timeline"


class Collector(logging.Handler):
    """A regular handler (pytest's caplog re-raises handler errors): keeps the records and what it renders, and counts the errors it is given."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.messages: list[str] = []
        self.errors = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            self.messages.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self.errors += 1


@pytest.fixture
def collector() -> Iterator[Collector]:
    handler = Collector()
    logger = logging.getLogger(LOGGER)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)
    logger.propagate, logger.level = propagate, level


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no str")


@logged_event("run")
async def run(value: object = 1, fail: BaseException | None = None) -> str:
    annotate_event(value=value)
    if fail is not None:
        raise fail

    return "done"


async def test_a_run_logs_one_debug_record_with_its_fields(collector):
    assert await run(1.26) == "done"

    [record] = collector.records
    assert record.levelno == logging.DEBUG
    assert list(record.fields)[:2] == ["event", "value"]  # type: ignore[attr-defined]
    assert record.fields["value"] == 1.26  # type: ignore[attr-defined]
    assert "error" not in record.fields  # type: ignore[attr-defined]
    assert collector.messages[0] == 'run value="1.3"'


async def test_the_message_is_not_built_below_debug(collector):
    built: list[int] = []

    class Counting:
        def __str__(self) -> str:
            built.append(1)
            return "x"

    logging.getLogger(LOGGER).setLevel(logging.INFO)
    await run(Counting())
    assert collector.records == []
    assert built == []

    logging.getLogger(LOGGER).setLevel(logging.DEBUG)
    await run(Counting())
    assert built


async def test_a_rendering_failure_is_given_to_the_logging_system_and_not_raised(collector):
    assert await run(Unprintable()) == "done"

    assert collector.errors == 1
    [record] = collector.records
    assert isinstance(record.fields["value"], Unprintable)  # type: ignore[attr-defined]


@pytest.mark.parametrize(("failure", "error"), [
    (ValueError('bad "x"'), 'ValueError: bad "x"'),
    (asyncio.CancelledError(), "CancelledError: "),
])
async def test_a_failing_or_cancelled_run_logs_its_error_and_the_exception_propagates(collector, failure, error):
    with pytest.raises(type(failure)):
        await run(2, fail=failure)

    [record] = collector.records
    assert record.fields["error"] == error  # type: ignore[attr-defined]
    assert record.fields["value"] == 2  # type: ignore[attr-defined]


class Mode(Enum):
    ECO = 1


@dataclass
class Item:
    on: bool
    level: float | None
    mode: Mode


async def test_values_render_by_type_and_the_fields_keep_the_objects(collector):
    value = {"item": Item(on=True, level=None, mode=Mode.ECO), "pair": (1.26, False), "items": [Item(on=False, level=2.0, mode=Mode.ECO)]}

    await run(value)

    assert collector.records[0].fields["value"] is value  # type: ignore[attr-defined]
    assert collector.messages[0] == (
        'run value="{item:{on:true, level:?, mode:ECO}, pair:[1.3, false], items:[{on:false, level:2.0, mode:ECO}]}"'
    )


async def test_a_double_quote_in_a_value_is_escaped(collector):
    await run('say "hi"')

    assert collector.messages[0] == r'run value="say \"hi\""'


def test_annotating_outside_an_event_does_nothing(collector):
    annotate_event(value=1)

    assert collector.records == []
