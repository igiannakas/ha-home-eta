"""Base entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .runtime import GroupRuntime, PersonRuntime


class PersonEntity(Entity):
    """An entity fed by a person runtime."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: PersonRuntime, key: str) -> None:
        """Bind to the runtime."""
        self._runtime = runtime
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        """Follow the runtime."""
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return False while the person entity is missing."""
        return self._runtime.view.available


class GroupEntity(Entity):
    """An entity fed by a group runtime."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: GroupRuntime, key: str) -> None:
        """Bind to the runtime."""
        self._runtime = runtime
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        """Follow the runtime."""
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
