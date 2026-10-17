"""Diagnostics for Home ETA."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HomeEtaConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomeEtaConfigEntry
) -> dict[str, Any]:
    """Return estimator state; coordinates are rounded to about 1 km."""
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "runtime": entry.runtime_data.diagnostics(),
    }
