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

Both scripts use the standard library only (Python 3.9+). The export is long-form, one row per state change: the cabin temperature sensor, the heater relay, and the climate entity as `mode|hvac_action|target`. Runs recorded before v0.3.2 hold the raw, flickering reading; from v0.3.2 the sensor is de-dithered. The fit handles both.

`fit.py` resamples to 5 s, takes a 61 s rolling median (the raw reading flickers 1-3 °F for seconds at a time), fits the heat-up from the relay turning on to the first relay off or the peak, and reports `tau`, `T_inf`, how well `T_inf` is pinned down, and ETA checks against the run itself.

## Data

| File | Run |
|---|---|
| `data/2026-10-12-max.csv` | Max run, 76 °F start, 175 °F target, 60 min, both heaters 100 %, door closed, nobody inside. Raw history from HA (de-dither filter not yet installed). |

## Findings so far

**2026-10-12 max run.** The cabin never got near 175 °F: it was still climbing slowly (about 0.6 °F/min) at 146 °F when the 60 min timer ended. Smoothed, it reached 100 °F at 8 min, 120 °F at 18 min, 130 °F at 36 min and 140 °F at 51 min.

The heat-up is **not** a single first-order curve. It rises fast to about 120 °F (3-4 °F/min at first, then about 2), then slows abruptly to under 1 °F/min. Around 26 min (127 °F) it fell back 2-4 °F for a few minutes before resuming; the heater relay stayed on throughout, so the cause isn't visible in the data. The best single-exponential fit (`tau` 23.5 min, `T_inf` 148 °F, RMSE 2.5 °F, negative dead time) runs about 25 % slow below 120 °F, so ETAs from it would be off by several minutes.

Next: normal cold-start runs will show whether the knee near 120 °F and the dip near 27 min repeat. If they do, an ETA built on the measured curve (time from heaters-on to each temperature, shifted for ambient) will beat any single time constant.

After the timer ended it cooled at about 2 °F/min (local time constant about 28 min, which will lengthen as the walls give up their heat).
