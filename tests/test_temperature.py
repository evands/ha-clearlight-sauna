"""Cabin temperature de-dither filter (pure Python, no HA)."""

from __future__ import annotations

from custom_components.clearlight_sauna.temperature import REVERSAL_HOLD, CabinTempFilter


def _feed(f: CabinTempFilter, readings: list[tuple[float, int]]) -> list[int]:
    return [f.update(raw, t) for t, raw in readings]


def test_first_reading_is_shown() -> None:
    f = CabinTempFilter()
    assert f.update(76, 0.0) == 76
    assert f.pending_deadline is None


def test_idle_dither_is_suppressed() -> None:
    """The real pattern: 75/76 alternating every few seconds while the sauna is off."""
    f = CabinTempFilter()
    f.update(76, 0.0)
    f.set_trend(-1)  # not heating
    readings = [(2.0 * i, 75 if i % 2 else 76) for i in range(1, 200)]
    shown = _feed(f, readings)
    # Settles on the first value in the expected direction and stays there.
    assert set(shown) == {75}
    assert shown.count(75) == len(shown)


def test_reversal_shown_after_hold() -> None:
    f = CabinTempFilter(hold=180)
    f.update(76, 0.0)
    f.set_trend(-1)
    assert f.update(75, 1.0) == 75  # falling: immediate
    assert f.update(76, 10.0) == 75  # rising against trend: held
    assert f.pending_deadline == 10.0 + 180
    assert f.update(76, 100.0) == 75
    assert f.update(76, 190.0) == 76  # persisted for the hold
    assert f.direction == 1
    assert f.pending_deadline is None


def test_returning_to_shown_value_cancels_pending() -> None:
    f = CabinTempFilter(hold=180)
    f.update(75, 0.0)
    f.set_trend(-1)
    f.update(76, 10.0)  # held
    f.update(75, 20.0)  # back: dither, not a change
    assert f.pending_deadline is None
    # A later excursion starts its own clock.
    f.update(76, 150.0)
    assert f.update(76, 200.0) == 75
    assert f.update(76, 331.0) == 76


def test_heat_up_tracks_without_lag_and_ignores_boundary_flicker() -> None:
    f = CabinTempFilter()
    f.update(76, 0.0)
    f.set_trend(1)  # session started
    t = 0.0
    shown: list[int] = []
    for temp in range(77, 141):
        t += 30
        shown.append(f.update(temp, t))  # first sight of each new degree
        t += 1
        shown.append(f.update(temp - 1, t))  # flicker back at the boundary
        t += 1
        shown.append(f.update(temp, t))
    # Every degree appears the moment it is first reported; flicker never shows.
    assert shown[0::3] == list(range(77, 141))
    assert all(a <= b for a, b in zip(shown, shown[1:]))
    assert shown[-1] == 140


def test_cool_down_tracks_after_trend_flips() -> None:
    f = CabinTempFilter()
    f.update(140, 0.0)
    f.set_trend(-1)  # session ended
    shown = _feed(f, [(60.0 * i, 140 - i) for i in range(1, 30)])
    assert shown == [140 - i for i in range(1, 30)]


def test_big_jump_is_accepted_immediately_either_way() -> None:
    f = CabinTempFilter()
    f.update(100, 0.0)
    f.set_trend(1)
    assert f.update(98, 1.0) == 98  # 2 degrees against the trend: real
    assert f.direction == -1


def test_unknown_direction_accepts_first_step() -> None:
    f = CabinTempFilter()
    f.update(76, 0.0)
    assert f.update(75, 1.0) == 75
    assert f.direction == -1
    assert f.update(76, 2.0) == 75  # now a reversal: held
    assert REVERSAL_HOLD > 60
