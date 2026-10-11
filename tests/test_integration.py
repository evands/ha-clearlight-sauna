"""End-to-end tests: config flow, setup, entities and service calls against the fake device."""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clearlight_sauna.const import CONF_MAC, DOMAIN

from .fake_gagent import FAKE_MAC, FakeGAgent

# Tests talk to a fake GAgent on 127.0.0.1.
pytestmark = pytest.mark.usefixtures("socket_enabled")

CLIMATE = "climate.clearlight_sauna"
INSIDE = "light.clearlight_sauna_inside_light"
OUTSIDE = "light.clearlight_sauna_outside_light"
SESSION = "number.clearlight_sauna_session_length"
CABIN = "sensor.clearlight_sauna_cabin_temperature"


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


async def test_config_flow_discovery(hass: HomeAssistant, patch_client) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAC] == FAKE_MAC
    assert result["result"].unique_id == FAKE_MAC


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
