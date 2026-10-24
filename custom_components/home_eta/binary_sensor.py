"""Binary sensors for Home ETA: a group's "someone arriving soon"."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeEtaConfigEntry
from .const import KEY_SOMEONE_SOON
from .entity import GroupEntity
from .runtime import GroupRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensor for a group (people have none)."""
    runtime = entry.runtime_data
    assert isinstance(runtime, GroupRuntime)
    async_add_entities([SomeoneArrivingSoonSensor(runtime)])


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
