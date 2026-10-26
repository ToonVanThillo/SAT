"""Climate inputs held through short gaps: a sensor that drops out briefly doesn't turn the boiler off."""
from __future__ import annotations

from enum import Enum
from time import monotonic

from . import timeline

# Seconds an input's last valid value stands in for it once it reads as missing, counted from the last capture that read it as valid
MISSING_INPUT_GRACE = 300


class _Status(Enum):
    VALID = "valid"
    HELD = "held"
    EXPIRED = "expired"  # missing after a hold ran out
    LAPSED = "lapsed"  # missing after a valid value too old to hold
    MISSING = "missing"  # missing without ever having been valid since start


class HeldInput:
    """One climate input, read once per cycle at capture; kept in memory only.

    A missing reading (None) is replaced by the last valid one while that was read less than `MISSING_INPUT_GRACE` seconds ago (monotonic
    time). Entering a hold, a hold running out, going missing with a valid value too old to hold, and recovering from any of these are
    logged on the timeline at info; an input that hasn't been valid since start is missing silently.
    """

    def __init__(self, name: str, label: str) -> None:
        """`name` is the short name of the cycle line (e.g. `inside`), `label` the name of the timeline lines (e.g. `inside temperature`)."""
        self.name: str = name
        self._label: str = label
        self._value: float | None = None
        self._valid_at: float | None = None
        self._status: _Status = _Status.MISSING

    @property
    def held(self) -> bool:
        """Whether the last read returned the held value."""
        return self._status == _Status.HELD

    @property
    def missing(self) -> bool:
        """Whether the last read returned nothing: missing without a value to hold, or after the hold ran out."""
        return self._status in (_Status.EXPIRED, _Status.LAPSED, _Status.MISSING)

    def read(self, value: float | None) -> float | None:
        """Return `value` if it is valid (not None) and keep it; otherwise the last valid value while it is held, else None."""
        previous = self._status

        if value is not None:
            self._value, self._valid_at, self._status = value, monotonic(), _Status.VALID
            if previous in (_Status.HELD, _Status.EXPIRED, _Status.LAPSED):
                timeline.LOGGER.info("%s back", self._label)
            return value

        if self._valid_at is not None and monotonic() - self._valid_at < MISSING_INPUT_GRACE:
            self._status = _Status.HELD
            if previous != _Status.HELD:
                timeline.LOGGER.info("%s missing, holding %s for up to %g min", self._label, timeline.fmt(self._value), MISSING_INPUT_GRACE / 60)
            return self._value

        if previous == _Status.HELD:
            self._status = _Status.EXPIRED
            timeline.LOGGER.info("%s still missing after %g min, no longer held", self._label, MISSING_INPUT_GRACE / 60)
        elif previous == _Status.VALID:
            # Its last valid value is already too old to hold
            self._status = _Status.LAPSED
            timeline.LOGGER.info("%s missing", self._label)

        return None
