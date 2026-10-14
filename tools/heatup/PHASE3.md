# Phase 3: time-to-target in the integration

Design and handoff, written 2026-10-13 at the end of a cloud session. The model is drafted and tested; **nothing is wired into the integration yet**. Continue in Claude Code on the Mac.

## Goal (the owner's plan)

- A **time to target** sensor and a **ready at** timestamp sensor.
- **Self-calibrating**, because other people's saunas differ: start each session from stored values, refit ΔT with τ held fixed as it heats, and blend the result into the stored values at session end.
- Show **"~"** until there are about 5 minutes of heat-up data.
- The payoff is the reverse: a **"ready by 7:00"** automation that starts the sauna at 7:00 minus the ETA for the current room temperature.

## What's already here

| File | What it is |
|---|---|
| `custom_components/clearlight_sauna/heatup.py` | The model, no HA imports: `eta_seconds`, `fit_ceiling` (τ fixed, optional prior), `fit_tau_and_ceiling` (joint, only when identified), `HeatUp` (one session: samples, live ceiling, "~" flag, calibration result), `Calibration` (stored values, blending, dict round-trip), `live_prior_samples`. |
| `tests/test_heatup.py` | 10 tests, including a replay of `data/2026-10-13-175.csv`. |
| `tools/heatup/evaluate_live.py` | Replays the recorded runs through `heatup.py` and scores the ETAs. Every number below comes from `python3 evaluate_live.py data/*.csv`. |
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
4. **Calibrate only from cold starts:** start within 5 °F of room temperature after at least 3 h idle, at least 10 min of heat-up and 15 °F of rise. Joint τ + ceiling fit when the session ran at least one τ and the fit is identified (error climbs ≥ 1.5× at 0.7τ and 1.4τ); otherwise ΔT only, τ fixed.
5. **Room temperature:** the de-dithered cabin reading once the sauna has been idle 3 h (it took 3-4 h to settle on 2026-10-13), persisted. Used for planning ETAs, warm starts, and to turn a session's ceiling into ΔT.
6. **Home Assistant restart mid-session:** start a new `HeatUp` from that moment, marked not cold (no calibration); the live fit still works because the model is memoryless.
7. **Persistence:** `homeassistant.helpers.storage.Store`, key `clearlight_sauna.heatup.<entry_id>`: calibration, room temperature and when it was taken, when heating last stopped. Delete it in `async_remove_entry`.

## Entities to add

| Entity | State | Notes |
|---|---|---|
| `sensor` **Time to target** (`time_to_target`), duration, min | Heating: minutes left (0 once reached; readings within 3 °F below the target still count as there, since the controller cycles). Off: heat-up time if started now. Unknown when the target is out of reach. | Attributes: `approximate`, `ceiling_f`, `room_temperature_f`, `delta_t_f`, `time_constant_min`, `calibration_sessions`. |
| `sensor` **Ready at** (`ready_at`), timestamp | Heating: now + time left, rounded up to the minute; once reached, when it was reached. Off: unknown. | |
| `time` **Ready by** (`ready_by`), config category | Default 07:00, restored across restarts. | New platform: add `Platform.TIME`. |
| `sensor` **Start by** (`start_by`), timestamp | The next Ready by minus the heat-up time from the current cabin temperature, rounded down to the minute. Unknown while heating or when unreachable. | **Freeze it once it's within 15 min**, so a `time` trigger on it can't be skipped by the value moving into the past. |

Only publish a change when an output actually changes (the sampling timer runs every 15 s).

The ready-by automation then needs no templates:

```yaml
triggers:
  - trigger: time
    at: sensor.clearlight_sauna_start_by
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
actions:
  - action: climate.turn_on
    target:
      entity_id: climate.clearlight_sauna
```

## Open questions for the owner

- **The session timer includes the heat-up.** Sessions max out at 60 min, and 140 °F takes ~45 min from cold, so a ready-by start leaves ~15 min at temperature, less with the default 10-min session. Should the automation set the session to 60 first? Should the integration offer to restart the timer when the target is reached (power off, then on, which resets it)? That changes what the heater does, so it's the owner's call.
- **Near-ceiling targets.** If calibration ends with the ceiling within 2 °F of the target, the ETA reads unknown even though the cabin is still climbing (seen in the PRIOR_SESSIONS = 0 replay). A fallback from the recent slope would cover it.
- **Calibration reset:** a button, or only the store file? The owner may want one after hardware changes (the upgrade heater adds 300 W).

## Implementation steps

1. Read `heatup.py` and `tests/test_heatup.py`; they're the contract.
2. Add an estimator (e.g. `estimate.py`) owned by the coordinator: load and save the store, a 15 s timer while heating, session start and end, room tracking, outputs as a small dataclass the entities read; `async_update_listeners()` only on change.
3. Hook it into `coordinator.async_set_state` and `_async_update_data`, and load the store in `async_setup_entry` before forwarding platforms.
4. Entities: three sensors in `sensor.py`, the new `time.py`, names in `translations/en.json` under `entity.sensor` / `entity.time`. Calibration and session state go in `diagnostics.py`.
5. Integration tests with the fake device and `freezer` (see `test_cabin_temperature_dither_is_filtered`): off shows a planning ETA marked approximate; heating with rising readings drops "~" after 5 minutes; reaching the target gives 0 and a Ready at; session end blends and saves (sessions 1); a warm start doesn't calibrate; Start by freezes inside 15 min.
6. Docs: a README section with the automation above, a CLAUDE.md entry, `manifest.json` 0.4.0.
7. Afterwards, in the `ha-master-bath-dashboard` repo: show "~" and "ready 7:42" on the sauna card.

## Repo state at handoff

- `origin/main` is at 14e7315. Local commits on top: the 10-13 run data, the cabin-filter redesign that never made it into the pushed v0.3.2 (`temperature.py`: heat-up dips of 2-3 °F are now held, only 5+ °F against the trend passes at once), and this handoff.
- **v0.3.2 isn't released yet** (HACS has v0.3.1 installed). The filter commit belongs in it, and so does the setup-flow work below, which documents itself as v0.3.2. Release once that's committed, pushed and through CI. Then Phase 3 as v0.4.0.
- **Uncommitted work from another session:** an upgrade-heater question during setup (`config_flow.py`, `translations/en.json`, `tests/test_integration.py`), with matching notes in `README.md` and `CLAUDE.md`. It was in progress at handoff and was left alone: the CLAUDE.md changes in these commits took only their own lines. The full suite passes with it in the tree.
- **Tests:** a Python 3.13+ venv, `pip install -r requirements_test.txt`, `pytest`.
- `.git/stale-*` files and `tmp_obj_*` leftovers in `.git/objects` came from the cloud session's file bridge, which couldn't delete. Safe to delete.
