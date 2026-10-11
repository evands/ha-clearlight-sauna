# Clearlight Sauna for Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=evands&repository=ha-clearlight-sauna&category=integration)

This integration gives Home Assistant local control of Clearlight infrared saunas with Wi-Fi, the kind that use the 2.4 GHz **Clearlight Sauna Connect** app. Home Assistant talks to the sauna's Wi-Fi module directly on your network, with no cloud account and no internet involved.

> **Status: early (0.x).** It has been tested on one sauna so far. Reports from other models are very welcome; see [Compatibility](#compatibility).

## Features

| Entity | What it does |
|---|---|
| `climate.clearlight_sauna` | Heat on/off, target temperature (70–175 °F), cabin temperature |
| `light.clearlight_sauna_inside_light` | Cabin light |
| `light.clearlight_sauna_outside_light` | Exterior and mood lights |
| `number.clearlight_sauna_session_length` | Session length, 5–60 min |
| `sensor.clearlight_sauna_cabin_temperature` | Cabin temperature, for history and automations |

- **Push updates.** The sauna pushes every state change, including changes made at the keypad or in the app, so Home Assistant doesn't need to poll.
- **Survives restarts and IP changes.** If the sauna is power-cycled, the integration reconnects. If DHCP gives the sauna a new address, the integration finds it again by MAC.
- **Fahrenheit internally.** The controller always reports °F, even when its display is set to °C. Home Assistant converts to your unit system.

## Installation

### HACS (recommended)

1. Click the button above, or in HACS go to **⋮ → Custom repositories**, add `https://github.com/evands/ha-clearlight-sauna`, and choose type **Integration**.
2. Download **Clearlight Sauna** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Clearlight Sauna**. Saunas on your network are discovered automatically; if yours isn't found, enter its IP address.

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

This integration can turn infrared heaters on when nobody is in the room, exactly as the official app's remote start can. Clearlight's warnings about towels or other items on or against the heaters apply to every remote start. The integration caps sessions at 60 minutes, the same limit as the keypad, and the sauna turns off when the session timer runs out.

Use automations that start the sauna with care, and consider a notification whenever heating starts.

## Security

The sauna's Wi-Fi module (Gizwits GAgent firmware) has no meaningful local authentication. It gives its login passcode to any client on the LAN that asks, and that is how this integration logs in. Anyone on your network can therefore control the sauna, and the device ID plus that passcode are also what Gizwits' cloud binding API asks for. Consider putting the sauna on an isolated IoT network or VLAN that Home Assistant can still reach.

## How it works

The Wi-Fi board is an ESP8266 running Gizwits GAgent 4.1.2 with a separate controller behind it. The integration:

1. finds saunas with a GAgent discovery broadcast (UDP 12414);
2. connects on TCP 12416, requests the passcode and logs in with it;
3. reads the 12-byte datapoint block and sends a heartbeat every 4 seconds;
4. receives pushed status reports whenever anything changes.

Each write sets only the targeted datapoint's flag. As a second layer, every other field in the write is copied from the sauna's current state, so a write can never touch an unrelated setting.

### Datapoints

| Byte | Field | Meaning | Status |
|---|---|---|---|
| 0 bit 0 | `EXTERNAL_LIGHT` | Outside/mood light | ✅ verified |
| 0 bit 1 | `INTERNAL_LIGHT` | Inside light | ✅ verified |
| 0 bit 2 | `PRE_TIME_FLAG` | Reservation active | not exposed |
| 0 bit 3 | `power_flag` | Heat on/off | ✅ verified |
| 0 bit 4 | `cf_flag` | Display unit (°C when set) | ✅ verified (values stay °F) |
| 0 bit 5–6 | `UVB_FLAG`, `N_FLAG` | Unknown | not exposed |
| 1 | `LED` | Probably chromotherapy color | not exposed |
| 2, 3 | `RIGHT`, `LEFT` | Probably full-spectrum heater intensity (both read 100) | not exposed |
| 4 | `SET_TEMP` | Target temperature, °F | ✅ verified |
| 5, 6 | `SET_HOUR`, `SET_MINUTE` | Session length | ✅ verified |
| 7, 8 | `PRE_TIME_HOUR`, `PRE_TIME_MINUTE` | Reservation delay | not exposed |
| 9 | `SN` | Unknown | not exposed |
| 10 | `CURRENT_TEMP` | Cabin temperature, °F | ✅ verified |
| 11 | `heart_pulse` | Unknown (reads 0) | not exposed |

The unexposed fields are in the diagnostics download. If you can work out what one of them does (for example by changing a setting in the app and comparing diagnostics), please open an issue.

**Known quirk:** the controller occasionally reports the session length one minute short for about a second and then corrects it. This happens with no client connected.

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
