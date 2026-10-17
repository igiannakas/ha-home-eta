"""Binary sensors for Home ETA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeEtaConfigEntry
from .const import KEY_ARRIVING_SOON, KEY_SOMEONE_SOON
from .core.approach import ApproachState
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, PersonRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the arriving-soon sensor for a person or a group."""
    runtime = entry.runtime_data
    if isinstance(runtime, GroupRuntime):
        async_add_entities([SomeoneArrivingSoonSensor(runtime)])
    else:
        async_add_entities([ArrivingSoonSensor(runtime)])


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
