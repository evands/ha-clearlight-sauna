"""Cabin temperature, session time remaining, and the heat-up estimate."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
            ClearlightTimeToTarget(coordinator, "time_to_target"),
            ClearlightReadyAt(coordinator, "ready_at"),
            ClearlightScheduledStart(coordinator, "scheduled_start"),
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


class ClearlightTimeToTarget(ClearlightEntity, SensorEntity):
    """Minutes until the cabin reaches the setpoint.

    Heating: the live estimate, 0 once there. Off: the heat-up time if a
    session started now. Unknown when the setpoint is out of reach.
    """

    _attr_translation_key = "time_to_target"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:thermometer-chevron-up"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.estimator.output.minutes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        estimator = self.coordinator.estimator
        out, cal = estimator.output, estimator.calibration
        return {
            # True until five minutes of this session's data back the number.
            "approximate": out.approximate,
            "ceiling_f": out.ceiling,
            "room_temperature_f": out.room,
            "delta_t_f": round(cal.delta_t, 1),
            "time_constant_min": round(cal.tau / 60, 1),
            "calibration_sessions": cal.sessions,
        }


class ClearlightReadyAt(ClearlightEntity, SensorEntity):
    """When the cabin is expected to reach the setpoint (or did); unknown when off."""

    _attr_translation_key = "ready_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.estimator.output.ready_at


class ClearlightScheduledStart(ClearlightEntity, SensorEntity):
    """When a pending ready-by plan will start the sauna; unknown with no plan.

    The attributes also say whether a ready-by session is running and what was
    asked for last time.
    """

    _attr_translation_key = "scheduled_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.estimator.output.scheduled_start

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        out = self.coordinator.estimator.output
        last = out.last_request
        return {
            "ready_by": out.plan_ready_by,
            "target_temperature_f": out.plan_target,
            "minutes_at_temperature": out.plan_hold,
            "session_minutes": out.plan_session_minutes,
            # A ready-by session started by a plan is heating now.
            "running": out.ready_running,
            # The last request, for a form's defaults.
            "last_ready_by": last[0] if last else None,
            "last_target_temperature_f": last[1] if last else None,
            "last_minutes_at_temperature": last[2] if last else None,
        }
