"""Home ETA: say who is heading home, when they will be there and how near they are.

Each person is its own config entry with its own entities. A separate group
entry combines people heading to the same zone: the first expected, whether
anyone is arriving soon, and how near the nearest is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData
from homeassistant.helpers.event import async_track_entity_registry_updated_event
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .const import (
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    CONF_PERSON,
    CONF_ZONE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
)
from .migration import async_migrate_entry, async_remove_retired, legacy_store
from .runtime import DATA_KEY, GroupRuntime, HomeEtaData, PersonRuntime

__all__ = ["async_migrate_entry"]

type HomeEtaConfigEntry = ConfigEntry[PersonRuntime | GroupRuntime]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PERSON_PLATFORMS = [Platform.NUMBER, Platform.SENSOR]
GROUP_PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

ISSUES = ("person_missing", "zone_missing", "group_no_members")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the registry of loaded people."""
    hass.data[DATA_KEY] = HomeEtaData()
    return True


def _platforms(entry: ConfigEntry) -> list[Platform]:
    if entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_PERSON:
        return PERSON_PLATFORMS
    return GROUP_PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: HomeEtaConfigEntry) -> bool:
    """Set up a person or a group."""
    await async_remove_retired(hass, entry)
    runtime: PersonRuntime | GroupRuntime
    if entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_PERSON:
        runtime = PersonRuntime(hass, entry)
    else:
        runtime = GroupRuntime(hass, entry)
    entry.runtime_data = runtime
    # Registered first, so a start that fails part-way still cleans up.
    entry.async_on_unload(runtime.async_stop)
    await runtime.async_start()
    entry.async_on_unload(
        async_track_entity_registry_updated_event(
            hass, sorted(_references(entry)), _rename_follower(hass, entry)
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HomeEtaConfigEntry) -> bool:
    """Unload a person or a group."""
    return await hass.config_entries.async_unload_platforms(entry, _platforms(entry))


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete open issues and take a removed person out of groups."""
    for issue in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
        return
    person, zone = entry.data[CONF_PERSON], entry.data[CONF_ZONE]
    for group in hass.config_entries.async_entries(DOMAIN):
        members = group.options.get(CONF_MEMBERS) or []
        if (
            group.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_GROUP
            or group.data.get(CONF_ZONE) != zone
            or person not in members
        ):
            continue
        hass.config_entries.async_update_entry(
            group,
            options={
                **group.options,
                CONF_MEMBERS: [m for m in members if m != person],
            },
        )
        if group.state is ConfigEntryState.LOADED:
            hass.config_entries.async_schedule_reload(group.entry_id)
    # An entry from before 2.0 that was never set up again (disabled, say)
    # still has its learned trips: they are location history, so they go.
    await legacy_store(hass, entry.entry_id).async_remove()


def _references(entry: ConfigEntry) -> set[str]:
    """Return every entity id an entry refers to."""
    if entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_GROUP:
        return {entry.data[CONF_ZONE], *(entry.options.get(CONF_MEMBERS) or [])}
    return {entry.data[CONF_PERSON], entry.data[CONF_ZONE]}


def _rename_follower(
    hass: HomeAssistant, entry: ConfigEntry
) -> Callable[[Event[EventEntityRegistryUpdatedData]], None]:
    """Return a listener that follows renamed entities into the entry."""

    @callback
    def renamed(event: Event[EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update":
            return
        old, new = data.get("old_entity_id"), data["entity_id"]
        if old is None or old == new:
            return
        _async_rename_reference(hass, entry, old, new)

    return renamed


@callback
def _async_rename_reference(
    hass: HomeAssistant, entry: ConfigEntry, old: str, new: str
) -> None:
    """Replace ``old`` by ``new`` in an entry and reload it."""

    def swap(value: Any) -> Any:
        if value == old:
            return new
        if isinstance(value, list):
            return [new if item == old else item for item in value]
        return value

    data: Mapping[str, Any] = {key: swap(value) for key, value in entry.data.items()}
    options = {key: swap(value) for key, value in entry.options.items()}
    if data[CONF_ENTRY_TYPE] == ENTRY_TYPE_GROUP:
        # group|<zone>|<name at creation>: only the zone can be renamed.
        _, _, name = (entry.unique_id or "").partition("|")[2].partition("|")
        unique_id = f"group|{data[CONF_ZONE]}|{name or slugify(entry.title)}"
    else:
        unique_id = f"{data[CONF_PERSON]}|{data[CONF_ZONE]}"
    clash = any(
        other.unique_id == unique_id and other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if clash:
        _LOGGER.warning(
            "%s was renamed to %s, which another Home ETA entry already tracks",
            old,
            new,
        )
        unique_id = entry.unique_id or unique_id
    _LOGGER.info("Following rename of %s to %s in %s", old, new, entry.title)
    hass.config_entries.async_update_entry(
        entry, data=data, options=options, unique_id=unique_id
    )
    hass.config_entries.async_schedule_reload(entry.entry_id)
