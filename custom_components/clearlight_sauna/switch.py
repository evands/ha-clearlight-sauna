"""AUX outlet."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity
from .protocol import DP_AUX

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ClearlightAux(entry.runtime_data, "aux")])


class ClearlightAux(ClearlightEntity, SwitchEntity):
    """The keypad's AUX button: switches the accessory outlet in the roof.

    It works whether or not the sauna is heating, so treat whatever is
    plugged into it as independently powered.
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
