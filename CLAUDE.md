# CLAUDE.md: ha-clearlight-sauna

A Home Assistant custom integration (HACS) for Clearlight infrared saunas that use the 2.4 GHz "Clearlight Sauna Connect" app. Control is local push over the sauna's Gizwits GAgent Wi-Fi module. Read this before changing protocol, safety, or release behavior.

## Layout
- `custom_components/clearlight_sauna/protocol.py`: LAN client with no HA imports; must stay Python 3.9-compatible so it can be bench-tested with macOS system Python.
- `coordinator.py` is push-driven, with a 60 s watchdog poll. Entities live in `climate`, `light`, `number`, `sensor` and `switch`.
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
  - bytes 5–6: hours/minutes. Session length when off; **counts down once a minute while heating**.
  - bytes 7–8: reservation fields (untested)
  - byte9: `SN` (unknown)
  - byte10: cabin temp (°F)
  - byte11: `heart_pulse` (always 0)
- **Timer at 0:00:** the heaters stop but the **power bit stays set**. Turning power off resets the timer to the configured length. v0.2.0 handles this: the climate entity shows off/idle with `session_ended`, and `turn_on` cycles power.

### AUX / upgrade heater (critical)
- **Default mode:** AUX is a manual toggle that is **independent of the session**. It can be on with power off, and it survives both timer expiry and power-off. No timer.
- **Heater mode:** Clearlight's 300 W Full Spectrum Upgrade Heater plugs into an IEC C13 receptacle by the door. A one-time keypad sequence (power on → °C → 60 °C → hold AUX 5 s → beep) puts that output into heater mode. The bit then mirrors the **heater relay**: set with power on, cleared at power off, at timer 0, and when the setpoint is below cabin temp. There's about a 60 s delay before it re-energizes. Heater mode survives normal power cycles. Survival through a mains power loss is untested.
- **Toggling AUX from the app or over the network breaks heater mode** and reverts it to the timer-less manual mode until the keypad sequence is re-run (verified). The v0.2.0 AUX switch does exactly this.
- **Activation is not visible in any datapoint.** Only the behavior shows it.

## Planned v0.3.0 (designed, not built)
- **Options flow:** "Upgrade heater activated" (default off).
- **When on:**
  - no writable AUX switch: remove the stale `switch` entity from the registry;
  - a read-only `binary_sensor` "Heater relay" (bit 5);
  - climate `hvac_action` = HEATING if bit 5, else IDLE while a session runs;
  - **never write bit 5**;
  - a Repairs issue (`heater_mode_lost`) if bit 5 is set while not heating (power off or 0:00) for more than about 10 s. Clear it when bit 5 is off while not heating.
- **When off:** keep the AUX switch, with a README warning.
- Bump to 0.3.0 and add tests for the options flow, entity swap, hvac_action and repair debounce.

## Safety rules when touching real hardware
- Use bounded, single-purpose commands. Restore state afterwards and report exactly what changed.
- Get the owner's explicit OK before anything that energizes heaters. Never toggle AUX on a sauna in heater mode.
- Never commit device identity (DID, passcode, MAC, IP). DID plus passcode is what Gizwits' cloud binding API takes.

## Workflow notes
- **Tests:** `pip install -r requirements_test.txt && pytest`. The tests need `socket_enabled` (already applied via `pytestmark`).
- **Hassfest:** clone `home-assistant/core` sparse (`script/` only), then run `python -m script.hassfest --integration-path custom_components/clearlight_sauna`.
- **Pushing:** cloud sessions can't push to this repo (no GitHub App install). Pushes happen from the maintainer's Mac over **SSH**; the `gh` OAuth token there lacks `workflow` scope, so HTTPS pushes that touch `.github/workflows` are rejected.
- **Commits and releases:** commits are authored `Evan D. Schoenberg, M.D. <evands@users.noreply.github.com>`. Releases are full GitHub releases (`vX.Y.Z`) matching `manifest.json` `version`, cut after CI passes. Don't rewrite published history; release tags back HACS installs.
- **Later:** submit to the HACS default store (hacs/default, `integration` list) once the owner calls it stable. All checks already pass.
