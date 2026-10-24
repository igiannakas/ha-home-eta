"""Runtime objects: one per config entry.

``PersonRuntime`` feeds a person's state changes into a ``PersonTracker`` and
publishes a ``PersonView`` to its entities. ``GroupRuntime`` watches the
person runtimes for one zone and combines them.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.zone import async_get_enclosing_zones
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    ADVANCED_PARAMS,
    CONF_ADVANCED,
    CONF_MEMBERS,
    CONF_NEAR_HOME_FADE_AFTER_MIN,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_ZONE,
    DEFAULT_NEAR_HOME_FADE_AFTER_MIN,
    DEFAULT_NEAR_HOME_RADIUS_M,
    DIAGNOSTICS_COORDINATE_DECIMALS,
    DOMAIN,
    FIX_LOG_SIZE,
    NEAR_HOME_FADE_MIN,
    NEAR_HOME_FULL_M,
    SIGNAL_PERSON_UPDATED,
    TICK_INTERVAL,
)
from .core.homeward import HomewardRestore, NearHomeConfig
from .core.kinematic import FixOutcome, Target
from .core.params import EstimatorParams
from .core.tracker import (
    STATUS_ORDER,
    PersonReport,
    PersonStatus,
    PersonTracker,
    TrackerConfig,
    TrackerSnapshot,
    build_report,
)

if TYPE_CHECKING:
    from . import HomeEtaConfigEntry

_LOGGER = logging.getLogger(__name__)

ATTR_RADIUS = "radius"


@dataclass(slots=True)
class HomeEtaData:
    """Domain-wide registry of loaded person runtimes."""

    persons: dict[tuple[str, str], PersonRuntime] = field(default_factory=dict)
    """Keyed by (person entity id, zone entity id)."""


DATA_KEY: HassKey[HomeEtaData] = HassKey(DOMAIN)


def params_from_options(options: Mapping[str, Any]) -> EstimatorParams:
    """Build estimator parameters from an entry's advanced options."""
    advanced = options.get(CONF_ADVANCED) or {}
    overrides: dict[str, Any] = {
        key: float(advanced[key]) for key in ADVANCED_PARAMS if key in advanced
    }
    return EstimatorParams(**overrides)


def near_home_config_from_options(options: Mapping[str, Any]) -> NearHomeConfig:
    """Build a person's near-home settings from their options."""
    radius = float(options.get(CONF_NEAR_HOME_RADIUS_M, DEFAULT_NEAR_HOME_RADIUS_M))
    fade_after = float(
        options.get(CONF_NEAR_HOME_FADE_AFTER_MIN, DEFAULT_NEAR_HOME_FADE_AFTER_MIN)
    )
    return NearHomeConfig(
        radius_m=radius,
        full_m=NEAR_HOME_FULL_M,
        fade_after_s=None if fade_after == 0 else fade_after * 60.0,
        fade_s=NEAR_HOME_FADE_MIN * 60.0,
    )


def target_from_state(state: State | None) -> Target | None:
    """Return the target described by a zone state, if usable."""
    if state is None or state.state == STATE_UNAVAILABLE:
        return None
    try:
        latitude = float(state.attributes[ATTR_LATITUDE])
        longitude = float(state.attributes[ATTR_LONGITUDE])
        radius = float(state.attributes[ATTR_RADIUS])
    except KeyError, TypeError, ValueError:
        return None
    return Target(latitude, longitude, radius)


def friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return an entity's friendly name, or its id."""
    state = hass.states.get(entity_id)
    if state is not None and (name := state.attributes.get(ATTR_FRIENDLY_NAME)):
        return str(name)
    return entity_id


def minutes_until(arrival: float, now: float) -> int:
    """Return whole minutes from ``now`` until ``arrival``, never negative."""
    return max(0, round((arrival - now) / 60.0))


@dataclass(frozen=True, slots=True)
class PersonView:
    """What a person's entities publish."""

    available: bool
    snapshot: TrackerSnapshot | None
    """None until the person's whereabouts are first known."""

    @property
    def expected_home(self) -> datetime | None:
        """Return the expected arrival while heading home."""
        snap = self.snapshot
        if snap is None or (arrival := snap.expected_home) is None:
            return None
        return dt_util.utc_from_timestamp(arrival)

    @property
    def minutes_to_home(self) -> int | None:
        """Return whole minutes until the expected arrival while heading home."""
        snap = self.snapshot
        if snap is None or (arrival := snap.expected_home) is None:
            return None
        return minutes_until(arrival, snap.now)


@dataclass(frozen=True, slots=True)
class FixLogEntry:
    """One processed fix, for diagnostics."""

    at: float
    verdict: str | None
    latitude: float | None
    longitude: float | None
    accuracy_m: float | None
    state: str


class PersonRuntime:
    """Track one person for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: HomeEtaConfigEntry) -> None:
        """Read the entry; nothing is subscribed until ``async_start``."""
        self.hass = hass
        self.entry = entry
        self.person_id: str = entry.data[CONF_PERSON]
        self.zone_id: str = entry.data[CONF_ZONE]
        try:
            self.config = TrackerConfig(
                params_from_options(entry.options),
                near_home_config_from_options(entry.options),
            )
        except (TypeError, ValueError) as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="invalid_options",
                translation_placeholders={"error": str(err)},
            ) from err
        self._tracker: PersonTracker | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._kinematics_enabled = False
        self._person_present: bool | None = None
        self._has_report = False
        """A usable report of the person has arrived (until then: no data)."""
        self._fix_log: deque[FixLogEntry] = deque(maxlen=FIX_LOG_SIZE)
        self.view = PersonView(False, None)

    # -- lifecycle -------------------------------------------------------

    @property
    def tracker(self) -> PersonTracker:
        """Return the tracker (only valid after ``async_start``)."""
        assert self._tracker is not None
        return self._tracker

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device that groups this person's entities."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            entry_type=DeviceEntryType.SERVICE,
            model="Person",
        )

    async def async_start(self) -> None:
        """Build the tracker and subscribe to changes."""
        hass = self.hass
        target = target_from_state(hass.states.get(self.zone_id))
        if target is None:
            # Zones are set up before this integration, so a zone missing now
            # was deleted: say so, and keep retrying in case it comes back.
            self._create_zone_issue()
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="zone_not_ready",
                translation_placeholders={"zone": self.zone_id},
            )

        person_state = hass.states.get(self.person_id)
        enclosing = self._enclosing_zones()
        report = self._report(person_state, enclosing)
        self._tracker = PersonTracker(
            self.config, target, at_target=report is not None and report.in_target
        )
        self._tracker.set_rings(self._rings(enclosing))
        if report is not None:
            # While Home Assistant starts, states carry the restore time, not
            # the time of the fix; after a reload they are genuine.
            self._tracker.update(
                report, use_for_kinematics=hass.state is CoreState.running
            )
            self._has_report = True
        self._set_person_present(person_state is not None)
        # The zone is valid now; clear a warning left from an earlier run.
        ir.async_delete_issue(hass, DOMAIN, f"zone_missing_{self.entry.entry_id}")

        self._unsubscribes.append(
            async_track_state_change_event(
                hass, [self.person_id], self._handle_person_event
            )
        )
        self._unsubscribes.append(
            async_track_state_change_event(
                hass, [self.zone_id], self._handle_zone_event
            )
        )
        self._unsubscribes.append(
            async_track_time_interval(
                hass,
                self._handle_tick,
                TICK_INTERVAL,
                name=f"{DOMAIN} tick {self.person_id}",
                cancel_on_shutdown=True,
            )
        )
        self._unsubscribes.append(async_at_started(hass, self._handle_started))
        hass.data[DATA_KEY].persons[(self.person_id, self.zone_id)] = self
        self._refresh()

    async def async_stop(self) -> None:
        """Unsubscribe and leave the registry of loaded people."""
        while self._unsubscribes:
            self._unsubscribes.pop()()
        persons = self.hass.data[DATA_KEY].persons
        if persons.get((self.person_id, self.zone_id)) is self:
            del persons[(self.person_id, self.zone_id)]
            self._signal_group()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update`` whenever the view changes; return a remover."""
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    # -- events ----------------------------------------------------------

    @callback
    def _handle_started(self, _hass: HomeAssistant) -> None:
        """Start using fixes once restored states are behind us."""
        self._kinematics_enabled = True

    @callback
    def _handle_person_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        self._set_person_present(new_state is not None)
        enclosing = self._enclosing_zones()
        report = self._report(new_state, enclosing)
        if report is not None and new_state is not None:
            self.tracker.set_rings(self._rings(enclosing))
            outcome = self.tracker.update(
                report, use_for_kinematics=self._kinematics_enabled
            )
            self._has_report = True
            self._log_fix(new_state, report, outcome)
        self._refresh()

    @callback
    def _handle_zone_event(self, event: Event[EventStateChangedData]) -> None:
        target = target_from_state(event.data["new_state"])
        if target is None:
            self._create_zone_issue()
            return
        ir.async_delete_issue(self.hass, DOMAIN, f"zone_missing_{self.entry.entry_id}")
        if target != self.tracker.target:
            _LOGGER.debug("%s: target zone %s changed", self.person_id, self.zone_id)
            self.tracker.set_target(target, event.time_fired.timestamp())
            self._refresh()

    @callback
    def _create_zone_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"zone_missing_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="zone_missing",
            translation_placeholders={"zone": self.zone_id, "entry": self.entry.title},
        )

    @callback
    def _handle_tick(self, now: datetime) -> None:
        self.tracker.tick(now.timestamp())
        self._refresh(now)

    # -- controls ----------------------------------------------------------

    @callback
    def set_threshold(self, minutes: float, *, saved: bool = False) -> None:
        """Set the arriving-soon threshold (from the number entity).

        ``saved`` when it is the value saved before a restart or reload,
        rather than a change: an arriving soon restored with the trip home
        was judged with that value and holds.
        """
        self.tracker.set_threshold(minutes * 60.0, saved=saved)
        self.tracker.tick(dt_util.utcnow().timestamp())
        self._refresh()

    @callback
    def restore_homeward(self, data: HomewardRestore) -> None:
        """Restore the trip home and stay saved before a restart or reload.

        A trip home only if the person was heard from within the longest trip
        home; the overdue rule then ends it at once if it should have finished
        by now. A stay whenever: the next fix elsewhere ends it.
        """
        params = self.config.params
        now = dt_util.utcnow().timestamp()
        age = now - data.last_heard
        if age < -params.clock_step_back_s:
            # Saved "in the future": the clock was wrong then or is now.
            return
        if age > params.max_eta_s:
            data = replace(data, trip=None)
        if data.trip is None and data.stay is None:
            return
        if self.tracker.restore_homeward(data, data.last_heard, now):
            _LOGGER.debug("%s: restored the trip home or stay", self.person_id)
            self._refresh()

    # -- internals ---------------------------------------------------------

    def _enclosing_zones(self) -> list[str]:
        """Return the zones that contain the target (rings around home)."""
        return async_get_enclosing_zones(self.hass, self.zone_id)

    def _rings(self, enclosing: list[str]) -> tuple[Target, ...]:
        """Return the shapes of the enclosing zones."""
        rings = (target_from_state(self.hass.states.get(zone)) for zone in enclosing)
        return tuple(ring for ring in rings if ring is not None)

    def _report(self, state: State | None, enclosing: list[str]) -> PersonReport | None:
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return build_report(
            state.state,
            state.attributes,
            state.last_updated.timestamp(),
            self.zone_id,
            friendly_name(self.hass, self.zone_id),
            enclosing_zones=enclosing,
        )

    @callback
    def _set_person_present(self, present: bool) -> None:
        if present == self._person_present:
            return
        self._person_present = present
        issue_id = f"person_missing_{self.entry.entry_id}"
        if present:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="person_missing",
                translation_placeholders={
                    "person": self.person_id,
                    "entry": self.entry.title,
                },
            )

    def _log_fix(
        self, state: State, report: PersonReport, outcome: FixOutcome | None
    ) -> None:
        self._fix_log.append(
            FixLogEntry(
                at=report.timestamp,
                verdict=None if outcome is None else outcome.verdict.value,
                latitude=report.latitude,
                longitude=report.longitude,
                accuracy_m=report.accuracy_m,
                state=state.state,
            )
        )
        if outcome is not None:
            _LOGGER.debug(
                "%s: fix %s, %s m from %s",
                self.person_id,
                outcome.verdict,
                "n/a" if outcome.distance_m is None else f"{outcome.distance_m:.0f}",
                self.zone_id,
            )

    @callback
    def _refresh(self, now: datetime | None = None) -> None:
        moment = (now or dt_util.utcnow()).timestamp()
        snapshot = self.tracker.snapshot(moment) if self._has_report else None
        self._log_changes(self.view.snapshot, snapshot)
        self.view = PersonView(
            available=bool(self._person_present),
            snapshot=snapshot,
        )
        for update in list(self._listeners):
            update()
        self._signal_group()

    def _log_changes(
        self, before: TrackerSnapshot | None, after: TrackerSnapshot | None
    ) -> None:
        if after is None or (before is not None and before.status is after.status):
            return
        _LOGGER.debug(
            "%s: %s (%s)", self.person_id, after.status, after.homeward.reason
        )

    @callback
    def _signal_group(self) -> None:
        async_dispatcher_send(
            self.hass, SIGNAL_PERSON_UPDATED.format(zone=self.zone_id)
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return runtime internals with coordinates rounded."""
        decimals = DIAGNOSTICS_COORDINATE_DECIMALS
        near = self.config.near_home
        return {
            "person": self.person_id,
            "zone": self.zone_id,
            "kinematics_enabled": self._kinematics_enabled,
            "person_present": self._person_present,
            "has_report": self._has_report,
            "params": {
                key: getattr(self.config.params, key) for key in ADVANCED_PARAMS
            },
            "near_home": {
                "radius_m": near.radius_m,
                "full_m": near.full_m,
                "fade_after_s": near.fade_after_s,
                "fade_s": near.fade_s,
            },
            "tracker": self.tracker.diagnostics(),
            "recent_fixes": [
                {
                    "at": dt_util.utc_from_timestamp(entry.at).isoformat(),
                    "verdict": entry.verdict,
                    "latitude": _round(entry.latitude, decimals),
                    "longitude": _round(entry.longitude, decimals),
                    "accuracy_m": entry.accuracy_m,
                    "state": entry.state,
                }
                for entry in self._fix_log
            ],
        }


@dataclass(frozen=True, slots=True)
class HeadingHome:
    """A member who is heading home."""

    person_id: str
    name: str
    arrival: datetime
    minutes: int


@dataclass(frozen=True, slots=True)
class NearHome:
    """How near home the nearest member is, and who that is."""

    value: float
    """0 (far, or staying somewhere for long) to 1 (home or close by)."""
    person_id: str
    name: str
    distance_m: float


@dataclass(frozen=True, slots=True)
class GroupView:
    """What a group's entities publish."""

    heading_home: tuple[HeadingHome, ...] = ()
    """Members heading home, the one expected first leading."""
    by_status: Mapping[PersonStatus, tuple[str, ...]] = field(default_factory=dict)
    """Members with data, by status."""
    arriving_soon_names: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    near: NearHome | None = None
    """None when no member's whereabouts are known."""

    @property
    def first(self) -> HeadingHome | None:
        """Return the member heading home who is expected first."""
        return self.heading_home[0] if self.heading_home else None

    @property
    def reporting(self) -> int:
        """Return how many members have data; with none, the group knows nothing."""
        return sum(len(members) for members in self.by_status.values())

    @property
    def persons_home(self) -> tuple[str, ...]:
        """Return the members at home."""
        return self.by_status.get(PersonStatus.HOME, ())

    @property
    def arriving_soon(self) -> tuple[str, ...]:
        """Return the members arriving soon."""
        return self.by_status.get(PersonStatus.ARRIVING_SOON, ())

    @property
    def status(self) -> PersonStatus | None:
        """Return the most-home status of any member, None with no data."""
        return next(
            (status for status in STATUS_ORDER if self.by_status.get(status)), None
        )


class GroupRuntime:
    """Combine the members of a group heading to one zone."""

    def __init__(self, hass: HomeAssistant, entry: HomeEtaConfigEntry) -> None:
        """Read the entry."""
        self.hass = hass
        self.entry = entry
        self.zone_id: str = entry.data[CONF_ZONE]
        self.members: tuple[str, ...] = tuple(entry.options.get(CONF_MEMBERS) or ())
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: Callable[[], None] | None = None
        self.view = GroupView()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device that groups this group's entities."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            entry_type=DeviceEntryType.SERVICE,
            model="Group",
        )

    async def async_start(self) -> None:
        """Subscribe to member updates."""
        issue_id = f"group_no_members_{self.entry.entry_id}"
        if self.members:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            # Everyone in it was removed: the group can only say "unknown".
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="group_no_members",
                translation_placeholders={"entry": self.entry.title},
            )
        self._unsubscribe = async_dispatcher_connect(
            self.hass, SIGNAL_PERSON_UPDATED.format(zone=self.zone_id), self._refresh
        )
        self._refresh()

    async def async_stop(self) -> None:
        """Unsubscribe."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update`` whenever the view changes; return a remover."""
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _refresh(self) -> None:
        persons = self.hass.data[DATA_KEY].persons
        heading: list[HeadingHome] = []
        by_status: dict[PersonStatus, list[str]] = {}
        soon_names: list[str] = []
        missing: list[str] = []
        near: NearHome | None = None
        for person_id in self.members:
            runtime = persons.get((person_id, self.zone_id))
            if runtime is None or not runtime.view.available:
                missing.append(person_id)
                continue
            view = runtime.view
            snapshot = view.snapshot
            if snapshot is None:
                missing.append(person_id)
                continue
            name = runtime.entry.title
            status = snapshot.status
            by_status.setdefault(status, []).append(person_id)
            expected, minutes = view.expected_home, view.minutes_to_home
            if expected is not None and minutes is not None:
                heading.append(HeadingHome(person_id, name, expected, minutes))
            if status is PersonStatus.ARRIVING_SOON:
                soon_names.append(name)
            value, distance = snapshot.near_home, snapshot.distance_m
            if (
                value is not None
                and distance is not None
                and (
                    near is None or (value, -distance) > (near.value, -near.distance_m)
                )
            ):
                near = NearHome(value, person_id, name, distance)
        heading.sort(key=lambda member: (member.arrival, member.person_id))
        self.view = GroupView(
            heading_home=tuple(heading),
            by_status={status: tuple(ids) for status, ids in by_status.items()},
            arriving_soon_names=tuple(soon_names),
            missing=tuple(missing),
            near=near,
        )
        for update in list(self._listeners):
            update()

    def diagnostics(self) -> dict[str, Any]:
        """Return the group's state."""
        view = self.view
        return {
            "zone": self.zone_id,
            "members": list(self.members),
            "heading_home": [
                {
                    "person": member.person_id,
                    "arrival": member.arrival.isoformat(),
                    "minutes": member.minutes,
                }
                for member in view.heading_home
            ],
            "status": None if view.status is None else view.status.value,
            "by_status": {
                status.value: list(view.by_status[status])
                for status in STATUS_ORDER
                if status in view.by_status
            },
            "missing": list(view.missing),
            "near_home": None
            if view.near is None
            else {
                "value": round(view.near.value, 3),
                "person": view.near.person_id,
                "distance_m": round(view.near.distance_m),
            },
        }


def _round(value: float | None, decimals: int) -> float | None:
    return None if value is None else round(value, decimals)
