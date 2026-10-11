"""Inside and outside lights."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClearlightConfigEntry, ClearlightCoordinator
from .entity import ClearlightEntity
from .protocol import DP_EXTERNAL_LIGHT, DP_INTERNAL_LIGHT

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ClearlightLight(coordinator, "inside_light", DP_INTERNAL_LIGHT, "internal_light"),
            ClearlightLight(coordinator, "outside_light", DP_EXTERNAL_LIGHT, "external_light"),
        ]
    )


class ClearlightLight(ClearlightEntity, LightEntity):
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(
        self, coordinator: ClearlightCoordinator, key: str, datapoint: int, field: str
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._datapoint = datapoint
        self._field = field

    @property
    def is_on(self) -> bool:
        return getattr(self.state_data, self._field)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write({self._datapoint: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write({self._datapoint: 0})
