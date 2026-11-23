"""Per group member: ignore them (a phone that went silent at home, say).

The group keeps who it ignores, and why and since when, in Home Assistant's
storage, and takes them back before its first update after a restart or a
reload: so the switches show it, and are not where it is kept. Restoring
the switches instead would show an ignored member home for a moment at every
start, and "someone home" would turn on and off again.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HomeEtaConfigEntry
from .const import KEY_IGNORE_MEMBER, IgnoreCause, IgnoreReason
from .entity import GroupEntity
from .runtime import GroupRuntime

PARALLEL_UPDATES = 0


def _unique_id(runtime: GroupRuntime, person_id: str) -> str:
    return f"{runtime.entry.entry_id}_{KEY_IGNORE_MEMBER}_{person_id}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an ignore switch for each member of a group."""
    runtime = entry.runtime_data
    assert isinstance(runtime, GroupRuntime)
    _remove_former_members(hass, runtime)
    async_add_entities(
        IgnoreMemberSwitch(runtime, person_id) for person_id in runtime.members
    )


@callback
def _remove_former_members(hass: HomeAssistant, runtime: GroupRuntime) -> None:
    """Remove the switches of people no longer in the group."""
    registry = er.async_get(hass)
    current = {_unique_id(runtime, person_id) for person_id in runtime.members}
    prefix = f"{runtime.entry.entry_id}_{KEY_IGNORE_MEMBER}_"
    for registered in er.async_entries_for_config_entry(
        registry, runtime.entry.entry_id
    ):
        if (
            registered.domain == "switch"
            and registered.unique_id.startswith(prefix)
            and registered.unique_id not in current
        ):
            registry.async_remove(registered.entity_id)


class IgnoreMemberSwitch(GroupEntity, SwitchEntity):
    """On while the group ignores this member.

    An ignored member counts as nowhere in the group's entities: not home,
    not close, not heading home, and not missing either. Their own entities
    are unchanged. The group stops ignoring them when anyone it counts
    arrives home, or when their own phone reports again.
    """

    _unrecorded_attributes = frozenset({"status"})

    def __init__(self, runtime: GroupRuntime, person_id: str) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_IGNORE_MEMBER)
        self._person_id = person_id
        self._attr_unique_id = _unique_id(runtime, person_id)
        self._attr_translation_placeholders = {"name": runtime.member_name(person_id)}

    @property
    def available(self) -> bool:
        """Stay available: a member can be ignored without data."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while the member is ignored."""
        return self._person_id in self._runtime.ignored

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who, since when and why ignored, and their own status."""
        item = self._runtime.ignored.get(self._person_id)
        status = self._runtime.member_status(self._person_id)
        return {
            "person": self._person_id,
            "since": None
            if item is None
            else dt_util.utc_from_timestamp(item.since).isoformat(),
            "reason": None if item is None else item.reason.value,
            "status": None if status is None else status.value,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Ignore the member."""
        self._runtime.ignore(
            [self._person_id], IgnoreReason.BY_HAND, IgnoreCause.SWITCH
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Count the member again."""
        self._runtime.stop_ignoring([self._person_id], IgnoreCause.SWITCH)
