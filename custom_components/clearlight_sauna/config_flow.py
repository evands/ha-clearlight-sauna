"""Config flow: discover the sauna on the LAN or take an IP address."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_DID, CONF_MAC, DOMAIN
from .protocol import (
    CLEARLIGHT_PRODUCT_KEY,
    ClearlightClient,
    DiscoveredDevice,
    GizwitsAuthError,
    GizwitsError,
    async_discover,
)

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


async def _async_probe(host: str) -> tuple[str, DiscoveredDevice | None]:
    """Connect, log in, and confirm this is a Clearlight controller.

    Returns the product key and the unicast discovery reply (for the MAC), if any.
    """
    client = ClearlightClient(host)
    try:
        await client.connect()
        product_key = client.module_info.product_key if client.module_info else ""
    finally:
        await client.disconnect()
    found = await async_discover(timeout=2.0, targets=(host,), product_key=None)
    return product_key, next((d for d in found if d.host == host), None)


class ClearlightConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredDevice] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_HOST] == MANUAL:
                return await self.async_step_manual()
            return await self._async_create(user_input[CONF_HOST])

        try:
            devices = await async_discover(timeout=3.0)
        except OSError:
            devices = []
        configured = {e.data.get(CONF_MAC) for e in self._async_current_entries()}
        self._discovered = {d.host: d for d in devices if format_mac(d.mac) not in configured}
        if not self._discovered:
            return await self.async_step_manual()

        options = {host: f"Clearlight sauna ({host}, {d.mac})" for host, d in self._discovered.items()}
        options[MANUAL] = "Enter IP address manually"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): vol.In(options)}),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return await self._async_create(user_input[CONF_HOST].strip())
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def _async_create(self, host: str) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            product_key, reply = await _async_probe(host)
        except GizwitsAuthError:
            errors["base"] = "invalid_auth"
        except (GizwitsError, OSError):
            errors["base"] = "cannot_connect"
        else:
            if product_key != CLEARLIGHT_PRODUCT_KEY:
                _LOGGER.warning("Device at %s has product key %s", host, product_key)
                errors["base"] = "not_clearlight"
            else:
                mac = format_mac(reply.mac) if reply else None
                await self.async_set_unique_id(mac or host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                data = {CONF_HOST: host, CONF_MAC: mac, CONF_DID: reply.did if reply else None}
                return self.async_create_entry(title="Clearlight Sauna", data=data)

        # Errors (from either step) are shown on the manual form so the user can fix the IP.
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=host): str}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                product_key, _ = await _async_probe(host)
            except (GizwitsError, OSError):
                errors["base"] = "cannot_connect"
            else:
                if product_key != CLEARLIGHT_PRODUCT_KEY:
                    errors["base"] = "not_clearlight"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_HOST: host}
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str}
            ),
            errors=errors,
        )
