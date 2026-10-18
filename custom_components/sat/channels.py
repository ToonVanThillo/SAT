"""Channels: track each controllable quantity from requested value to confirmed device setting."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from numbers import Real
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .coordinator import BoilerSnapshot, DeviceState, Reading

_LOGGER = logging.getLogger(__name__)

# Held-back first-write cycles tolerated before warning; after a restart the first live read-back takes a cycle or two.
WAITING_CYCLES_BEFORE_WARNING = 3


class Release(Enum):
    """Type of the `RELEASE` sentinel."""
    RELEASE = "release"


# Requested instead of a value: the channel drops its target and stops writing and comparing read-backs.
RELEASE: Final = Release.RELEASE


@dataclass(frozen=True, slots=True, kw_only=True)
class DesiredState:
    """What the control logic wants this cycle; None keeps the channel's current target, RELEASE drops it."""
    control_setpoint: float | Release | None = None
    heater_state: DeviceState | None = None
    maximum_relative_modulation: int | None = None
    maximum_setpoint: float | None = None
    thermostat_setpoint: float | None = None


class Quantity(str, Enum):
    """A controllable quantity; each value is the name of its `DesiredState` field."""
    CONTROL_SETPOINT = "control_setpoint"
    HEATER_STATE = "heater_state"
    MAXIMUM_RELATIVE_MODULATION = "maximum_relative_modulation"
    MAXIMUM_SETPOINT = "maximum_setpoint"
    THERMOSTAT_SETPOINT = "thermostat_setpoint"


class ChannelStatus(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    DRIFTED = "drifted"
    WRITE_ONLY = "write_only"
    READ_ONLY = "read_only"


def _identity(value: Any) -> Any:
    return value


def _log_failure(repeated: bool, err: Exception, message: str, *args: Any) -> None:
    """Log a failure with traceback, or as a one-line warning if it continues a run of failures."""
    if repeated:
        _LOGGER.warning(f"{message}: %s", *args, err)
    else:
        _LOGGER.error(message, *args, exc_info=err)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelDefinition:
    """A backend's channel for one quantity; without `read` it is write-only, without `write` read-only."""
    quantity: Quantity
    write: Callable[[Any], Awaitable[None]] | None = None
    read: Callable[[BoilerSnapshot], Reading | None] | None = None
    encode: Callable[[Any], Any] = _identity
    tolerance: float = 0.0
    write_every_cycle: bool = False

    def __post_init__(self) -> None:
        if self.write is None and self.read is None:
            raise ValueError(f"Channel {self.quantity.value} has neither read nor write")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelState:
    """Tracked state of one channel; `sent` is the encoded value of the last successful write or adopted read-back."""
    status: ChannelStatus
    target: Any = None
    sent: Any = None
    sent_at: datetime | None = None
    read_back: Any = None
    read_back_at: datetime | None = None
    drift_count: int = 0


class _Channel:
    def __init__(self, definition: ChannelDefinition) -> None:
        self.definition = definition
        self.state = ChannelState(status=ChannelStatus.READ_ONLY if definition.write is None else ChannelStatus.IDLE)
        self._mismatch = False
        self._read_failing = False
        self._write_failing = False
        self._warned_read_only = False
        self._warned_write_only = False
        self._held_back = 0

    @property
    def name(self) -> str:
        return self.definition.quantity.value

    def read(self, snapshot: BoilerSnapshot) -> Reading | None:
        if self.definition.read is None:
            if not self._warned_write_only:
                _LOGGER.warning("Channel %s is write-only and cannot be read", self.name)
                self._warned_write_only = True
            return None

        try:
            reading = self.definition.read(snapshot)
        except Exception as err:  # noqa: BLE001
            _log_failure(self._read_failing, err, "Failed to read channel %s", self.name)
            self._read_failing = True
            return None

        self._read_failing = False
        return reading

    def verify(self, snapshot: BoilerSnapshot) -> None:
        # The mismatch flag only lives from this verify to the next write.
        self._mismatch = False
        if self.definition.read is None or (reading := self.read(snapshot)) is None:
            return

        state = replace(self.state, read_back=reading.value, read_back_at=reading.received_at)

        # Only a live read-back received after the last send says something about that send.
        if (
            self.definition.write is not None
            and state.sent_at is not None
            and reading.received_at is not None
            and reading.received_at > state.sent_at
        ):
            if self._matches(reading.value, state.sent):
                state = replace(state, status=ChannelStatus.IN_SYNC)
            else:
                self._mismatch = True
                if state.status == ChannelStatus.IN_SYNC:
                    state = replace(state, status=ChannelStatus.DRIFTED, drift_count=state.drift_count + 1)
                else:
                    state = replace(state, status=ChannelStatus.OUT_OF_SYNC)

        self.state = state

    async def async_write(self, requested: Any) -> None:
        if self.definition.write is None:
            if requested not in (None, RELEASE) and not self._warned_read_only:
                _LOGGER.warning("Channel %s is read-only; ignoring requested value %s", self.name, requested)
                self._warned_read_only = True
            return

        rewrite, self._mismatch = self._mismatch, False
        if requested is RELEASE:
            # Without a sent value verify only records read-backs, so the next request is a first write against the latest one.
            self.state = replace(self.state, status=ChannelStatus.IDLE, target=None, sent=None, sent_at=None)
            return

        if requested is not None:
            self.state = replace(self.state, target=requested)

        if self.state.target is None:
            return

        write_only = self.definition.read is None
        # Before the first send, wait for a live read-back (recorded by verify); a matching one is taken as sent.
        first_send = not write_only and self.state.sent_at is None
        if first_send and self.state.read_back_at is None:
            self._held_back += 1
            if self._held_back == WAITING_CYCLES_BEFORE_WARNING + 1:
                _LOGGER.warning("Channel %s is waiting for a live read-back before its first write", self.name)
            return

        try:
            value = self.definition.encode(self.state.target)
            adopt = first_send and self._matches(self.state.read_back, value)
            changed = value != self.state.sent
            needs_write = not adopt and (write_only or changed or rewrite or self.definition.write_every_cycle)
            if needs_write:
                await self.definition.write(value)
        except Exception as err:  # noqa: BLE001
            # A failed encode or write must never stop the other channels; the sent value stays as it was.
            _log_failure(self._write_failing, err, "Failed to write target %s to channel %s", self.state.target, self.name)
            self._write_failing = True
            self.state = replace(self.state, status=ChannelStatus.WRITE_ONLY if write_only else ChannelStatus.OUT_OF_SYNC)
            return

        self._write_failing = False
        if adopt:
            # Dated at the read-back, so only read-backs received after it verify the adopted value.
            self.state = replace(self.state, status=ChannelStatus.IN_SYNC, sent=value, sent_at=self.state.read_back_at)
            return

        if not needs_write:
            return

        _LOGGER.debug("Wrote %s to channel %s", value, self.name)
        if write_only:
            status = ChannelStatus.WRITE_ONLY
        elif changed:
            status = ChannelStatus.PENDING
        else:
            status = self.state.status

        self.state = replace(self.state, status=status, sent=value, sent_at=dt_util.utcnow())

    def _matches(self, read_back: Any, sent: Any) -> bool:
        if isinstance(read_back, Real) and isinstance(sent, Real):
            return abs(float(read_back) - float(sent)) <= self.definition.tolerance

        return bool(read_back == sent)


class Channels:
    """A backend's channel set: verifies the previous cycle's writes and reconciles a `DesiredState`.

    Errors in a channel's encode, write or read are logged and never propagate or affect other channels.
    """

    def __init__(self, definitions: Iterable[ChannelDefinition]) -> None:
        self._channels: dict[Quantity, _Channel] = {}
        for definition in definitions:
            if definition.quantity in self._channels:
                raise ValueError(f"Duplicate channel for {definition.quantity.value}")
            self._channels[definition.quantity] = _Channel(definition)

    @property
    def states(self) -> Mapping[Quantity, ChannelState]:
        """Current tracked state per channel, in definition order."""
        return MappingProxyType({quantity: channel.state for quantity, channel in self._channels.items()})

    def read(self, quantity: Quantity, snapshot: BoilerSnapshot) -> Reading | None:
        """Read the channel's device setting; None if write-only (warns once) or if the read fails (logged)."""
        return self._channels[quantity].read(snapshot)

    def verify(self, snapshot: BoilerSnapshot) -> None:
        """Compare each channel's read-back with its last sent value; run right after the snapshot."""
        for channel in self._channels.values():
            channel.verify(snapshot)

    async def async_write(self, desired: DesiredState) -> None:
        """Update targets from `desired` and write what is needed, channel by channel in definition order."""
        for quantity, channel in self._channels.items():
            await channel.async_write(getattr(desired, quantity.value))
