"""Heat-up model (Phase 3 draft): pure maths, plus a replay of a recorded run."""

from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

import pytest

from custom_components.clearlight_sauna import heatup as h

DATA = Path(__file__).parent.parent / "tools" / "heatup" / "data"
CABIN = "sensor.clearlight_sauna_cabin_temperature"


def _curve(tau: float, ceiling: float, start: float, minutes: float, step: float = 15.0):
    t, out = 0.0, []
    while t <= minutes * 60:
        out.append((t, ceiling - (ceiling - start) * math.exp(-t / tau)))
        t += step
    return out


def test_eta_basics() -> None:
    assert h.eta_seconds(140, 140, 161, 1920) == 0.0
    assert h.eta_seconds(150, 140, 161, 1920) == 0.0
    assert h.eta_seconds(100, 160, 161, 1920) is None  # within UNREACHABLE_MARGIN of the ceiling
    # 76 -> 140 heading for 161: tau * ln(85 / 21)
    assert h.eta_seconds(76, 140, 161, 1920) == pytest.approx(1920 * math.log(85 / 21))


def test_fixed_tau_fit_recovers_ceiling_and_respects_prior() -> None:
    samples = _curve(tau=1920, ceiling=161, start=76, minutes=20)
    assert h.fit_ceiling(samples, 76, 1920) == pytest.approx(161, abs=1e-6)
    # A heavy prior holds the answer near the stored ceiling.
    pulled = h.fit_ceiling(samples[:4], 76, 1920, prior=150, prior_weight=1e6)
    assert pulled == pytest.approx(150, abs=0.1)


def test_joint_fit_needs_a_bent_curve() -> None:
    long = _curve(tau=1920, ceiling=161, start=76, minutes=60)
    tau, ceiling = h.fit_tau_and_ceiling(long, 76)
    assert tau == pytest.approx(1920, abs=60)
    assert ceiling == pytest.approx(161, abs=1.5)
    # Ten minutes of an almost straight rise can't separate tau from the ceiling.
    noisy_short = [(t, temp + (0.6 if i % 2 else -0.6)) for i, (t, temp) in
                   enumerate(_curve(tau=1920, ceiling=161, start=76, minutes=10))]
    assert h.fit_tau_and_ceiling(noisy_short, 76) is None


def test_session_shows_stored_model_until_five_minutes() -> None:
    cal = h.Calibration()
    hu = h.HeatUp(start=1000.0, start_temp=76, room=76, calibration=cal, cold=True)
    for t, temp in _curve(tau=1920, ceiling=150, start=76, minutes=4.5):
        hu.add(1000.0 + t, temp, target=140)
    ceiling, approximate = hu.ceiling()
    assert approximate and ceiling == 76 + h.DEFAULT_DELTA_T
    for t, temp in _curve(tau=1920, ceiling=150, start=76, minutes=12)[19:]:
        hu.add(1000.0 + t, temp, target=140)
    ceiling, approximate = hu.ceiling()
    assert not approximate
    assert 150 <= ceiling < 161  # pulled from the stored 161 toward the true 150


def test_window_closes_when_target_reached() -> None:
    hu = h.HeatUp(start=0.0, start_temp=76, room=76, calibration=h.Calibration(), cold=True)
    for t, temp in _curve(tau=1920, ceiling=161, start=76, minutes=60):
        hu.add(t, temp, target=120)
    assert hu.reached_at is not None
    assert hu.samples[-1][1] >= 120 > hu.samples[-2][1]


def test_only_cold_long_sessions_calibrate() -> None:
    warm = h.HeatUp(start=0.0, start_temp=110, room=76, calibration=h.Calibration(), cold=False)
    short = h.HeatUp(start=0.0, start_temp=76, room=76, calibration=h.Calibration(), cold=True)
    for t, temp in _curve(tau=1920, ceiling=161, start=110, minutes=30):
        warm.add(t, temp, target=175)
    for t, temp in _curve(tau=1920, ceiling=161, start=76, minutes=5):
        short.add(t, temp, target=175)
    assert warm.calibration_result() is None
    assert short.calibration_result() is None


def test_blending_weights() -> None:
    cal = h.Calibration()  # delta_t 85
    once = cal.blended(delta_t=75)
    assert once.delta_t == pytest.approx(80)  # the first session counts as much as the defaults
    assert once.sessions == 1 and once.tau == cal.tau
    many = h.Calibration(delta_t=85, sessions=20)
    assert many.blended(delta_t=75).delta_t == pytest.approx(83)  # floor weight 0.2
    with_tau = cal.blended(delta_t=85, tau=1800)
    assert with_tau.tau == pytest.approx((h.DEFAULT_TAU + 1800) / 2) and with_tau.tau_sessions == 1


def test_calibration_round_trip_and_bad_data() -> None:
    cal = h.Calibration(tau=1700, delta_t=80, sessions=3, tau_sessions=1)
    assert h.Calibration.from_dict(cal.as_dict()) == cal
    assert h.Calibration.from_dict({"tau": "x"}) == h.Calibration()
    assert h.Calibration.from_dict({"tau": 1, "delta_t": 999}).tau == h.TAU_RANGE[0]


def test_live_weight_grows_with_confirmed_sessions() -> None:
    weights = [h.live_prior_samples(n) for n in range(6)]
    assert weights == sorted(weights) and weights[3] == weights[5]


def _replay(name: str) -> tuple[float, list[tuple[float, float]]]:
    """(start temp, raw reading every 15 s from heaters on to the first relay off)."""
    rows = list(csv.DictReader((DATA / name).open()))
    t0 = datetime.fromisoformat(rows[0]["time"])
    cabin, relay = [], []
    for row in rows:
        t = (datetime.fromisoformat(row["time"]) - t0).total_seconds()
        if row["entity_id"] == CABIN and row["state"].isdigit():
            cabin.append((t, float(row["state"])))
        elif row["entity_id"].endswith("_heater_relay"):
            relay.append((t, row["state"] == "on"))
    on = next(t for t, v in relay if v)
    off = next(t for t, v in relay if not v and t > on)
    start = next(temp for t, temp in reversed(cabin) if t <= on)
    samples, i, last, t = [], 0, start, on
    while t <= off:
        while i < len(cabin) and cabin[i][0] <= t:
            last = cabin[i][1]
            i += 1
        samples.append((t - on, last))
        t += h.SAMPLE_INTERVAL
    return start, samples


def test_replay_of_recorded_run_calibrates_to_its_own_fit() -> None:
    start, samples = _replay("2026-10-13-175.csv")
    hu = h.HeatUp(start=0.0, start_temp=start, room=start, calibration=h.Calibration(), cold=True)
    for t, temp in samples:
        hu.add(t, temp, target=175)
    delta_t, tau = hu.calibration_result()
    assert delta_t == pytest.approx(84.5, abs=2)
    assert tau == pytest.approx(31.5 * 60, abs=120)
    # The stored model from a cold 77 °F cabin: 140 °F was reached at 43.2 min.
    eta = h.eta_seconds(start, 140, start + h.DEFAULT_DELTA_T, h.DEFAULT_TAU)
    assert eta / 60 == pytest.approx(43.2, abs=2)
