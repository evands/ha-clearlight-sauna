# Phase 3: time-to-target in the integration

Design and handoff, written 2026-10-13 at the end of a cloud session. **Built the same day in Claude Code on the Mac** (`estimate.py` and the entities below, 58 tests passing), with the owner's ready-by decisions folded in, and released as v0.4.0; see [Status](#status-2026-10-13).

## Goal (the owner's plan)

- A **time to target** sensor and a **ready at** timestamp sensor.
- **Self-calibrating**, because other people's saunas differ: start each session from stored values, refit ΔT with τ held fixed as it heats, and blend the result into the stored values at session end.
- Show **"~"** until there are about 5 minutes of heat-up data.
- The payoff is the reverse: a **"ready by 7:00"** automation that starts the sauna at 7:00 minus the ETA for the current room temperature.
- **Ready by takes three inputs** (owner, 2026-10-13): ready by, target temperature, and session length, where the session length starts when the temperature is estimated to be reached.

## What's already here

| File | What it is |
|---|---|
| `custom_components/clearlight_sauna/heatup.py` | The model, no HA imports: `eta_seconds`, `fit_ceiling` (τ fixed, optional prior), `fit_tau_and_ceiling` (joint, only when identified), `HeatUp` (one session: samples, live ceiling, "~" flag, calibration result), `Calibration` (stored values, blending, dict round-trip), `live_prior_samples`. |
| `tests/test_heatup.py` | 10 tests, including a replay of `data/2026-10-13-175.csv`. |
| `tools/heatup/evaluate_live.py` | Replays the recorded runs through `heatup.py` and scores the ETAs. Every number below comes from `python3 evaluate_live.py` over the three runs recorded by 2026-10-13 (`data/` now also holds the filtered 2026-10-14 run, so `data/*.csv` gives different tables). |
| `tools/heatup/README.md` | The three calibration runs and the model fits. |

## The model

While the heaters run flat out: `T(t) = T_inf - (T_inf - T0) * exp(-t / tau)`, with `T_inf = room + ΔT`, so `ETA(T -> target) = tau * ln((T_inf - T) / (T_inf - target))`. The exponential is memoryless: an ETA from the current reading needs no history, and the same fit works from a warm cabin or from partway through a session.

Defaults (`DEFAULT_TAU` 32 min, `DEFAULT_DELTA_T` +85 °F) come from the 2026-10-13 175 °F run. They predict the 140 °F run that morning within about a minute at every step; the 2026-10-12 run is an outlier (fast start, then a knee and a dip).

## What the replays showed (and how the plan changed)

Errors are minutes, ETA to 120/130/140 °F checked every 5 minutes.

**1. The live refit hurts a sauna whose stored values are right.** With the defaults:

| Live weight (samples) | 10-13 140 °F: mean / worst | 10-13 175 °F | 10-12 outlier |
|---|---|---|---|
| 20 | 2.2 / 6.8 | 1.7 / 5.2 | 8.4 / 19.8 |
| 80 | 1.8 / 5.7 | 1.2 / 2.8 | 7.7 / 16.5 |
| 320 | 1.4 / 4.1 | 0.9 / 1.8 | 6.9 / 14.7 |
| 1280 | 1.1 / 3.0 | 0.7 / 1.6 | 6.2 / 12.3 |
| stored model only | 1.0 / 2.4 | 0.6 / 1.7 | 5.5 / 10.2 |

The first ~10 minutes heat faster than one exponential allows, so a fixed-τ fit on early data overstates the ceiling and predicts far targets too early. (Caveat: the defaults were fitted to the 175 °F run, so that column is in-sample; the 140 °F run is not.)

**2. It helps a lot when they're wrong**, as for another model on day one (all runs together, mean / worst):

| Stored values | weight 80 | stored only |
|---|---|---|
| τ 20 min, +110 °F | 8.8 / 30.6 | 12.0 / 31.5 |
| τ 45 min, +70 °F | 7.9 / 46.5 | 24.6 / 60.6 |
| τ 32 min, +70 °F | 5.1 / 29.8 | 12.5 / 31.3 |
| τ 32 min, +100 °F | 5.4 / 20.4 | 6.5 / 19.1 |

**Decision:** keep the live refit, but weight the stored ceiling by trust: `live_prior_samples(sessions)` = 80, 320, then 1280 from 2 calibrated sessions on (equivalent samples at the 5-minute mark). Weight 20 was worse than 80 in table 1, and in table 2 it failed outright on the fast wrong prior (worst 99 = "unreachable") for a slightly better mean on the others.

**3. Blending at session end.** The stored values count as `PRIOR_SESSIONS` sessions. With 0, calibrating from the 10-12 outlier first made the 140 °F target "unreachable" on the next run (mean error 36 min). With 2, every wrong-start case did worse on its third session (mean 4.9-10.4 min, against 4.1-8.9 with 1), in exchange for slightly better results when the defaults were already right. **Decision: 1** (the first session counts as much as the defaults; later weights 1/3, 1/4, then a floor of 0.2 so it keeps following seasons).

## Design decisions

1. **ETA input:** the de-dithered `coordinator.cabin_temp` for "where it is now"; the raw `SaunaState.current_temp` sampled every 15 s for fitting (75 s rolling median inside the fit), the same treatment `fit.py` used.
2. **Fit window:** from the heaters coming on (`state.heating` turning true) until the target is first reached or heating stops. Without the upgrade heater there's no relay signal; reaching the setpoint is the cut.
3. **"~":** an attribute (`approximate: true`) until 5 minutes of samples. A numeric sensor can't hold "~"; dashboards prefix it.
4. **Calibrate only from cold starts:** start within 5 °F of room temperature after at least 3 h idle, at least 10 min of heat-up and 15 °F of rise. Joint τ + ceiling fit when the session ran at least **two** τ (`JOINT_FIT_TAUS`, v0.4.3; one τ before) and the fit is identified (error climbs ≥ 1.5× at 0.7τ and 1.4τ); otherwise ΔT only, τ fixed. The first calibrating session on hardware (2026-10-15, cold start to 140 °F) showed why: a window that stops at 140 °F (≈1.4 τ) fits τ 25.5–29 min and ΔT 75–79 °F on every such run, and that session made the stored model 2–5 min pessimistic on the others (mean error 1.2 → 1.7 min). With τ fixed the same runs give ΔT 83–87 °F (mean error 1.1 min). The calibration was reset and the rule changed.
5. **Room temperature:** the de-dithered cabin reading once the sauna has been idle 3 h (it took 3-4 h to settle on 2026-10-13), persisted. Used for planning ETAs, warm starts, and to turn a session's ceiling into ΔT.
6. **Home Assistant restart mid-session:** start a new `HeatUp` from that moment, marked not cold (no calibration); the live fit still works because the model is memoryless.
7. **Persistence:** `homeassistant.helpers.storage.Store`, key `clearlight_sauna.heatup.<entry_id>`: calibration, room temperature and when it was taken, when heating last stopped. Delete it in `async_remove_entry`.
8. **Ready by is an action** (owner's choice over device entities): `clearlight_sauna.schedule_ready` on the climate entity, with `ready_by` (time of day; the next occurrence), `temperature` (HA's unit, converted to °F) and `minutes_at_temperature` (5-90). An automation calls it any time beforehand; the integration starts the sauna itself. This replaces the planned `time` Ready by entity and the Start by sensor with its 15-minute freeze, which existed only so a `time` trigger couldn't be skipped. `cancel_ready` drops a pending plan. One pending plan per sauna; a new call replaces it.
9. **Start time:** recomputed every 15 s as ready by minus the planning ETA from the current cabin temperature, rounded down to the minute (the Scheduled start sensor shows the same minute). Already heating at that point: the plan is dropped and the session left alone. Ready by passed without a start (sauna unreachable): dropped with a warning.
10. **Session length = heat-up estimate + time at temperature** (owner's decision): written as the session length before power-on, the way the session-time entity does it. Over 90 minutes is allowed for these sessions (owner: "raise the cap"): the action refuses a target that would take more than `READY_MAX_HEATUP_MINUTES` (90) to reach from room temperature, so a session is at most 180 minutes. The manual slider stays at 90.
11. **Restore the session length afterwards:** a long ready-by length would otherwise become the controller's configured length, and the next keypad session would run for it. The length from before is persisted and written back once the session has been seen heating and then stops. At 0:00 the power flag is cleared first (heaters already off) so the timer is never written while the power flag is set. If the session is never seen heating within 2 minutes of the start, the length is put back anyway.
12. **Calibration reset:** a button entity in the configuration category (owner's choice), back to the defaults.
13. **Near-ceiling fallback:** when the live ceiling is within 2 °F of the target, the ETA comes from the slope of the last 5 minutes (smoothed) while it's still climbing at least 0.1 °F/min, marked approximate.

## Entities and actions (built)

| Entity / action | State | Notes |
|---|---|---|
| `sensor` **Time to target** (`time_to_target`), duration, min | Heating: minutes left (0 once reached; readings within 3 °F below the target still count as there). Off: heat-up time if started now. Unknown when out of reach. | Attributes: `approximate`, `ceiling_f`, `room_temperature_f`, `delta_t_f`, `time_constant_min`, `calibration_sessions`. |
| `sensor` **Ready at** (`ready_at`), timestamp | Heating: now + time left, rounded up to the minute; once reached, when it was reached. Off: unknown. | |
| `sensor` **Scheduled start** (`scheduled_start`), timestamp | When a pending ready-by plan will start the sauna. Unknown with no plan. | Attributes: `ready_by`, `target_temperature_f`, `minutes_at_temperature`, `session_minutes`. |
| `button` **Reset heat-up calibration** (`reset_calibration`), config | | |
| `clearlight_sauna.schedule_ready` / `cancel_ready` | | Target: the climate entity. Errors `target_out_of_reach`, `temperature_out_of_range` are translated. |

Outputs are a frozen `Estimate` dataclass on `coordinator.estimator.output`; listeners are only notified from the 15 s tick when it changes.

The ready-by automation:

```yaml
triggers:
  - trigger: time
    at: "04:00:00"
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
actions:
  - action: clearlight_sauna.schedule_ready
    target:
      entity_id: climate.clearlight_sauna
    data:
      ready_by: "07:00:00"
      temperature: 140
      minutes_at_temperature: 45
```

## Status (2026-10-13)

Built: `estimate.py` (estimator, plans, persistence), hooks in `coordinator.py` and `__init__.py`, the sensors, `button.py`, `services.yaml`, translations, diagnostics, `tests/test_estimate.py` (9 integration tests on the fake device: planning ETA, cold session to target with "~" for 5 min and calibration, warm start, ready-by start and restore, 0:00 restore, refusals, reload and cancel, reset button, °C input). README section "Time to target and ready by". The fake controller now takes a session length written while off as its configured length, as the real one does.

The open questions are settled: the automation doesn't set the session (the action does, from the estimate); no timer restart at target; calibration reset is a button; near-ceiling uses the slope fallback.

Still to do:

- ~~Hardware checks~~ **done 2026-10-14** (ready by 08:42, 140 °F, 30 min; room 75 °F, default model, not a cold start because of a 5 s on/off at 06:35). It started at 07:56 with a 76-minute session; the countdown passed 61 → 60 → 59 normally. The cabin reached 140 °F at 08:39:28, 43.4 min in, against the 46 min planned, so 2.5 min early and 32 min at temperature. At 0:00 (09:11:51) the power flag was cleared within a second, the controller reset to 76, and 60 was written back by 09:11:54. The upgrade heater's relay cycled with the thermostat throughout.
- **Warm starts run fast** (2026-10-15, `data/2026-10-15-warm.csv`): from 100 °F, 38 min after a session, the heat-up to 140 °F took 21.7 min against the model's 32.2, like a cold cabin at ~118 °F, because the benches and walls stay hotter than the air. The memoryless single exponential can't see that, so the planning ETA (Time to target when off, and a ready-by's start) is pessimistic soon after a session; the live refit was within a minute by 12 min in. A correction from time since off and peak temperature needs more warm starts (e.g. 30 min, 1 h, 2 h after a session) to fit. Not done.
- ~~Ready at jitters~~ **steadied in v0.4.2** (`heatup.SteadyReadyTime`): the shown time is the median of the last 3 minutes of estimates and moves only when that median shifts by more than a minute; a shown time that passes before the target is reached is replaced at once. Time to target counts down to it, so the two agree. Replaying the four runs: direction reversals 25-46 -> 1-3 per heat-up, mean error over the last 15 min 0.9-1.2 min (was 1.0-1.5). Reset when the target changes or the live fit takes over (the 5-minute mark).
- **Door opening (v0.4.4).** On 2026-10-19 (`data/2026-10-19-ready.csv`) the owner went in at 138 °F, 41.7 min into a cold 70 °F start, before the cabin reached 140 °F, so the window that normally closes at the target stayed open: the reading fell to 131 °F and climbed back to 139 °F over the next 14 min, and the session calibrated a rise of 86.7 °F instead of the 90.1 °F the heat-up before the door gives. `HeatUp.add` now closes the window when the median of the last 5 samples falls 5 °F below its peak and cuts the samples back to the smoothed peak (`interrupted_at`; logged at info). On that run it trips 75 s after the fall, keeps 42.2 min and gives 90.1 °F; it never trips on the six other recorded runs. The live estimate after the door holds up: from 131 °F the session's own fit (ceiling 160 °F) gives 12.0 min to 140 °F, and the cabin took 12.4 min to get back to 139 °F.
- **A colder room didn't slow the end of the heat-up (2026-10-19).** From 70 °F (6-8 °F colder than every earlier run) the cabin reached 120 °F 4-5 min later than from 76-78 °F, but 135 °F at 37.4 min and 138 °F at 40.8, the same as the others (36.8-38.4 and 39.1-41.5). The model puts the ceiling at room + ΔT, so a 70 °F room lowers it by 6-8 °F, and with 140 °F close under it that stretched the planned heat-up to 55.5 min: about 12 min pessimistic, and the cabin was nearly ready when the owner got up. The ceiling looks much less tied to room temperature than 1:1. One run isn't enough to fit the dependence; record two or three more cold mornings first. Not changed.
- **Session-length restore read back (v0.4.4).** On 2026-10-19 the owner turned the ready session off at the keypad at 06:00:27. The integration wrote the old 60 min back at 06:00:32.8; the controller acknowledged it, but its status 50 ms later still said 76, and it stayed 76 (the same write by hand at 06:05 held). Nothing noticed: the restore didn't check. `async_restore_session_length` now reads the length back and raises unless it held, and the estimator tries again after 15 s, 30 s, 1, 2 and 4 min (6 tries, about 8 min), then gives up with a warning. Why the first write was dropped isn't known; on 2026-10-14 the same write 1.5 s after the integration's own power-off held.
- **Released:** v0.3.2 (filter fix, setup step, 90-minute cap), then Phase 3 as v0.4.0, both 2026-10-13.
- ~~Dashboard~~ done in `ha-master-bath-dashboard` v1.2.0: "Heating · ready ~7:42", a ready-by sheet behind a clock button (defaults from `last_*`, lit while a plan is pending or `running`), and a heat button that's lit while heating.
- Tests jump the clock with `freezer`; `tests/test_estimate.py` pushes the client heartbeat out of the way, since a ping in flight across a jump times out and drops the fake connection.

## Repo state at handoff

(As the cloud session left it; superseded by [Status](#status-2026-10-13).)

- `origin/main` is at 14e7315. Local commits on top: the 10-13 run data, the cabin-filter redesign that never made it into the pushed v0.3.2 (`temperature.py`: heat-up dips of 2-3 °F are now held, only 5+ °F against the trend passes at once), and this handoff.
- **v0.3.2 isn't released yet** (HACS has v0.3.1 installed). The filter commit belongs in it, and so does the setup-flow work below, which documents itself as v0.3.2. Release once that's committed, pushed and through CI. Then Phase 3 as v0.4.0.
- **Uncommitted work from another session:** an upgrade-heater question during setup (`config_flow.py`, `translations/en.json`, `tests/test_integration.py`), with matching notes in `README.md` and `CLAUDE.md`. It was in progress at handoff and was left alone: the CLAUDE.md changes in these commits took only their own lines. The full suite passes with it in the tree.
- **Tests:** a Python 3.13+ venv, `pip install -r requirements_test.txt`, `pytest`.
- `.git/stale-*` files and `tmp_obj_*` leftovers in `.git/objects` came from the cloud session's file bridge, which couldn't delete. Safe to delete.
