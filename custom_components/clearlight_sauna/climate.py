"""Sauna heater as a climate entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MAX_TEMP_F, MIN_TEMP_F
from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity
from .protocol import DP_SET_TEMP

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClearlightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ClearlightClimate(entry.runtime_data, "sauna")])


class ClearlightClimate(ClearlightEntity, ClimateEntity):
    _attr_name = None
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1
    # The controller reports °F regardless of its display flag (verified on
    # hardware); HA converts for users on metric.
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_min_temp = MIN_TEMP_F
    _attr_max_temp = MAX_TEMP_F

    @property
    def current_temperature(self) -> float:
        return self.state_data.current_temp

    @property
    def target_temperature(self) -> float:
        return self.state_data.set_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.HEAT if self.state_data.heating else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        s = self.state_data
        if s.heating:
            coordinator = self.coordinator
            if coordinator.upgrade_heater and not coordinator.heater_mode_lost:
                # In heater mode AUX mirrors the heater relay (thermostat).
                return HVACAction.HEATING if s.aux else HVACAction.IDLE
            return HVACAction.HEATING
        return HVACAction.IDLE if s.session_ended else HVACAction.OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.state_data
        return {"session_minutes": s.session_minutes, "session_ended": s.session_ended}

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_set_power(hvac_mode == HVACMode.HEAT)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        changes: dict[int, int] = {}
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            temp = max(self.min_temp, min(self.max_temp, round(temp)))
            changes[DP_SET_TEMP] = int(temp)
        if changes:
            await self.coordinator.async_write(changes)
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(mode)
