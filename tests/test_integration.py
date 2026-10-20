"""End-to-end tests: config flow, setup, entities and service calls against the fake device."""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    CONF_HOST,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.clearlight_sauna.const import CONF_MAC, CONF_UPGRADE_HEATER, DOMAIN
from custom_components.clearlight_sauna.temperature import REVERSAL_HOLD

from .fake_gagent import FAKE_MAC, FakeGAgent

# Tests talk to a fake GAgent on 127.0.0.1.
pytestmark = pytest.mark.usefixtures("socket_enabled")

CLIMATE = "climate.clearlight_sauna"
INSIDE = "light.clearlight_sauna_inside_light"
OUTSIDE = "light.clearlight_sauna_outside_light"
SESSION = "number.clearlight_sauna_session_time"
CABIN = "sensor.clearlight_sauna_cabin_temperature"
CABIN_RAW = "sensor.clearlight_sauna_cabin_temperature_raw"
REMAINING = "sensor.clearlight_sauna_time_remaining"
AUX = "switch.clearlight_sauna_aux_outlet"
LEFT = "number.clearlight_sauna_left_full_spectrum_heater"
RIGHT = "number.clearlight_sauna_right_full_spectrum_heater"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Clearlight Sauna",
        unique_id=FAKE_MAC,
        data={CONF_HOST: "127.0.0.1", CONF_MAC: FAKE_MAC, "did": "x"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("choice", "upgrade_heater", "aux_exists"),
    [("heater_activated", True, False), ("no_heater", False, True)],
)
async def test_config_flow_discovery(
    hass: HomeAssistant, patch_client, choice: str, upgrade_heater: bool, aux_exists: bool
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    # Setup won't finish until the upgrade-heater question is answered.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "upgrade_heater"
    assert result["menu_options"] == ["heater_activated", "no_heater"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": choice}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAC] == FAKE_MAC
    assert result["options"] == {CONF_UPGRADE_HEATER: upgrade_heater}
    assert result["result"].unique_id == FAKE_MAC
    await hass.async_block_till_done()
    assert (hass.states.get(AUX) is not None) is aux_exists


async def test_config_flow_manual_cannot_connect(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "manual"})
    assert result["step_id"] == "manual"
    await fake_device.stop()  # nothing listening any more
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_entities_reflect_device(hass: HomeAssistant, patch_client) -> None:
    await _setup(hass)
    climate = hass.states.get(CLIMATE)
    assert climate.state == HVACMode.OFF
    assert climate.attributes["current_temperature"] == pytest.approx(
        hass.config.units.temperature(76, "°F"), abs=0.6
    )
    assert hass.states.get(INSIDE).state == STATE_OFF
    assert hass.states.get(OUTSIDE).state == STATE_OFF
    assert float(hass.states.get(SESSION).state) == 10
    assert hass.states.get(CABIN).state not in (None, STATE_UNAVAILABLE)


async def test_services_write_only_their_field(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    await _setup(hass)
    before = bytes(fake_device.status)

    await hass.services.async_call("light", "turn_on", {"entity_id": INSIDE}, blocking=True)
    await hass.async_block_till_done()
    assert fake_device.status[0] & 0x02
    assert bytes(fake_device.status[1:]) == before[1:]
    assert not fake_device.status[0] & 0x08  # heaters untouched
    assert hass.states.get(INSIDE).state == STATE_ON

    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "heat"}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake_device.status[0] & 0x08
    assert hass.states.get(CLIMATE).state == HVACMode.HEAT

    await hass.services.async_call(
        "number", "set_value", {"entity_id": SESSION, "value": 45}, blocking=True
    )
    await hass.async_block_till_done()
    assert (fake_device.status[5], fake_device.status[6]) == (0, 45)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": SESSION, "value": 60}, blocking=True
    )
    await hass.async_block_till_done()
    assert (fake_device.status[5], fake_device.status[6]) == (0, 60)

    # Past the keypad's 60 minutes, still all in SET_MINUTE.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": SESSION, "value": 90}, blocking=True
    )
    await hass.async_block_till_done()
    assert (fake_device.status[5], fake_device.status[6]) == (0, 90)
    assert float(hass.states.get(SESSION).state) == 90

    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": CLIMATE}, blocking=True
    )
    await hass.async_block_till_done()
    assert not fake_device.status[0] & 0x08
    assert fake_device.status[0] & 0x02  # light still on


async def test_unavailable_on_disconnect_and_recovers(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    import asyncio

    entry = await _setup(hass)
    await fake_device.drop_clients()
    for _ in range(40):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        if hass.states.get(INSIDE).state == STATE_UNAVAILABLE:
            break
    assert hass.states.get(INSIDE).state == STATE_UNAVAILABLE
    for _ in range(100):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        if hass.states.get(INSIDE).state != STATE_UNAVAILABLE:
            break
    assert hass.states.get(INSIDE).state == STATE_OFF
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_aux_and_heater_intensity(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    await _setup(hass)
    assert hass.states.get(AUX).state == STATE_OFF
    assert float(hass.states.get(LEFT).state) == 100
    assert float(hass.states.get(RIGHT).state) == 100
    assert float(hass.states.get(REMAINING).state) == 0  # off

    await hass.services.async_call("switch", "turn_on", {"entity_id": AUX}, blocking=True)
    await hass.async_block_till_done()
    assert fake_device.status[0] & 0x20
    assert not fake_device.status[0] & 0x08  # AUX never touches heat
    assert hass.states.get(AUX).state == STATE_ON

    await hass.services.async_call(
        "number", "set_value", {"entity_id": LEFT, "value": 55}, blocking=True
    )
    await hass.async_block_till_done()
    assert (fake_device.status[2], fake_device.status[3]) == (100, 55)  # RIGHT, LEFT
    assert float(hass.states.get(LEFT).state) == 55
    assert float(hass.states.get(RIGHT).state) == 100

    await hass.services.async_call(
        "number", "set_value", {"entity_id": RIGHT, "value": 0}, blocking=True
    )
    await hass.async_block_till_done()
    assert (fake_device.status[2], fake_device.status[3]) == (0, 55)

    await hass.services.async_call("switch", "turn_off", {"entity_id": AUX}, blocking=True)
    await hass.async_block_till_done()
    assert not fake_device.status[0] & 0x20


async def test_time_remaining_tracks_countdown(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    await _setup(hass)
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    assert float(hass.states.get(REMAINING).state) == 10

    # The controller decrements the session minutes while heating.
    fake_device.status[6] = 7
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.client.async_request_status(wait=True)
    await hass.async_block_till_done()
    assert float(hass.states.get(REMAINING).state) == 7
    assert float(hass.states.get(SESSION).state) == 7


async def test_turn_on_after_session_timed_out(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    """At 0:00 the power flag stays set; turning on must cycle power to restart."""
    await _setup(hass)
    client = hass.config_entries.async_entries(DOMAIN)[0].runtime_data.client
    fake_device.status[0] |= 0x08  # power flag on
    fake_device.status[6] = 0  # timer ran out
    await client.async_request_status(wait=True)
    await hass.async_block_till_done()
    climate = hass.states.get(CLIMATE)
    assert climate.state == HVACMode.OFF
    assert climate.attributes["hvac_action"] == "idle"
    assert climate.attributes["session_ended"] is True
    assert float(hass.states.get(REMAINING).state) == 0

    fake_device.writes.clear()
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    power_writes = [w[3] & 0x08 for w in fake_device.writes if int.from_bytes(w[1:3], "big") >> 3 & 1]
    assert power_writes == [0, 0x08]  # off, then on
    assert fake_device.status[0] & 0x08 and fake_device.status[6] == 10
    assert hass.states.get(CLIMATE).state == HVACMode.HEAT

    # Turning on while already heating sends nothing.
    fake_device.writes.clear()
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert fake_device.writes == []


async def test_cabin_temperature_dither_is_filtered(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent, freezer
) -> None:
    """A whole-degree reading flickering at a boundary must not reach the entities."""
    hass.config.units = US_CUSTOMARY_SYSTEM  # compare in the device's own °F
    await _setup(hass)
    client = hass.config_entries.async_entries(DOMAIN)[0].runtime_data.client
    cabin_changes: list[str] = []

    @callback
    def _listen(event: Event) -> None:
        if event.data["entity_id"] == CABIN:
            cabin_changes.append(event.data["new_state"].state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _listen)

    async def report(raw: int) -> None:
        fake_device.status[10] = raw
        # A status reply already in flight (e.g. a write ack) can answer the
        # request first; ask until the client has seen this reading.
        for _ in range(3):
            await client.async_request_status(wait=True)
            if client.state.current_temp == raw:
                break
        await hass.async_block_till_done()
        assert client.state.current_temp == raw

    assert hass.states.get(CABIN).state == "76"
    for raw in (75, 76, 75, 76, 75, 76, 75, 76):
        await report(raw)
    assert cabin_changes == ["75"]  # one step in the cooling direction, then silence
    assert hass.states.get(CLIMATE).attributes["current_temperature"] == 75

    # A reversal that persists is shown after the hold, even with no new report.
    freezer.tick(REVERSAL_HOLD + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(CABIN).state == "76"

    # While heating, each new degree appears at once and boundary flicker does not.
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    cabin_changes.clear()
    for raw in (77, 76, 77, 78, 77, 78, 79):
        await report(raw)
    assert cabin_changes == ["77", "78", "79"]


async def test_raw_cabin_temperature_records_every_reading(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    """Disabled by default; once enabled it shows what the filter holds back."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = await _setup(hass)
    registry = er.async_get(hass)
    reg = registry.async_get(CABIN_RAW)
    assert reg is not None and reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(CABIN_RAW) is None

    registry.async_update_entity(CABIN_RAW, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CABIN_RAW).state == "76"
    assert hass.states.get(CABIN_RAW).attributes.get("state_class") is None  # no statistics

    client = entry.runtime_data.client
    raw_changes: list[str] = []

    @callback
    def _listen(event: Event) -> None:
        if event.data["entity_id"] == CABIN_RAW:
            raw_changes.append(event.data["new_state"].state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _listen)
    for raw in (75, 76, 75):
        fake_device.status[10] = raw
        for _ in range(3):
            await client.async_request_status(wait=True)
            if client.state.current_temp == raw:
                break
        await hass.async_block_till_done()
    assert raw_changes == ["75", "76", "75"]  # every reading, dither included
    assert hass.states.get(CABIN).state == "75"  # the shown value took one step
