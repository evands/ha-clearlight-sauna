"""Push-driven coordinator wrapping the LAN client."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, WATCHDOG_INTERVAL
from .protocol import ClearlightClient, GizwitsError, SaunaState

_LOGGER = logging.getLogger(__name__)

type ClearlightConfigEntry = ConfigEntry[ClearlightCoordinator]


class ClearlightCoordinator(DataUpdateCoordinator[SaunaState]):
    """Holds the latest sauna state; updated by device pushes."""

    config_entry: ClearlightConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ClearlightConfigEntry, client: ClearlightClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=WATCHDOG_INTERVAL,
            always_update=False,
        )
        self.client = client
        client.set_callbacks(self._handle_state, self._handle_connection)

    @callback
    def _handle_state(self, state: SaunaState) -> None:
        self.async_set_updated_data(state)

    @callback
    def _handle_connection(self, connected: bool) -> None:
        if connected:
            if self.client.state is not None:
                self.async_set_updated_data(self.client.state)
        else:
            self.async_set_update_error(UpdateFailed("connection to sauna lost"))

    async def _async_update_data(self) -> SaunaState:
        if not self.client.connected:
            raise UpdateFailed("not connected to sauna")
        try:
            state = await self.client.async_request_status(wait=True)
        except (GizwitsError, TimeoutError) as err:
            raise UpdateFailed(f"status request failed: {err}") from err
        if state is None:
            raise UpdateFailed("no status from sauna")
        return state

    async def async_write(self, changes: dict[int, int]) -> None:
        """Send a write; surface failures to the caller of the service."""
        try:
            await self.client.async_write(changes)
        except GizwitsError as err:
            raise HomeAssistantError(f"Sauna did not accept the command: {err}") from err
