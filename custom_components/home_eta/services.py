"""Actions: ignore the members of a group shown at home, or stop ignoring them."""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.selector import ConfigEntrySelector
import probatio

from .const import (
    ATTR_MEMBERS,
    ATTR_REASON,
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    SERVICE_IGNORE_MEMBERS_AT_HOME,
    SERVICE_STOP_IGNORING,
    IgnoreCause,
    IgnoreReason,
)
from .runtime import GroupRuntime

_GROUP_FIELDS: dict[probatio.Marker, Any] = {
    probatio.Optional(ATTR_CONFIG_ENTRY_ID): ConfigEntrySelector(
        {"integration": DOMAIN}
    ),
    probatio.Optional(ATTR_MEMBERS): cv.entities_domain("person"),
}
IGNORE_SCHEMA = probatio.Schema(
    {
        **_GROUP_FIELDS,
        probatio.Optional(ATTR_REASON, default=IgnoreReason.BY_HAND.value): probatio.In(
            [reason.value for reason in IgnoreReason]
        ),
    }
)
STOP_SCHEMA = probatio.Schema(_GROUP_FIELDS)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions (once, for every group)."""

    @callback
    def ignore_members_at_home(call: ServiceCall) -> ServiceResponse:
        runtime, members = _resolve(hass, call)
        added = runtime.ignore(
            members, IgnoreReason(call.data[ATTR_REASON]), IgnoreCause.ACTION
        )
        return _response(runtime, added)

    @callback
    def stop_ignoring(call: ServiceCall) -> ServiceResponse:
        runtime, members = _resolve(hass, call)
        removed = runtime.stop_ignoring(members, IgnoreCause.ACTION)
        return _response(runtime, removed)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IGNORE_MEMBERS_AT_HOME,
        ignore_members_at_home,
        schema=IGNORE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_IGNORING,
        stop_ignoring,
        schema=STOP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _resolve(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[GroupRuntime, list[str] | None]:
    """Return the group asked for and the members named, if any.

    Without a group, the only group loaded; refused when there are several.
    """
    entry_id: str | None = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id is None:
        groups = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP
        ]
        if not groups:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_group"
            )
        if len(groups) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="group_ambiguous",
                translation_placeholders={
                    "groups": ", ".join(entry.title for entry in groups)
                },
            )
        entry = groups[0]
    else:
        # Refuses an unknown entry, another integration's, or one not loaded.
        entry = service.async_get_config_entry(hass, DOMAIN, entry_id)
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_GROUP:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_group",
                translation_placeholders={"entry": entry.title},
            )
    runtime = entry.runtime_data
    assert isinstance(runtime, GroupRuntime)
    members: list[str] | None = call.data.get(ATTR_MEMBERS)
    if members is not None and (
        strangers := [person for person in members if person not in runtime.members]
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_members",
            translation_placeholders={
                "persons": ", ".join(strangers),
                "group": entry.title,
            },
        )
    return runtime, members


def _response(runtime: GroupRuntime, changed: tuple[str, ...]) -> dict[str, Any]:
    """Return who changed, and who is ignored now."""
    view = runtime.view
    return {
        "persons": list(changed),
        "names": [runtime.member_name(person) for person in changed],
        "ignored": list(view.ignored),
        "ignored_names": list(view.ignored_names),
    }
