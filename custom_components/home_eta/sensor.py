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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)
from homeassistant.util import dt as dt_util

from . import HomeEtaConfigEntry
from .const import (
    KEY_DISTANCE,
    KEY_EXPECTED_HOME,
    KEY_MINUTES,
    KEY_NEAR_HOME,
    KEY_STATUS,
    KEY_TYPICAL_SPEEDS,
)
from .core.calibration import (
    HALF_LIFE_DAYS,
    MIN_REPORTED_CHANGE,
    PRIOR_TRIPS,
    CurveFit,
    KnotChange,
    SpeedCalibrator,
)
from .core.homeward import HomewardRestore
from .core.tracker import STATUS_ORDER
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, GroupView, PersonRuntime, PersonView

PARALLEL_UPDATES = 0

STATUS_OPTIONS = [status.value for status in STATUS_ORDER]


def _percent(value: float | None) -> int | None:
    return None if value is None else round(value * 100.0)


def _iso(value: float | None) -> str | None:
    return None if value is None else dt_util.utc_from_timestamp(value).isoformat()


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
                GroupStatusSensor(runtime),
                GroupExpectedHomeSensor(runtime),
                GroupMinutesSensor(runtime),
                GroupNearHomeSensor(runtime),
            ]
        )
        return
    entities: list[SensorEntity] = [StatusSensor(runtime)]
    entities.extend(
        PersonSensor(runtime, description) for description in PERSON_SENSORS
    )
    entities.append(NearHomeSensor(runtime))
    entities.append(TypicalSpeedsSensor(runtime))
    async_add_entities(entities)


class StatusSensor(PersonEntity, SensorEntity, RestoreEntity):
    """Where the person is on the way home: home, arriving soon, heading home or away.

    Heading home comes on at the first clear progress towards home, or on
    entering a zone around home, and lasts until the person gets home or the
    trip clearly is not one. Arriving soon is the last part of it: from when
    the person is expected within their threshold, or is close by, until the
    trip ends; held off inside another zone. The trip home (and a stay
    somewhere) is saved for a restart.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _unrecorded_attributes = frozenset({"speed_mps", "farthest_m"})

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_STATUS)

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
    def native_value(self) -> str | None:
        """Return the status, None until the person's whereabouts are known."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why and since when, and the speed behind the expected arrival.

        The reason and its time are those of the trip home: when it started,
        or when and why it last ended.
        """
        snap = self._runtime.view.snapshot
        if snap is None:
            return {}
        homeward = snap.homeward
        return {
            "reason": homeward.reason.value,
            "since": _iso(homeward.since),
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
            "staying_since": _iso(still_since),
            "radius_m": round(config.radius_m),
            "fade_after_min": None if fade_after is None else round(fade_after / 60.0),
        }


def _table(fit: CurveFit) -> list[dict[str, float]]:
    return [
        {
            "distance_m": round(knot.distance_m),
            "built_in_mps": round(knot.built_in_mps, 2),
            "learned_mps": round(knot.learned_mps, 2),
            "factor": round(knot.factor, 3),
            "trips": round(knot.trips, 2),
            "measured_weight": round(knot.weight, 3),
            "weight_trips": round(knot.weight_trips, 2),
        }
        for knot in fit.knots
    ]


def _change(change: KnotChange) -> dict[str, Any]:
    """Return what one trip changed at one knot: only what moved."""
    entry: dict[str, Any] = {
        "curve": change.curve.value,
        "distance_m": round(change.distance_m),
    }
    if abs(change.factor_after - change.factor_before) >= MIN_REPORTED_CHANGE:
        entry["factor"] = [
            round(change.factor_before, 3),
            round(change.factor_after, 3),
        ]
    if abs(change.weight_after - change.weight_before) >= MIN_REPORTED_CHANGE:
        entry["measured_weight"] = [
            round(change.weight_before, 3),
            round(change.weight_after, 3),
        ]
    return entry


def speeds_attributes(speeds: SpeedCalibrator) -> dict[str, Any]:
    """Return the learned tables and what the latest trips changed in them."""
    calibration = speeds.calibration
    trips = speeds.trips
    return {
        "all_trips": _table(calibration.all_trips),
        "driving": _table(calibration.driving),
        "recent_trips": [
            {
                "arrived_at": _iso(change.trip.arrived_at),
                "from_m": round(change.trip.from_m),
                "samples": len(change.trip.samples),
                "changes": [_change(knot) for knot in change.changes],
            }
            for change in speeds.recent_changes
        ],
        "first_trip": _iso(trips[0].arrived_at) if trips else None,
        "learned_from_history": speeds.history_learned,
        "fitted_at": _iso(calibration.at) if trips else None,
        "prior_trips": PRIOR_TRIPS,
        "half_life_days": HALF_LIFE_DAYS,
    }


class TypicalSpeedsSensor(PersonEntity, SensorEntity):
    """How many trips home the person's typical speeds were learned from.

    Its attributes are the two learned tables, for any trip home and when
    driving: at each distance, the built-in speed, the learned one, the
    correction factor between them, the weight of a measured speed against
    it, and how many trips (after ageing) are behind each; and what each of
    the latest trips changed.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_TYPICAL_SPEEDS)

    @property
    def available(self) -> bool:
        """Stay available: the tables do not depend on the person's location."""
        return True

    @property
    def native_value(self) -> int:
        """Return the number of trips learned from."""
        return len(self._runtime.speeds.trips)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the learned tables and the latest changes."""
        return speeds_attributes(self._runtime.speeds)


class GroupStatusSensor(GroupEntity, SensorEntity):
    """The most-home status of any member: home if anyone is, and so on.

    Unknown while a member has no data and nobody is home. Its attributes
    list who is where.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_STATUS)

    @property
    def native_value(self) -> str | None:
        """Return the most-home status."""
        status = self._runtime.view.status
        return None if status is None else status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the members in each status, and those without data."""
        view: GroupView = self._runtime.view
        attributes: dict[str, Any] = {
            f"persons_{status.value}": list(view.by_status.get(status, ()))
            for status in STATUS_ORDER
        }
        attributes["missing_members"] = list(view.missing)
        return attributes


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
    """The highest near home of any member, 0-100 %; 100 % while anyone is home.

    Each member's near home uses their own radius and fade, so the member it
    shows is not always the one nearest.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _unrecorded_attributes = frozenset({"distance_m"})
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
            "distance_m": None if near is None else round(near.distance_m),
            "persons_home": list(self._runtime.view.persons_home),
        }
