"""Cabin temperature de-dither filter (pure Python, no HA)."""

from __future__ import annotations

from custom_components.clearlight_sauna.temperature import REAL_JUMP, REVERSAL_HOLD, CabinTempFilter


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


def test_two_degree_dip_while_heating_is_noise() -> None:
    f = CabinTempFilter()
    f.update(100, 0.0)
    f.set_trend(1)
    assert f.update(98, 1.0) == 100
    assert f.update(101, 3.0) == 101


def test_reading_far_from_shown_value_is_real() -> None:
    f = CabinTempFilter()
    f.update(100, 0.0)
    f.set_trend(1)
    assert f.update(100 - REAL_JUMP, 1.0) == 100 - REAL_JUMP
    assert f.direction == -1


def test_heat_up_noise_from_real_run() -> None:
    """Raw readings from the 2026-10-12 calibration run, 22:12:00-22:12:42 (s, °F)."""
    f = CabinTempFilter()
    f.update(122, -1.0)
    f.set_trend(1)
    raw = [(0.8, 123), (4.7, 122), (6.7, 123), (8.6, 124), (10.5, 123), (24.5, 122),
           (26.5, 123), (42.0, 124), (63.8, 123), (73.5, 124), (81.3, 125), (85.1, 124),
           (89.0, 123), (91.0, 124), (94.8, 125), (103.0, 124), (112.7, 125), (118.6, 126)]
    shown = _feed(f, raw)
    assert shown == sorted(shown)  # never steps back during the heat-up
    assert shown[-1] == 126


def test_noisy_reversal_is_accepted_after_hold() -> None:
    """The cabin turns down while the trend says up; readings wander as it does."""
    f = CabinTempFilter(hold=180)
    f.update(128, 0.0)
    f.set_trend(1)
    readings = [(10.0 * i, v) for i, v in enumerate([127, 126, 127, 125, 126, 125, 124, 125, 126,
                                                     125, 124, 125, 124, 124, 124, 125, 124, 124,
                                                     124, 125], start=1)]
    shown = _feed(f, readings)
    assert shown[:18] == [128] * 18  # held until 180 s after the first low reading
    assert shown[18] == 124  # then the latest reading, and the trend flips
    assert f.direction == -1
    assert shown[19] == 124  # rising is now the reversal: held


def test_unknown_direction_accepts_first_step() -> None:
    f = CabinTempFilter()
    f.update(76, 0.0)
    assert f.update(75, 1.0) == 75
    assert f.direction == -1
    assert f.update(76, 2.0) == 75  # now a reversal: held
    assert REVERSAL_HOLD > 60
