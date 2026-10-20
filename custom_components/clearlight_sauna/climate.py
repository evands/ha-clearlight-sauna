"""Sauna heater as a climate entity."""

from __future__ import annotations

from datetime import time
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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import DOMAIN, MAX_TEMP_F, MIN_TEMP_F
from .coordinator import ClearlightConfigEntry
from .entity import ClearlightEntity
from .heatup import DELTA_T_RANGE
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
        temp = self.coordinator.cabin_temp  # de-dithered, as the sensor shows
        return self.state_data.current_temp if temp is None else temp

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

    async def async_schedule_ready(
        self, ready_by: time, temperature: float, minutes_at_temperature: int
    ) -> None:
        """The clearlight_sauna.schedule_ready action; temperature is in HA's unit."""
        unit = self.hass.config.units.temperature_unit
        target = round(
            TemperatureConverter.convert(temperature, unit, UnitOfTemperature.FAHRENHEIT)
        )
        if not MIN_TEMP_F <= target <= MAX_TEMP_F:
            low, high = (
                round(TemperatureConverter.convert(t, UnitOfTemperature.FAHRENHEIT, unit))
                for t in (MIN_TEMP_F, MAX_TEMP_F)
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="temperature_out_of_range",
                translation_placeholders={"min": f"{low}{unit}", "max": f"{high}{unit}"},
            )
        self.coordinator.estimator.async_schedule(ready_by, target, minutes_at_temperature)

    async def async_cancel_ready(self) -> None:
        """The clearlight_sauna.cancel_ready action."""
        self.coordinator.estimator.async_cancel()

    async def async_set_heatup_calibration(
        self, rise: float | None = None, time_constant: float | None = None
    ) -> None:
        """The clearlight_sauna.set_heatup_calibration action.

        `rise` is a temperature difference in HA's unit; `time_constant` is in
        minutes. Either may be left out to keep its stored value.
        """
        if rise is None and time_constant is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="calibration_nothing_to_set"
            )
        delta_t = None
        if rise is not None:
            unit = self.hass.config.units.temperature_unit
            delta_t = TemperatureConverter.convert_interval(
                rise, unit, UnitOfTemperature.FAHRENHEIT
            )
            if not DELTA_T_RANGE[0] <= delta_t <= DELTA_T_RANGE[1]:
                low, high = (
                    round(
                        TemperatureConverter.convert_interval(
                            t, UnitOfTemperature.FAHRENHEIT, unit
                        ),
                        1,
                    )
                    for t in DELTA_T_RANGE
                )
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="rise_out_of_range",
                    translation_placeholders={"min": f"{low}{unit}", "max": f"{high}{unit}"},
                )
        tau = None if time_constant is None else time_constant * 60
        self.coordinator.estimator.async_set_calibration(delta_t, tau)
