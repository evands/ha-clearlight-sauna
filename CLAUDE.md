# CLAUDE.md: ha-clearlight-sauna

A Home Assistant custom integration (HACS) for Clearlight infrared saunas that use the 2.4 GHz "Clearlight Sauna Connect" app. Control is local push over the sauna's Gizwits GAgent Wi-Fi module. Read this before changing protocol, safety, or release behavior.

## Layout
- `custom_components/clearlight_sauna/protocol.py`: LAN client with no HA imports; must stay Python 3.9-compatible so it can be bench-tested with macOS system Python.
- `coordinator.py` is push-driven, with a 60 s watchdog poll. Entities live in `climate`, `light`, `number`, `sensor` and `switch`.
- `heatup.py`: the heat-up model (no HA imports). `estimate.py` wires it in: time to target, ready at, scheduled start, the ready-by action and calibration (see below). Design and replay results: `tools/heatup/PHASE3.md`. `tools/heatup/` also holds the calibration runs (`data/`), `fit.py`, and `evaluate_live.py`, which replays the runs through `heatup.py`.
- `tests/fake_gagent.py`: a simulated controller. Frame layouts match real captures; the device identity in it is **synthetic**.
- CI (`.github/workflows/validate.yml`) runs hassfest, the HACS action, and pytest on the latest HA (Python 3.14), on push and weekly.

## Protocol facts (verified on hardware, 2026-10-10/11)
- **Discovery and connection:** UDP 12414 for discovery; TCP 12416 for control. The module hands its passcode to any LAN client that asks (cmd 0x06), then the client logs in (0x08). Keep a heartbeat (0x15) every 4 s; status pushes arrive as 0x91.
- **Writes:** cmd 0x93, payload P0 `01` + 2-byte flags + 10 value bytes. **The flag mask is honored**: unflagged bytes are ignored (verified). We still copy current values into unflagged bytes as a second safeguard.
- **Status:** 12 bytes:
  - byte0 bit0: outside light
  - byte0 bit1: inside light
  - byte0 bit2: reservation
  - byte0 bit3: power
  - byte0 bit4: °C display flag only. Values are **always °F**, even in °C mode.
  - byte0 bit5: `UVB_FLAG` = AUX (see below)
  - byte0 bit6: `N_FLAG` (unknown)
  - byte1: `LED`. Stored but no effect on an IR-remote color light. Values 1–4 accepted, 255 refused. Not exposed.
  - byte2: RIGHT full-spectrum heater intensity, 0–100 %
  - byte3: LEFT full-spectrum heater intensity, 0–100 %
  - byte4: setpoint (°F)
  - bytes 5–6: hours/minutes. Session length when off; **counts down once a minute while heating**. The keypad stores 60 min as 0 h 60 min. Since v0.3.2 the integration allows up to 90 min (the owner's call, 2026-10-13; the keypad stops at 60), written the same way as 0 h 90 min. **A countdown from above 60 hasn't been seen on hardware yet**; check the first 90-minute session counts down and stops at 0:00.
  - bytes 7–8: reservation fields (untested)
  - byte9: `SN` (unknown)
  - byte10: cabin temp (°F)
  - byte11: `heart_pulse` (always 0)
- **Quirk (confirmed):** while off, the controller emits SET_MINUTE 10 -> 9 -> 10 within about 1 s, roughly every 40 min, with no keypad input. It's harmless; nothing filters it.
- **Cabin temperature dither (observed 2026-10-11/12):** byte10 flips between neighbouring values (e.g. 75/76) every ~2 s to ~2 min when the true temperature sits near a boundary, ~1,500 HA state changes a day while idle; while heating it dips 1-2 °F (occasionally 3) for seconds at a time. Since v0.3.2 the coordinator runs it through `temperature.CabinTempFilter` (direction-of-travel hysteresis: steps with the trend pass at once; readings against it must stay on that side for 180 s, wandering allowed; a gap of 5+ °F passes at once; trend set +1 at session start, -1 at stop). Heat-up noise reaches 2-3 °F for a few seconds (calibration run 2026-10-12), so smaller thresholds flicker. Entities read `coordinator.cabin_temp`; `SaunaState.current_temp` stays raw.
- **Timer at 0:00:** the heaters stop but the **power bit stays set**. Turning power off resets the timer to the configured length. v0.2.0 handles this: the climate entity shows off/idle with `session_ended`, and `turn_on` cycles power.

### AUX / upgrade heater (critical)
- **Default mode:** AUX is a manual toggle that is **independent of the session**. It can be on with power off, and it survives both timer expiry and power-off. No timer.
- **Heater mode:** Clearlight's 300 W Full Spectrum Upgrade Heater plugs into an IEC C13 receptacle by the door. A one-time keypad sequence (power on → °C → 60 °C → hold AUX 5 s → beep) puts that output into heater mode. The bit then mirrors the **heater relay**: set with power on, cleared at power off, at timer 0, and when the setpoint is below cabin temp. There's about a 60 s delay before it re-energizes. Heater mode survives normal power cycles. Survival through a mains power loss is untested.
- **Toggling AUX from the app or over the network breaks heater mode** and reverts it to the timer-less manual mode until the keypad sequence is re-run (verified). The v0.2.0 AUX switch does exactly this.
- **Activation is not visible in any datapoint.** Only the behavior shows it.

## Upgrade-heater option (v0.3.0)
- **Options flow:** `upgrade_heater` (default off), handled by `OptionsFlowWithReload`.
- **Initial setup (v0.3.2):** the config flow ends with a menu step `upgrade_heater` (`heater_activated` / `no_heater`) and writes the answer to `entry.options`, where the options flow reads it. It's a menu rather than a checkbox so there's no default to click past. The text tells owners with the heater installed but not activated to pick `heater_activated` too, so HA never exposes an AUX switch to a receptacle with a heater plugged in.
- **When on:**
  - the AUX `switch` entity is removed from the registry, and nothing ever writes bit 5 (a test asserts this);
  - a read-only `binary_sensor` "Heater relay" (bit 5) is added;
  - climate `hvac_action` = HEATING or IDLE from bit 5 while a session runs.
- **Mode-lost detection:** the coordinator raises a Repairs issue (`heater_mode_lost_<entry_id>`, severity error) after `HEATER_MODE_LOST_DELAY` (10 s) if bit 5 is set while not heating. It clears when bit 5 is off while not heating, and on unload.
- **Limitation:** a mode lost with AUX *off* can't be distinguished from a satisfied thermostat, so it isn't flagged. That case is safe (the receptacle stays dead) but the heater won't run.
- **When off:** the AUX switch stays and the binary_sensor is removed.

## Heat-up estimate and ready by (Phase 3, v0.4.0)
- **Estimator:** `coordinator.estimator` (`estimate.py`) is fed every state (push, poll, filter recheck) plus a 15 s tick while heating or while a plan or restore is pending. Entities read the frozen `estimator.output`. Persisted in `Store` `clearlight_sauna.heatup.<entry_id>` (calibration, room, idle since, plan, restore length), flushed on unload, deleted in `async_remove_entry`.
- **Calibration:** only from cold starts (idle 3 h+, within 5 °F of room, 10 min / 15 °F of heat-up); blended at session end. The reset button restores the defaults.
- **`clearlight_sauna.schedule_ready`** (on the climate entity): ready by, temperature (HA's unit), minutes at temperature. The integration starts the sauna at ready by minus the planning ETA, with setpoint = target and session = ceil(ETA) + minutes at temperature, up to 180 min (heat-up over 90 min from room is refused). This is the only path that writes a session over 90.
- **Session length is restored** after a ready session. It's written back only once the session has been seen heating and then stops; at 0:00 the power flag is cleared first. **Never write SET_MINUTE while the power flag is set:** the effect mid-session is unverified.
- **Tests:** `tests/test_estimate.py` jumps the clock with `freezer` and sets `protocol.PING_INTERVAL` huge, since a heartbeat in flight across a jump times out and drops the fake connection. The fake now treats a session length written while off as its configured length.

## Safety rules when touching real hardware
- Use bounded, single-purpose commands. Restore state afterwards and report exactly what changed.
- Get the owner's explicit OK before anything that energizes heaters. Never toggle AUX on a sauna in heater mode.
- Never commit device identity (DID, passcode, MAC, IP). DID plus passcode is what Gizwits' cloud binding API takes.

## Workflow notes
- **Tests:** `pip install -r requirements_test.txt && pytest`. The tests need `socket_enabled` (already applied via `pytestmark`).
- **Hassfest:** clone `home-assistant/core` sparse (`script/` only), then run `python -m script.hassfest --integration-path custom_components/clearlight_sauna`.
- **Pushing:** cloud sessions can't push to this repo (no GitHub App install). Pushes happen from the maintainer's Mac over **SSH**; the `gh` OAuth token there lacks `workflow` scope, so HTTPS pushes that touch `.github/workflows` are rejected.
- **Commits and releases:** commits are authored `Evan D. Schoenberg, M.D. <evands@users.noreply.github.com>`. Releases are full GitHub releases (`vX.Y.Z`) matching `manifest.json` `version`, cut after CI passes. Don't rewrite published history; release tags back HACS installs.
- **Later:** submit to the HACS default store (hacs/default, `integration` list) once the owner calls it stable. All checks already pass. On 2026-10-13 the owner said it isn't stable yet; revisit around mid-December 2026. The entry goes between `Eunanibus/ha-emporia-ev` and `evantaur/seiverkot-consumption`; open the PR from a branch, not the fork's `master`, and fill in the PR template's release and CI-run links.
