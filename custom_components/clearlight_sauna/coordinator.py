"""Push-driven coordinator wrapping the LAN client."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_UPGRADE_HEATER, DOMAIN, HEATER_MODE_LOST_DELAY, WATCHDOG_INTERVAL
from .protocol import DP_POWER, ClearlightClient, GizwitsError, SaunaState

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
        self.heater_mode_lost = False
        self._lost_timer: Callable[[], None] | None = None
        client.set_callbacks(self._handle_state, self._handle_connection)

    @property
    def upgrade_heater(self) -> bool:
        """The owner has activated Clearlight's upgrade heater (AUX = heater relay)."""
        return bool(self.config_entry.options.get(CONF_UPGRADE_HEATER, False))

    @property
    def heater_mode_issue_id(self) -> str:
        return f"heater_mode_lost_{self.config_entry.entry_id}"

    @callback
    def _handle_state(self, state: SaunaState) -> None:
        self.async_set_updated_data(state)
        self.async_check_heater_mode(state)

    @callback
    def async_check_heater_mode(self, state: SaunaState | None = None) -> None:
        """Watch for the controller dropping out of upgrade-heater mode.

        In heater mode the AUX output follows the heater relay, so it is never
        on while the sauna isn't heating. Toggling AUX from the app (or any
        network client) reverts it to a manual outlet with no timer.
        """
        state = state or self.client.state
        if not self.upgrade_heater or state is None:
            self._cancel_lost_timer()
            return
        if state.aux and not state.heating:
            if self._lost_timer is None and not self.heater_mode_lost:
                self._lost_timer = async_call_later(
                    self.hass, HEATER_MODE_LOST_DELAY, self._async_lost_timer_fired
                )
            return
        self._cancel_lost_timer()
        if self.heater_mode_lost and not state.aux and not state.heating:
            # AUX turned off with the sauna: the hazard is gone.
            self._set_heater_mode_lost(False)

    @callback
    def _async_lost_timer_fired(self, _now: datetime) -> None:
        self._lost_timer = None
        state = self.client.state
        if self.upgrade_heater and state is not None and state.aux and not state.heating:
            self._set_heater_mode_lost(True)

    @callback
    def _cancel_lost_timer(self) -> None:
        if self._lost_timer is not None:
            self._lost_timer()
            self._lost_timer = None

    @callback
    def _set_heater_mode_lost(self, lost: bool) -> None:
        self.heater_mode_lost = lost
        if lost:
            _LOGGER.warning(
                "%s: AUX is on while the sauna is not heating; upgrade-heater mode "
                "appears to be off", self.config_entry.title
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.heater_mode_issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="heater_mode_lost",
                translation_placeholders={"name": self.config_entry.title},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self.heater_mode_issue_id)
        self.async_update_listeners()

    @callback
    def async_shutdown_heater_watch(self) -> None:
        self._cancel_lost_timer()
        ir.async_delete_issue(self.hass, DOMAIN, self.heater_mode_issue_id)

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

    async def async_set_power(self, on: bool) -> None:
        """Start or stop a session.

        When a session times out, the controller keeps the power flag set
        with 0 minutes left, so setting it again does nothing. Turning power
        off resets the timer to the configured length (verified), after which
        power on starts a fresh session.
        """
        state = self.client.state
        if on and state is not None and state.session_ended:
            await self.async_write({DP_POWER: 0})
            try:
                await self.client.async_request_status(wait=True)
            except (GizwitsError, TimeoutError) as err:
                raise HomeAssistantError(f"Sauna did not confirm power off: {err}") from err
        if on and self.client.state is not None and self.client.state.heating:
            return
        await self.async_write({DP_POWER: int(on)})

    async def async_write(self, changes: dict[int, int]) -> None:
        """Send a write; surface failures to the caller of the service."""
        try:
            await self.client.async_write(changes)
        except GizwitsError as err:
            raise HomeAssistantError(f"Sauna did not accept the command: {err}") from err
