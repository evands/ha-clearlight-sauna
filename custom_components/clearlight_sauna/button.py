"""Reset the heat-up calibration (e.g. after a hardware change)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ClearlightResetCalibration(entry.runtime_data, "reset_calibration")])


class ClearlightResetCalibration(ClearlightEntity, ButtonEntity):
    """Back to the default heat-up model; it re-learns over the next cold starts."""

    _attr_translation_key = "reset_calibration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restore"

    async def async_press(self) -> None:
        self.coordinator.estimator.async_reset_calibration()
