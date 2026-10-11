"""Local control for Clearlight saunas (Gizwits GAgent Wi-Fi module)."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_MAC
from .coordinator import ClearlightConfigEntry, ClearlightCoordinator
from .protocol import ClearlightClient, GizwitsError, async_discover

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.LIGHT, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


async def _async_find_moved_device(hass: HomeAssistant, entry: ClearlightConfigEntry) -> str | None:
    """If the sauna got a new DHCP address, find it again by MAC."""
    mac = entry.data.get(CONF_MAC)
    if not mac:
        return None
    try:
        devices = await async_discover(timeout=3.0)
    except OSError:
        return None
    for dev in devices:
        if dev.mac == mac and dev.host != entry.data[CONF_HOST]:
            _LOGGER.info("Clearlight sauna %s moved to %s", mac, dev.host)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_HOST: dev.host}
            )
            return dev.host
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ClearlightConfigEntry) -> bool:
    client = ClearlightClient(entry.data[CONF_HOST])
    try:
        await client.connect()
    except GizwitsError as err:
        new_host = await _async_find_moved_device(hass, entry)
        if new_host is None:
            raise ConfigEntryNotReady(f"Cannot connect to sauna: {err}") from err
        client = ClearlightClient(new_host)
        try:
            await client.connect()
        except GizwitsError as err2:
            raise ConfigEntryNotReady(f"Cannot connect to sauna: {err2}") from err2

    coordinator = ClearlightCoordinator(hass, entry, client)
    if client.state is None:
        try:
            await client.async_request_status(wait=True)
        except (GizwitsError, TimeoutError) as err:
            await client.disconnect()
            raise ConfigEntryNotReady(f"Sauna did not report status: {err}") from err
    coordinator.async_set_updated_data(client.state)
    client.start()  # keeps the connection alive and reconnects with backoff
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClearlightConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.disconnect()
    return unloaded
