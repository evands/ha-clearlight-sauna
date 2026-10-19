# Heat-up calibration (phase 1)

Tools for measuring how fast a sauna heats, so the integration can estimate when it will reach its target.

## Model

While the heaters run flat out, the cabin behaves roughly like a single thermal mass:

```
T(t) = T_inf - (T_inf - T0) * exp(-(t - t_d) / tau)
ETA(T -> Ts) = tau * ln((T_inf - T) / (T_inf - Ts))
```

- `tau`: the cabin's time constant (heat capacity over heat loss).
- `T_inf`: where it would level off if the heaters never cycled; report it as `dT = T_inf - ambient` so one calibration covers summer and winter.
- `t_d`: a short dead time while the heaters warm up.

If `T_inf` sits well above the usual target, a heat-up to that target is nearly straight and `tau` and `T_inf` can't be separated. One run toward the 175 °F maximum fixes that.

## Runs

1. **Max run** (once): cold cabin at room temperature, door and vent closed, nobody inside, target 175 °F, session 60 min.
2. **Normal runs** (4-6): cold starts to your usual target. Ordinary use counts, as long as nobody is inside while it heats.

Skip warm starts (cabin more than ~5 °F above room temperature): the walls hold heat and the model will be too slow.

## Export and fit

Home Assistant keeps full-resolution history for about 10 days, so export each run within that.

```sh
HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  python3 export_session.py 2026-10-12T21:45 2026-10-12T22:55 data/2026-10-12-max.csv
python3 fit.py data/*.csv
```

Both scripts use the standard library only (Python 3.9+). The export is long-form, one row per state change: the cabin temperature sensor, the heater relay, and the climate entity as `mode|hvac_action|target`. Add a row with entity `note` for anything the history can't show; a note whose text starts `door opened` ends the fit window there. Runs recorded before v0.3.2 hold the raw, flickering reading; from v0.3.2 the sensor is de-dithered. The fit handles both.

`fit.py` resamples to 5 s, takes a 61 s rolling median (the raw reading flickers 1-3 °F for seconds at a time), fits the heat-up from the relay turning on to the first relay off or the peak, and reports `tau`, `T_inf`, how well `T_inf` is pinned down, and ETA checks against the run itself.

## Data

| File | Run |
|---|---|
| `data/2026-10-12-max.csv` | Max run, 76 °F start, 175 °F target, 60 min, both heaters 100 %, door closed, nobody inside. Raw history from HA (de-dither filter not yet installed). |
| `data/2026-10-13-140.csv` | Normal run, 76 °F start, 140 °F target, 60 min, door closed, nobody inside; includes 2.5 h of cool-down. Raw history. |
| `data/2026-10-13-175.csv` | Repeat of the max run: 77 °F start (cabin at room temperature for 5+ h), 175 °F target, 60 min, door closed, nobody inside. A 2 s connection drop at 57 min. Raw history. |
| `data/2026-10-15-warm.csv` | Warm start: began at 100 °F, 38 min after the cold run ended (cabin at 137 °F then), 140 °F target, ready-by with 10 min at temperature. Reached 140 °F in **21.7 min** against 32.2 planned, as if from about 118 °F: the cabin's mass was still hot. The live estimate was within a minute from 12 min in. **Filtered** history, pulled through the MCP tools. |
| `data/2026-10-15-cold.csv` | First calibrating session: cold start at 78 °F after 34 h idle (an evening start; the room value was 78), 140 °F target, ready-by with 10 min at temperature, upgrade heater active. Reached 140 °F at 42.9 min. Its joint fit (τ 28.5 min, ΔT 77) made the stored model pessimistic, which led to the 2 τ rule in v0.4.3. **Filtered** history, pulled through the MCP tools. |
| `data/2026-10-19-ready.csv` | Ready-by session from the coldest room yet: 70 °F start after 8 h idle (room 70 °F), 140 °F target, 76 min session (56 heat-up + 20 at temperature), upgrade heater active. Reached 120 °F at 26.3 min (4-5 min slower than the 76-78 °F starts) but 135 °F at 37.4 and 138 °F at 40.8, as fast as they were; the plan's 55.5 min heat-up was about 12 min pessimistic. The owner opened the door and went in at 05:46 (138 °F, 41.7 min; marked by a `note` row): the reading fell to 131 °F, recovered to 139 °F by 06:00, and he turned it off at the keypad at 06:00:27. Cabin rows come from the climate entity's current temperature (the same de-dithered reading; values checked against the sensor's own history). **Filtered** history, pulled through the MCP tools. |
| `data/2026-10-14-ready.csv` | The first ready-by session: 76 °F start (room 75 °F, but not cold by the integration's rule after a 5 s on/off at 06:35), 140 °F target, 76 min session, upgrade heater active. Reached 140 °F at 43.4 min, then the relay cycled at the setpoint until 0:00. **Filtered** history (the integration's de-dithered cabin sensor), pulled from HA through the MCP tools, not `export_session.py`. |

## Findings so far

**2026-10-12 max run.** The cabin never got near 175 °F: it was still climbing slowly (about 0.6 °F/min) at 146 °F when the 60 min timer ended. Smoothed, it reached 100 °F at 8 min, 120 °F at 18 min, 130 °F at 36 min and 140 °F at 51 min.

The heat-up is **not** a single first-order curve. It rises fast to about 120 °F (3-4 °F/min at first, then about 2), then slows abruptly to under 1 °F/min. Around 26 min (127 °F) it fell back 2-4 °F for a few minutes before resuming; the heater relay stayed on throughout, so the cause isn't visible in the data. The best single-exponential fit (`tau` 23.5 min, `T_inf` 148 °F, RMSE 2.5 °F, negative dead time) runs about 25 % slow below 120 °F, so ETAs from it would be off by several minutes.

Next: normal cold-start runs will show whether the knee near 120 °F and the dip near 27 min repeat. If they do, an ETA built on the measured curve (time from heaters-on to each temperature, shifted for ambient) will beat any single time constant.

After the timer ended it cooled at about 2 °F/min (local time constant about 28 min, which will lengthen as the walls give up their heat).

**2026-10-13 140 °F run.** This one was a clean single exponential: `tau` 26 min, `T_inf` 152 °F, RMSE 1.0 °F, and the fit's own ETAs land within 1-3 min. Smoothed, it reached 100 °F at 10 min, 120 °F at 22 min, 130 °F at 33 min and 140 °F at 46 min. There was no dip and no knee, so the 2026-10-12 dip looks like a one-off; that run was also faster below 120 °F (18 vs 22 min) and slower above it (140 °F at 51 vs 46 min), which a single time constant can't reproduce.

At target the controller held 138-140 °F by dropping the heaters for about 89 s at a time (three times in the last 14 min). Cool-down was fast at first (139 to 107 °F in 22 min), then slow: still 82 °F 2.4 h after the timer ended, falling about 0.1 °F/min (local time constant about 80 min). Allow 3-4 h between cold-start runs.

**2026-10-13 175 °F run.** It peaked at 150 °F as the timer ended (146 °F on 2026-10-12) and fit a single exponential well: `tau` 31.8 min, `T_inf` 162 °F (85 °F above ambient), RMSE 1.1 °F. Below 140 °F it tracked the morning's 140 °F run within a minute or two: 100 °F at 9 min, 120 °F at 22 min, 130 °F at 32 min, 140 °F at 43 min.

**Where this leaves the ETA.** One time constant is enough after all: with this run's numbers (`tau` 32 min, `T_inf` = ambient + 85 °F) the predicted times to 100, 120, 130 and 140 °F match the 140 °F run within about a minute. The 2026-10-12 run is the outlier (up to 6 min off, with its fast start, knee and dip); more normal runs will show how often that happens.

| Run | 100 °F | 120 °F | 130 °F | 140 °F |
|---|---|---|---|---|
| Model (`tau` 32 min, ambient + 85 °F) | 10.6 | 23.3 | 32.2 | 44.6 |
| 2026-10-13 140 °F | 9.5 | 22.2 | 32.7 | 45.8 |
| 2026-10-13 175 °F | 9.3 | 21.7 | 31.5 | 43.2 |
| 2026-10-12 max | 7.9 | 18.2 | 36.5 | 50.8 |

Minutes from heaters on, smoothed; model shown for a 76 °F start.
