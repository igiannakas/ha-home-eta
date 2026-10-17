"""Helpers for the Home Assistant tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.home_eta.const import (
    CONF_ENABLE_HISTORICAL,
    CONF_ENTRY_TYPE,
    CONF_IGNORED_ZONES,
    CONF_MEMBERS,
    CONF_PERSON,
    CONF_ZONE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
)
from tests.sim import HOME_LAT, HOME_LON, HOME_RADIUS_M, to_latlon

__all__ = ["HOME_LAT", "HOME_LON", "HOME_RADIUS_M"]

ALEX = "person.alex"
SAM = "person.sam"


def person_entry(
    person: str = ALEX,
    *,
    title: str | None = None,
    zone: str = "zone.home",
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Return a person config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title or person.split(".")[1].title(),
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON, CONF_PERSON: person, CONF_ZONE: zone},
        options={
            CONF_ENABLE_HISTORICAL: False,
            CONF_IGNORED_ZONES: [],
            **(options or {}),
        },
        unique_id=f"{person}|{zone}",
    )


def group_entry(members: list[str], *, title: str = "Home ETA") -> MockConfigEntry:
    """Return a first-arrival group entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_ZONE: "zone.home"},
        options={CONF_MEMBERS: members},
    )


def set_person(
    hass: HomeAssistant,
    person: str,
    east_m: float | None,
    *,
    zones: tuple[str, ...] = (),
    accuracy: float = 10.0,
) -> None:
    """Set a person's state ``east_m`` metres east of home (None: no position)."""
    attributes: dict[str, Any] = {
        "in_zones": list(zones),
        "friendly_name": person.split(".")[1].title(),
    }
    if east_m is not None:
        lat, lon = to_latlon(east_m, 0.0)
        attributes.update({"latitude": lat, "longitude": lon, "gps_accuracy": accuracy})
    if "zone.home" in zones:
        state = "home"
    elif zones:
        state = zones[0].split(".")[1].title()
    else:
        state = "not_home"
    hass.states.async_set(person, state, attributes)


async def step(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    person: str,
    east_m: float | None,
    *,
    seconds: float = 60.0,
    zones: tuple[str, ...] = (),
) -> None:
    """Advance time, fire the minute tick, then move the person."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    set_person(hass, person, east_m, zones=zones)
    await hass.async_block_till_done()


async def drive_home(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    person: str,
    *,
    start_m: float,
    speed_mps: float,
    interval_s: float = 60.0,
    stop_m: float = 0.0,
) -> None:
    """Move a person towards home at constant speed until ``stop_m``."""
    east = start_m
    set_person(hass, person, east)
    await hass.async_block_till_done()
    while east - speed_mps * interval_s > stop_m:
        east -= speed_mps * interval_s
        await step(hass, freezer, person, east, seconds=interval_s)
