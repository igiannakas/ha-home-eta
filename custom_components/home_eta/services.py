"""Services for Home ETA."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
import probatio

from .const import ATTR_PERSON, DOMAIN, SERVICE_EXPORT_HISTORY, SERVICE_RESET_HISTORY
from .runtime import PersonRuntime

SCHEMA = probatio.Schema({probatio.Optional(ATTR_PERSON): cv.entity_ids_or_uuids})


def _people(hass: HomeAssistant, call: ServiceCall) -> list[PersonRuntime]:
    """Return the loaded person runtimes the call targets."""
    registry = er.async_get(hass)
    # Entity registry ids (as the UI may send) resolve to entity ids.
    wanted = {
        er.async_resolve_entity_id(registry, person) or person
        for person in call.data.get(ATTR_PERSON) or []
    }
    runtimes = [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and isinstance(entry.runtime_data, PersonRuntime)
    ]
    if wanted:
        runtimes = [r for r in runtimes if r.person_id in wanted]
    if not runtimes:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_matching_person",
            translation_placeholders={"persons": ", ".join(sorted(wanted)) or "any"},
        )
    return runtimes


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services."""

    async def reset_history(call: ServiceCall) -> None:
        runtimes = [r for r in _people(hass, call) if r.historical_enabled]
        if not runtimes:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="history_disabled"
            )
        for runtime in runtimes:
            await runtime.async_reset_history()

    async def export_history(call: ServiceCall) -> ServiceResponse:
        people: list[dict[str, Any]] = [r.export_history() for r in _people(hass, call)]
        return cast(ServiceResponse, {"people": people})

    hass.services.async_register(
        DOMAIN, SERVICE_RESET_HISTORY, reset_history, schema=SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_HISTORY,
        export_history,
        schema=SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
