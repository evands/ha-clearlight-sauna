"""Session time and full-spectrum heater intensity."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SESSION_MAX_MINUTES, SESSION_MIN_MINUTES, SESSION_STEP_MINUTES
from .coordinator import ClearlightConfigEntry, ClearlightCoordinator
from .entity import ClearlightEntity
from .protocol import DP_LEFT, DP_RIGHT, DP_SET_HOUR, DP_SET_MINUTE

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ClearlightSessionLength(coordinator, "session_length"),
            ClearlightHeaterIntensity(coordinator, "left_heater", DP_LEFT, "left"),
            ClearlightHeaterIntensity(coordinator, "right_heater", DP_RIGHT, "right"),
        ]
    )


class ClearlightSessionLength(ClearlightEntity, NumberEntity):
    """Session length when off; time remaining (counting down) while heating."""

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


class ClearlightHeaterIntensity(ClearlightEntity, NumberEntity):
    """Intensity of one full-spectrum heater, 0 (off) to 100 %."""

    _attr_icon = "mdi:heat-wave"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self, coordinator: ClearlightCoordinator, key: str, datapoint: int, field: str
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._datapoint = datapoint
        self._field = field

    @property
    def native_value(self) -> int:
        return getattr(self.state_data, self._field)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write({self._datapoint: max(0, min(100, int(value)))})
