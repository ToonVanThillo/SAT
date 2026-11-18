"""The write records `Channels.async_write` returns, which the cycle record shows as `sent`: value as encoded and why it was written."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.sat.channels import (
    RELEASE,
    ChannelDefinition,
    Channels,
    ChannelStatus,
    DesiredState,
    Quantity,
    WriteKind,
)
from custom_components.sat.coordinator import DeviceState, Reading

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CS = Quantity.CONTROL_SETPOINT
HS = Quantity.HEATER_STATE


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to(T0)


class Fake:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def write(self, value):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(value)


def snap(**readings):
    return SimpleNamespace(readings=readings)


def live(value, seconds=-60):
    return Reading(value=value, received_at=T0 + timedelta(seconds=seconds))


def make(quantity=CS, **kwargs):
    fake = Fake()
    definition = ChannelDefinition(quantity=quantity, write=fake.write, read=lambda s: s.readings.get(quantity.value), **kwargs)
    return definition, fake


def summary(records):
    return [(r.quantity, r.value, r.kind) for r in records]


async def test_a_new_value_is_a_send_and_the_same_value_again_writes_nothing():
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))

    assert summary(await channels.async_write(DesiredState(control_setpoint=50.0))) == [(CS, 50.0, WriteKind.SEND)]
    assert await channels.async_write(DesiredState(control_setpoint=50.0)) == []
    assert fake.calls == [50.0]


async def test_the_record_holds_the_encoded_value():
    definition, fake = make(encode=lambda v: 0 if v == 10 else v)
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(5)))

    assert summary(await channels.async_write(DesiredState(control_setpoint=10))) == [(CS, 0, WriteKind.SEND)]
    assert fake.calls == [0]


async def test_an_enum_value_is_recorded_as_given():
    definition, _ = make(HS)
    channels = Channels([definition])
    channels.verify(snap(heater_state=live(DeviceState.ON)))

    assert summary(await channels.async_write(DesiredState(heater_state=DeviceState.OFF))) == [(HS, DeviceState.OFF, WriteKind.SEND)]


async def test_a_mismatching_read_back_makes_the_next_write_a_resend(freezer):
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))
    freezer.move_to(T0 + timedelta(seconds=10))
    channels.verify(snap(control_setpoint=live(50.0, 5)))

    freezer.move_to(T0 + timedelta(seconds=20))
    changes = channels.verify(snap(control_setpoint=live(40.0, 15)))

    assert [(c.quantity, c.previous, c.current) for c in changes] == [(CS, ChannelStatus.IN_SYNC, ChannelStatus.DRIFTED)]
    assert summary(await channels.async_write(DesiredState(control_setpoint=50.0))) == [(CS, 50.0, WriteKind.RESEND)]
    assert fake.calls == [50.0, 50.0]
    assert channels.states[CS].read_back == 40.0


async def test_a_pending_channel_confirmed_by_its_read_back_is_a_verify_change(freezer):
    definition, _ = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))
    freezer.move_to(T0 + timedelta(seconds=10))

    changes = channels.verify(snap(control_setpoint=live(50.0, 5)))

    assert [(c.previous, c.current) for c in changes] == [(ChannelStatus.PENDING, ChannelStatus.IN_SYNC)]
    assert channels.states[CS].sent == 50.0


async def test_a_write_every_cycle_channel_gives_a_rewrite():
    definition, fake = make(write_every_cycle=True)
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))

    assert summary(await channels.async_write(DesiredState(control_setpoint=50.0))) == [(CS, 50.0, WriteKind.REWRITE)]
    assert fake.calls == [50.0, 50.0]


async def test_a_write_only_channel_sends_once_then_rewrites():
    fake = Fake()
    channels = Channels([ChannelDefinition(quantity=Quantity.MAXIMUM_SETPOINT, write=fake.write)])

    first = await channels.async_write(DesiredState(maximum_setpoint=55.0))
    second = await channels.async_write(DesiredState(maximum_setpoint=55.0))

    assert ([r.kind for r in first], [r.kind for r in second]) == ([WriteKind.SEND], [WriteKind.REWRITE])
    assert fake.calls == [55.0, 55.0]


async def test_a_release_writes_nothing_and_the_next_request_is_a_send_again():
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    await channels.async_write(DesiredState(control_setpoint=50.0))

    assert await channels.async_write(DesiredState(control_setpoint=RELEASE)) == []
    assert channels.states[CS].status == ChannelStatus.IDLE
    assert await channels.async_write(DesiredState(control_setpoint=RELEASE)) == []

    channels.verify(snap(control_setpoint=live(7.0, -30)))
    assert summary(await channels.async_write(DesiredState(control_setpoint=50.0))) == [(CS, 50.0, WriteKind.SEND)]
    assert fake.calls == [50.0, 50.0]


async def test_a_failed_write_gives_no_record():
    definition, fake = make()
    fake.fail = True
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))

    assert await channels.async_write(DesiredState(control_setpoint=50.0)) == []
    assert channels.states[CS].status == ChannelStatus.OUT_OF_SYNC


async def test_an_adopted_read_back_is_no_write():
    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(50.0)))

    assert await channels.async_write(DesiredState(control_setpoint=50.0)) == []
    assert fake.calls == []


async def test_an_unprintable_value_does_not_stop_the_write():
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no str")

    definition, fake = make()
    channels = Channels([definition])
    channels.verify(snap(control_setpoint=live(1.0)))
    value = Unprintable()

    records = await channels.async_write(DesiredState(control_setpoint=value))  # type: ignore[arg-type]

    assert fake.calls == [value]
    assert [r.value for r in records] == [value]
