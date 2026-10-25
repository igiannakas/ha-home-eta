"""Binary sensors for Home ETA.

Per person: close to home (home or within 800 m) and nearby (home or within
their near-home radius). Per group: someone arriving soon, someone close to
home and someone nearby.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeEtaConfigEntry
from .const import (
    KEY_CLOSE_TO_HOME,
    KEY_NEARBY,
    KEY_SOMEONE_CLOSE,
    KEY_SOMEONE_NEARBY,
    KEY_SOMEONE_SOON,
)
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, PersonRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors of a person or a group."""
    runtime = entry.runtime_data
    if isinstance(runtime, PersonRuntime):
        async_add_entities([CloseToHomeSensor(runtime), NearbySensor(runtime)])
        return
    async_add_entities(
        [
            SomeoneArrivingSoonSensor(runtime),
            SomeoneCloseSensor(runtime),
            SomeoneNearbySensor(runtime),
        ]
    )


class CloseToHomeSensor(PersonEntity, BinarySensorEntity):
    """On while the person is home or within 800 m of the zone centre.

    Unknown while their distance is not known. It stays on however long they
    stay there.
    """

    _unrecorded_attributes = frozenset({"distance_m"})

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_CLOSE_TO_HOME)

    @property
    def is_on(self) -> bool | None:
        """Return True when close to home, None while unknown."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.close_to_home

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the distance and the limit."""
        return {
            "distance_m": _distance(self._runtime),
            "within_m": round(self._runtime.config.near_home.full_m),
        }


class NearbySensor(PersonEntity, BinarySensorEntity):
    """On while the person is home or within their near-home radius.

    Unknown while their distance is not known. It stays on however long they
    stay there.
    """

    _unrecorded_attributes = frozenset({"distance_m"})

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_NEARBY)

    @property
    def is_on(self) -> bool | None:
        """Return True when nearby, None while unknown."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.nearby

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the distance and the radius."""
        return {
            "distance_m": _distance(self._runtime),
            "radius_m": round(self._runtime.config.near_home.radius_m),
        }


def _distance(runtime: PersonRuntime) -> int | None:
    snap = runtime.view.snapshot
    if snap is None or snap.distance_m is None:
        return None
    return round(snap.distance_m)


class SomeoneArrivingSoonSensor(GroupEntity, BinarySensorEntity):
    """On while any member is arriving soon by their own threshold.

    Unknown while it cannot be ruled out: a member has no data.
    """

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_SOMEONE_SOON)

    @property
    def is_on(self) -> bool | None:
        """Return True when any member is arriving soon, None when unsure."""
        return self._runtime.view.someone_arriving_soon

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is arriving soon."""
        view = self._runtime.view
        return {
            "persons": list(view.arriving_soon),
            "names": list(view.arriving_soon_names),
        }


class SomeoneCloseSensor(GroupEntity, BinarySensorEntity):
    """On while any member is home or within 800 m.

    Unknown while it cannot be ruled out: a member has no data or no known
    distance.
    """

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_SOMEONE_CLOSE)

    @property
    def is_on(self) -> bool | None:
        """Return True when any member is close to home, None when unsure."""
        return self._runtime.view.someone_close

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is close to home."""
        view = self._runtime.view
        return {"persons": list(view.close), "names": list(view.close_names)}


class SomeoneNearbySensor(GroupEntity, BinarySensorEntity):
    """On while any member is home or within their own near-home radius.

    Unknown while it cannot be ruled out, as for someone close to home.
    """

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_SOMEONE_NEARBY)

    @property
    def is_on(self) -> bool | None:
        """Return True when any member is nearby, None when unsure."""
        return self._runtime.view.someone_nearby

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is nearby."""
        view = self._runtime.view
        return {"persons": list(view.nearby), "names": list(view.nearby_names)}
