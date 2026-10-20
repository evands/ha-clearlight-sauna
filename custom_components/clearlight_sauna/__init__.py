"""Local control for Clearlight saunas (Gizwits GAgent Wi-Fi module)."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_TEMPERATURE, CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_MINUTES_AT_TEMPERATURE,
    ATTR_READY_BY,
    ATTR_RISE,
    ATTR_TIME_CONSTANT,
    CONF_MAC,
    DOMAIN,
    READY_HOLD_MAX_MINUTES,
    READY_HOLD_MIN_MINUTES,
    SERVICE_CANCEL_READY,
    SERVICE_SCHEDULE_READY,
    SERVICE_SET_HEATUP_CALIBRATION,
)
from .heatup import TAU_RANGE
from .coordinator import ClearlightConfigEntry, ClearlightCoordinator
from .estimate import STORAGE_VERSION, storage_key
from .protocol import ClearlightClient, GizwitsError, async_discover

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions; they target the sauna's climate entity."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SCHEDULE_READY,
        entity_domain=CLIMATE_DOMAIN,
        schema={
            vol.Required(ATTR_READY_BY): cv.time,
            vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
            vol.Required(ATTR_MINUTES_AT_TEMPERATURE): vol.All(
                vol.Coerce(int),
                vol.Range(min=READY_HOLD_MIN_MINUTES, max=READY_HOLD_MAX_MINUTES),
            ),
        },
        func="async_schedule_ready",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CANCEL_READY,
        entity_domain=CLIMATE_DOMAIN,
        schema=None,
        func="async_cancel_ready",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_HEATUP_CALIBRATION,
        entity_domain=CLIMATE_DOMAIN,
        schema={
            vol.Optional(ATTR_RISE): vol.Coerce(float),
            vol.Optional(ATTR_TIME_CONSTANT): vol.All(
                vol.Coerce(float),
                vol.Range(min=TAU_RANGE[0] / 60, max=TAU_RANGE[1] / 60),
            ),
        },
        func="async_set_heatup_calibration",
    )
    return True


async def _async_find_moved_device(hass: HomeAssistant, entry: ClearlightConfigEntry) -> str | None:
    """If the sauna got a new DHCP address, find it again by MAC."""
    mac = entry.data.get(CONF_MAC)
    if not mac:
        return None
    try:
        devices = await async_discover(timeout=3.0)
    except OSError:
        return None
    for dev in devices:
        if dev.mac == mac and dev.host != entry.data[CONF_HOST]:
            _LOGGER.info("Clearlight sauna %s moved to %s", mac, dev.host)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_HOST: dev.host}
            )
            return dev.host
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ClearlightConfigEntry) -> bool:
    client = ClearlightClient(entry.data[CONF_HOST])
    try:
        await client.connect()
    except GizwitsError as err:
        new_host = await _async_find_moved_device(hass, entry)
        if new_host is None:
            raise ConfigEntryNotReady(f"Cannot connect to sauna: {err}") from err
        client = ClearlightClient(new_host)
        try:
            await client.connect()
        except GizwitsError as err2:
            raise ConfigEntryNotReady(f"Cannot connect to sauna: {err2}") from err2

    coordinator = ClearlightCoordinator(hass, entry, client)
    if client.state is None:
        try:
            await client.async_request_status(wait=True)
        except (GizwitsError, TimeoutError) as err:
            await client.disconnect()
            raise ConfigEntryNotReady(f"Sauna did not report status: {err}") from err
    await coordinator.estimator.async_load()
    coordinator.async_set_state(client.state)
    client.start()  # keeps the connection alive and reconnects with backoff
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_shutdown_heater_watch)
    entry.async_on_unload(coordinator.estimator.async_unload)
    coordinator.async_check_heater_mode()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClearlightConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.disconnect()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ClearlightConfigEntry) -> None:
    """Forget this sauna's heat-up calibration and plans."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
