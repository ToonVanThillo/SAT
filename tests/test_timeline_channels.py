"""Timeline logging of the channel engine (unit level), from the spec: send, resend, rewrite, release and problem transitions."""
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.sat.channels import (
    RELEASE,
    ChannelDefinition,
    Channels,
    DesiredState,
    Quantity,
    WriteKind,
)
from custom_components.sat.coordinator import DeviceState, Reading
from tests.conftest import TIMELINE

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CS = Quantity.CONTROL_SETPOINT
HS = Quantity.HEATER_STATE


@pytest.fixture(autouse=True)
def frozen(freezer, caplog):
    freezer.move_to(T0)
    caplog.set_level(logging.DEBUG, logger=TIMELINE)


class Fake:
    def __init__(self, order=None):
        self.calls = []
        self.fail = False
        self.order = order

    async def write(self, value):
        if self.order is not None:
            self.order.append(("write", value))
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(value)


def snap(**readings):
    return SimpleNamespace(readings=readings)


def live(value, seconds=-60):
    return Reading(value=value, received_at=T0 + timedelta(seconds=seconds))


def make(quantity=CS, fake=None, **kwargs):
    fake = fake or Fake()
    definition = ChannelDefinition(quantity=quantity, write=fake.write, read=lambda s: s.readings.get(quantity.value), **kwargs)
    return definition, fake


def lines(caplog, level=logging.INFO):
    return [r.getMessage() for r in caplog.records if r.name == TIMELINE and r.levelno == level]


async def test_first_send_logs_send_before_the_write(caplog):
    order = []
    definition, _fake = make(fake=Fake(order))
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))

    class Handler(logging.Handler):
        def emit(self, record):
            order.append(("log", record.getMessage()))

    handler = Handler()
    logging.getLogger(TIMELINE).addHandler(handler)
    try:
        records = await channels.async_write(DesiredState(control_setpoint=50.0))
    finally:
        logging.getLogger(TIMELINE).removeHandler(handler)

    assert order == [("log", "send control_setpoint=50.0"), ("write", 50.0)]
    assert [(r.quantity, r.value, r.kind) for r in records] == [(CS, 50.0, WriteKind.SEND)]


async def test_same_value_again_is_not_logged_and_not_written(caplog):
    definition, _fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))
    caplog.clear()

    assert await channels.async_write(DesiredState(control_setpoint=50.0)) == []
    assert lines(caplog) == []


async def test_encoding_change_shows_requested(caplog):
    definition, _ = make(encode=lambda v: 0 if v == 10 else v)
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(5)))
    await channels.async_write(DesiredState(control_setpoint=10))

    assert lines(caplog) == ["send control_setpoint=0 (requested 10)"]


async def test_enum_is_shown_by_name(caplog):
    definition, _ = make(HS)
    channels = Channels([definition])
    channels.verify(snap(heater_state=live(DeviceState.ON)))
    await channels.async_write(DesiredState(heater_state=DeviceState.OFF))

    assert lines(caplog) == ["send heater_state=OFF"]


async def test_resend_after_mismatch_logs_resend_and_drift(caplog, freezer):
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))

    freezer.move_to(T0 + timedelta(seconds=10))
    channels.verify(snap(control_setpoint=live(50.0, 5)))
    caplog.clear()
    freezer.move_to(T0 + timedelta(seconds=20))
    changes = channels.verify(snap(control_setpoint=live(40.0, 15)))
    assert [(c.previous.name, c.current.name) for c in changes] == [("IN_SYNC", "DRIFTED")]
    assert lines(caplog) == ["channel control_setpoint IN_SYNC -> DRIFTED (sent 50.0, read back 40.0)"]

    caplog.clear()
    records = await channels.async_write(DesiredState(control_setpoint=50.0))
    assert lines(caplog) == ["resend control_setpoint=50.0 (read back 40.0)"]
    assert [r.kind for r in records] == [WriteKind.RESEND]
    assert fake.calls == [50.0, 50.0]


async def test_pending_to_in_sync_handshake_is_not_logged(caplog, freezer):
    definition, _ = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))
    caplog.clear()
    freezer.move_to(T0 + timedelta(seconds=10))

    channels.verify(snap(control_setpoint=live(50.0, 5)))

    assert lines(caplog) == []


async def test_out_of_sync_transitions_in_and_out(caplog, freezer):
    definition, _ = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))
    caplog.clear()

    freezer.move_to(T0 + timedelta(seconds=10))
    channels.verify(snap(control_setpoint=live(40.0, 5)))
    assert lines(caplog) == ["channel control_setpoint PENDING -> OUT_OF_SYNC (sent 50.0, read back 40.0)"]

    # Staying out of sync is not a transition
    caplog.clear()
    await channels.async_write(DesiredState(control_setpoint=50.0))
    freezer.move_to(T0 + timedelta(seconds=20))
    channels.verify(snap(control_setpoint=live(40.0, 15)))
    assert [m for m in lines(caplog) if m.startswith("channel")] == []

    await channels.async_write(DesiredState(control_setpoint=50.0))
    caplog.clear()
    freezer.move_to(T0 + timedelta(seconds=30))
    channels.verify(snap(control_setpoint=live(50.0, 25)))
    assert lines(caplog) == ["channel control_setpoint OUT_OF_SYNC -> IN_SYNC (sent 50.0, read back 50.0)"]


async def test_write_every_cycle_rewrite_is_not_logged(caplog):
    definition, fake = make(write_every_cycle=True)
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))
    caplog.clear()

    records = await channels.async_write(DesiredState(control_setpoint=50.0))

    assert fake.calls == [50.0, 50.0]
    assert [r.kind for r in records] == [WriteKind.REWRITE]
    assert lines(caplog) == []


async def test_write_only_channel_sends_once_then_rewrites_silently(caplog):
    fake = Fake()
    channels = Channels([ChannelDefinition(quantity=Quantity.MAXIMUM_SETPOINT, write=fake.write)])

    first = await channels.async_write(DesiredState(maximum_setpoint=55.0))
    assert lines(caplog) == ["send maximum_setpoint=55.0"]

    caplog.clear()
    second = await channels.async_write(DesiredState(maximum_setpoint=55.0))
    assert lines(caplog) == []
    assert ([r.kind for r in first], [r.kind for r in second]) == ([WriteKind.SEND], [WriteKind.REWRITE])
    assert fake.calls == [55.0, 55.0]


async def test_release_logged_only_when_a_target_existed(caplog):
    definition, _ = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))

    await channels.async_write(DesiredState(control_setpoint=RELEASE))
    assert lines(caplog) == []

    await channels.async_write(DesiredState(control_setpoint=50.0))
    caplog.clear()
    await channels.async_write(DesiredState(control_setpoint=RELEASE))
    assert lines(caplog) == ["release control_setpoint"]

    caplog.clear()
    await channels.async_write(DesiredState(control_setpoint=RELEASE))
    assert lines(caplog) == []


async def test_failed_write_logs_send_then_the_failure(caplog):
    definition, fake = make()
    fake.fail = True
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))

    records = await channels.async_write(DesiredState(control_setpoint=50.0))

    assert records == []
    ordered = [(r.name, r.getMessage()) for r in caplog.records if r.name in (TIMELINE, "custom_components.sat.channels")]
    assert ordered[0] == (TIMELINE, "send control_setpoint=50.0")
    assert ordered[1][0] == "custom_components.sat.channels"
    assert "Failed to write" in ordered[1][1]


async def test_adopted_read_back_is_not_a_send(caplog):
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(50.0)))

    assert await channels.async_write(DesiredState(control_setpoint=50.0)) == []

    assert fake.calls == []
    assert lines(caplog) == []


async def test_old_write_lines_are_gone(caplog):
    definition, _ = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    caplog.set_level(logging.DEBUG)
    await channels.async_write(DesiredState(control_setpoint=50.0))

    assert "Wrote " not in caplog.text
    assert "Publishing MQTT command" not in caplog.text


async def test_nothing_is_logged_at_info_when_the_logger_is_off(caplog):
    caplog.set_level(logging.WARNING, logger=TIMELINE)
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))

    assert fake.calls == [50.0]
    assert lines(caplog) == []


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no str")


async def test_unprintable_value_does_not_stop_the_write(caplog):
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    value = Unprintable()

    records = await channels.async_write(DesiredState(control_setpoint=value))  # type: ignore[arg-type]

    assert fake.calls == [value]
    assert [r.value for r in records] == [value]
    assert any(r.name == TIMELINE and r.levelno >= logging.WARNING for r in caplog.records)
