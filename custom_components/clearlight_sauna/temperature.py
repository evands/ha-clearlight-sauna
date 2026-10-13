"""Cabin temperature de-dithering.

The controller reports whole degrees F. When the true cabin temperature sits
near a half-degree boundary, the reading flips between the two neighbouring
values every few seconds (observed: 75/76 about 1,500 times a day while idle).
Each flip is a state change in Home Assistant and a row in the recorder.

The filter remembers which way the temperature is travelling:

* a 1 degree step that continues that direction is accepted at once, so a
  heat-up or cool-down is tracked with no added lag;
* a 1 degree step against it is held until the new reading has persisted for
  ``hold`` seconds, which swallows the dither;
* a jump of 2 degrees or more is real and is accepted at once.

The coordinator sets the direction to rising when a session starts and to
falling when it stops, since that is what the cabin will do next.

No Home Assistant imports, so it can be unit-tested on its own.
"""

from __future__ import annotations

REVERSAL_HOLD = 180.0  # seconds a 1-degree reversal must persist before it is shown


class CabinTempFilter:
    """Hysteresis on direction of travel for a whole-degree sensor."""

    def __init__(self, hold: float = REVERSAL_HOLD) -> None:
        self._hold = hold
        self.value: int | None = None
        self._direction = 0  # +1 rising, -1 falling, 0 unknown
        self._pending: int | None = None
        self._pending_since = 0.0

    @property
    def direction(self) -> int:
        return self._direction

    @property
    def pending_deadline(self) -> float | None:
        """When a held reversal would be accepted (same clock as ``update``), if any."""
        if self._pending is None:
            return None
        return self._pending_since + self._hold

    def set_trend(self, direction: int) -> None:
        """Tell the filter which way the cabin is expected to move (+1, -1 or 0)."""
        self._direction = (direction > 0) - (direction < 0)

    def update(self, raw: int, now: float) -> int:
        """Feed a raw reading taken at ``now`` (seconds); return the value to show."""
        if self.value is None:
            self.value = raw
            return raw
        delta = raw - self.value
        if delta == 0:
            self._pending = None
            return self.value
        if abs(delta) >= 2 or self._direction == 0 or (delta > 0) == (self._direction > 0):
            self._accept(raw)
            return self.value
        # A single-degree step against the direction of travel: hold it.
        if self._pending != raw:
            self._pending = raw
            self._pending_since = now
        elif now - self._pending_since >= self._hold:
            self._accept(raw)
        return self.value

    def _accept(self, raw: int) -> None:
        assert self.value is not None
        if raw != self.value:
            self._direction = 1 if raw > self.value else -1
        self.value = raw
        self._pending = None
