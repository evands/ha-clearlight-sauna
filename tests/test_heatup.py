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


def test_window_closes_when_the_door_opens() -> None:
    """A sustained fall closes the window; the fall itself never reaches the fit."""
    clean = _curve(tau=1920, ceiling=161, start=76, minutes=30)
    door = [(t, temp - 7) for t, temp in _curve(tau=1920, ceiling=161, start=76, minutes=34)[121:]]
    hu = h.HeatUp(start=0.0, start_temp=76, room=76, calibration=h.Calibration(), cold=True)
    for t, temp in clean + door:
        hu.add(t, temp, target=140)
    assert hu.closed and hu.reached_at is None
    assert hu.interrupted_at == pytest.approx(30 * 60 + 45, abs=15)  # 3 of 5 samples down
    assert hu.samples == clean[: len(hu.samples)] and len(hu.samples) >= len(clean) - 1
    delta_t, tau = hu.calibration_result()
    assert tau is None and delta_t == pytest.approx(85, abs=0.5)
    hu.add(40 * 60, 139, target=140)  # the cabin recovering adds nothing
    assert hu.samples[-1][0] <= 30 * 60


def test_short_dips_do_not_close_the_window() -> None:
    """The reading dips 1-3 °F for seconds at a time; even two 6 °F samples in a row pass."""
    hu = h.HeatUp(start=0.0, start_temp=76, room=76, calibration=h.Calibration(), cold=True)
    for i, (t, temp) in enumerate(_curve(tau=1920, ceiling=161, start=76, minutes=40)):
        hu.add(t, temp - (6 if i % 20 in (0, 1) else 0), target=140)
    assert not hu.closed


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
    cabin, raw, relay = [], [], []
    for row in rows:
        t = (datetime.fromisoformat(row["time"]) - t0).total_seconds()
        if row["entity_id"] == CABIN and row["state"].isdigit():
            cabin.append((t, float(row["state"])))
        elif row["entity_id"].endswith("_cabin_temperature_raw") and row["state"].isdigit():
            raw.append((t, float(row["state"])))
        elif row["entity_id"].endswith("_heater_relay"):
            relay.append((t, row["state"] == "on"))
    cabin = raw or cabin  # the integration samples the raw reading
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
    # 59.5 min of heat-up is under 2 tau, so tau stays fixed and only the rise is fitted.
    delta_t, tau = hu.calibration_result()
    assert tau is None
    assert delta_t == pytest.approx(85, abs=2)
    # Fitted jointly, the same run gives the defaults' tau (they came from it).
    pair = h.fit_tau_and_ceiling(h.smooth(hu.samples), start)
    assert pair[0] == pytest.approx(31.5 * 60, abs=120)
    # The stored model from a cold 77 °F cabin: 140 °F was reached at 43.2 min.
    eta = h.eta_seconds(start, 140, start + h.DEFAULT_DELTA_T, h.DEFAULT_TAU)
    assert eta / 60 == pytest.approx(43.2, abs=2)


def test_steady_ready_time_ignores_the_sawtooth() -> None:
    # The raw estimate alternating across a minute boundary every 30 s for 10 min:
    # rounded up on its own it flips between 40:00 and 41:00 the whole time.
    raw = [(t, 2380 if (t // 30) % 2 else 2440) for t in range(0, 600, 15)]
    assert {math.ceil(r / 60) * 60 for _, r in raw} == {2400, 2460}
    steady = h.SteadyReadyTime()
    assert len({steady.update(t, r) for t, r in raw}) == 1  # one minute, never moved


def test_steady_ready_time_follows_a_real_shift() -> None:
    steady = h.SteadyReadyTime()
    for t in range(0, 300, 15):
        steady.update(t, 2400)
    assert steady.shown == 2400
    for t in range(300, 600, 15):  # the estimate moves 3 minutes later and stays there
        steady.update(t, 2580)
    assert steady.shown == 2580


def test_steady_ready_time_never_shows_a_time_already_past() -> None:
    steady = h.SteadyReadyTime()
    for t in range(0, 180, 15):
        steady.update(t, 300)  # ready at 5:00
    # 5:00 comes and the cabin isn't there; the estimate slips 30 s at a time.
    steady.update(300, 330)
    assert steady.shown > 300


def test_replay_of_2026_10_14_run_keeps_ready_at_steady() -> None:
    """The run where the raw Ready at reversed direction 25 times before 140 °F."""
    _, samples = _replay("2026-10-14-ready.csv")
    hu = h.HeatUp(start=0.0, start_temp=samples[0][1], room=75, calibration=h.Calibration(), cold=False)
    steady = h.SteadyReadyTime()
    shown, approx_prev = [], None
    for t, temp in samples:
        hu.add(t, temp, target=140)
        if temp >= 140:
            break
        ceiling, approx = hu.ceiling()
        if approx != approx_prev:
            steady.reset()  # as the estimator does when the live fit takes over
            approx_prev = approx
        eta = h.eta_seconds(temp, 140, ceiling, hu.tau)
        if eta is not None:
            shown.append(steady.update(t, t + eta))
    steps = [b - a for a, b in zip(shown, shown[1:]) if b != a]
    reversals = sum(1 for a, b in zip(steps, steps[1:]) if (a > 0) != (b > 0))
    assert reversals <= 3
    # It got to 140 °F at 43.4 min; the last shown time was within 2 min of that.
    assert abs(shown[-1] - 43.4 * 60) <= 120


@pytest.mark.parametrize("name, room", [
    ("2026-10-13-140.csv", 76), ("2026-10-14-ready.csv", 75), ("2026-10-15-cold.csv", 78),
])
def test_sessions_that_stop_at_140_learn_only_the_rise(name: str, room: float) -> None:
    """A fit window ending at 140 °F once taught tau ~27 min and a rise ~77 °F (2026-10-15)."""
    start, samples = _replay(name)
    hu = h.HeatUp(start=0.0, start_temp=start, room=room, calibration=h.Calibration(), cold=True)
    for t, temp in samples:
        hu.add(t, temp, target=140)
    delta_t, tau = hu.calibration_result()
    assert tau is None
    assert 82 <= delta_t <= 88


def test_replay_of_2026_10_19_run_drops_the_door_opening() -> None:
    """Owner went in at 138 °F (05:46); the reading fell to 131 °F, then recovered."""
    start, samples = _replay("2026-10-19-ready.csv")
    assert start == 70
    hu = h.HeatUp(start=0.0, start_temp=start, room=70, calibration=h.Calibration(), cold=True)
    for t, temp in samples:
        hu.add(t, temp, target=140)
    assert hu.reached_at is None and hu.interrupted_at is not None
    # Heaters on at 05:04:15; the window keeps the heat-up to 138 °F, before 05:46.
    assert 41 * 60 <= hu.elapsed <= 42.5 * 60
    delta_t, tau = hu.calibration_result()
    assert tau is None
    assert delta_t == pytest.approx(90, abs=1)
    # Left in, the fall and recovery pulled it down to about 87 °F.
    unguarded = h.fit_ceiling(h.smooth(samples), start, h.DEFAULT_TAU) - 70
    assert unguarded == pytest.approx(87, abs=1)
