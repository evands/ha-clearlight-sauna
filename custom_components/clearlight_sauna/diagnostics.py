"""Diagnostics: raw datapoints help map the still-unverified fields."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import ClearlightConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ClearlightConfigEntry
) -> dict[str, Any]:
    client = entry.runtime_data.client
    state = client.state
    state_dict = asdict(state) if state else None
    if state_dict:
        state_dict["raw"] = state.raw.hex()
    return {
        "host": entry.data.get("host"),
        "connected": client.connected,
        "module_info": asdict(client.module_info) if client.module_info else None,
        "state": state_dict,
    }
