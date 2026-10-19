"""Diagnostics download for the SAT config entry."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .channels import diagnostic_status
from .const import CONF_DEVICE, COORDINATOR, DOMAIN
from .coordinator import SatDataUpdateCoordinator

# The unique_id and discovery keys hold the device address, hostname or MAC address.
TO_REDACT = {CONF_DEVICE, "unique_id", "discovery_keys"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the config entry with its identifying addresses redacted, the tracked state per channel and the raw device values."""
    coordinator: SatDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    now = dt_util.utcnow()

    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "channels": {
            quantity.value: {**asdict(state), "diagnostic_status": diagnostic_status(state, now)}
            for quantity, state in coordinator.channel_states.items()
        },
        "readings": {
            key: {"value": reading.value, "received_at": reading.received_at, "restored": reading.restored}
            for key, reading in coordinator.data.readings().items()
        },
    }
