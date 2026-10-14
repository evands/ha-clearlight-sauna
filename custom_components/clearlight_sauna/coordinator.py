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
from homeassistant.util import dt as dt_util

from .const import CONF_UPGRADE_HEATER, DOMAIN, HEATER_MODE_LOST_DELAY, WATCHDOG_INTERVAL
from .estimate import HeatUpEstimator
from .protocol import (
    DP_POWER,
    DP_SET_HOUR,
    DP_SET_MINUTE,
    DP_SET_TEMP,
    ClearlightClient,
    GizwitsError,
    SaunaState,
)
from .temperature import CabinTempFilter

_LOGGER = logging.getLogger(__name__)

type ClearlightConfigEntry = ConfigEntry[ClearlightCoordinator]


def _now() -> float:
    """Seconds on HA's clock (frozen and advanced together with timers in tests)."""
    return dt_util.utcnow().timestamp()


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
        # The controller's whole-degree reading dithers at a boundary; entities
        # show the filtered value (see temperature.py).
        self._temp_filter = CabinTempFilter()
        self._temp_timer: Callable[[], None] | None = None
        self._was_heating: bool | None = None
        self.cabin_temp: int | None = None
        # Time to target, room temperature, calibration and ready-by plans.
        self.estimator = HeatUpEstimator(self)
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
        self.async_set_state(state)

    @callback
    def async_set_state(self, state: SaunaState) -> None:
        """Publish a new device state (push, or the first status at setup)."""
        self._filter_cabin_temp(state)
        self.estimator.async_update(state)
        self.async_set_updated_data(state)
        self.async_check_heater_mode(state)

    @callback
    def _filter_cabin_temp(self, state: SaunaState) -> bool:
        """Run the raw cabin reading through the de-dither filter.

        Returns True when the value entities show has changed.
        """
        if state.heating != self._was_heating:
            # Heating starts: the cabin will rise. Stops: it will fall.
            self._temp_filter.set_trend(1 if state.heating else -1)
            self._was_heating = state.heating
        before = self.cabin_temp
        self.cabin_temp = self._temp_filter.update(state.current_temp, _now())
        self._schedule_temp_recheck()
        return self.cabin_temp != before

    @callback
    def _schedule_temp_recheck(self) -> None:
        """A held reversal is accepted on time even if the device sends nothing new."""
        self._cancel_temp_timer()
        deadline = self._temp_filter.pending_deadline
        if deadline is not None:
            delay = max(0.0, deadline - _now()) + 0.05
            self._temp_timer = async_call_later(self.hass, delay, self._async_temp_recheck)

    @callback
    def _async_temp_recheck(self, _fired: datetime) -> None:
        self._temp_timer = None
        state = self.client.state or self.data
        if state is not None and self._filter_cabin_temp(state):
            self.estimator.async_update(state)
            self.async_update_listeners()

    @callback
    def _cancel_temp_timer(self) -> None:
        if self._temp_timer is not None:
            self._temp_timer()
            self._temp_timer = None

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
        self._cancel_temp_timer()
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
        self._filter_cabin_temp(state)
        self.estimator.async_update(state)
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
            await self._async_power_off_confirmed()
        if on and self.client.state is not None and self.client.state.heating:
            return
        await self.async_write({DP_POWER: int(on)})

    async def _async_power_off_confirmed(self) -> None:
        await self.async_write({DP_POWER: 0})
        try:
            await self.client.async_request_status(wait=True)
        except (GizwitsError, TimeoutError) as err:
            raise HomeAssistantError(f"Sauna did not confirm power off: {err}") from err

    async def async_start_session(self, target: int, minutes: int) -> int:
        """Start a session at `target` °F lasting `minutes`; return the old length.

        The length is set while the sauna is off, as the session-time entity
        does, so it is what the controller counts down from at power-on.
        """
        state = self.client.state
        if state is None:
            raise HomeAssistantError("No status from the sauna")
        if state.heating:
            raise HomeAssistantError("The sauna is already heating")
        if state.session_ended:
            await self._async_power_off_confirmed()  # also resets the timer
        previous = self.client.state.session_minutes
        await self.async_write({DP_SET_TEMP: target, DP_SET_HOUR: 0, DP_SET_MINUTE: minutes})
        await self.async_write({DP_POWER: 1})
        return previous

    async def async_restore_session_length(self, minutes: int) -> None:
        """After a ready-by session, set the length the controller had before.

        Never while heating. At 0:00 the power flag is still set (the heaters
        are already off); clear it first so the timer isn't written mid-session.
        """
        state = self.client.state
        if state is None or state.heating:
            return
        if state.power:
            await self._async_power_off_confirmed()
        state = self.client.state
        if state is not None and not state.power and state.session_minutes != minutes:
            await self.async_write({DP_SET_HOUR: 0, DP_SET_MINUTE: minutes})

    async def async_write(self, changes: dict[int, int]) -> None:
        """Send a write; surface failures to the caller of the service."""
        try:
            await self.client.async_write(changes)
        except GizwitsError as err:
            raise HomeAssistantError(f"Sauna did not accept the command: {err}") from err
