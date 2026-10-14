"""Cabin temperature de-dithering.

The controller reports whole degrees F, and the reading is noisy: near a
half-degree boundary it flips between neighbours every few seconds while idle
(observed: 75/76 about 1,500 times a day), and during a heat-up it jumps back
by 1-2 degrees for a few seconds at a time (observed 2026-10-12: 124 -> 122 ->
124 within 30 s). Each flip is a state change in Home Assistant and a row in
the recorder.

The filter remembers which way the temperature is travelling:

* any step in that direction is shown at once, so a heat-up or cool-down is
  tracked with no added lag;
* a step against it is held until the reading has stayed on that side of the
  shown value for ``hold`` seconds (it may wander while it does); then the
  latest reading is shown and the direction flips;
* a reading 5 or more degrees against it is taken as real and shown at once.
  Heat-up noise reaches 2-3 degrees; a gap that large means the cabin really
  has turned (a door left open, heaters cut out).

The coordinator sets the direction to rising when a session starts and to
falling when it stops, since that is what the cabin will do next.

No Home Assistant imports, so it can be unit-tested on its own.
"""

from __future__ import annotations

REVERSAL_HOLD = 180.0  # seconds a reversal must persist before it is shown
REAL_JUMP = 5  # degrees against the trend that are believed immediately


class CabinTempFilter:
    """Hysteresis on direction of travel for a noisy whole-degree sensor."""

    def __init__(self, hold: float = REVERSAL_HOLD, real_jump: int = REAL_JUMP) -> None:
        self._hold = hold
        self._real_jump = real_jump
        self.value: int | None = None
        self._direction = 0  # +1 rising, -1 falling, 0 unknown
        self._pending_since: float | None = None  # reading has been against the trend since

    @property
    def direction(self) -> int:
        return self._direction

    @property
    def pending_deadline(self) -> float | None:
        """When a held reversal would be shown (same clock as ``update``), if one is pending."""
        if self._pending_since is None:
            return None
        return self._pending_since + self._hold

    def set_trend(self, direction: int) -> None:
        """Tell the filter which way the cabin is expected to move (+1, -1 or 0)."""
        self._direction = (direction > 0) - (direction < 0)
        self._pending_since = None

    def update(self, raw: int, now: float) -> int:
        """Feed a raw reading taken at ``now`` (seconds); return the value to show."""
        if self.value is None:
            self.value = raw
            return raw
        delta = raw - self.value
        if delta == 0:
            self._pending_since = None  # back to the shown value: it was noise
            return self.value
        with_trend = self._direction == 0 or (delta > 0) == (self._direction > 0)
        if with_trend or abs(delta) >= self._real_jump:
            self._accept(raw)
            return self.value
        # Against the direction of travel: hold it.
        if self._pending_since is None:
            self._pending_since = now
        elif now - self._pending_since >= self._hold:
            self._accept(raw)
        return self.value

    def _accept(self, raw: int) -> None:
        assert self.value is not None
        if raw != self.value:
            self._direction = 1 if raw > self.value else -1
        self.value = raw
        self._pending_since = None
