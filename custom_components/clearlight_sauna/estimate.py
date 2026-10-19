"""Heat-up estimate, room temperature and ready-by plans, wired to the coordinator.

The maths lives in heatup.py. This module feeds it: it samples the cabin while
the heaters run, tracks room temperature while the sauna is idle, calibrates
from cold starts, persists what it learns, and runs ready-by plans:

* ``schedule_ready`` stores (ready by, target, minutes at temperature);
* every tick until then, the start time is recomputed as ready by minus the
  heat-up estimate from the current cabin temperature;
* at that time the sauna starts with the target as its setpoint and a session
  of heat-up estimate + minutes at temperature, so the time at temperature
  starts when the target is expected to be reached;
* when that session ends, the session length the controller had before is
  written back, so the next keypad session isn't the long one.

See tools/heatup/PHASE3.md for the design and the replay results behind it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import heatup as h
from .const import (
    DOMAIN,
    READY_MAX_HEATUP_MINUTES,
    READY_SESSION_MAX_MINUTES,
    SESSION_MIN_MINUTES,
)
from .protocol import SaunaState

if TYPE_CHECKING:
    from .coordinator import ClearlightCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Idle this long and the cabin reads room temperature (it took 3-4 h to settle
# on 2026-10-13).
ROOM_IDLE = 3 * 3600.0
ROOM_SAVE_DELAY = 300.0  # room readings change often; save them lazily
TICK = timedelta(seconds=h.SAMPLE_INTERVAL)
# With the ceiling too close to the target the model can't give an ETA; while
# the cabin is still clearly climbing, extrapolate from the last few minutes.
SLOPE_WINDOW = 300.0
MIN_SLOPE = 0.1 / 60  # °F per second
# A started ready session that hasn't been seen heating after this long never
# got going; put the session length back anyway.
START_CONFIRM = 120.0
# Writing the session length back is read back and retried if the controller
# didn't keep it (it ignored the write 5 s after a keypad power-off on
# 2026-10-19), waiting 15 s, 30 s, ... 4 min between tries: about 8 min in all.
RESTORE_RETRY = 15.0
RESTORE_ATTEMPTS = 6


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.heatup.{entry_id}"


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ReadyPlan:
    """A pending ready-by request. Times are timestamps; the target is °F."""

    ready_by: float
    target: int
    hold: int  # minutes at temperature
    start_at: float | None = None  # current estimate of when to start
    heatup: float | None = None  # current heat-up estimate, seconds

    @classmethod
    def from_dict(cls, data: Any) -> ReadyPlan | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                ready_by=float(data["ready_by"]),
                target=int(data["target"]),
                hold=int(data["hold"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def as_dict(self) -> dict[str, Any]:
        return {"ready_by": self.ready_by, "target": self.target, "hold": self.hold}

    @property
    def session_minutes(self) -> int | None:
        if self.heatup is None:
            return None
        return min(math.ceil(self.heatup / 60) + self.hold, READY_SESSION_MAX_MINUTES)


@dataclass(frozen=True)
class Estimate:
    """What the entities show. Changes are published only when this changes."""

    minutes: int | None = None  # time to the setpoint; 0 once there
    approximate: bool = True
    ready_at: datetime | None = None
    ceiling: float | None = None
    room: float | None = None
    scheduled_start: datetime | None = None
    plan_ready_by: datetime | None = None
    plan_target: int | None = None
    plan_hold: int | None = None
    plan_session_minutes: int | None = None
    ready_running: bool = False  # a ready-by session is heating now
    last_request: tuple[str, int, int] | None = None  # (ready by "HH:MM:SS", °F, minutes)


class HeatUpEstimator:
    """Owned by the coordinator; entities read ``output``."""

    def __init__(self, coordinator: ClearlightCoordinator) -> None:
        self._coordinator = coordinator
        self.hass = coordinator.hass
        self._store: Store[dict[str, Any]] = Store(
            self.hass, STORAGE_VERSION, storage_key(coordinator.config_entry.entry_id)
        )
        self.calibration = h.Calibration()
        self.room: float | None = None
        self.room_at: float | None = None
        self.idle_since: float | None = None
        self.plan: ReadyPlan | None = None
        # Session length to write back once a ready-by session ends.
        self.restore_minutes: int | None = None
        # The last schedule_ready request, offered as defaults the next time.
        self.last_request: tuple[str, int, int] | None = None
        self.session: h.HeatUp | None = None
        # Ready at is steadied: the raw estimate saws by a minute or two.
        self._ready = h.SteadyReadyTime()
        self._ready_key: tuple[float, bool] | None = None  # (target, approximate)
        self.output = Estimate()
        self._heating: bool | None = None
        self._ticker: CALLBACK_TYPE | None = None
        self._busy = False  # a plan's start or finish is being written
        # Loaded from the store, a pending restore is for a session that may
        # already have ended while Home Assistant was down.
        self._restore_armed = True
        self._started_at = 0.0
        self._restore_failures = 0
        self._restore_retry_at = 0.0

    @staticmethod
    def _now() -> float:
        return dt_util.utcnow().timestamp()

    # -- persistence -------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.calibration = h.Calibration.from_dict(data.get("calibration"))
        self.room = _float(data.get("room"))
        self.room_at = _float(data.get("room_at"))
        self.idle_since = _float(data.get("idle_since"))
        self.plan = ReadyPlan.from_dict(data.get("plan"))
        restore = data.get("restore_minutes")
        self.restore_minutes = restore if isinstance(restore, int) else None
        last = data.get("last_request")
        try:
            self.last_request = (str(last["ready_by"]), int(last["target"]), int(last["hold"]))
        except (KeyError, TypeError, ValueError):
            self.last_request = None

    def _data(self) -> dict[str, Any]:
        return {
            "calibration": self.calibration.as_dict(),
            "room": self.room,
            "room_at": self.room_at,
            "idle_since": self.idle_since,
            "plan": self.plan.as_dict() if self.plan else None,
            "restore_minutes": self.restore_minutes,
            "last_request": (
                dict(zip(("ready_by", "target", "hold"), self.last_request))
                if self.last_request
                else None
            ),
        }

    @callback
    def _save(self, delay: float = 0) -> None:
        self._store.async_delay_save(self._data, delay)

    async def async_unload(self) -> None:
        self._stop_ticker()
        await self._store.async_save(self._data())

    # -- following the device ----------------------------------------------

    @callback
    def async_update(self, state: SaunaState) -> bool:
        """Follow a new device state (or a tick); True if the outputs changed."""
        now = self._now()
        if state.heating != self._heating:
            if state.heating:
                # None: the first state since setup, so HA started mid-session.
                self._session_started(state, now, resumed=self._heating is None)
            elif self._heating:
                self._session_stopped(now)
            elif self.idle_since is None:
                self.idle_since = now  # unknown how long it's been off; assume just now
                self._save()
            self._heating = state.heating
        if state.heating and self.session is not None:
            session = self.session
            was_open = session.interrupted_at is None
            session.add(now, state.current_temp, state.set_temp)
            if was_open and session.interrupted_at is not None:
                _LOGGER.info(
                    "%s: cabin fell %.0f °F or more while heating (door opened?); "
                    "heat-up data kept up to %.1f min",
                    self._coordinator.config_entry.title,
                    h.DOOR_DROP,
                    session.elapsed / 60,
                )
        self._track_room(state, now)
        self._check_plan(state, now)
        self._update_ticker(state)
        return self._recompute(state, now)

    def _session_started(self, state: SaunaState, now: float, resumed: bool) -> None:
        self._ready.reset()
        start_temp = float(state.current_temp)
        idle = None if self.idle_since is None else now - self.idle_since
        cold = (
            not resumed
            and idle is not None
            and idle >= ROOM_IDLE
            and self.room is not None
            and abs(start_temp - self.room) <= h.COLD_MARGIN
        )
        self.session = h.HeatUp(
            start=now,
            start_temp=start_temp,
            room=self.room if self.room is not None else start_temp,
            calibration=self.calibration,
            cold=cold,
        )

    def _session_stopped(self, now: float) -> None:
        self._ready.reset()
        session, self.session = self.session, None
        self.idle_since = now
        if session is not None and (result := session.calibration_result()) is not None:
            delta_t, tau = result
            self.calibration = self.calibration.blended(delta_t, tau)
            _LOGGER.debug(
                "Heat-up calibrated: delta_t %.1f, tau %s -> %s",
                delta_t,
                None if tau is None else round(tau / 60, 1),
                self.calibration.as_dict(),
            )
        self._save()

    def _track_room(self, state: SaunaState, now: float) -> None:
        if state.heating or self.idle_since is None or now - self.idle_since < ROOM_IDLE:
            return
        temp = self._coordinator.cabin_temp
        if temp is not None and temp != self.room:
            self.room, self.room_at = float(temp), now
            self._save(ROOM_SAVE_DELAY)

    @callback
    def _update_ticker(self, state: SaunaState) -> None:
        if state.heating or self.plan is not None or self.restore_minutes is not None:
            if self._ticker is None:
                self._ticker = async_track_time_interval(self.hass, self._async_tick, TICK)
        else:
            self._stop_ticker()

    @callback
    def _stop_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker()
            self._ticker = None

    @callback
    def _async_tick(self, _now: datetime) -> None:
        state = self._coordinator.client.state
        if state is not None and self.async_update(state):
            self._coordinator.async_update_listeners()

    # -- outputs -----------------------------------------------------------

    def _current(self, state: SaunaState) -> float:
        temp = self._coordinator.cabin_temp  # de-dithered: "where it is now"
        return float(state.current_temp if temp is None else temp)

    def _room_for_planning(self, state: SaunaState) -> float | None:
        if self.room is not None:
            return self.room
        return None if state.heating else self._current(state)

    def planning_eta(self, state: SaunaState, target: float) -> float | None:
        """Heat-up seconds if a session started now, from the stored model."""
        room = self._room_for_planning(state)
        if room is None:
            return None
        cal = self.calibration
        return h.eta_seconds(self._current(state), target, room + cal.delta_t, cal.tau)

    def _session_eta(self, state: SaunaState, now: float) -> tuple[float | None, bool, float]:
        """(seconds to the setpoint, approximate, ceiling) during a session."""
        s = self.session
        assert s is not None
        current, target = self._current(state), float(state.set_temp)
        ceiling, approximate = s.ceiling()
        if current >= target and s.reached_at is None:
            s.reached_at = now  # the filtered reading got there first; close the fit
        if s.reached_at is not None and current >= target - h.HOLD_MARGIN:
            return 0.0, approximate, ceiling
        eta = h.eta_seconds(current, target, ceiling, s.tau)
        if eta is None:
            eta = self._slope_eta(s, current, target)
            approximate = True
        return eta, approximate, ceiling

    @staticmethod
    def _slope_eta(s: h.HeatUp, current: float, target: float) -> float | None:
        if s.elapsed < h.LIVE_FIT_AFTER:
            return None
        points = h.smooth(s.samples)
        t_end, temp_end = points[-1]
        window = [p for p in points if p[0] >= t_end - SLOPE_WINDOW]
        t0, temp0 = window[0]
        if t_end <= t0:
            return None
        slope = (temp_end - temp0) / (t_end - t0)
        if slope < MIN_SLOPE:
            return None
        return max(0.0, (target - current) / slope)

    def _recompute(self, state: SaunaState, now: float) -> bool:
        minutes: int | None = None
        approximate = True
        ready_at: datetime | None = None
        ceiling: float | None = None
        if state.heating and self.session is not None:
            eta, approximate, ceiling = self._session_eta(state, now)
            # A new target, or the live fit taking over, moves the estimate for
            # a reason: start the steadying over rather than lag behind it.
            key = (float(state.set_temp), approximate)
            if key != self._ready_key:
                self._ready.reset()
                self._ready_key = key
            if eta is not None:
                reached = self.session.reached_at
                if eta == 0 and reached is not None:
                    minutes = 0
                    ready_at = dt_util.utc_from_timestamp(math.floor(reached))
                else:
                    shown = self._ready.update(now, now + eta)
                    ready_at = dt_util.utc_from_timestamp(shown)
                    # Count down to the steadied time, so the two always agree.
                    minutes = max(1, math.ceil((shown - now) / 60))
        elif not state.heating:
            if (room := self._room_for_planning(state)) is not None:
                ceiling = room + self.calibration.delta_t
                eta = self.planning_eta(state, state.set_temp)
                minutes = None if eta is None else math.ceil(eta / 60)
        plan = self.plan
        out = Estimate(
            minutes=minutes,
            approximate=approximate,
            ready_at=ready_at,
            ceiling=None if ceiling is None else round(ceiling, 1),
            room=self.room,
            scheduled_start=(
                dt_util.utc_from_timestamp(plan.start_at)
                if plan and plan.start_at is not None
                else None
            ),
            plan_ready_by=dt_util.utc_from_timestamp(plan.ready_by) if plan else None,
            plan_target=plan.target if plan else None,
            plan_hold=plan.hold if plan else None,
            plan_session_minutes=plan.session_minutes if plan else None,
            ready_running=state.heating and self.restore_minutes is not None,
            last_request=self.last_request,
        )
        changed = out != self.output
        self.output = out
        return changed

    # -- ready-by plans ----------------------------------------------------

    @callback
    def async_schedule(self, ready_by: dt_time, target: int, hold: int) -> None:
        """Plan a session that reaches ``target`` (°F) by the next ``ready_by``."""
        state = self._coordinator.client.state or self._coordinator.data
        if state is None:
            raise HomeAssistantError("No status from the sauna yet")
        local_now = dt_util.now()
        ready = datetime.combine(local_now.date(), ready_by, tzinfo=local_now.tzinfo)
        if ready <= local_now:
            ready += timedelta(days=1)
        # Check the worst case, a cold cabin at room temperature.
        room = self._room_for_planning(state)
        if room is None:
            room = self._current(state)
        cal = self.calibration
        start = min(room, self._current(state))
        cold_eta = h.eta_seconds(start, target, room + cal.delta_t, cal.tau)
        if cold_eta is None or cold_eta > READY_MAX_HEATUP_MINUTES * 60:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="target_out_of_reach",
                translation_placeholders={
                    "target": str(target),
                    "minutes": str(READY_MAX_HEATUP_MINUTES),
                },
            )
        self.plan = ReadyPlan(ready_by=ready.timestamp(), target=target, hold=hold)
        self.last_request = (ready_by.strftime("%H:%M:%S"), target, hold)
        _LOGGER.info(
            "%s: ready at %d °F by %s, %d min at temperature",
            self._coordinator.config_entry.title, target, ready.isoformat(), hold,
        )
        self._save()
        self.async_update(state)
        self._coordinator.async_update_listeners()

    @callback
    def async_cancel(self) -> None:
        if self.plan is None:
            return
        self.plan = None
        self._save()
        state = self._coordinator.client.state or self._coordinator.data
        if state is not None:
            self.async_update(state)
        self._coordinator.async_update_listeners()

    def _check_plan(self, state: SaunaState, now: float) -> None:
        if self._busy:
            return
        if self.restore_minutes is not None:
            if state.heating:
                self._restore_armed = True
                self._restore_failures, self._restore_retry_at = 0, 0.0
            elif now < self._restore_retry_at:
                pass  # waiting to try again
            elif self._restore_armed or now - self._started_at > START_CONFIRM:
                # The ready session is over (or never got going).
                self._busy = True
                self._coordinator.config_entry.async_create_task(
                    self.hass, self._async_finish(self.restore_minutes)
                )
                return
        plan = self.plan
        if plan is None:
            return
        if now >= plan.ready_by:
            _LOGGER.warning(
                "%s: missed the ready-by time (sauna unreachable?); plan dropped",
                self._coordinator.config_entry.title,
            )
            self.plan = None
            self._save()
            return
        eta = self.planning_eta(state, plan.target)
        if eta is not None:
            plan.heatup = eta
            # On the minute, as the Scheduled start sensor shows it.
            plan.start_at = math.floor((plan.ready_by - eta) / 60) * 60
        if plan.start_at is None or now < plan.start_at:
            return
        if state.heating:
            # Someone started it already; don't change a session in progress.
            _LOGGER.info(
                "%s: already heating at the planned start; plan dropped",
                self._coordinator.config_entry.title,
            )
            self.plan = None
            self._save()
            return
        self._busy = True
        self._coordinator.config_entry.async_create_task(self.hass, self._async_start(plan))

    async def _async_start(self, plan: ReadyPlan) -> None:
        minutes = plan.session_minutes or plan.hold
        try:
            restore = await self._coordinator.async_start_session(plan.target, minutes)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not start the planned session, will retry: %s", err)
            return
        finally:
            self._busy = False
        if self.plan is plan:
            self.plan = None
        if self.restore_minutes is None:
            self.restore_minutes = max(restore, SESSION_MIN_MINUTES)
        # else a restore from the last ready session was still being retried:
        # the length read just now was that session's, so keep the older one.
        self._restore_failures, self._restore_retry_at = 0, 0.0
        # Restore only after the session has been seen running: the state in
        # hand may predate the power-on.
        self._restore_armed = False
        self._started_at = self._now()
        self._save()
        self._refresh()

    async def _async_finish(self, minutes: int) -> None:
        title = self._coordinator.config_entry.title
        try:
            await self._coordinator.async_restore_session_length(minutes)
        except HomeAssistantError as err:
            self._restore_failures += 1
            if self._restore_failures < RESTORE_ATTEMPTS:
                delay = RESTORE_RETRY * 2 ** (self._restore_failures - 1)
                self._restore_retry_at = self._now() + delay
                _LOGGER.info(
                    "%s: could not restore the session length to %d min, trying again in %.0f s: %s",
                    title, minutes, delay, err,
                )
                return
            _LOGGER.warning(
                "%s: gave up restoring the session length to %d min after %d tries: %s",
                title, minutes, self._restore_failures, err,
            )
        else:
            if self._restore_failures:
                _LOGGER.info(
                    "%s: session length restored to %d min on try %d",
                    title, minutes, self._restore_failures + 1,
                )
        finally:
            self._busy = False
        self._restore_failures, self._restore_retry_at = 0, 0.0
        self.restore_minutes = None
        self._save()
        self._refresh()

    @callback
    def _refresh(self) -> None:
        state = self._coordinator.client.state
        if state is not None and self.async_update(state):
            self._coordinator.async_update_listeners()

    # -- maintenance -------------------------------------------------------

    @callback
    def async_reset_calibration(self) -> None:
        self.calibration = h.Calibration()
        self._save()
        self._refresh()

    def diagnostics(self) -> dict[str, Any]:
        s = self.session
        return {
            "calibration": self.calibration.as_dict(),
            "room": self.room,
            "room_at": self.room_at,
            "idle_since": self.idle_since,
            "plan": self.plan.as_dict() if self.plan else None,
            "restore_minutes": self.restore_minutes,
            "session": None
            if s is None
            else {
                "start": s.start,
                "start_temp": s.start_temp,
                "room": s.room,
                "cold": s.cold,
                "samples": len(s.samples),
                "elapsed": s.elapsed,
                "reached_at": s.reached_at,
                "interrupted_at": s.interrupted_at,
            },
        }
