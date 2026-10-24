"""Upgrade config entries saved by earlier versions.

Version 1.2 (Home ETA 2.0) keeps only what turned out to be useful:

- The kinematic and past-trips ETAs, closing speed, ETA confidence, approach
  state and the first-arrival sensors are gone; their registry entries are
  removed so they do not linger as "no longer provided".
- Learning from past trips is gone, so learned trips (personal location
  history) are deleted.
- The activity, reference travel-time and "zones that end a trip" options fed
  only those, and go with them.
- Near home is measured per person now: a group's radius and fade move to
  each of its members.

Each entry is migrated on its own, in whatever order Home Assistant sets them
up, so a group hands its near-home settings to members not yet migrated, and
a person not yet handed any looks them up in a group not yet migrated.
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
    CONF_NEAR_HOME_FADE_AFTER_MIN,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_ZONE,
    DEFAULT_NEAR_HOME_FADE_AFTER_MIN,
    DEFAULT_NEAR_HOME_RADIUS_M,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
    MAX_NEAR_HOME_FADE_AFTER_MIN,
    MAX_NEAR_HOME_RADIUS_M,
    MIN_NEAR_HOME_RADIUS_M,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_VERSION = 1
CONFIG_MINOR_VERSION = 2

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
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
        _migrate_group(hass, entry)
    else:
        _migrate_person(hass, entry)
        # After the entry is updated, so another entry's migration running
        # meanwhile sees this one as done.
        await legacy_store(hass, entry.entry_id).async_remove()
    _LOGGER.info("Migrated %s to version 1.2", entry.title)
    return True


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
    _remove_entities(hass, entry, LEGACY_PERSON_ENTITIES)
    hass.config_entries.async_update_entry(
        entry, options=options, minor_version=CONFIG_MINOR_VERSION
    )


@callback
def _migrate_group(hass: HomeAssistant, entry: ConfigEntry) -> None:
    near = _near_home_options(entry.options)
    members = set(entry.options.get(CONF_MEMBERS) or [])
    for person in _legacy_person_entries(hass, entry.data.get(CONF_ZONE)):
        if (
            person.data.get(CONF_PERSON) in members
            and CONF_NEAR_HOME_RADIUS_M not in person.options
        ):
            hass.config_entries.async_update_entry(
                person, options={**person.options, **near}
            )
    _remove_entities(hass, entry, LEGACY_GROUP_ENTITIES)
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_MEMBERS: list(entry.options.get(CONF_MEMBERS) or [])},
        minor_version=CONFIG_MINOR_VERSION,
    )


def _legacy_person_entries(hass: HomeAssistant, zone: object) -> list[ConfigEntry]:
    return [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_PERSON
        and other.data.get(CONF_ZONE) == zone
        and other.version == CONFIG_VERSION
        and other.minor_version < CONFIG_MINOR_VERSION
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
            and other.minor_version < CONFIG_MINOR_VERSION
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
        CONF_NEAR_HOME_FADE_AFTER_MIN: _whole(
            options.get(CONF_NEAR_HOME_FADE_AFTER_MIN),
            DEFAULT_NEAR_HOME_FADE_AFTER_MIN,
            0,
            MAX_NEAR_HOME_FADE_AFTER_MIN,
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
