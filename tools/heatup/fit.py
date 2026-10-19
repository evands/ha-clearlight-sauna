#!/usr/bin/env python3
"""Fit a heat-up model to exported sauna sessions and report ETA accuracy.

    python3 fit.py data/*.csv

Model, during the heat-up (heaters on, cabin rising):

    T(t) = T_inf - (T_inf - T0) * exp(-(t - t_d) / tau)        for t >= t_d

tau is the cabin's time constant, T_inf the temperature it would level off
at if the heaters never cycled, and t_d a short dead time while the heaters
warm up. ETA from a reading T to a target Ts is tau * ln((T_inf - T) / (T_inf - Ts)).

The raw reading is whole degrees and noisy (it flickers 1-3 F for seconds at
a time), so it is resampled to 5 s steps and smoothed with a 61 s rolling
median before fitting. The fit stops at the heat-up's peak, or earlier at the
first heater-relay off, because after that the heaters are no longer simply on,
or at a `note` row whose text starts "door opened" (someone went in).

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import csv
import math
import sys
from datetime import datetime
from statistics import median

CABIN = "sensor.clearlight_sauna_cabin_temperature"
CLIMATE = "climate.clearlight_sauna"
RELAY_SUFFIX = "_heater_relay"
STEP = 5.0  # s
WINDOW = 61.0  # s, rolling median


def load(path: str) -> dict:
    cabin: list[tuple[float, float]] = []
    relay: list[tuple[float, bool]] = []
    climate: list[tuple[float, str, str, str]] = []
    notes: list[tuple[float, str]] = []
    t0: datetime | None = None
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            when = datetime.fromisoformat(row["time"])
            if t0 is None:
                t0 = when
            t = (when - t0).total_seconds()
            ent, state = row["entity_id"], row["state"]
            if state in ("unavailable", "unknown", ""):
                continue
            if ent == CABIN:
                cabin.append((t, float(state)))
            elif ent.endswith(RELAY_SUFFIX):
                relay.append((t, state == "on"))
            elif ent == CLIMATE:
                mode, action, target = (state.split("|") + ["", ""])[:3]
                climate.append((t, mode, action, target))
            elif ent == "note":
                notes.append((t, state))
    return {"path": path, "start": t0, "cabin": cabin, "relay": relay, "climate": climate,
            "notes": notes}


def heat_window(s: dict) -> tuple[float, float]:
    """From the relay (or climate) turning on to the first relay off or door opening."""
    on = next((t for t, v in s["relay"] if v), None)
    if on is None:
        on = next(t for t, mode, *_ in s["climate"] if mode == "heat")
    off = next((t for t, v in s["relay"] if not v and t > on), math.inf)
    door = next((t for t, text in s.get("notes", []) if t > on and text.startswith("door opened")),
                math.inf)
    return on, min(off, door)


def resample(points: list[tuple[float, float]], t_from: float, t_to: float) -> list[tuple[float, float]]:
    out, i, last = [], 0, None
    t = t_from
    while t <= t_to:
        while i < len(points) and points[i][0] <= t:
            last = points[i][1]
            i += 1
        if last is not None:
            out.append((t, last))
        t += STEP
    return out


def smooth(series: list[tuple[float, float]]) -> list[tuple[float, float]]:
    half = int(WINDOW / STEP / 2)
    vals = [v for _, v in series]
    return [(series[k][0], median(vals[max(0, k - half): k + half + 1])) for k in range(len(series))]


def fit_first_order(ts: list[float], temps: list[float], t0_temp: float) -> dict:
    """Grid over T_inf; for each, regress ln(T_inf - T) on t (closed form)."""
    best = None
    curve = []
    t_inf = max(temps) + 0.5
    while t_inf <= 400:
        xs, ys = [], []
        for t, T in zip(ts, temps):
            xs.append(t)
            ys.append(math.log(t_inf - T))
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        if slope < 0:
            tau = -1 / slope
            b = my - slope * mx
            sse = sum((T - (t_inf - math.exp(b + slope * t))) ** 2 for t, T in zip(xs, temps))
            td = tau * (b - math.log(t_inf - t0_temp))
            curve.append((t_inf, sse))
            if best is None or sse < best["sse"]:
                best = {"t_inf": t_inf, "tau": tau, "td": td, "sse": sse, "n": n}
        t_inf += 0.5
    assert best is not None
    best["rmse"] = math.sqrt(best["sse"] / best["n"])
    # How well T_inf is pinned down: the range whose SSE is within 25 % of the best.
    ok = [ti for ti, sse in curve if sse <= best["sse"] * 1.25]
    best["t_inf_range"] = (min(ok), max(ok))
    return best


def eta(model: dict, temp: float, target: float) -> float:
    if target >= model["t_inf"]:
        return math.inf
    return model["tau"] * math.log((model["t_inf"] - temp) / (model["t_inf"] - target))


def analyse(s: dict) -> dict:
    on, off = heat_window(s)
    cabin = [(t - on, T) for t, T in s["cabin"]]
    ambient = next((T for t, T in reversed(cabin) if t <= 0), cabin[0][1])
    series = smooth(resample(cabin, 0.0, min(off - on, cabin[-1][0])))
    peak_t, peak_T = max(series, key=lambda p: (p[1], -p[0]))
    ramp = [(t, T) for t, T in series if t <= peak_t]
    model = fit_first_order([t for t, _ in ramp], [T for _, T in ramp], ambient)
    model.update(ambient=ambient, peak=(peak_t, peak_T), on=on, off=off, ramp=ramp,
                 delta_t=model["t_inf"] - ambient)
    return model


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    for path in paths:
        s = load(path)
        m = analyse(s)
        print(f"\n{path}")
        print(f"  start {s['start']:%Y-%m-%d %H:%M}, ambient {m['ambient']:.0f} F")
        print(f"  peak {m['peak'][1]:.0f} F at {m['peak'][0] / 60:.1f} min"
              + ("" if math.isinf(m["off"]) else f", window ends (relay off or door) at {(m['off'] - m['on']) / 60:.1f} min"))
        print(f"  tau {m['tau'] / 60:.1f} min, T_inf {m['t_inf']:.0f} F (dT {m['delta_t']:.0f} F above ambient),"
              f" dead time {m['td']:.0f} s, RMSE {m['rmse']:.2f} F")
        lo, hi = m["t_inf_range"]
        print(f"  T_inf within 25% of best SSE: {lo:.0f}-{hi:.0f} F"
              + ("  <- poorly identified" if hi - lo > 20 else ""))
        reached = []
        for target in range(int(m["ambient"]) // 10 * 10 + 10, int(m["peak"][1]) + 1, 10):
            t = next((t for t, T in m["ramp"] if T >= target), None)
            if t is not None:
                reached.append(f"{target} F {t / 60:.0f} min")
        print("  Reached (smoothed, from heaters on): " + ", ".join(reached))
        print("  ETA check (model vs this run's own smoothed curve):")
        start = m["ambient"] + 4
        for frm, to in ((start, 100), (start, 120), (start, 140), (100, 120), (120, 140)):
            actual_from = next((t for t, T in m["ramp"] if T >= frm), None)
            actual_to = next((t for t, T in m["ramp"] if T >= to), None)
            if actual_from is None or actual_to is None:
                continue
            pred = eta(m, frm, to)
            print(f"    {frm:.0f}->{to:.0f} F: model {pred / 60:5.1f} min, actual {(actual_to - actual_from) / 60:5.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
