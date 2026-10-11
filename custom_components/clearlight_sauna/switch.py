"""AUX outlet (only when the upgrade heater is not activated)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity, async_remove_stale_entity
from .protocol import DP_AUX

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.upgrade_heater:
        # Writing AUX takes the controller out of upgrade-heater mode, so the
        # integration must never offer it once the heater is activated.
        async_remove_stale_entity(hass, entry, "switch", "aux")
        return
    async_add_entities([ClearlightAux(entry.runtime_data, "aux")])


class ClearlightAux(ClearlightEntity, SwitchEntity):
    """The keypad's AUX button: switches the accessory outlet.

    Without upgrade-heater activation this outlet has no timer: it works
    whether or not the sauna is heating and survives power-off.
    """

    _attr_translation_key = "aux"
    _attr_device_class = SwitchDeviceClass.OUTLET

    @property
    def is_on(self) -> bool:
        return self.state_data.aux

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write({DP_AUX: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write({DP_AUX: 0})
