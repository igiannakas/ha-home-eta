"""Sensors for Home ETA."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)
from homeassistant.util import dt as dt_util

from . import HomeEtaConfigEntry
from .const import (
    DOMAIN,
    KEY_APPROACH_STATE,
    KEY_CLOSING_SPEED,
    KEY_CONFIDENCE,
    KEY_DISTANCE,
    KEY_ETA_HISTORICAL,
    KEY_ETA_KINEMATIC,
    KEY_EXPECTED_HOME,
    KEY_FIRST_ARRIVAL,
    KEY_FIRST_MINUTES,
    KEY_FIRST_PERSON,
    KEY_MINUTES,
    KEY_NEAR_HOME,
)
from .core.approach import ApproachState
from .core.geo import is_valid_coordinate
from .core.homeward import HomewardRestore, HomewardStay, HomewardTrip
from .core.kinematic import Confidence
from .entity import GroupEntity, PersonEntity
from .runtime import GroupRuntime, GroupView, PersonRuntime, PersonView

PARALLEL_UPDATES = 0

RESTORABLE_STATES = frozenset(
    {ApproachState.APPROACHING, ApproachState.ARRIVING_SOON, ApproachState.STATIONARY}
)


def _ts(value: float | None) -> datetime | None:
    return None if value is None else dt_util.utc_from_timestamp(value)


def _iso(value: float | None) -> str | None:
    return None if value is None else dt_util.utc_from_timestamp(value).isoformat()


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _whole(value: float | None) -> int | None:
    return None if value is None else round(value)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _homeward_restore(value: object) -> HomewardRestore | None:
    """Return a saved trip home and stay, if the saved data is usable.

    The trip is stored flat (``since``, ``farthest_m``, ``closest_m``), as
    1.1.0 saved it, beside ``last_heard`` and a ``stay``.
    """
    if not isinstance(value, dict):
        return None
    trip: HomewardTrip | None = None
    since = _number(value.get("since"))
    farthest = _number(value.get("farthest_m"))
    closest = _number(value.get("closest_m"))
    if (
        since is not None
        and farthest is not None
        and closest is not None
        and farthest >= 0
        and closest >= 0
    ):
        trip = HomewardTrip(since, farthest, closest)
    stay: HomewardStay | None = None
    saved_stay = value.get("stay")
    if isinstance(saved_stay, dict):
        stay_since = _number(saved_stay.get("since"))
        latitude = _number(saved_stay.get("latitude"))
        longitude = _number(saved_stay.get("longitude"))
        distance = _number(saved_stay.get("distance_m"))
        if (
            stay_since is not None
            and latitude is not None
            and longitude is not None
            and distance is not None
            and is_valid_coordinate(latitude, longitude)
            and distance >= 0
        ):
            stay = HomewardStay(stay_since, latitude, longitude, distance)
    if trip is None and stay is None:
        return None
    return HomewardRestore(_number(value.get("last_heard")), trip, stay)


@dataclass(frozen=True, kw_only=True)
class PersonSensorDescription(SensorEntityDescription):
    """Describe a person sensor."""

    value_fn: Callable[[PersonView], datetime | float | int | str | None]
    attrs_fn: Callable[[PersonView], dict[str, Any]] | None = None


def _kinematic_value(view: PersonView) -> datetime | None:
    snap = view.snapshot
    if snap is None or snap.kinematic is None:
        return None
    return _ts(snap.kinematic.arrival)


def _kinematic_attrs(view: PersonView) -> dict[str, Any]:
    snap = view.snapshot
    attrs: dict[str, Any] = {"method": "kinematic"}
    if snap is not None:
        estimate = snap.kinematic
        last = snap.last_kinematic
        attrs.update(
            {
                "eta_low": None if estimate is None else _iso(estimate.earliest),
                "eta_high": None if estimate is None else _iso(estimate.latest),
                "closing_speed_mps": _round(snap.closing_speed_mps, 2),
                "distance_m": _whole(snap.distance_m),
                "samples": 0 if estimate is None else estimate.samples,
                "confidence": None
                if snap.confidence is None
                else snap.confidence.value,
                "stale": snap.stale,
                "last_fix_at": _iso(snap.last_fix_time),
                "last_eta": None if last is None else _iso(last.arrival),
            }
        )
    if view.reference_eta is not None:
        attrs["reference_eta"] = view.reference_eta
        attrs["reference_eta_unit"] = view.reference_eta_unit
    if view.activity is not None:
        attrs["activity"] = view.activity
    return attrs


def _historical_value(view: PersonView) -> datetime | None:
    snap = view.snapshot
    if snap is None or snap.historical is None:
        return None
    return _ts(snap.historical.arrival)


def _historical_attrs(view: PersonView) -> dict[str, Any]:
    snap = view.snapshot
    attrs: dict[str, Any] = {"method": "historical"}
    if snap is None:
        return attrs
    estimate = snap.historical
    attrs.update(
        {
            "eta_low": None if estimate is None else _iso(estimate.earliest),
            "eta_high": None if estimate is None else _iso(estimate.latest),
            "trips": snap.historical_trips,
            "trips_learned": snap.trips_learned,
            "last_trip_at": _iso(snap.last_trip_at),
        }
    )
    return attrs


def _minutes_value(view: PersonView) -> int | None:
    snap = view.snapshot
    if snap is None or snap.eta_seconds is None:
        return None
    return round(snap.eta_seconds / 60.0)


def _distance_value(view: PersonView) -> int | None:
    return None if view.snapshot is None else _whole(view.snapshot.distance_m)


def _approach_attrs(view: PersonView) -> dict[str, Any]:
    snap = view.snapshot
    if snap is None:
        return {}
    attrs: dict[str, Any] = {
        "direction": snap.direction.value,
        "reason": snap.reason.value,
        "since": _iso(snap.state_since),
        "zones": list(view.other_zone_names),
        "stale": snap.stale,
        "trip_started_at": _iso(snap.trip_started_at),
    }
    if view.activity is not None:
        attrs["activity"] = view.activity
    return attrs


PERSON_SENSORS: tuple[PersonSensorDescription, ...] = (
    PersonSensorDescription(
        key=KEY_ETA_KINEMATIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_kinematic_value,
        attrs_fn=_kinematic_attrs,
    ),
    PersonSensorDescription(
        key=KEY_MINUTES,
        # A countdown, not a measurement: no long-term statistics.
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=_minutes_value,
    ),
    PersonSensorDescription(
        key=KEY_DISTANCE,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=0,
        value_fn=_distance_value,
    ),
    PersonSensorDescription(
        key=KEY_CLOSING_SPEED,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda view: (
            None
            if view.snapshot is None
            else _round(view.snapshot.closing_speed_mps, 2)
        ),
    ),
    PersonSensorDescription(
        key=KEY_CONFIDENCE,
        device_class=SensorDeviceClass.ENUM,
        options=[c.value for c in Confidence],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda view: (
            None
            if view.snapshot is None or view.snapshot.confidence is None
            else view.snapshot.confidence.value
        ),
    ),
)

HISTORICAL_SENSOR = PersonSensorDescription(
    key=KEY_ETA_HISTORICAL,
    device_class=SensorDeviceClass.TIMESTAMP,
    value_fn=_historical_value,
    attrs_fn=_historical_attrs,
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
                FirstArrivalSensor(runtime),
                FirstArrivalPersonSensor(runtime),
                FirstArrivalMinutesSensor(runtime),
                ExpectedHomeSensor(runtime),
                NearHomeSensor(runtime),
            ]
        )
        return

    entities: list[SensorEntity] = [
        PersonSensor(runtime, description) for description in PERSON_SENSORS
    ]
    entities.append(ApproachStateSensor(runtime))
    historical_id = f"{entry.entry_id}_{KEY_ETA_HISTORICAL}"
    if runtime.historical_enabled:
        entities.append(PersonSensor(runtime, HISTORICAL_SENSOR))
    else:
        # Turning history off removes its sensor rather than leaving it orphaned.
        registry = er.async_get(hass)
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, historical_id):
            registry.async_remove(entity_id)
    async_add_entities(entities)


class PersonSensor(PersonEntity, SensorEntity):
    """A sensor computed from a person's view."""

    entity_description: PersonSensorDescription
    _unrecorded_attributes = frozenset(
        {"reference_eta", "reference_eta_unit", "activity", "last_eta"}
    )

    def __init__(
        self, runtime: PersonRuntime, description: PersonSensorDescription
    ) -> None:
        """Initialise."""
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> datetime | float | int | str | None:
        """Return the value."""
        return self.entity_description.value_fn(self._runtime.view)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the attributes."""
        attrs_fn = self.entity_description.attrs_fn
        return None if attrs_fn is None else attrs_fn(self._runtime.view)


class ApproachStateSensor(PersonEntity, SensorEntity, RestoreEntity):
    """The person's approach state, restored across restarts."""

    _attr_device_class = SensorDeviceClass.ENUM
    _unrecorded_attributes = frozenset({"activity", "since"})

    def __init__(self, runtime: PersonRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_APPROACH_STATE)
        self._attr_options = [state.value for state in ApproachState]

    async def async_added_to_hass(self) -> None:
        """Restore the state saved at shutdown if it is still fresh."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is None:
            return
        data = extra.as_dict()
        state = data.get("state")
        homeward = _homeward_restore(data.get("homeward"))
        since, heard = _number(data.get("since")), _number(data.get("last_heard"))
        if heard is None and homeward is not None:
            # Restored after a restart and not heard from since: the trip's
            # own record of the last report.
            heard = homeward.last_heard
        if heard is None:
            return
        approach = (
            ApproachState(state)
            if isinstance(state, str) and state in {s.value for s in RESTORABLE_STATES}
            else None
        )
        if approach is None and homeward is None:
            return
        self._runtime.restore_state(
            approach, heard if since is None else since, heard, homeward
        )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Save the state, when it began, the trip home and the last report."""
        snap = self._runtime.view.snapshot
        homeward = self._runtime.tracker.homeward.restore_data()
        saved: dict[str, Any] | None = None
        if homeward is not None:
            saved = {"last_heard": homeward.last_heard}
            if (trip := homeward.trip) is not None:
                saved.update(
                    since=trip.since,
                    farthest_m=trip.farthest_m,
                    closest_m=trip.closest_m,
                )
            if (stay := homeward.stay) is not None:
                saved["stay"] = {
                    "since": stay.since,
                    "latitude": stay.latitude,
                    "longitude": stay.longitude,
                    "distance_m": stay.distance_m,
                }
        return RestoredExtraData(
            {
                "state": None if snap is None else snap.state.value,
                "since": None if snap is None else snap.state_since,
                "last_heard": None if snap is None else snap.last_heard,
                "homeward": saved,
            }
        )

    @property
    def native_value(self) -> str | None:
        """Return the state."""
        snap = self._runtime.view.snapshot
        return None if snap is None else snap.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes."""
        return _approach_attrs(self._runtime.view)


class FirstArrivalSensor(GroupEntity, SensorEntity):
    """When the first member is due."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"candidates"})

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_FIRST_ARRIVAL)

    @property
    def native_value(self) -> datetime | None:
        """Return the first arrival time."""
        first = self._runtime.view.first
        return None if first is None else first.arrival

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes."""
        view: GroupView = self._runtime.view
        first = view.first
        return {
            "person": None if first is None else first.person_id,
            "person_name": None if first is None else first.name,
            "confidence": None if first is None else first.confidence,
            "candidates": [
                {
                    "person": c.person_id,
                    "name": c.name,
                    "eta": c.arrival.isoformat(),
                    "minutes": c.minutes,
                    "confidence": c.confidence,
                    "state": c.state.value,
                }
                for c in view.candidates
            ],
            "persons_home": list(view.persons_home),
            "missing_members": list(view.missing),
        }


class FirstArrivalPersonSensor(GroupEntity, SensorEntity):
    """Who is due first."""

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_FIRST_PERSON)

    @property
    def native_value(self) -> str | None:
        """Return the name of the member due first."""
        first = self._runtime.view.first
        return None if first is None else first.name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the member's entity id."""
        first = self._runtime.view.first
        return {"person_entity_id": None if first is None else first.person_id}


class FirstArrivalMinutesSensor(GroupEntity, SensorEntity):
    """Minutes until the first member is due."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_FIRST_MINUTES)

    @property
    def native_value(self) -> int | None:
        """Return the minutes."""
        first = self._runtime.view.first
        return None if first is None else first.minutes


class ExpectedHomeSensor(GroupEntity, SensorEntity):
    """When the first member heading home is expected.

    Unknown while nobody is heading home. Meant for warming the house: each
    room can start once this is closer than the time it takes to heat up.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"minutes", "heading_home", "distance_m"})

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_EXPECTED_HOME)

    @property
    def native_value(self) -> datetime | None:
        """Return the expected arrival of whoever is due first."""
        first = self._runtime.view.expected_first
        return None if first is None else first.arrival

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who, how the time was worked out, and everyone heading home."""
        view: GroupView = self._runtime.view
        first = view.expected_first
        return {
            "person": None if first is None else first.person_id,
            "person_name": None if first is None else first.name,
            "minutes": None if first is None else first.minutes,
            "reason": None if first is None else first.reason.value,
            "speed_source": None
            if first is None or first.speed_source is None
            else first.speed_source.value,
            "distance_m": None if first is None else _whole(first.distance_m),
            "heading_home": [
                {
                    "person": c.person_id,
                    "name": c.name,
                    "expected_at": c.arrival.isoformat(),
                    "minutes": c.minutes,
                    "reason": c.reason.value,
                    "speed_source": None
                    if c.speed_source is None
                    else c.speed_source.value,
                }
                for c in view.heading_home
            ],
            "persons_home": list(view.persons_home),
            "missing_members": list(view.missing),
        }


class NearHomeSensor(GroupEntity, SensorEntity):
    """How near home the nearest member is, 0-100 %, in any direction.

    100 % when someone is home or within 800 m, falling to 0 % at the
    group's near-home radius. A long stay at one place fades it back to 0 %.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _unrecorded_attributes = frozenset({"distance_m"})

    def __init__(self, runtime: GroupRuntime) -> None:
        """Initialise."""
        super().__init__(runtime, KEY_NEAR_HOME)

    @property
    def native_value(self) -> int | None:
        """Return the percentage, None when nobody's whereabouts are known."""
        near = self._runtime.view.near
        return None if near is None else round(near.value * 100.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who is nearest and the settings in use."""
        near = self._runtime.view.near
        config = self._runtime.near_config
        fade_after = config.fade_after_s
        return {
            "person": None if near is None else near.person_id,
            "person_name": None if near is None else near.name,
            "distance_m": None if near is None else _whole(near.distance_m),
            "staying_since": None if near is None else _iso(near.still_since),
            "radius_m": round(config.radius_m),
            "fade_after_min": None if fade_after is None else round(fade_after / 60.0),
            "persons_home": list(self._runtime.view.persons_home),
        }
