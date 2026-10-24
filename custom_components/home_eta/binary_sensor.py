"""Binary sensors for Home ETA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)
from homeassistant.util import dt as dt_util

from . import HomeEtaConfigEntry
from .const import KEY_ARRIVING_SOON, KEY_HEADING_HOME, KEY_SOMEONE_SOON
from .core.homeward import HomewardRestore
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, PersonRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors for a person or a group."""
    runtime = entry.runtime_data
    if isinstance(runtime, GroupRuntime):
        async_add_entities([SomeoneArrivingSoonSensor(runtime)])
    else:
        async_add_entities([HeadingHomeSensor(runtime), ArrivingSoonSensor(runtime)])


class HeadingHomeSensor(PersonEntity, BinarySensorEntity, RestoreEntity):
    """On while the person is on their way home, from early in the trip.

    It comes on at the first clear progress towards home, or on entering a
    zone around home, and stays on until the person gets home or the trip
    clearly is not one. The trip home (and a stay somewhere) is saved for a
    restart.
    """

    _unrecorded_attributes = frozenset({"speed_mps", "farthest_m"})

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_HEADING_HOME)

    async def async_added_to_hass(self) -> None:
        """Restore the trip home and stay saved before a restart or reload."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is None:
            return
        data = HomewardRestore.from_dict(extra.as_dict())
        if data is not None:
            self._runtime.restore_homeward(data)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        """Save the trip home and stay, if any."""
        data = self._runtime.tracker.homeward.restore_data()
        return None if data is None else RestoredExtraData(data.as_dict())

    @property
    def is_on(self) -> bool | None:
        """Return True while heading home, None with no data."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.heading_home

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why and since when, and the speed behind the expected arrival."""
        snap = self._runtime.view.snapshot
        if snap is None:
            return {}
        homeward = snap.homeward
        return {
            "reason": homeward.reason.value,
            "since": None
            if homeward.since is None
            else dt_util.utc_from_timestamp(homeward.since).isoformat(),
            "speed_mps": None
            if homeward.speed_mps is None
            else round(homeward.speed_mps, 1),
            "speed_source": None
            if homeward.speed_source is None
            else homeward.speed_source.value,
            "farthest_m": None
            if homeward.farthest_m is None
            else round(homeward.farthest_m),
        }


class ArrivingSoonSensor(PersonEntity, BinarySensorEntity):
    """On once the person heading home is due within the threshold, or close by.

    Held off while inside any zone other than the target and the zones around
    it; once on, it stays on until the trip home ends.
    """

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_ARRIVING_SOON)

    @property
    def is_on(self) -> bool | None:
        """Return True while arriving soon, None with no data."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.arriving_soon

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the threshold in use."""
        return {"threshold_min": self._runtime.view.threshold_min}


class SomeoneArrivingSoonSensor(GroupEntity, BinarySensorEntity):
    """On while any member is arriving soon by their own threshold."""

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_SOMEONE_SOON)

    @property
    def is_on(self) -> bool | None:
        """Return True when any member is arriving soon, None with no data."""
        view = self._runtime.view
        return None if view.reporting == 0 else bool(view.arriving_soon)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is arriving soon."""
        view = self._runtime.view
        return {
            "persons": list(view.arriving_soon),
            "names": list(view.arriving_soon_names),
        }
