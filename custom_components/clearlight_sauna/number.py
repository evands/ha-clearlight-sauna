"""Session length."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SESSION_MAX_MINUTES, SESSION_MIN_MINUTES, SESSION_STEP_MINUTES
from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity
from .protocol import DP_SET_HOUR, DP_SET_MINUTE

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ClearlightSessionLength(entry.runtime_data, "session_length")])


class ClearlightSessionLength(ClearlightEntity, NumberEntity):
    _attr_translation_key = "session_length"
    _attr_icon = "mdi:timer-outline"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = SESSION_MIN_MINUTES
    _attr_native_max_value = SESSION_MAX_MINUTES
    _attr_native_step = SESSION_STEP_MINUTES
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    @property
    def native_value(self) -> int:
        return self.state_data.session_minutes

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(SESSION_MIN_MINUTES, min(SESSION_MAX_MINUTES, int(value)))
        # The controller accepts both 1h00m and 0h60m; every value seen from the
        # keypad has hour 0, so keep minutes in SET_MINUTE.
        await self.coordinator.async_write({DP_SET_HOUR: 0, DP_SET_MINUTE: minutes})
