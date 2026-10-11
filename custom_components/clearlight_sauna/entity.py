"""Base entity for Clearlight Sauna."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC, DOMAIN
from .coordinator import ClearlightCoordinator
from .protocol import SaunaState


def entity_unique_id(entry: ConfigEntry, key: str) -> str:
    device_id = entry.data.get(CONF_MAC) or entry.unique_id or entry.entry_id
    return f"{device_id}_{key}"


@callback
def async_remove_stale_entity(
    hass: HomeAssistant, entry: ConfigEntry, platform: str, key: str
) -> None:
    """Drop an entity this configuration no longer provides (e.g. after an option change)."""
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(platform, DOMAIN, entity_unique_id(entry, key)):
        registry.async_remove(entity_id)


class ClearlightEntity(CoordinatorEntity[ClearlightCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ClearlightCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        device_id = entry.data.get(CONF_MAC) or entry.unique_id or entry.entry_id
        self._attr_unique_id = entity_unique_id(entry, key)
        info = coordinator.client.module_info
        connections = set()
        if mac := entry.data.get(CONF_MAC):
            connections.add((CONNECTION_NETWORK_MAC, mac))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            connections=connections,
            manufacturer="Clearlight",
            model="Sanctuary (Gizwits Wi-Fi)",
            name=entry.title,
            sw_version=info.wifi_sw if info else None,
            hw_version=info.wifi_hw if info else None,
        )

    @property
    def state_data(self) -> SaunaState:
        return self.coordinator.data
