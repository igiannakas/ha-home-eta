"""Per-person action: relearn typical speeds from the recorder's history."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeEtaConfigEntry
from .const import KEY_RELEARN_SPEEDS
from .entity import PersonEntity
from .runtime import PersonRuntime

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the relearn button for a person."""
    runtime = entry.runtime_data
    assert isinstance(runtime, PersonRuntime)
    async_add_entities([RelearnSpeedsButton(runtime)])


class RelearnSpeedsButton(PersonEntity, ButtonEntity):
    """Learn the typical speeds afresh from the trips in the recorder's history.

    What was learned before is replaced, so trips older than the history
    (10 days by default) are forgotten. Refused without the recorder.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_RELEARN_SPEEDS)

    @property
    def available(self) -> bool:
        """Stay available: relearning does not need the person's location."""
        return True

    async def async_press(self) -> None:
        """Relearn."""
        await self._runtime.async_relearn_speeds()
