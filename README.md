# Clearlight Sauna for Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=evands&repository=ha-clearlight-sauna&category=integration)

This integration gives Home Assistant local control of Clearlight infrared saunas with Wi-Fi, the kind that use the 2.4 GHz **Clearlight Sauna Connect** app. Home Assistant talks to the sauna's Wi-Fi module directly on your network, with no cloud account and no internet involved.

> **Status: early (0.x).** It has been tested on one sauna so far. Reports from other models are very welcome; see [Compatibility](#compatibility).

## Features

Entity IDs below are for a sauna named Clearlight Sauna that isn't in an area. Home Assistant 2026.10 builds the ID of a new entity from its area, device and name, so with the sauna in an area called Master Bathroom, Time to target is created as `sensor.master_bathroom_clearlight_sauna_time_to_target`. Entities that already existed keep their IDs when you update, so an older install can have both forms. **Settings → Devices & services → Entities** shows the actual IDs.

| Entity | What it does |
|---|---|
| `climate.clearlight_sauna` | Heat on/off, target temperature (70–175 °F), cabin temperature |
| `light.clearlight_sauna_inside_light` | Cabin light |
| `light.clearlight_sauna_outside_light` | Exterior and mood lights |
| `number.clearlight_sauna_left_full_spectrum_heater` | Left full-spectrum heater intensity, 0–100 % |
| `number.clearlight_sauna_right_full_spectrum_heater` | Right full-spectrum heater intensity, 0–100 % |
| `number.clearlight_sauna_session_time` | Session length (5–90 min) when off; time remaining while heating |
| `switch.clearlight_sauna_aux_outlet` | The keypad's AUX button (only when the upgrade-heater option is off) |
| `binary_sensor.clearlight_sauna_heater_relay` | Read-only: heaters energized (only when the upgrade-heater option is on) |
| `sensor.clearlight_sauna_cabin_temperature` | Cabin temperature, for history and automations |
| `sensor.clearlight_sauna_time_remaining` | Minutes left in the session; 0 when off |
| `sensor.clearlight_sauna_time_to_target` | Minutes until the cabin reaches the setpoint; when off, how long a heat-up would take |
| `sensor.clearlight_sauna_ready_at` | When the cabin is expected to reach the setpoint (or did) |
| `sensor.clearlight_sauna_scheduled_start` | When a [ready-by](#ready-by) plan will start the sauna |
| `button.clearlight_sauna_reset_heat_up_calibration` | Forget what the heat-up estimate has learned (configuration) |

- **Push updates.** The sauna pushes every state change, including changes made at the keypad or in the app, so Home Assistant doesn't need to poll.
- **Survives restarts and IP changes.** If the sauna is power-cycled, the integration reconnects. If DHCP gives the sauna a new address, the integration finds it again by MAC.
- **Per-heater intensity.** The two full-spectrum heaters can be set independently, from 0 (off) to 100 %.
- **Heat-up estimate that learns your sauna.** See [Time to target and ready by](#time-to-target-and-ready-by).
- **Fahrenheit internally.** The controller always reports °F, even when its display is set to °C. Home Assistant converts to your unit system.

## Installation

### HACS (recommended)

1. Click the button above, or in HACS go to **⋮ → Custom repositories**, add `https://github.com/evands/ha-clearlight-sauna`, and choose type **Integration**.
2. Download **Clearlight Sauna** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Clearlight Sauna**. Saunas on your network are discovered automatically; if yours isn't found, enter its IP address. Setup then asks whether Clearlight's upgrade heater has been activated (see [AUX and the upgrade heater](#aux-and-the-upgrade-heater)).

### Manual

Copy `custom_components/clearlight_sauna` into your Home Assistant `config/custom_components/` folder and restart.

### Before you start

- Set the sauna up once with the Clearlight app so it is on your Wi-Fi.
- Give the sauna a DHCP reservation on your router. The integration copes with address changes, but a fixed address is simpler.
- Home Assistant must be on the same network segment as the sauna. Discovery uses a UDP broadcast on port 12414; control uses TCP port 12416.

## Compatibility

| Sauna | Status |
|---|---|
| Clearlight sauna with the **Clearlight Sauna Connect** (2.4 GHz) app | ✅ Tested: reads and all writes verified on hardware |
| Jacuzzi®-branded Clearlight saunas (**Jacuzzi Sauna Connect** app) | ❓ Probably the same hardware; untested |
| Saunas that use the **Clearlight Sauna Connect 5GHz** app | ❓ Different, Bluetooth-provisioned Wi-Fi module; likely not compatible |

If you try a model that isn't listed, please [open an issue](https://github.com/evands/ha-clearlight-sauna/issues). Include the app you use and the integration's diagnostics (**Settings → Devices & services → Clearlight Sauna → ⋮ → Download diagnostics**). If you enter the IP of a Gizwits device that isn't a recognized Clearlight sauna, setup says so.

## Safety

This integration can turn infrared heaters on when nobody is in the room, exactly as the official app's remote start can. Clearlight's warnings about towels or other items on or against the heaters apply to every remote start. The integration caps manual sessions at 90 minutes, and the sauna turns off when the session timer runs out. That's 30 minutes more than the keypad allows, because the timer includes the heat-up: from a cold cabin, 140 °F takes about 45 minutes, so a 60-minute session leaves only about 15 at temperature. A [ready-by](#ready-by) session runs for the heat-up estimate plus up to 90 minutes at temperature, at most 180 minutes in all.

Use automations that start the sauna with care, and consider a notification whenever heating starts.

### AUX and the upgrade heater

What the AUX output does depends on whether Clearlight's **Full Spectrum Upgrade Heater** has been activated on the keypad. Both modes were verified on hardware.

- **Not activated (default):** AUX is a manual toggle with **no timer**. It can be on while the sauna is off, and it stays on through timer expiry and power-off. Don't plug anything that heats into it.
- **Activated:** the upgrade heater plugs into the IEC C13 receptacle by the door. The one-time keypad sequence (power on → °C → 60 °C → hold AUX 5 s until it beeps) puts that output into heater mode. It then follows the sauna's heater relay: on while heating, off at power-off, at 0:00, and when the thermostat is satisfied.

> ⚠️ **Toggling AUX over the network, including with the Clearlight app's AUX button, takes the sauna out of heater mode** and back to the timer-less behavior, until you re-run the keypad sequence.

**If you've activated the upgrade heater, choose Upgrade heater activated when you set up the integration.** If it's installed but not activated yet, choose it anyway so Home Assistant leaves AUX alone. To change the answer later: **Settings → Devices & services → Clearlight Sauna → Configure → Upgrade heater activated.** With it on:

- The AUX switch is removed, and the integration never writes AUX.
- A read-only **Heater relay** sensor shows when the heaters are energized. The climate entity's `hvac_action` reports **heating** or **idle** from it, so you can see the thermostat cycling. Without the option, `hvac_action` only knows whether a session is running.
- If AUX is ever on while the sauna isn't heating (for example, after someone presses AUX in the app), Home Assistant raises a **Repairs** issue telling you to re-run the keypad sequence. The issue clears on its own once AUX turns off together with the sauna.

Clearlight's support pages describe pressing AUX three times on saunas with a built-in smoke detector to disable the detector for that session. It's unknown whether quick AUX toggles over the network do the same, so don't automate rapid toggling.

### When the timer runs out

When the session timer reaches 0:00, the heaters stop, but the controller keeps its power flag set. The integration shows this as **Off**, with `hvac_action: idle` and `session_ended: true`. Turning the climate entity on from this state cycles power, so a fresh session starts. Powering off resets the timer to the configured length.

## Time to target and ready by

The integration estimates how long the cabin takes to reach its setpoint, and can have the sauna at a temperature by a given time.

### The estimate

With the heaters on, the cabin warms like a single mass heading for a ceiling some way above room temperature, and the approach slows as it gets closer. The integration starts from values measured on a Sanctuary 2 and adjusts them to your sauna:

- **During a session** it refits the ceiling to the readings so far. **Time to target** carries an `approximate: true` attribute until there are 5 minutes of data. Dashboards can show "~" in front of the number while that's true.
- **After a session that started from a cold cabin** (idle 3 hours or more, within 5 °F of room temperature, and heating for at least 10 minutes), the result is blended into the stored values. Warm starts don't count. Only the heat-up up to the setpoint is used, and if the cabin suddenly cools by 5 °F or more on the way (someone opened the door), only the part before that.
- **Room temperature** is the cabin reading once the sauna has been idle for 3 hours.

When the sauna is off, **Time to target** shows how long a session started now would take to reach the setpoint. It's unknown when the setpoint is above what the cabin levels off at. Press **Reset heat-up calibration** after a hardware change, such as adding the upgrade heater; the estimate then learns again over the next cold starts.

### Ready by

The **Schedule ready by** action (`clearlight_sauna.schedule_ready`) takes three values:

- `ready_by`: the time of day the sauna should be at temperature (the next time that clock time comes round);
- `temperature`: the target, in your Home Assistant unit;
- `minutes_at_temperature`: how long the session should run once it gets there.

The integration then starts the sauna at the right time itself, using the current cabin temperature: the start is ready by minus the heat-up estimate. It sets the target as the setpoint and the session to the heat-up estimate plus the time at temperature, so the time at temperature starts when the target is expected to be reached. **Scheduled start** shows when that will be. When the session ends, the session length the controller had before is put back, so the next keypad session isn't the long one. The integration checks that the controller kept it and tries again for a few minutes if not. If it ended by running out of time, the integration first clears the power flag the controller leaves set at 0:00; the heaters are already off by then. A target that would take more than 90 minutes to reach from room temperature is refused. A ready-by session can therefore run up to 180 minutes, longer than a manual one (see [Safety](#safety)).

Call the action any time before the start, for example from an early-morning automation:

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

A new call replaces the pending plan, and **Cancel ready by** (`clearlight_sauna.cancel_ready`) drops it. Besides the plan, **Scheduled start** has a `running` attribute, true while a ready-by session is heating, and `last_ready_by`, `last_target_temperature_f` and `last_minutes_at_temperature` from the most recent request, for a dashboard form's defaults. If the sauna is already heating at the planned start, the plan is dropped and the running session is left alone. Plans survive a Home Assistant restart.

## Security

The sauna's Wi-Fi module (Gizwits GAgent firmware) has no meaningful local authentication. It gives its login passcode to any client on the LAN that asks, and that is how this integration logs in. Anyone on your network can therefore control the sauna, and the device ID plus that passcode are also what Gizwits' cloud binding API asks for. Consider putting the sauna on an isolated IoT network or VLAN that Home Assistant can still reach.

## How it works

The Wi-Fi board is an ESP8266 running Gizwits GAgent 4.1.2 with a separate controller behind it. The integration:

1. finds saunas with a GAgent discovery broadcast (UDP 12414);
2. connects on TCP 12416, requests the passcode and logs in with it;
3. reads the 12-byte datapoint block and sends a heartbeat every 4 seconds;
4. receives pushed status reports whenever anything changes.

Each write sets only the targeted datapoint's flag. As a second layer, every other field in the write is copied from the sauna's current state, so a write can never touch an unrelated setting.

The controller reports cabin temperature in whole °F, and near a half-degree boundary the reading flips between the two neighbours every few seconds. The integration filters this: a 1 °F step in the direction the cabin is heading (up during a session, down after) shows immediately, while a step back has to hold for 3 minutes first. Heat-up and cool-down track with no lag, and an idle sauna no longer writes a state change every few seconds.

### Datapoints

| Byte | Field | Meaning | Status |
|---|---|---|---|
| 0 bit 0 | `EXTERNAL_LIGHT` | Outside/mood light | ✅ verified |
| 0 bit 1 | `INTERNAL_LIGHT` | Inside light | ✅ verified |
| 0 bit 2 | `PRE_TIME_FLAG` | Reservation active | not exposed |
| 0 bit 3 | `power_flag` | Heat on/off | ✅ verified |
| 0 bit 4 | `cf_flag` | Display unit (°C when set) | ✅ verified (values stay °F) |
| 0 bit 5 | `UVB_FLAG` | AUX outlet (the name is left over from a UVB-lamp accessory) | ✅ verified |
| 0 bit 6 | `N_FLAG` | Unknown | not exposed |
| 1 | `LED` | Probably chromotherapy color | not exposed |
| 2 | `RIGHT` | Right full-spectrum heater intensity, 0–100 % | ✅ verified |
| 3 | `LEFT` | Left full-spectrum heater intensity, 0–100 % | ✅ verified |
| 4 | `SET_TEMP` | Target temperature, °F | ✅ verified |
| 5, 6 | `SET_HOUR`, `SET_MINUTE` | Session length; counts down once a minute while heating | ✅ verified |
| 7, 8 | `PRE_TIME_HOUR`, `PRE_TIME_MINUTE` | Reservation delay | not exposed |
| 9 | `SN` | Unknown | not exposed |
| 10 | `CURRENT_TEMP` | Cabin temperature, °F | ✅ verified |
| 11 | `heart_pulse` | Unknown (reads 0) | not exposed |

The unexposed fields are in the diagnostics download. If you can work out what one of them does (for example by changing a setting in the app and comparing diagnostics), please open an issue.

**Known quirk:** while the sauna is off, the controller sometimes reports the session length one minute short for about a second and then corrects it (10 → 9 → 10). It was seen three times with no power change and no keypad input, at intervals that are multiples of about 40 minutes. It appears to be an internal timer tick that is immediately reset. The only visible effect is a one-second blip in the session-time history.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The tests run against a simulated GAgent device whose frames match those captured from real hardware. They cover protocol framing, the config flow, entity state, service calls, and disconnect/reconnect. CI runs these tests, along with [hassfest](https://github.com/home-assistant/actions) and the [HACS action](https://github.com/hacs/action), on every push and weekly.

## Credits

The Gizwits LAN protocol notes in [Apollon77/node-ph803w](https://github.com/Apollon77/node-ph803w) were a big help; they already list Clearlight's product key.

## Disclaimer

This is an independent project. It is not affiliated with, endorsed by, or supported by Clearlight Infrared, Sauna Works Inc., Jacuzzi Inc. or Gizwits. All trademarks belong to their owners. Use it at your own risk.
