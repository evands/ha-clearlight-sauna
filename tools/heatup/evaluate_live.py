#!/usr/bin/env python3
"""Replay recorded heat-ups through the integration's estimator and score its ETAs.

    python3 evaluate_live.py data/*.csv

For each run, the raw reading is sampled every 15 s from the heater relay
turning on (as the integration will see it) and, every 5 minutes, the ETA to
120, 130 and 140 °F is compared with when the smoothed curve actually got
there. Errors are minutes (positive = predicted late). Three tables:

1. How much weight the live refit should get when the stored model is right.
2. The same when the stored model is wrong (another sauna on day one).
3. Calibrating session by session, with the live weight following the
   number of sessions, for different PRIOR_SESSIONS.

Loads custom_components/clearlight_sauna/heatup.py by path, so it needs no
Home Assistant install. Standard library only; Python 3.9+.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import fit  # tools/heatup/fit.py

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "heatup", ROOT / "custom_components" / "clearlight_sauna" / "heatup.py"
)
h = importlib.util.module_from_spec(_spec)
sys.modules["heatup"] = h
_spec.loader.exec_module(h)

TARGETS = (120, 130, 140)
CHECK_MINUTES = range(5, 45, 5)


def load(path: str):
    """(name, start temp, 15 s raw samples, 5 s smoothed curve) for one recorded run."""
    s = fit.load(path)
    on, off = fit.heat_window(s)
    raw = [(t - on, temp) for t, temp in s["cabin"]]
    end = min(off - on, raw[-1][0])
    start = next(temp for t, temp in reversed(raw) if t <= 0)
    smoothed = fit.smooth(fit.resample(raw, 0, end))
    samples, i, last, t = [], 0, start, 0.0
    while t <= end:
        while i < len(raw) and raw[i][0] <= t:
            last = raw[i][1]
            i += 1
        samples.append((t, last))
        t += h.SAMPLE_INTERVAL
    return Path(path).stem, start, samples, smoothed


def ceiling_at(samples, start, cal, weight_samples):
    """The live ceiling after `samples`; weight_samples None = stored model only."""
    prior = start + cal.delta_t
    if weight_samples is None or samples[-1][0] < h.LIVE_FIT_AFTER:
        return prior
    points = h.smooth(samples)
    g_ref = 1 - math.exp(-h.LIVE_FIT_AFTER / cal.tau)
    fitted = h.fit_ceiling(points, start, cal.tau, prior, weight_samples * g_ref * g_ref)
    return max(fitted, max(temp for _, temp in points) + 1)


def errors(run, cal, weight_samples):
    _, start, samples, smoothed = run
    out = []
    for target in TARGETS:
        actual = next((t for t, temp in smoothed if temp >= target), None)
        if actual is None:
            continue
        for minute in CHECK_MINUTES:
            now = minute * 60
            if now >= actual - 60:
                continue
            seen = [p for p in samples if p[0] <= now]
            current = smoothed[min(int(now / 5), len(smoothed) - 1)][1]
            eta = h.eta_seconds(current, target, ceiling_at(seen, start, cal, weight_samples), cal.tau)
            out.append(99.0 if eta is None else (now + eta - actual) / 60)
    return out


def score(runs, cal, weight_samples):
    errs = [abs(e) for run in runs for e in errors(run, cal, weight_samples)]
    return f"mean {sum(errs) / len(errs):4.1f}  worst {max(errs):5.1f}"


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    runs = [load(p) for p in paths]
    # 20 = a lighter weight than any session count gives, for comparison.
    weights = [20.0] + sorted({h.live_prior_samples(n) for n in range(4)}) + [None]
    label = lambda w: "stored only" if w is None else f"weight {w:>6.0f}"  # noqa: E731

    print("1. Stored model right (defaults), live weight vs error, per run")
    for w in weights:
        print(f"   {label(w)}:  " + "   ".join(f"{run[0]} {score([run], h.Calibration(), w)}" for run in runs))

    print("\n2. Stored model wrong, all runs together")
    for name, cal in [
        ("tau 20 min, +110 F", h.Calibration(tau=1200, delta_t=110)),
        ("tau 45 min, +70 F", h.Calibration(tau=2700, delta_t=70)),
        ("tau 32 min, +70 F", h.Calibration(tau=1920, delta_t=70)),
        ("tau 32 min, +100 F", h.Calibration(tau=1920, delta_t=100)),
    ]:
        print(f"   {name}")
        for w in weights:
            print(f"      {label(w)}:  {score(runs, cal, w)}")

    print("\n3. Session by session (live weight follows sessions); each run scored before it calibrates")
    default_prior = h.PRIOR_SESSIONS
    for prior_sessions in (0, 1, 2):
        h.PRIOR_SESSIONS = prior_sessions
        print(f"   PRIOR_SESSIONS = {prior_sessions}")
        for name, cal0 in [("defaults", h.Calibration()), ("wrong fast", h.Calibration(tau=1200, delta_t=110)),
                           ("wrong slow", h.Calibration(tau=2700, delta_t=70))]:
            for order in (runs, list(reversed(runs))):
                cal, steps = cal0, []
                for run in order:
                    steps.append(f"{run[0][5:]} {score([run], cal, h.live_prior_samples(cal.sessions))}")
                    hu = h.HeatUp(start=0.0, start_temp=run[1], room=run[1], calibration=cal, cold=True)
                    for t, temp in run[2]:
                        hu.add(t, temp, 175)
                    result = hu.calibration_result()
                    if result:
                        cal = cal.blended(result[0], result[1])
                print(f"      {name:10} " + "  ->  ".join(steps)
                      + f"   => tau {cal.tau / 60:.1f} min, +{cal.delta_t:.1f} F")
    h.PRIOR_SESSIONS = default_prior
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
