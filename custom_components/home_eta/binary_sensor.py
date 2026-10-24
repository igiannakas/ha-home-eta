"""Binary sensors for Home ETA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HomeEtaConfigEntry
from .const import KEY_ARRIVING_SOON, KEY_HEADING_HOME, KEY_SOMEONE_SOON
from .core.approach import ApproachState
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, PersonRuntime, minutes_until

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
        async_add_entities([ArrivingSoonSensor(runtime), HeadingHomeSensor(runtime)])


class ArrivingSoonSensor(PersonEntity, BinarySensorEntity):
    """On while the person is arriving soon.

    Held off while the person is inside any zone other than the target.
    """

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_ARRIVING_SOON)

    @property
    def is_on(self) -> bool | None:
        """Return True when arriving soon."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.state is ApproachState.ARRIVING_SOON

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the threshold in use."""
        return {"threshold_min": self._runtime.view.threshold_min}


class HeadingHomeSensor(PersonEntity, BinarySensorEntity):
    """On while the person is on their way home, from early in the trip.

    For warming the house: it comes on well before "arriving soon" (at the
    first clear progress towards home, or on entering a zone around home) and
    stays on until the person gets home or the trip clearly is not one.
    """

    _unrecorded_attributes = frozenset({"expected_at", "minutes", "speed_mps"})

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_HEADING_HOME)

    @property
    def is_on(self) -> bool | None:
        """Return True while heading home, None with no data."""
        snap = self._runtime.view.snapshot
        if snap is None:
            return None
        return snap.homeward is not None and snap.homeward.heading

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why, since when, and when the person is expected."""
        snap = self._runtime.view.snapshot
        homeward = None if snap is None else snap.homeward
        if homeward is None:
            return {}
        arrival = (
            None
            if homeward.arrival is None
            else dt_util.utc_from_timestamp(homeward.arrival)
        )
        return {
            "reason": homeward.reason.value,
            "since": _iso(homeward.since),
            "expected_at": None if arrival is None else arrival.isoformat(),
            "minutes": None
            if arrival is None
            else minutes_until(arrival, dt_util.utcnow()),
            "speed_mps": None
            if homeward.speed_mps is None
            else round(homeward.speed_mps, 1),
            "speed_source": None
            if homeward.speed_source is None
            else homeward.speed_source.value,
            "farthest_m": None
            if homeward.farthest_m is None
            else round(homeward.farthest_m),
            "staying_since": _iso(homeward.still_since),
        }


def _iso(value: float | None) -> str | None:
    return None if value is None else dt_util.utc_from_timestamp(value).isoformat()


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
