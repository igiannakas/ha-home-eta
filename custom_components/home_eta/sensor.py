"""Sensors for Home ETA."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HomeEtaConfigEntry
from .const import KEY_DISTANCE, KEY_EXPECTED_HOME, KEY_MINUTES, KEY_NEAR_HOME
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, GroupView, PersonRuntime, PersonView

PARALLEL_UPDATES = 0


def _percent(value: float | None) -> int | None:
    return None if value is None else round(value * 100.0)


@dataclass(frozen=True, kw_only=True)
class PersonSensorDescription(SensorEntityDescription):
    """Describe a person sensor."""

    value_fn: Callable[[PersonView], datetime | int | None]


PERSON_SENSORS: tuple[PersonSensorDescription, ...] = (
    PersonSensorDescription(
        key=KEY_EXPECTED_HOME,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda view: view.expected_home,
    ),
    PersonSensorDescription(
        key=KEY_MINUTES,
        # A countdown, not a measurement: no long-term statistics.
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda view: view.minutes_to_home,
    ),
    PersonSensorDescription(
        key=KEY_DISTANCE,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=0,
        value_fn=lambda view: (
            None
            if view.snapshot is None or view.snapshot.distance_m is None
            else round(view.snapshot.distance_m)
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEtaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for a person or a group."""
    runtime = entry.runtime_data
    if isinstance(runtime, GroupRuntime):
        async_add_entities(
            [
                GroupExpectedHomeSensor(runtime),
                GroupMinutesSensor(runtime),
                GroupNearHomeSensor(runtime),
            ]
        )
        return
    entities: list[SensorEntity] = [
        PersonSensor(runtime, description) for description in PERSON_SENSORS
    ]
    entities.append(NearHomeSensor(runtime))
    async_add_entities(entities)


class PersonSensor(PersonEntity, SensorEntity):
    """A sensor computed from a person's view."""

    entity_description: PersonSensorDescription

    def __init__(
        self, runtime: PersonRuntime, description: PersonSensorDescription
    ) -> None:
        """Initialise."""
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> datetime | int | None:
        """Return the value."""
        return self.entity_description.value_fn(self._runtime.view)


class NearHomeSensor(PersonEntity, SensorEntity):
    """How near home the person is, 0-100 %, whichever way they are going.

    100 % at home or within 800 m, falling to 0 % at the person's near-home
    radius. A long stay at one place fades it back to 0 %.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_NEAR_HOME)

    @property
    def native_value(self) -> int | None:
        """Return the percentage, None while the person's whereabouts are unknown."""
        snap = self._runtime.view.snapshot
        return None if snap is None else _percent(snap.near_home)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the settings in use and since when the person has stayed put."""
        config = self._runtime.config.near_home
        snap = self._runtime.view.snapshot
        still_since = None if snap is None else snap.homeward.still_since
        fade_after = config.fade_after_s
        return {
            "staying_since": None
            if still_since is None
            else dt_util.utc_from_timestamp(still_since).isoformat(),
            "radius_m": round(config.radius_m),
            "fade_after_min": None if fade_after is None else round(fade_after / 60.0),
        }


class GroupExpectedHomeSensor(GroupEntity, SensorEntity):
    """When the first member heading home is expected; unknown while nobody is."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"heading_home"})

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_EXPECTED_HOME)

    @property
    def native_value(self) -> datetime | None:
        """Return the expected arrival of whoever is due first."""
        first = self._runtime.view.first
        return None if first is None else first.arrival

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is due first and everyone heading home."""
        view: GroupView = self._runtime.view
        first = view.first
        return {
            "person": None if first is None else first.person_id,
            "person_name": None if first is None else first.name,
            "heading_home": [
                {
                    "person": member.person_id,
                    "name": member.name,
                    "expected_at": member.arrival.isoformat(),
                    "minutes": member.minutes,
                }
                for member in view.heading_home
            ],
            "persons_home": list(view.persons_home),
            "missing_members": list(view.missing),
        }


class GroupMinutesSensor(GroupEntity, SensorEntity):
    """Minutes until the first member heading home is expected."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_MINUTES)

    @property
    def native_value(self) -> int | None:
        """Return the minutes."""
        first = self._runtime.view.first
        return None if first is None else first.minutes


class GroupNearHomeSensor(GroupEntity, SensorEntity):
    """How near home the nearest member is, 0-100 %; 100 % while anyone is home."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_NEAR_HOME)

    @property
    def native_value(self) -> int | None:
        """Return the percentage, None while nobody's whereabouts are known."""
        near = self._runtime.view.near
        return None if near is None else _percent(near.value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is nearest and who is home."""
        near = self._runtime.view.near
        return {
            "person": None if near is None else near.person_id,
            "person_name": None if near is None else near.name,
            "persons_home": list(self._runtime.view.persons_home),
        }
