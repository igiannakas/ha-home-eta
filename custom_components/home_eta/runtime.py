"""Runtime objects: one per config entry.

``PersonRuntime`` feeds a person's state changes into a ``PersonTracker`` and
publishes a ``PersonView`` to its entities. ``GroupRuntime`` watches the
person runtimes for one zone and picks the first arrival.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.zone import async_get_enclosing_zones
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_UNIT_OF_MEASUREMENT,
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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    ADVANCED_PARAMS,
    CONF_ACTIVITY_ENTITY,
    CONF_ADVANCED,
    CONF_ENABLE_HISTORICAL,
    CONF_IGNORED_ZONES,
    CONF_MEMBERS,
    CONF_NEAR_HOME_FADE_AFTER_MIN,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_REFERENCE_ETA_ENTITY,
    CONF_ZONE,
    DEFAULT_NEAR_HOME_FADE_AFTER_MIN,
    DEFAULT_NEAR_HOME_RADIUS_M,
    DIAGNOSTICS_COORDINATE_DECIMALS,
    DOMAIN,
    FIX_LOG_SIZE,
    NEAR_HOME_FADE_MIN,
    NEAR_HOME_FULL_M,
    SIGNAL_PERSON_UPDATED,
    STORE_VERSION,
    TICK_INTERVAL,
)
from .core.approach import ApproachState, Transition
from .core.homeward import (
    HomewardReason,
    HomewardRestore,
    NearHomeConfig,
    SpeedSource,
    near_home,
)
from .core.kinematic import Target
from .core.params import EstimatorParams
from .core.tracker import (
    PersonReport,
    PersonTracker,
    TrackerConfig,
    TrackerSnapshot,
    UpdateResult,
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


def near_home_config_from_options(options: Mapping[str, Any]) -> NearHomeConfig:
    """Build a group's near-home settings from its options."""
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


def friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return an entity's friendly name, or its id."""
    state = hass.states.get(entity_id)
    if state is not None and (name := state.attributes.get(ATTR_FRIENDLY_NAME)):
        return str(name)
    return entity_id


@dataclass(frozen=True, slots=True)
class PersonView:
    """What a person's entities publish."""

    available: bool
    snapshot: TrackerSnapshot | None
    threshold_min: float
    other_zone_names: tuple[str, ...] = ()
    activity: str | None = None
    reference_eta: str | None = None
    reference_eta_unit: str | None = None


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
        options = entry.options
        self.activity_entity: str | None = options.get(CONF_ACTIVITY_ENTITY)
        self.reference_entity: str | None = options.get(CONF_REFERENCE_ETA_ENTITY)
        self.historical_enabled = bool(options.get(CONF_ENABLE_HISTORICAL, False))
        try:
            params = params_from_options(options)
        except (TypeError, ValueError) as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="invalid_options",
                translation_placeholders={"error": str(err)},
            ) from err
        self.config = TrackerConfig(
            params,
            frozenset(options.get(CONF_IGNORED_ZONES) or ()),
            historical=self.historical_enabled,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, store_key(entry.entry_id)
        )
        self._tracker: PersonTracker | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._kinematics_enabled = False
        self._person_present: bool | None = None
        self._has_report = False
        """A usable report of the person has arrived (until then: no data)."""
        self._fix_log: deque[FixLogEntry] = deque(maxlen=FIX_LOG_SIZE)
        self._saves: set[asyncio.Task[None]] = set()
        self.view = PersonView(False, None, 0.0)

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
            model="Person ETA",
        )

    async def async_start(self) -> None:
        """Load history, build the tracker and subscribe to changes."""
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
        if not self.historical_enabled:
            # Learning is off: learned trips are personal location history,
            # so they are not kept.
            await self._store.async_remove()
        stored: object = (
            await self._store.async_load() if self.historical_enabled else None
        )
        if stored is not None and not isinstance(stored, Mapping):
            _LOGGER.warning(
                "%s: ignoring learned history in an unexpected format", self.person_id
            )
            stored = None

        person_state = hass.states.get(self.person_id)
        enclosing = self._enclosing_zones()
        report = self._report(person_state, enclosing)
        self._tracker = PersonTracker(
            self.config,
            target,
            stored,
            at_target=report is not None and report.in_target,
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

        watched = [self.person_id]
        self._unsubscribes.append(
            async_track_state_change_event(hass, watched, self._handle_person_event)
        )
        self._unsubscribes.append(
            async_track_state_change_event(
                hass, [self.zone_id], self._handle_zone_event
            )
        )
        extras = [e for e in (self.activity_entity, self.reference_entity) if e]
        if extras:
            self._unsubscribes.append(
                async_track_state_change_event(hass, extras, self._handle_extra_event)
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
        """Unsubscribe and finish pending history writes."""
        while self._unsubscribes:
            self._unsubscribes.pop()()
        persons = self.hass.data[DATA_KEY].persons
        if persons.get((self.person_id, self.zone_id)) is self:
            del persons[(self.person_id, self.zone_id)]
            self._signal_group()
        if self._saves:
            await asyncio.gather(*self._saves)

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
        """Start using fixes for speed once restored states are behind us."""
        self._kinematics_enabled = True

    @callback
    def _handle_person_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        self._set_person_present(new_state is not None)
        enclosing = self._enclosing_zones()
        report = self._report(new_state, enclosing)
        if report is not None and new_state is not None:
            self.tracker.set_rings(self._rings(enclosing))
            result = self.tracker.update(
                report, use_for_kinematics=self._kinematics_enabled
            )
            self._has_report = True
            self._log_update(new_state, report, result)
            if result.learned_samples:
                self._save_history()
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
            if self.tracker.set_target(target, event.time_fired.timestamp()):
                _LOGGER.info(
                    "%s: %s moved; learned trips were forgotten",
                    self.person_id,
                    self.zone_id,
                )
                self._save_history()
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
    def _handle_extra_event(self, _event: Event[EventStateChangedData]) -> None:
        self._refresh()

    @callback
    def _handle_tick(self, now: datetime) -> None:
        transition = self.tracker.tick(now.timestamp())
        if transition is not None:
            self._log_transition(transition)
        self._refresh(now)

    # -- controls ----------------------------------------------------------

    @callback
    def set_threshold(self, minutes: float) -> None:
        """Set the arriving-soon threshold (from the number entity)."""
        self.tracker.set_threshold(minutes * 60.0)
        transition = self.tracker.tick(dt_util.utcnow().timestamp())
        if transition is not None:
            self._log_transition(transition)
        self._refresh()

    @callback
    def restore_state(
        self,
        state: ApproachState | None,
        since: float,
        last_heard: float,
        homeward: HomewardRestore | None = None,
    ) -> None:
        """Restore the approach state and trip home saved before a restart.

        The approach state only when the person was heard from within the
        stale period: the silence before the restart counts, so a restored
        state can never outlive it, however often Home Assistant restarts or
        reloads. A trip home within the longest ETA; the overdue rule then
        ends it at once if it should have finished by now.
        """
        params = self.config.params
        now = dt_util.utcnow().timestamp()
        age = now - last_heard
        if age < -params.clock_step_back_s:
            return
        changed = False
        if (
            state is not None
            and age <= params.stale_after_s
            and self.tracker.restore_state(state, since, last_heard)
        ):
            _LOGGER.debug("%s: restored approach state %s", self.person_id, state)
            changed = True
        if (
            homeward is not None
            and age <= params.max_eta_s
            and self.tracker.restore_homeward(homeward, last_heard, now)
        ):
            _LOGGER.debug("%s: restored the trip home", self.person_id)
            changed = True
        if changed:
            self._refresh()

    async def async_reset_history(self) -> None:
        """Forget learned history (service call)."""
        historical = self.tracker.historical
        if historical is None:
            return
        historical.reset()
        await self._store.async_save(historical.as_dict())
        self._refresh()

    def export_history(self) -> dict[str, Any]:
        """Return learned history for the export service."""
        historical = self.tracker.historical
        return {
            "person": self.person_id,
            "zone": self.zone_id,
            "entry_id": self.entry.entry_id,
            "enabled": historical is not None,
            **(historical.export() if historical is not None else {}),
        }

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

    def _log_update(
        self, state: State, report: PersonReport, result: UpdateResult
    ) -> None:
        outcome = result.outcome
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
                "%s: fix %s, %.0f m from %s, sample %s m/s, filtered %s m/s",
                self.person_id,
                outcome.verdict,
                outcome.distance_m if outcome.distance_m is not None else -1.0,
                self.zone_id,
                _fmt(outcome.speed_sample_mps),
                _fmt(self.tracker.closing_speed_mps),
            )
        if result.transition is not None:
            self._log_transition(result.transition)
        if result.learned_samples:
            _LOGGER.debug(
                "%s: learned %d samples from the trip",
                self.person_id,
                result.learned_samples,
            )

    def _log_transition(self, transition: Transition) -> None:
        _LOGGER.debug(
            "%s: %s -> %s (%s)",
            self.person_id,
            transition.previous,
            transition.current,
            transition.reason,
        )

    @callback
    def _save_history(self) -> None:
        historical = self.tracker.historical
        if historical is None:
            return
        task = self.hass.async_create_task(
            self._store.async_save(historical.as_dict()),
            f"{DOMAIN} save history {self.person_id}",
        )
        self._saves.add(task)
        task.add_done_callback(self._saves.discard)

    @callback
    def _refresh(self, now: datetime | None = None) -> None:
        moment = (now or dt_util.utcnow()).timestamp()
        snapshot = self.tracker.snapshot(moment) if self._has_report else None
        self.view = PersonView(
            available=bool(self._person_present),
            snapshot=snapshot,
            threshold_min=self.tracker.threshold_s / 60.0,
            other_zone_names=()
            if snapshot is None
            else tuple(friendly_name(self.hass, zone) for zone in snapshot.other_zones),
            activity=self._entity_state(self.activity_entity),
            reference_eta=self._entity_state(self.reference_entity),
            reference_eta_unit=self._entity_unit(self.reference_entity),
        )
        for update in list(self._listeners):
            update()
        self._signal_group()

    @callback
    def _signal_group(self) -> None:
        async_dispatcher_send(
            self.hass, SIGNAL_PERSON_UPDATED.format(zone=self.zone_id)
        )

    def _entity_state(self, entity_id: str | None) -> str | None:
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state.state

    def _entity_unit(self, entity_id: str | None) -> str | None:
        if entity_id is None or (state := self.hass.states.get(entity_id)) is None:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        return None if unit is None else str(unit)

    def diagnostics(self) -> dict[str, Any]:
        """Return runtime internals with coordinates rounded."""
        decimals = DIAGNOSTICS_COORDINATE_DECIMALS
        return {
            "person": self.person_id,
            "zone": self.zone_id,
            "kinematics_enabled": self._kinematics_enabled,
            "person_present": self._person_present,
            "has_report": self._has_report,
            "params": {
                key: getattr(self.config.params, key) for key in ADVANCED_PARAMS
            },
            "ignored_zones": sorted(self.config.ignored_zones),
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
class Candidate:
    """A member who is on the way."""

    person_id: str
    name: str
    arrival: datetime
    minutes: int
    confidence: str | None
    state: ApproachState


@dataclass(frozen=True, slots=True)
class HomewardCandidate:
    """A member who is heading home."""

    person_id: str
    name: str
    arrival: datetime
    minutes: int
    reason: HomewardReason
    speed_source: SpeedSource | None
    distance_m: float | None


@dataclass(frozen=True, slots=True)
class NearHome:
    """How near home the nearest member is, and who that is."""

    value: float
    """0 (far, or staying somewhere for long) to 1 (home or close by)."""
    person_id: str
    name: str
    distance_m: float
    still_since: float | None


@dataclass(frozen=True, slots=True)
class GroupView:
    """What a group's entities publish."""

    candidates: tuple[Candidate, ...] = ()
    persons_home: tuple[str, ...] = ()
    arriving_soon: tuple[str, ...] = ()
    arriving_soon_names: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    reporting: int = 0
    """Members with data; with none, the group knows nothing."""
    heading_home: tuple[HomewardCandidate, ...] = ()
    """Members heading home, the one expected first leading."""
    near: NearHome | None = None
    """None when no member's whereabouts are known."""

    @property
    def first(self) -> Candidate | None:
        """Return the member due first."""
        return self.candidates[0] if self.candidates else None

    @property
    def expected_first(self) -> HomewardCandidate | None:
        """Return the member heading home who is expected first."""
        return self.heading_home[0] if self.heading_home else None


class GroupRuntime:
    """Pick the first arrival among a group's members."""

    def __init__(self, hass: HomeAssistant, entry: HomeEtaConfigEntry) -> None:
        """Read the entry."""
        self.hass = hass
        self.entry = entry
        self.zone_id: str = entry.data[CONF_ZONE]
        self.members: tuple[str, ...] = tuple(entry.options.get(CONF_MEMBERS) or ())
        try:
            self.near_config = near_home_config_from_options(entry.options)
        except (TypeError, ValueError) as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="invalid_options",
                translation_placeholders={"error": str(err)},
            ) from err
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
            model="First arrival",
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
        now = dt_util.utcnow()
        moment = now.timestamp()
        persons = self.hass.data[DATA_KEY].persons
        candidates: list[Candidate] = []
        heading: list[HomewardCandidate] = []
        home: list[str] = []
        home_names: list[str] = []
        soon: list[str] = []
        soon_names: list[str] = []
        missing: list[str] = []
        near: NearHome | None = None
        for person_id in self.members:
            runtime = persons.get((person_id, self.zone_id))
            if runtime is None or not runtime.view.available:
                missing.append(person_id)
                continue
            snapshot = runtime.view.snapshot
            if snapshot is None:
                missing.append(person_id)
                continue
            if snapshot.state is ApproachState.HOME:
                home.append(person_id)
                home_names.append(runtime.entry.title)
                continue
            homeward = snapshot.homeward
            if (
                homeward is not None
                and homeward.heading
                and homeward.arrival is not None
            ):
                arrival = dt_util.utc_from_timestamp(homeward.arrival)
                heading.append(
                    HomewardCandidate(
                        person_id=person_id,
                        name=runtime.entry.title,
                        arrival=arrival,
                        minutes=minutes_until(arrival, now),
                        reason=homeward.reason,
                        speed_source=homeward.speed_source,
                        distance_m=snapshot.distance_m,
                    )
                )
            distance = snapshot.distance_m
            if homeward is not None and distance is not None:
                value = near_home(
                    distance, homeward.still_since, moment, self.near_config
                )
                if near is None or (value, -distance) > (near.value, -near.distance_m):
                    near = NearHome(
                        value,
                        person_id,
                        runtime.entry.title,
                        distance,
                        homeward.still_since,
                    )
            if snapshot.state is ApproachState.ARRIVING_SOON:
                soon.append(person_id)
                soon_names.append(runtime.entry.title)
            estimate = snapshot.kinematic
            if snapshot.on_the_way and estimate is not None and not snapshot.stale:
                arrival = dt_util.utc_from_timestamp(estimate.arrival)
                candidates.append(
                    Candidate(
                        person_id=person_id,
                        name=runtime.entry.title,
                        arrival=arrival,
                        minutes=minutes_until(arrival, now),
                        confidence=estimate.confidence.value,
                        state=snapshot.state,
                    )
                )
        candidates.sort(key=lambda c: (c.arrival, c.person_id))
        heading.sort(key=lambda c: (c.arrival, c.person_id))
        if home:
            # Someone is home: as near as it gets.
            near = NearHome(1.0, home[0], home_names[0], 0.0, None)
        self.view = GroupView(
            candidates=tuple(candidates),
            persons_home=tuple(home),
            arriving_soon=tuple(soon),
            arriving_soon_names=tuple(soon_names),
            missing=tuple(missing),
            reporting=len(self.members) - len(missing),
            heading_home=tuple(heading),
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
            "candidates": [
                {
                    "person": c.person_id,
                    "arrival": c.arrival.isoformat(),
                    "minutes": c.minutes,
                    "confidence": c.confidence,
                    "state": c.state.value,
                }
                for c in view.candidates
            ],
            "persons_home": list(view.persons_home),
            "arriving_soon": list(view.arriving_soon),
            "missing": list(view.missing),
            "heading_home": [
                {
                    "person": c.person_id,
                    "arrival": c.arrival.isoformat(),
                    "minutes": c.minutes,
                    "reason": c.reason.value,
                    "speed_source": None
                    if c.speed_source is None
                    else c.speed_source.value,
                }
                for c in view.heading_home
            ],
            "near_home": None
            if view.near is None
            else {
                "value": round(view.near.value, 3),
                "person": view.near.person_id,
                "distance_m": round(view.near.distance_m),
                "still_since": view.near.still_since,
            },
            "near_home_config": {
                "radius_m": self.near_config.radius_m,
                "full_m": self.near_config.full_m,
                "fade_after_s": self.near_config.fade_after_s,
                "fade_s": self.near_config.fade_s,
            },
        }


def minutes_until(arrival: datetime, now: datetime) -> int:
    """Return whole minutes until ``arrival``, never negative."""
    return max(0, round((arrival - now).total_seconds() / 60.0))


def store_key(entry_id: str) -> str:
    """Return the storage key for an entry's learned history."""
    return f"{DOMAIN}.history.{entry_id}"


def _round(value: float | None, decimals: int) -> float | None:
    return None if value is None else round(value, decimals)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
