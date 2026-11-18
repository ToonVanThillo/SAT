"""Climate inputs held through short gaps: a sensor that drops out briefly doesn't turn the boiler off."""
from __future__ import annotations

from time import monotonic

# Seconds an input's last valid value stands in for it once it reads as missing, counted from the last capture that read it as valid
MISSING_INPUT_GRACE = 300


class HeldInput:
    """One climate input, read once per cycle at capture; kept in memory only.

    A missing reading (None) is replaced by the last valid one while that was read less than `MISSING_INPUT_GRACE` seconds ago (monotonic
    time).
    """

    def __init__(self, name: str) -> None:
        """`name` is the input's short name in the cycle record (e.g. `inside`)."""
        self.name: str = name
        self._value: float | None = None
        self._valid_at: float | None = None
        self._held: bool = False

    @property
    def held(self) -> bool:
        """Whether the last read returned the held value."""
        return self._held

    def read(self, value: float | None) -> float | None:
        """Return `value` if it is valid (not None) and keep it; otherwise the last valid value while it is held, else None."""
        if value is not None:
            self._value, self._valid_at, self._held = value, monotonic(), False
            return value

        if self._valid_at is not None and monotonic() - self._valid_at < MISSING_INPUT_GRACE:
            self._held = True
            return self._value

        self._held = False
        return None
