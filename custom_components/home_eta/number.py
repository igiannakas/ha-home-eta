"""Per-person arriving-soon threshold."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeEtaConfigEntry
from .const import KEY_THRESHOLD
from .core.params import (
    DEFAULT_ARRIVING_SOON_MIN,
    MAX_ARRIVING_SOON_MIN,
    MIN_ARRIVING_SOON_MIN,
)
from .entity import PersonEntity
from .runtime import PersonRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the threshold for a person."""
    runtime = entry.runtime_data
    assert isinstance(runtime, PersonRuntime)
    async_add_entities([ArrivingSoonThreshold(runtime)])


class ArrivingSoonThreshold(PersonEntity, RestoreNumber):
    """How many minutes out counts as arriving soon, for this person."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_ARRIVING_SOON_MIN
    _attr_native_max_value = MAX_ARRIVING_SOON_MIN
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_THRESHOLD)
        self._attr_native_value = float(DEFAULT_ARRIVING_SOON_MIN)

    @property
    def available(self) -> bool:
        """Stay available so the threshold can be set at any time."""
        return True

    async def async_added_to_hass(self) -> None:
        """Restore the last value and apply it."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if (
            last is not None
            and last.native_value is not None
            and MIN_ARRIVING_SOON_MIN <= last.native_value <= MAX_ARRIVING_SOON_MIN
        ):
            self._attr_native_value = last.native_value
        assert self._attr_native_value is not None
        self._runtime.set_threshold(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Set the threshold."""
        self._attr_native_value = value
        self._runtime.set_threshold(value)
        self.async_write_ha_state()
