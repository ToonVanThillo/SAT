"""Diagnostics download for the SAT config entry."""
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .channels import diagnostic_status
from .const import CLIMATE, CONF_DEVICE, COORDINATOR, DOMAIN
from .coordinator import SatDataUpdateCoordinator

if TYPE_CHECKING:
    from .climate import SatClimate

# The unique_id and discovery keys hold the device address, hostname or MAC address.
TO_REDACT = {CONF_DEVICE, "unique_id", "discovery_keys"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the config entry with its identifying addresses redacted, the tracked state per channel, the raw device values and, once the
    climate exists, its control values."""
    coordinator: SatDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    climate: SatClimate | None = hass.data[DOMAIN][entry.entry_id].get(CLIMATE)
    now = dt_util.utcnow()

    diagnostics: dict[str, Any] = {
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

    if climate is not None:
        diagnostics["control"] = _control(climate, coordinator)

    return diagnostics


def _control(climate: SatClimate, coordinator: SatDataUpdateCoordinator) -> dict[str, Any]:
    """Return the climate's control values that have no entity."""
    return {
        "pid_error": climate.pid.last_error,
        "error_source": climate.max_error.entity_id,
        "control_setpoint": climate.setpoint,
        "relative_modulation_state": climate.relative_modulation_state.name,
        "relative_modulation_value": climate.get_relative_modulation_value(coordinator.boiler),
        "valves_open": climate.valves_open,
        "boiler_temperature_cold": coordinator.boiler_temperature_cold,
        "boiler_temperature_tracking": coordinator.boiler_temperature_tracking,
        "boiler_temperature_derivative": coordinator.boiler_temperature_derivative,
        "coefficient_derivative": climate.heating_curve.coefficient_derivative,
    }
