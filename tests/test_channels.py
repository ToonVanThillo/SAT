"""Tests for the channel engine, using fake channel definitions."""
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.sat.channels import (
    ChannelDefinition,
    Channels,
    ChannelStatus,
    DesiredState,
    Quantity,
)
from custom_components.sat.coordinator import Reading

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CS = Quantity.CONTROL_SETPOINT
MS = Quantity.MAXIMUM_SETPOINT
TS = Quantity.THERMOSTAT_SETPOINT


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to(T0)


class Fake:
    """Records writes; raises while `fail` is set."""

    def __init__(self, log=None, tag=None):
        self.calls = []
        self.fail = False
        self.log, self.tag = log, tag

    async def write(self, value):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(value)
        if self.log is not None:
            self.log.append(self.tag)


def snap(**readings):
    return SimpleNamespace(readings=readings)


def rw(quantity=CS, fake=None, **kwargs):
    fake = fake or Fake()
    return ChannelDefinition(quantity=quantity, write=fake.write, read=lambda s: s.readings.get(quantity), **kwargs), fake


def live(value, at):
    return Reading(value=value, received_at=at)


def channel_records(caplog):
    """Records from the channels logger."""
    return [r for r in caplog.records if r.name == "custom_components.sat.channels"]


def logged(caplog):
    """(level, has traceback) of each record from the channels logger."""
    return [(r.levelno, r.exc_info is not None) for r in channel_records(caplog)]


ERROR_WITH_TRACEBACK = (logging.ERROR, True)
WARNING_WITHOUT_TRACEBACK = (logging.WARNING, False)


async def test_initial_status():
    channels = Channels([rw()[0], ChannelDefinition(quantity=MS, read=lambda s: None), ChannelDefinition(quantity=TS, write=Fake().write)])

    assert {q: s.status for q, s in channels.states.items()} == {
        CS: ChannelStatus.IDLE, MS: ChannelStatus.READ_ONLY, TS: ChannelStatus.IDLE,
    }


async def test_full_cycle(freezer):
    definition, fake = rw()
    channels = Channels([definition])

    await channels.async_write(DesiredState())
    assert channels.states[CS].status == ChannelStatus.IDLE
    assert fake.calls == []

    await channels.async_write(DesiredState(control_setpoint=50.0))
    state = channels.states[CS]
    assert (state.status, state.target, state.sent, state.sent_at) == (ChannelStatus.PENDING, 50.0, 50.0, T0)

    freezer.move_to(T0 + timedelta(seconds=5))
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    state = channels.states[CS]
    assert (state.status, state.read_back, state.read_back_at) == (ChannelStatus.IN_SYNC, 50.0, T0 + timedelta(seconds=1))

    channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=2))))
    assert (channels.states[CS].status, channels.states[CS].drift_count) == (ChannelStatus.DRIFTED, 1)

    await channels.async_write(DesiredState(control_setpoint=50.0))
    assert fake.calls == [50.0, 50.0]
    assert channels.states[CS].status == ChannelStatus.DRIFTED
    assert channels.states[CS].sent_at == T0 + timedelta(seconds=5)

    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=6))))
    assert (channels.states[CS].status, channels.states[CS].drift_count) == (ChannelStatus.IN_SYNC, 1)


async def test_out_of_sync_on_first_mismatch_and_rewritten_each_cycle(freezer):
    definition, fake = rw()
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)

    for n in (1, 2):
        freezer.move_to(T0 + timedelta(seconds=10 * n))
        channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=10 * n - 1))))
        assert channels.states[CS].status == ChannelStatus.OUT_OF_SYNC
        await channels.async_write(desired)
        assert len(fake.calls) == 1 + n
        assert channels.states[CS].status == ChannelStatus.OUT_OF_SYNC

    assert channels.states[CS].drift_count == 0


async def test_stale_and_restored_readings_ignored():
    definition, _ = rw()
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))

    channels.verify(snap(control_setpoint=live(40.0, T0)))  # not strictly after send
    channels.verify(snap(control_setpoint=live(40.0, T0 - timedelta(seconds=1))))
    channels.verify(snap(control_setpoint=Reading(value=50.0, received_at=None)))
    assert channels.states[CS].status == ChannelStatus.PENDING

    channels.verify(snap())  # no reading at all
    assert channels.states[CS].status == ChannelStatus.PENDING


@pytest.mark.parametrize(("read_back", "status"), [(50.5, ChannelStatus.IN_SYNC), (50.6, ChannelStatus.OUT_OF_SYNC)])
async def test_tolerance_boundary(read_back, status):
    definition, _ = rw(tolerance=0.5)
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))

    channels.verify(snap(control_setpoint=live(read_back, T0 + timedelta(seconds=1))))
    assert channels.states[CS].status == status


async def test_non_numeric_compared_by_equality():
    definition, _ = rw(Quantity.HEATER_STATE, tolerance=5)
    channels = Channels([definition])
    await channels.async_write(DesiredState(heater_state="on"))

    channels.verify(snap(heater_state=live("off", T0 + timedelta(seconds=1))))
    assert channels.states[Quantity.HEATER_STATE].status == ChannelStatus.OUT_OF_SYNC
    channels.verify(snap(heater_state=live("on", T0 + timedelta(seconds=2))))
    assert channels.states[Quantity.HEATER_STATE].status == ChannelStatus.IN_SYNC


async def test_compares_against_encoded_value():
    definition, fake = rw(encode=lambda v: min(v, 60.0))
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=75.0))
    assert fake.calls == [60.0]

    channels.verify(snap(control_setpoint=live(60.0, T0 + timedelta(seconds=1))))
    state = channels.states[CS]
    assert (state.status, state.target, state.sent) == (ChannelStatus.IN_SYNC, 75.0, 60.0)


async def test_no_rewrite_without_fresh_mismatch(freezer):
    definition, fake = rw()
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)

    freezer.move_to(T0 + timedelta(seconds=10))
    mismatch = snap(control_setpoint=live(40.0, T0 + timedelta(seconds=5)))
    channels.verify(mismatch)
    await channels.async_write(desired)
    assert fake.calls == [50.0, 50.0]

    channels.verify(mismatch)  # read-back now predates the rewrite
    await channels.async_write(desired)
    assert fake.calls == [50.0, 50.0]
    assert channels.states[CS].status == ChannelStatus.OUT_OF_SYNC


async def test_unchanged_encoded_value_not_written():
    definition, fake = rw(encode=lambda v: min(v, 60.0))
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=75.0))
    await channels.async_write(DesiredState(control_setpoint=80.0))

    assert fake.calls == [60.0]
    assert (channels.states[CS].target, channels.states[CS].status) == (80.0, ChannelStatus.PENDING)


async def test_reading_on_idle_channel_recorded():
    definition, _ = rw()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(40.0, T0)))

    state = channels.states[CS]
    assert (state.status, state.read_back, state.read_back_at) == (ChannelStatus.IDLE, 40.0, T0)


async def test_none_keeps_target():
    definition, fake = rw()
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    await channels.async_write(DesiredState())

    assert channels.states[CS].target == 50.0
    assert fake.calls == [50.0]


async def test_changed_target_is_written(freezer):
    definition, fake = rw()
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    await channels.async_write(DesiredState(control_setpoint=55.0))

    assert fake.calls == [50.0, 55.0]


async def test_same_desired_twice_writes_once():
    definition, fake = rw()
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    await channels.async_write(desired)

    assert fake.calls == [50.0]
    assert channels.states[CS].status == ChannelStatus.IN_SYNC


async def test_write_every_cycle_stays_in_sync(freezer):
    definition, fake = rw(write_every_cycle=True)
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)

    for n in (1, 2):
        freezer.move_to(T0 + timedelta(seconds=10 * n))
        channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=10 * n - 1))))
        assert channels.states[CS].status == ChannelStatus.IN_SYNC
        await channels.async_write(desired)
        assert channels.states[CS].status == ChannelStatus.IN_SYNC

    assert fake.calls == [50.0, 50.0, 50.0]


async def test_write_failure_isolated_and_retried(caplog, freezer):
    (a, fa), (b, fb) = rw(CS), rw(MS)
    channels = Channels([a, b])
    desired = DesiredState(control_setpoint=50.0, maximum_setpoint=70.0)
    fa.fail = True
    with caplog.at_level(logging.WARNING):
        await channels.async_write(desired)

    assert logged(caplog) == [ERROR_WITH_TRACEBACK]
    assert fb.calls == [70.0]
    state = channels.states[CS]
    assert (state.status, state.sent, state.sent_at) == (ChannelStatus.OUT_OF_SYNC, None, None)
    assert channels.states[MS].status == ChannelStatus.PENDING

    fa.fail = False
    freezer.move_to(T0 + timedelta(seconds=5))
    await channels.async_write(desired)
    assert fa.calls == [50.0]
    assert channels.states[CS].status == ChannelStatus.PENDING


def fail(_):
    raise RuntimeError("boom")


async def test_encode_failure_isolated(caplog):
    (a, fa), (b, fb) = rw(CS, encode=fail), rw(MS)
    channels = Channels([a, b])
    with caplog.at_level(logging.WARNING):
        await channels.async_write(DesiredState(control_setpoint=50.0, maximum_setpoint=70.0))

    assert fa.calls == []
    assert fb.calls == [70.0]
    state = channels.states[CS]
    assert (state.status, state.target, state.sent) == (ChannelStatus.OUT_OF_SYNC, 50.0, None)
    assert logged(caplog) == [ERROR_WITH_TRACEBACK]


async def test_read_failure_isolated(caplog):
    a = ChannelDefinition(quantity=CS, write=Fake().write, read=fail)
    b, _ = rw(MS)
    channels = Channels([a, b])
    await channels.async_write(DesiredState(control_setpoint=50.0, maximum_setpoint=70.0))

    with caplog.at_level(logging.WARNING):
        channels.verify(snap(maximum_setpoint=live(70.0, T0 + timedelta(seconds=1))))
        assert channels.read(CS, snap()) is None

    assert channels.states[CS].status == ChannelStatus.PENDING
    assert channels.states[MS].status == ChannelStatus.IN_SYNC
    assert logged(caplog) == [ERROR_WITH_TRACEBACK, WARNING_WITHOUT_TRACEBACK]


async def test_failed_rewrite_keeps_previous_sent():
    definition, fake = rw()
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    fake.fail = True
    await channels.async_write(DesiredState(control_setpoint=55.0))

    state = channels.states[CS]
    assert (state.sent, state.sent_at, state.target) == (50.0, T0, 55.0)


async def test_write_only_writes_every_cycle_and_read_warns_once(caplog):
    fake = Fake()
    channels = Channels([ChannelDefinition(quantity=TS, write=fake.write)])
    assert channels.states[TS].status == ChannelStatus.IDLE
    await channels.async_write(DesiredState(thermostat_setpoint=20.0))
    await channels.async_write(DesiredState())

    assert fake.calls == [20.0, 20.0]
    assert channels.states[TS].status == ChannelStatus.WRITE_ONLY

    with caplog.at_level(logging.WARNING):
        assert channels.read(TS, snap()) is None
        assert channels.read(TS, snap()) is None
    assert logged(caplog) == [WARNING_WITHOUT_TRACEBACK]


async def test_read_returns_reading():
    definition, _ = rw()
    reading = live(50.0, T0)

    assert Channels([definition]).read(CS, snap(control_setpoint=reading)) == reading


async def test_read_only_ignores_request_and_warns_once(caplog):
    channels = Channels([ChannelDefinition(quantity=MS, read=lambda s: s.readings.get(MS))])
    with caplog.at_level(logging.WARNING):
        await channels.async_write(DesiredState(maximum_setpoint=70.0))
        await channels.async_write(DesiredState(maximum_setpoint=70.0))

    assert logged(caplog) == [WARNING_WITHOUT_TRACEBACK]
    assert (channels.states[MS].status, channels.states[MS].target) == (ChannelStatus.READ_ONLY, None)
    channels.verify(snap(maximum_setpoint=live(65.0, T0)))
    assert channels.states[MS].status == ChannelStatus.READ_ONLY
    assert channels.states[MS].read_back == 65.0


async def test_writes_in_definition_order_and_states_order():
    order = []
    defs = [rw(q, Fake(order, q))[0] for q in (TS, CS, MS)]
    channels = Channels(defs)
    await channels.async_write(DesiredState(control_setpoint=1.0, maximum_setpoint=2.0, thermostat_setpoint=3.0))

    assert order == [TS, CS, MS]
    assert list(channels.states) == [TS, CS, MS]


def test_quantity_values_match_desired_state_fields():
    assert {q.value for q in Quantity} == set(DesiredState.__dataclass_fields__)


def test_constructor_errors():
    with pytest.raises(ValueError):
        ChannelDefinition(quantity=CS)
    with pytest.raises(ValueError):
        Channels([rw(CS)[0], rw(CS)[0]])


async def test_states_is_read_only():
    states = Channels([rw()[0]]).states

    with pytest.raises(TypeError):
        states[CS] = None  # type: ignore[index]


@pytest.mark.parametrize("encode_fails", [True, False])
async def test_write_only_failure_stays_write_only(encode_fails):
    fake = Fake()
    fake.fail = not encode_fails
    channels = Channels([ChannelDefinition(quantity=TS, write=fake.write, encode=fail if encode_fails else lambda v: v)])
    await channels.async_write(DesiredState(thermostat_setpoint=20.0))

    state = channels.states[TS]
    assert (state.status, state.sent, state.sent_at) == (ChannelStatus.WRITE_ONLY, None, None)


async def test_failed_rewrite_of_same_value_gives_out_of_sync_and_is_retried(freezer):
    definition, fake = rw()
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    drifted = snap(control_setpoint=live(40.0, T0 + timedelta(seconds=2)))
    channels.verify(drifted)
    assert channels.states[CS].status == ChannelStatus.DRIFTED

    fake.fail = True
    freezer.move_to(T0 + timedelta(seconds=5))
    await channels.async_write(desired)

    state = channels.states[CS]
    assert (state.status, state.sent, state.sent_at, state.drift_count) == (ChannelStatus.OUT_OF_SYNC, 50.0, T0, 1)

    # The same read-back still postdates the last successful send, so the next cycle retries.
    fake.fail = False
    freezer.move_to(T0 + timedelta(seconds=10))
    channels.verify(drifted)
    await channels.async_write(desired)
    assert fake.calls == [50.0, 50.0]
    assert channels.states[CS].sent_at == T0 + timedelta(seconds=10)


async def test_drifted_alone_does_not_write(freezer):
    definition, fake = rw()
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=2))))
    await channels.async_write(desired)  # consumes the mismatch
    await channels.async_write(desired)  # no verify in between

    assert fake.calls == [50.0, 50.0]
    assert channels.states[CS].status == ChannelStatus.DRIFTED


async def test_verify_matching_stale_readback_again_stays_in_sync(freezer):
    definition, _ = rw()
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    matching = snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1)))

    channels.verify(matching)
    channels.verify(matching)
    assert (channels.states[CS].status, channels.states[CS].drift_count) == (ChannelStatus.IN_SYNC, 0)


async def test_new_value_after_mismatch_gives_pending():
    definition, fake = rw()
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=1))))
    await channels.async_write(DesiredState(control_setpoint=55.0))

    assert fake.calls == [50.0, 55.0]
    assert channels.states[CS].status == ChannelStatus.PENDING


async def test_write_every_cycle_keeps_out_of_sync_status():
    definition, fake = rw(write_every_cycle=True)
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)
    channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=1))))
    await channels.async_write(desired)

    assert fake.calls == [50.0, 50.0]
    assert channels.states[CS].status == ChannelStatus.OUT_OF_SYNC


async def test_encode_failure_after_send_keeps_sent_and_gives_out_of_sync(freezer):
    definition, fake = rw(encode=lambda v: fail(v) if v > 60 else v)
    channels = Channels([definition])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    freezer.move_to(T0 + timedelta(seconds=5))
    await channels.async_write(DesiredState(control_setpoint=70.0))

    state = channels.states[CS]
    assert (state.status, state.target, state.sent, state.sent_at) == (ChannelStatus.OUT_OF_SYNC, 70.0, 50.0, T0)
    assert fake.calls == [50.0]


async def test_second_mismatch_while_drifted_gives_out_of_sync(freezer):
    definition, _ = rw()
    channels = Channels([definition])
    desired = DesiredState(control_setpoint=50.0)
    await channels.async_write(desired)
    channels.verify(snap(control_setpoint=live(50.0, T0 + timedelta(seconds=1))))
    channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=2))))
    freezer.move_to(T0 + timedelta(seconds=5))
    await channels.async_write(desired)
    channels.verify(snap(control_setpoint=live(40.0, T0 + timedelta(seconds=6))))

    assert (channels.states[CS].status, channels.states[CS].drift_count) == (ChannelStatus.OUT_OF_SYNC, 1)


async def test_write_failure_traceback_only_first_of_consecutive(caplog):
    definition, fake = rw()
    channels = Channels([definition])
    fake.fail = True
    with caplog.at_level(logging.WARNING):
        await channels.async_write(DesiredState(control_setpoint=50.0))
        await channels.async_write(DesiredState(control_setpoint=50.0))
        fake.fail = False
        await channels.async_write(DesiredState(control_setpoint=50.0))
        fake.fail = True
        await channels.async_write(DesiredState(control_setpoint=55.0))

    assert logged(caplog) == [ERROR_WITH_TRACEBACK, WARNING_WITHOUT_TRACEBACK, ERROR_WITH_TRACEBACK]
    warning = channel_records(caplog)[1].getMessage()
    assert "boom" in warning and "control_setpoint" in warning


async def test_read_failure_traceback_only_first_of_consecutive(caplog):
    failing = True

    def read(_):
        if failing:
            raise RuntimeError("boom")

    channels = Channels([ChannelDefinition(quantity=CS, write=Fake().write, read=read)])
    with caplog.at_level(logging.WARNING):
        channels.verify(snap())
        assert channels.read(CS, snap()) is None
        failing = False
        channels.verify(snap())
        failing = True
        channels.verify(snap())

    assert logged(caplog) == [ERROR_WITH_TRACEBACK, WARNING_WITHOUT_TRACEBACK, ERROR_WITH_TRACEBACK]
    warning = channel_records(caplog)[1].getMessage()
    assert "boom" in warning and "control_setpoint" in warning


async def test_failure_runs_are_per_channel(caplog):
    (a, fa), (b, fb) = rw(CS), rw(MS)
    channels = Channels([a, b])
    desired = DesiredState(control_setpoint=50.0, maximum_setpoint=70.0)
    fa.fail = True
    with caplog.at_level(logging.WARNING):
        await channels.async_write(desired)
        await channels.async_write(desired)
        fb.fail = True
        await channels.async_write(DesiredState(maximum_setpoint=75.0))

    # a's target is retried each cycle (error, warning, warning); b's first failure still has a traceback
    assert logged(caplog) == [ERROR_WITH_TRACEBACK, WARNING_WITHOUT_TRACEBACK, WARNING_WITHOUT_TRACEBACK, ERROR_WITH_TRACEBACK]
    assert "maximum_setpoint" in channel_records(caplog)[-1].getMessage()


async def test_read_failures_are_per_channel(caplog):
    channels = Channels([ChannelDefinition(quantity=q, write=Fake().write, read=fail) for q in (CS, MS)])
    with caplog.at_level(logging.WARNING):
        channels.verify(snap())

    assert logged(caplog) == [ERROR_WITH_TRACEBACK, ERROR_WITH_TRACEBACK]


async def test_read_and_write_runs_are_independent(caplog):
    reading_fails = True

    def read(_):
        if reading_fails:
            raise RuntimeError("boom")

    fake = Fake()
    channels = Channels([ChannelDefinition(quantity=CS, write=fake.write, read=read)])
    fake.fail = True
    with caplog.at_level(logging.WARNING):
        channels.verify(snap())  # read run starts
        await channels.async_write(DesiredState(control_setpoint=50.0))  # write run starts: traceback
        channels.verify(snap())  # read run continues
        await channels.async_write(DesiredState(control_setpoint=50.0))  # write run continues
        reading_fails = False
        channels.verify(snap())  # read success must not end the write run
        await channels.async_write(DesiredState(control_setpoint=50.0))
        fake.fail = False
        await channels.async_write(DesiredState(control_setpoint=50.0))  # write success must not end read run
        reading_fails = True
        channels.verify(snap())

    assert logged(caplog) == [
        ERROR_WITH_TRACEBACK, ERROR_WITH_TRACEBACK,
        WARNING_WITHOUT_TRACEBACK, WARNING_WITHOUT_TRACEBACK, WARNING_WITHOUT_TRACEBACK,
        ERROR_WITH_TRACEBACK,
    ]


async def test_encode_and_write_failures_share_a_run(caplog):
    fake = Fake()
    broken = True

    def encode(value):
        if broken:
            raise ValueError("bad encode")
        return value

    channels = Channels([ChannelDefinition(quantity=CS, write=fake.write, encode=encode)])
    with caplog.at_level(logging.WARNING):
        await channels.async_write(DesiredState(control_setpoint=50.0))
        broken = False
        fake.fail = True
        await channels.async_write(DesiredState(control_setpoint=50.0))

    assert logged(caplog) == [ERROR_WITH_TRACEBACK, WARNING_WITHOUT_TRACEBACK]
    assert "boom" in channel_records(caplog)[-1].getMessage()


async def test_encode_success_without_write_ends_failure_run(caplog, freezer):
    broken = False

    def encode(value):
        if broken:
            raise ValueError("bad encode")
        return value

    fake = Fake()
    channels = Channels([ChannelDefinition(quantity=CS, write=fake.write, read=lambda s: None, encode=encode)])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    freezer.move_to(T0 + timedelta(seconds=5))
    with caplog.at_level(logging.WARNING):
        broken = True
        await channels.async_write(DesiredState(control_setpoint=50.0))
        broken = False
        await channels.async_write(DesiredState(control_setpoint=50.0))  # encodes, no write needed
        state = channels.states[CS]
        assert (state.status, state.sent, state.sent_at) == (ChannelStatus.OUT_OF_SYNC, 50.0, T0)
        broken = True
        await channels.async_write(DesiredState(control_setpoint=50.0))

    assert fake.calls == [50.0]
    assert logged(caplog) == [ERROR_WITH_TRACEBACK, ERROR_WITH_TRACEBACK]


async def test_write_only_failure_run_ends_on_success(caplog):
    fake = Fake()
    channels = Channels([ChannelDefinition(quantity=TS, write=fake.write)])
    with caplog.at_level(logging.WARNING):
        fake.fail = True
        await channels.async_write(DesiredState(thermostat_setpoint=20.0))
        await channels.async_write(DesiredState())
        fake.fail = False
        await channels.async_write(DesiredState())
        fake.fail = True
        await channels.async_write(DesiredState())

    assert logged(caplog) == [ERROR_WITH_TRACEBACK, WARNING_WITHOUT_TRACEBACK, ERROR_WITH_TRACEBACK]
    assert channels.states[TS].status == ChannelStatus.WRITE_ONLY


async def test_read_failure_leaves_state_unchanged(caplog):
    failing = False

    def read(_):
        if failing:
            raise RuntimeError("boom")
        return live(50.0, T0 + timedelta(seconds=1))

    channels = Channels([ChannelDefinition(quantity=CS, write=Fake().write, read=read)])
    await channels.async_write(DesiredState(control_setpoint=50.0))
    channels.verify(snap())
    before = channels.states[CS]
    failing = True
    channels.verify(snap())

    assert channels.states[CS] == before
    assert before.status == ChannelStatus.IN_SYNC
