"""Cabin temperature and session time remaining."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
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
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ClearlightCabinTemperature(coordinator, "cabin_temperature"),
            ClearlightTimeRemaining(coordinator, "time_remaining"),
        ]
    )


class ClearlightCabinTemperature(ClearlightEntity, SensorEntity):
    _attr_translation_key = "cabin_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT  # always °F on the wire

    @property
    def native_value(self) -> int:
        # De-dithered; the raw reading can flip between neighbours every few seconds.
        temp = self.coordinator.cabin_temp
        return self.state_data.current_temp if temp is None else temp


class ClearlightTimeRemaining(ClearlightEntity, SensorEntity):
    """Minutes left in the running session; 0 when the sauna is off.

    The controller counts the session-time datapoint down while heating.
    """

    _attr_translation_key = "time_remaining"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"

    @property
    def native_value(self) -> int:
        s = self.state_data
        return s.session_minutes if s.power else 0
