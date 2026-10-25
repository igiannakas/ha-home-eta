"""Upgrade config entries saved by earlier versions.

Version 1.2 (Home ETA 2.0.0) keeps only what turned out to be useful:

- The kinematic and past-trips ETAs, closing speed, ETA confidence, approach
  state and the first-arrival sensors are gone; their registry entries are
  removed so they do not linger as "no longer provided".
- Learning from past trips is gone, so learned trips (personal location
  history) are deleted.
- The activity, reference travel-time and "zones that end a trip" options fed
  only those, and go with them.
- Near home is measured per person now: a group's radius moves to each of
  its members (its fade did too, until 1.4 removed it).

Version 1.3 (Home ETA 2.1.0) puts heading home and arriving soon into one
status sensor per person (home, arriving soon, heading home, away); their
binary sensors are removed from the registry.

Version 1.4 (Home ETA 2.3.0) removes the "near home fades after" setting:
near home no longer fades after a long stay. The stored value is removed at
setup, like other leftovers, so a value an older release saved after a
downgrade goes too.

Each entry is migrated on its own, in whatever order Home Assistant sets them
up, so a group hands near-home settings to members not yet migrated, and a
person not yet handed any looks them up: both pick the same group (by title)
when a person is in several.

Retired entities and learned trips are removed at every setup rather than
only when migrating, so an entry set up by an older release in between (a
downgrade, then an upgrade) is cleaned up too.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ADVANCED,
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_ZONE,
    DEFAULT_NEAR_HOME_RADIUS_M,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
    MAX_NEAR_HOME_RADIUS_M,
    MIN_NEAR_HOME_RADIUS_M,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_VERSION = 1
CONFIG_MINOR_VERSION = 4

RETIRED_NEAR_HOME_FADE = "near_home_fade_after_min"
"""The near-home fade setting of a person, removed in 1.4."""
RETIRED_PERSON_OPTIONS = (RETIRED_NEAR_HOME_FADE,)

LEGACY_PERSON_OPTIONS = (
    "activity_entity",
    "reference_eta_entity",
    "enable_historical",
    "ignored_zones",
)
LEGACY_ADVANCED_OPTIONS = ("min_closing_speed_mps",)
LEGACY_PERSON_ENTITIES = (
    "eta_kinematic",
    "eta_historical",
    "closing_speed",
    "eta_confidence",
    "approach_state",
)
LEGACY_GROUP_ENTITIES = (
    "first_arrival",
    "first_arrival_person",
    "first_arrival_minutes",
)
REPLACED_BY_STATUS = ("heading_home", "arriving_soon")
"""Person binary sensors of 1.2, now states of the status sensor."""
LEGACY_STORE_VERSION = 1


def legacy_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the store that held a person's learned trips before 2.0."""
    return Store(hass, LEGACY_STORE_VERSION, f"{DOMAIN}.history.{entry_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry up to the current version."""
    if entry.version != CONFIG_VERSION:
        # Home Assistant refuses newer major versions before calling this;
        # there has never been an older one.
        return False
    if entry.minor_version >= CONFIG_MINOR_VERSION:
        # Saved by a newer release (then downgraded): minor versions are
        # backwards compatible.
        return True
    group = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP
    if entry.minor_version < 2:
        if group:
            _migrate_group(hass, entry)
        else:
            _migrate_person(hass, entry)
    if entry.minor_version < 4:
        # Options no longer read are removed at setup: async_remove_retired.
        hass.config_entries.async_update_entry(entry, minor_version=4)
    _LOGGER.info("Migrated %s to version 1.%s", entry.title, CONFIG_MINOR_VERSION)
    return True


async def async_remove_retired(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove what earlier releases made and this one does not provide.

    Entities no longer provided, a person's learned trips (location history)
    and options no longer read. Cheap, and a no-op once done.
    """
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
        _remove_entities(hass, entry, LEGACY_GROUP_ENTITIES)
        return
    _remove_entities(hass, entry, LEGACY_PERSON_ENTITIES + REPLACED_BY_STATUS)
    await legacy_store(hass, entry.entry_id).async_remove()
    if any(key in entry.options for key in RETIRED_PERSON_OPTIONS):
        options = {
            key: value
            for key, value in entry.options.items()
            if key not in RETIRED_PERSON_OPTIONS
        }
        hass.config_entries.async_update_entry(entry, options=options)


@callback
def _migrate_person(hass: HomeAssistant, entry: ConfigEntry) -> None:
    options = {
        key: value
        for key, value in entry.options.items()
        if key not in LEGACY_PERSON_OPTIONS
    }
    advanced = {
        key: value
        for key, value in (options.pop(CONF_ADVANCED, None) or {}).items()
        if key not in LEGACY_ADVANCED_OPTIONS
    }
    if advanced:
        options[CONF_ADVANCED] = advanced
    if CONF_NEAR_HOME_RADIUS_M not in options:
        # Not handed settings by a group migrated earlier: look for one.
        options.update(_near_home_options(_legacy_group_options(hass, entry)))
    hass.config_entries.async_update_entry(entry, options=options, minor_version=2)


@callback
def _migrate_group(hass: HomeAssistant, entry: ConfigEntry) -> None:
    members = set(entry.options.get(CONF_MEMBERS) or [])
    for person in _legacy_person_entries(hass, entry.data.get(CONF_ZONE)):
        if (
            person.data.get(CONF_PERSON) in members
            and CONF_NEAR_HOME_RADIUS_M not in person.options
        ):
            # The group the person's own migration would pick: this one, or
            # another not yet migrated whose title comes first.
            near = _near_home_options(_legacy_group_options(hass, person))
            hass.config_entries.async_update_entry(
                person, options={**person.options, **near}
            )
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_MEMBERS: list(entry.options.get(CONF_MEMBERS) or [])},
        minor_version=2,
    )


def _legacy_person_entries(hass: HomeAssistant, zone: object) -> list[ConfigEntry]:
    return [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_PERSON
        and other.data.get(CONF_ZONE) == zone
        and other.version == CONFIG_VERSION
        and other.minor_version < 2
    ]


def _legacy_group_options(hass: HomeAssistant, entry: ConfigEntry) -> Mapping[str, Any]:
    """Return the options of a not yet migrated group the person is in."""
    person, zone = entry.data.get(CONF_PERSON), entry.data.get(CONF_ZONE)
    groups = sorted(
        (
            other
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP
            and other.data.get(CONF_ZONE) == zone
            and other.minor_version < 2
            and person in (other.options.get(CONF_MEMBERS) or [])
        ),
        key=lambda other: (other.title, other.entry_id),
    )
    return groups[0].options if groups else {}


def _near_home_options(options: Mapping[str, Any]) -> dict[str, int]:
    """Return near-home settings from saved options, defaults where unusable."""
    return {
        CONF_NEAR_HOME_RADIUS_M: _whole(
            options.get(CONF_NEAR_HOME_RADIUS_M),
            DEFAULT_NEAR_HOME_RADIUS_M,
            MIN_NEAR_HOME_RADIUS_M,
            MAX_NEAR_HOME_RADIUS_M,
        ),
    }


def _whole(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not low <= value <= high:
        return default
    return round(value)


@callback
def _remove_entities(
    hass: HomeAssistant, entry: ConfigEntry, keys: tuple[str, ...]
) -> None:
    registry = er.async_get(hass)
    retired = {f"{entry.entry_id}_{key}" for key in keys}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.unique_id in retired:
            registry.async_remove(registered.entity_id)
