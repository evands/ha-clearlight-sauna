"""Upgrade-heater option: entity swap, relay-based hvac_action, mode-lost repair."""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.clearlight_sauna.const import (
    CONF_MAC,
    CONF_UPGRADE_HEATER,
    DOMAIN,
    HEATER_MODE_LOST_DELAY,
)

from .fake_gagent import FAKE_MAC, FakeGAgent

pytestmark = pytest.mark.usefixtures("socket_enabled")

CLIMATE = "climate.clearlight_sauna"
AUX = "switch.clearlight_sauna_aux_outlet"
RELAY = "binary_sensor.clearlight_sauna_heater_relay"

POWER = 0x08
AUX_BIT = 0x20


async def _setup(hass: HomeAssistant, upgrade_heater: bool) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Clearlight Sauna",
        unique_id=FAKE_MAC,
        data={CONF_HOST: "127.0.0.1", CONF_MAC: FAKE_MAC, "did": "x"},
        options={CONF_UPGRADE_HEATER: upgrade_heater},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _push(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeGAgent, byte0: int) -> None:
    """Make the fake controller report a new flags byte."""
    fake.status[0] = byte0
    await entry.runtime_data.client.async_request_status(wait=True)
    await hass.async_block_till_done()


async def test_option_off_keeps_aux_switch(hass: HomeAssistant, patch_client) -> None:
    await _setup(hass, upgrade_heater=False)
    assert hass.states.get(AUX) is not None
    assert hass.states.get(RELAY) is None


async def test_option_on_swaps_switch_for_relay(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    entry = await _setup(hass, upgrade_heater=False)
    assert hass.states.get(AUX) is not None

    # Turn the option on through the options flow; the entry reloads.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPGRADE_HEATER: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get(AUX) is None  # removed, not just unavailable
    assert hass.states.get(AUX) is None
    assert hass.states.get(RELAY).state == STATE_OFF

    # And back off again restores the switch and drops the sensor.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPGRADE_HEATER: False}
    )
    await hass.async_block_till_done()
    assert hass.states.get(AUX) is not None
    assert registry.async_get(RELAY) is None


async def test_relay_drives_hvac_action(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    entry = await _setup(hass, upgrade_heater=True)

    await _push(hass, entry, fake_device, POWER | AUX_BIT)  # heating, relay on
    assert hass.states.get(CLIMATE).attributes["hvac_action"] == "heating"
    assert hass.states.get(RELAY).state == STATE_ON

    await _push(hass, entry, fake_device, POWER)  # thermostat satisfied
    assert hass.states.get(CLIMATE).state == "heat"
    assert hass.states.get(CLIMATE).attributes["hvac_action"] == "idle"
    assert hass.states.get(RELAY).state == STATE_OFF


async def test_integration_never_writes_aux_in_heater_mode(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    await _setup(hass, upgrade_heater=True)
    fake_device.writes.clear()
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    for p0 in fake_device.writes:
        flags = int.from_bytes(p0[1:3], "big")
        assert not flags >> 5 & 1, "AUX bit must never be flagged in heater mode"


async def test_heater_mode_lost_raises_and_clears_repair(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    entry = await _setup(hass, upgrade_heater=True)
    issues = ir.async_get(hass)
    issue_id = f"heater_mode_lost_{entry.entry_id}"

    # AUX on with the sauna off: what the app's AUX button does.
    await _push(hass, entry, fake_device, AUX_BIT)
    assert issues.async_get_issue(DOMAIN, issue_id) is None  # debounced

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=HEATER_MODE_LOST_DELAY + 1))
    await hass.async_block_till_done()
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.severity == ir.IssueSeverity.ERROR
    assert hass.states.get(RELAY).attributes["heater_mode_lost"] is True

    # While "lost", the climate entity falls back to session-based heating.
    await _push(hass, entry, fake_device, POWER)
    assert hass.states.get(CLIMATE).attributes["hvac_action"] == "heating"

    # AUX off together with the sauna: hazard gone, issue cleared.
    await _push(hass, entry, fake_device, 0)
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get(RELAY).attributes["heater_mode_lost"] is False


async def test_transient_aux_does_not_raise(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    """Heater mode clears AUX in the same update as power-off; a blip must not alarm."""
    entry = await _setup(hass, upgrade_heater=True)
    await _push(hass, entry, fake_device, AUX_BIT)
    await _push(hass, entry, fake_device, 0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=HEATER_MODE_LOST_DELAY + 1))
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"heater_mode_lost_{entry.entry_id}") is None


async def test_unload_clears_issue(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent
) -> None:
    entry = await _setup(hass, upgrade_heater=True)
    await _push(hass, entry, fake_device, AUX_BIT)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=HEATER_MODE_LOST_DELAY + 1))
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"heater_mode_lost_{entry.entry_id}")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"heater_mode_lost_{entry.entry_id}") is None
