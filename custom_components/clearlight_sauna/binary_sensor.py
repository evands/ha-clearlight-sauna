"""Upgrade-heater relay (read-only; only when the upgrade heater is activated)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity, async_remove_stale_entity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.runtime_data.upgrade_heater:
        async_remove_stale_entity(hass, entry, "binary_sensor", "heater_relay")
        return
    async_add_entities([ClearlightHeaterRelay(entry.runtime_data, "heater_relay")])


class ClearlightHeaterRelay(ClearlightEntity, BinarySensorEntity):
    """On while the heaters (and the upgrade-heater receptacle) are energized.

    In upgrade-heater mode the controller drives the AUX output from the
    heater relay: on with power, off at power-off, at 0:00, and while the
    thermostat is satisfied. Never written by this integration.
    """

    _attr_translation_key = "heater_relay"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    @property
    def is_on(self) -> bool:
        return self.state_data.aux

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"heater_mode_lost": self.coordinator.heater_mode_lost}
