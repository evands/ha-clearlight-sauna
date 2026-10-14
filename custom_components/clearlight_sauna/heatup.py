"""Heat-up model behind the time-to-target estimate (no Home Assistant imports).

While the heaters run flat out, the cabin behaves like one thermal mass:

    T(t) = T_inf - (T_inf - T0) * exp(-t / tau),    T_inf = room + delta_t

so the time from T to a target is tau * ln((T_inf - T) / (T_inf - target)).
tau and delta_t start from values measured on a Sanctuary 2 (three runs,
tools/heatup/README.md) and calibrate themselves from each cold-start session.
During a session the ceiling T_inf is refitted with tau held fixed: with tau
fixed the model is linear in T_inf, so a few minutes of data give a stable
answer. The exponential is memoryless, so the same fit works from any starting
point, including a warm cabin or a session already running when Home
Assistant starts.

The live refit is weighted by how much the calibration has been confirmed. On
the recorded runs (tools/heatup/evaluate_live.py) it cut the error a lot when
the stored values were wrong, as for another model on day one, but doubled it
when they were right: the first ~10 minutes heat faster than one exponential
allows, so early data overstates the ceiling. See tools/heatup/PHASE3.md.

Draft for Phase 3: not wired into the integration yet.

Temperatures are °F (what the controller reports); times are seconds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import median
from typing import Any

# Measured on a Sanctuary 2 (2026-10-13, 175 °F run): predicts the other clean
# run within about a minute at every step from 100 to 140 °F.
DEFAULT_TAU = 32 * 60.0
DEFAULT_DELTA_T = 85.0
TAU_RANGE = (10 * 60.0, 120 * 60.0)
DELTA_T_RANGE = (20.0, 150.0)

SAMPLE_INTERVAL = 15.0  # seconds between samples kept for fitting
SMOOTH_SAMPLES = 5  # rolling median over 75 s; the raw reading flickers 1-3 °F
LIVE_FIT_AFTER = 300.0  # seconds of heat-up before the live fit is shown without "~"

COLD_MARGIN = 5.0  # a start within this of room temperature counts as cold
MIN_CAL_SECONDS = 600.0  # a session needs this much heat-up data...
MIN_CAL_RISE = 15.0  # ...and this much rise before it can calibrate anything
# The stored values count as this many sessions when blending in a new one: the
# first session counts as much as the defaults. 0 let one odd run (2026-10-12)
# wreck the calibration; 2 learned another model's sauna too slowly. The weight
# never drops below MIN_WEIGHT, so the calibration keeps following slow changes
# (seasons, a new heater).
PRIOR_SESSIONS = 1
MIN_WEIGHT = 0.2

UNREACHABLE_MARGIN = 2.0  # a target within this of the ceiling is treated as out of reach
HOLD_MARGIN = 3.0  # once reached, readings this far below the target still count as there


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def live_prior_samples(sessions: int) -> float:
    """Weight of the stored ceiling in the live fit, in samples taken at LIVE_FIT_AFTER.

    Light while the calibration is unconfirmed, so a sauna unlike the defaults
    corrects itself within the session; heavy after a few sessions, when the
    stored model beats anything a few minutes of data can say.
    """
    return 80.0 * 4 ** min(max(sessions, 0), 2)


@dataclass
class Calibration:
    """What this sauna has learned about itself; persisted between sessions."""

    tau: float = DEFAULT_TAU
    delta_t: float = DEFAULT_DELTA_T
    sessions: int = 0  # sessions blended into delta_t
    tau_sessions: int = 0  # sessions long enough to also refine tau

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Calibration:
        cal = cls()
        if not data:
            return cal
        try:
            cal.tau = _clamp(float(data["tau"]), *TAU_RANGE)
            cal.delta_t = _clamp(float(data["delta_t"]), *DELTA_T_RANGE)
            cal.sessions = max(0, int(data.get("sessions", 0)))
            cal.tau_sessions = max(0, int(data.get("tau_sessions", 0)))
        except (KeyError, TypeError, ValueError):
            return cls()
        return cal

    def as_dict(self) -> dict[str, Any]:
        return {
            "tau": round(self.tau, 1),
            "delta_t": round(self.delta_t, 2),
            "sessions": self.sessions,
            "tau_sessions": self.tau_sessions,
        }

    def blended(self, delta_t: float, tau: float | None = None) -> Calibration:
        """This calibration with one more session's results blended in."""
        weight = max(1 / (self.sessions + PRIOR_SESSIONS + 1), MIN_WEIGHT)
        out = Calibration(
            tau=self.tau,
            delta_t=_clamp(self.delta_t + weight * (delta_t - self.delta_t), *DELTA_T_RANGE),
            sessions=self.sessions + 1,
            tau_sessions=self.tau_sessions,
        )
        if tau is not None:
            tau_weight = max(1 / (self.tau_sessions + PRIOR_SESSIONS + 1), MIN_WEIGHT)
            out.tau = _clamp(self.tau + tau_weight * (tau - self.tau), *TAU_RANGE)
            out.tau_sessions += 1
        return out


def eta_seconds(current: float, target: float, ceiling: float, tau: float) -> float | None:
    """Seconds from `current` to `target` while heading for `ceiling`.

    0 when already there; None when the target is at or above where the cabin
    levels off, so it would never (or only very slowly) get there.
    """
    if current >= target:
        return 0.0
    if ceiling - target < UNREACHABLE_MARGIN:
        return None
    return tau * math.log((ceiling - current) / (ceiling - target))


def smooth(samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Centered rolling median: the reading dips 1-3 °F for seconds at a time."""
    half = SMOOTH_SAMPLES // 2
    temps = [temp for _, temp in samples]
    return [
        (samples[i][0], median(temps[max(0, i - half) : i + half + 1]))
        for i in range(len(samples))
    ]


def fit_ceiling(
    samples: list[tuple[float, float]],
    start_temp: float,
    tau: float,
    prior: float | None = None,
    prior_weight: float = 0.0,
) -> float | None:
    """Least-squares ceiling T_inf with tau fixed.

    T(t) = T_inf * g + T0 * (1 - g), with g = 1 - exp(-t / tau), so
    T - T0 * (1 - g) = T_inf * g: a straight line through the origin. An
    optional prior pulls the answer toward the stored ceiling with the given
    weight (in units of g squared).
    """
    num = den = 0.0
    for t, temp in samples:
        decay = math.exp(-max(t, 0.0) / tau)
        g = 1.0 - decay
        num += (temp - start_temp * decay) * g
        den += g * g
    if prior is not None and prior_weight > 0:
        num += prior_weight * prior
        den += prior_weight
    if den < 1e-9:
        return prior
    return num / den


def _sse(samples: list[tuple[float, float]], start_temp: float, tau: float, ceiling: float) -> float:
    total = 0.0
    for t, temp in samples:
        decay = math.exp(-max(t, 0.0) / tau)
        total += (temp - (ceiling * (1 - decay) + start_temp * decay)) ** 2
    return total


def fit_tau_and_ceiling(
    samples: list[tuple[float, float]], start_temp: float
) -> tuple[float, float] | None:
    """Fit tau and the ceiling together; None unless tau is clearly pinned down.

    Only a heat-up that has bent well over (run for about one tau or more)
    separates the two; on a nearly straight one many pairs fit equally well.
    """
    best: tuple[float, float, float] | None = None
    errors: dict[float, float] = {}
    tau = TAU_RANGE[0]
    while tau <= TAU_RANGE[1]:
        ceiling = fit_ceiling(samples, start_temp, tau)
        if ceiling is not None:
            sse = _sse(samples, start_temp, tau, ceiling)
            errors[tau] = sse
            if best is None or sse < best[2]:
                best = (tau, ceiling, sse)
        tau += 30.0
    if best is None:
        return None
    tau, ceiling, sse = best
    if tau <= TAU_RANGE[0] or tau >= TAU_RANGE[1]:
        return None  # ran into the bounds: not identified
    # Require the error to climb clearly on both sides of the best tau.
    floor = max(sse, 1e-6)
    low = errors.get(min(errors, key=lambda k: abs(k - tau * 0.7)))
    high = errors.get(min(errors, key=lambda k: abs(k - tau * 1.4)))
    if low is None or high is None or low < 1.5 * floor or high < 1.5 * floor:
        return None
    return tau, ceiling


@dataclass
class HeatUp:
    """One heat-up, from the heaters coming on until the target is first reached.

    `room` is the room temperature the stored ceiling is measured from. `cold`
    marks a start from a cabin at room temperature: only those calibrate.
    """

    start: float
    start_temp: float
    room: float
    calibration: Calibration
    cold: bool
    samples: list[tuple[float, float]] = field(default_factory=list)
    reached_at: float | None = None

    @property
    def tau(self) -> float:
        return self.calibration.tau

    @property
    def prior_ceiling(self) -> float:
        return self.room + self.calibration.delta_t

    @property
    def elapsed(self) -> float:
        return self.samples[-1][0] if self.samples else 0.0

    def add(self, now: float, temp: float, target: float) -> None:
        """Record a reading; the fit window closes when the target is first reached."""
        if self.reached_at is not None:
            return
        t = now - self.start
        if self.samples and t - self.samples[-1][0] < SAMPLE_INTERVAL * 0.9:
            return
        self.samples.append((t, float(temp)))
        if temp >= target:
            self.reached_at = now

    def ceiling(self) -> tuple[float, bool]:
        """Current best ceiling, and whether it is still only the stored model ("~")."""
        if self.elapsed < LIVE_FIT_AFTER:
            return self.prior_ceiling, True
        points = smooth(self.samples)
        g_ref = 1.0 - math.exp(-LIVE_FIT_AFTER / self.tau)
        weight = live_prior_samples(self.calibration.sessions) * g_ref * g_ref
        fitted = fit_ceiling(points, self.start_temp, self.tau, self.prior_ceiling, weight)
        if fitted is None:
            return self.prior_ceiling, True
        low, high = self.room + DELTA_T_RANGE[0], self.room + DELTA_T_RANGE[1]
        # It is still rising, so it levels off above anything it has reached.
        low = max(low, max(temp for _, temp in points) + 1.0)
        return _clamp(fitted, low, high), False

    def calibration_result(self) -> tuple[float, float | None] | None:
        """(delta_t, tau or None) this session supports, or None if it can't calibrate."""
        if not self.cold or len(self.samples) < 2:
            return None
        points = smooth(self.samples)
        rise = max(temp for _, temp in points) - self.start_temp
        if self.elapsed < MIN_CAL_SECONDS or rise < MIN_CAL_RISE:
            return None
        if self.elapsed >= self.tau:
            pair = fit_tau_and_ceiling(points, self.start_temp)
            if pair is not None:
                tau, ceiling = pair
                return ceiling - self.room, tau
        ceiling = fit_ceiling(points, self.start_temp, self.tau)
        if ceiling is None:
            return None
        return ceiling - self.room, None
