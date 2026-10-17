"""One person's tracking: zone membership, estimators and approach state.

This is everything between "the person's state changed" and "here are the
numbers to publish", kept free of Home Assistant so the integration and the
backtest replay exactly the same logic.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
import math
from typing import Any

from .approach import (
    ON_THE_WAY,
    ApproachMachine,
    ApproachState,
    MachineState,
    Observation,
    Reason,
    Transition,
)
from .geo import haversine_m, is_valid_coordinate
from .historical import HistoricalEstimate, HistoricalEstimator
from .kinematic import (
    Confidence,
    Fix,
    FixOutcome,
    FixVerdict,
    KinematicEstimate,
    KinematicEstimator,
    Target,
)
from .params import DEFAULT_ARRIVING_SOON_MIN, EstimatorParams


class Direction(StrEnum):
    """Which way the person is heading relative to the target."""

    TOWARDS = "towards"
    AWAY_FROM = "away_from"
    STATIONARY = "stationary"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PersonReport:
    """A person's state as reported by Home Assistant (or a recorder export)."""

    timestamp: float
    """When the state was last updated, UTC epoch seconds."""
    in_target: bool
    """Inside the target zone."""
    other_zones: tuple[str, ...] = ()
    """Zone entity ids the person is in, other than the target."""
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    """Reported horizontal accuracy; None when the source gives none."""


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Static configuration of one tracker."""

    params: EstimatorParams
    ignored_zones: frozenset[str] = frozenset()
    historical: bool = False


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What one report did."""

    outcome: FixOutcome | None
    """None when the report carried no usable position or was not used."""
    transition: Transition | None
    learned_samples: int = 0
    """Profile points learned by the historical estimator on arrival."""


@dataclass(frozen=True, slots=True)
class TrackerSnapshot:
    """Everything published for one person at an instant."""

    now: float
    state: ApproachState
    reason: Reason
    state_since: float | None
    kinematic: KinematicEstimate | None
    """Current estimate; None when undefined or stale."""
    last_kinematic: KinematicEstimate | None
    """Latest estimate even when stale, for reference."""
    historical: HistoricalEstimate | None
    historical_trips: int
    """Past trips covering the current distance."""
    distance_m: float | None
    closing_speed_mps: float | None
    confidence: Confidence | None
    stale: bool
    last_fix_time: float | None
    last_heard: float | None
    """Latest evidence of the person's whereabouts (a fix, or a restart)."""
    other_zones: tuple[str, ...]
    direction: Direction
    trip_started_at: float | None
    fixes_in_trip: int
    trips_learned: int
    last_trip_at: float | None

    @property
    def in_other_zone(self) -> bool:
        """Return True when inside a zone other than the target."""
        return bool(self.other_zones)

    @property
    def on_the_way(self) -> bool:
        """Return True when approaching or arriving soon."""
        return self.state in (ApproachState.APPROACHING, ApproachState.ARRIVING_SOON)

    @property
    def eta_seconds(self) -> float | None:
        """Return seconds until the kinematic arrival, never negative."""
        if self.kinematic is None:
            return None
        return max(0.0, self.kinematic.arrival - self.now)


class PersonTracker:
    """Track one person against one target zone."""

    def __init__(
        self,
        config: TrackerConfig,
        target: Target,
        stored_history: Mapping[str, Any] | None = None,
        *,
        at_target: bool = False,
    ) -> None:
        """Initialise; ``at_target`` sets the starting state to home."""
        self._config = config
        self._p = config.params
        self._target = target
        self._kinematic = KinematicEstimator(config.params, target)
        self._approach = ApproachMachine(
            config.params, ApproachState.HOME if at_target else ApproachState.AWAY
        )
        self._historical = (
            HistoricalEstimator(config.params, stored_history)
            if config.historical
            else None
        )
        self._threshold_s = DEFAULT_ARRIVING_SOON_MIN * 60.0
        self._other_zones: tuple[str, ...] = ()
        self._in_ignored = False
        self._distance_m: float | None = 0.0 if at_target else None
        self._trip_started_at: float | None = None
        self._fixes_in_trip = 0
        self._reference: float | None = None
        """When tracking (re)started without a fix: silence is measured from here."""
        self._leg_glitch_candidate = False
        """The latest leg fix was a move that may still be undone as a glitch."""
        self._before_move: MachineState | None = None
        """The approach state before the latest move, while it may be undone."""

    # -- configuration -------------------------------------------------------

    @property
    def config(self) -> TrackerConfig:
        """Return the configuration."""
        return self._config

    @property
    def target(self) -> Target:
        """Return the target zone."""
        return self._target

    @property
    def state(self) -> ApproachState:
        """Return the approach state."""
        return self._approach.state

    @property
    def threshold_s(self) -> float:
        """Return the arriving-soon threshold in seconds."""
        return self._threshold_s

    @property
    def closing_speed_mps(self) -> float | None:
        """Return the filtered closing speed."""
        return self._kinematic.closing_speed_mps

    @property
    def historical(self) -> HistoricalEstimator | None:
        """Return the historical estimator, when enabled."""
        return self._historical

    def set_threshold(self, seconds: float) -> None:
        """Set the arriving-soon threshold."""
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"threshold must be positive, got {seconds}")
        self._threshold_s = seconds

    def set_target(self, target: Target, now: float) -> bool:
        """Move the target (the zone was edited). Kinematics restart.

        The leg in progress was measured against the old centre and is
        dropped. Returns True when the centre moved so far that learned trips
        were forgotten too (the caller should save the history).
        """
        if target == self._target:
            return False
        moved_m = haversine_m(
            self._target.latitude,
            self._target.longitude,
            target.latitude,
            target.longitude,
        )
        self._target = target
        self._kinematic.set_target(target)
        self._before_move = None
        self._distance_m = None
        self._mark_reference(now)
        historical = self._historical
        if historical is None:
            return False
        historical.discard_leg()
        if moved_m > self._p.hist_reset_move_m:
            historical.reset()
            return True
        return False

    def restore_state(
        self, state: ApproachState, since: float, last_heard: float
    ) -> bool:
        """Restore the approach state saved before a restart.

        ``last_heard`` is when the person was last heard from before the
        restart. A restored state ends like any other: with no report for the
        stale period after that it becomes stationary, so it can never stick.
        """
        if not self._approach.restore(state, since):
            return False
        self._mark_reference(last_heard)
        return True

    def last_heard(self, now: float) -> float | None:
        """Return the latest evidence time, ignoring any in the future."""
        limit = now + self._p.clock_step_back_s
        heard = [
            t
            for t in (self._kinematic.last_fix_time, self._reference)
            if t is not None and t <= limit
        ]
        return max(heard) if heard else None

    def _mark_reference(self, t: float) -> None:
        reference = self._reference
        if (
            reference is None
            or t > reference
            # The clock stepped back: the old reference is in the future.
            or reference - t > self._p.clock_step_back_s
        ):
            self._reference = t

    # -- input ---------------------------------------------------------------

    def update(
        self, report: PersonReport, *, use_for_kinematics: bool = True
    ) -> UpdateResult:
        """Process a report of the person's state.

        ``use_for_kinematics`` is False while Home Assistant is still starting:
        restored states carry the restore time, not the time of the fix, so
        they may set presence and distance but neither measure speed nor
        count as hearing from the person.
        """
        t = report.timestamp
        if report.in_target:
            self._other_zones = ()
            self._distance_m = 0.0
            return self._arrive(t)

        self._other_zones = report.other_zones
        transition = self._approach.depart(t)
        if transition is not None:
            self._kinematic.reset()
            self._before_move = None
            self._trip_started_at = t
            self._fixes_in_trip = 0
            if use_for_kinematics:
                self._mark_reference(t)
        elif self._trip_started_at is None:
            self._trip_started_at = t

        was_ignored = self._in_ignored
        self._in_ignored = any(
            z in self._config.ignored_zones for z in report.other_zones
        )
        if self._historical is not None and self._in_ignored:
            self._historical.discard_leg()

        position = _position(report)
        # No accuracy, or 0 (what several trackers send when they do not
        # know), is taken as the worst usable accuracy rather than perfect.
        accuracy = (
            report.accuracy_m
            if report.accuracy_m is not None and report.accuracy_m > 0
            else self._p.max_gps_accuracy_m
        )
        outcome: FixOutcome | None = None
        if position is not None and use_for_kinematics:
            outcome = self._kinematic.add_fix(
                Fix(position[0], position[1], accuracy, t)
            )
            # Only fixes the estimator trusts move the published distance; a
            # wild indoor fix must not trigger the close-by rule.
            if outcome.accepted or outcome.verdict is FixVerdict.MERGED:
                self._distance_m = self._kinematic.distance_m
        elif position is not None:
            if accuracy <= self._p.max_gps_accuracy_m:
                self._distance_m = haversine_m(
                    position[0],
                    position[1],
                    self._target.latitude,
                    self._target.longitude,
                )

        if outcome is not None and outcome.rolled_back:
            transition = _chain(transition, self._undo_glitch(t))
        if outcome is not None and outcome.accepted:
            self._fixes_in_trip += 1
            # Kept until the next accepted fix shows the move was real.
            self._before_move = (
                self._approach.save() if outcome.verdict is FixVerdict.MOVED else None
            )
            step = self._approach.on_fix(
                self._observation(t),
                moved=outcome.verdict is FixVerdict.MOVED,
                new_sample=outcome.speed_updated,
            )
            transition = _chain(transition, step)
            self._end_leg_if_stopped()
            self._record_leg(t, outcome, was_ignored)
        else:
            transition = _chain(
                transition, self._approach.on_tick(self._observation(t))
            )
            self._end_leg_if_stopped()
        return UpdateResult(outcome, transition)

    def tick(self, now: float) -> Transition | None:
        """Re-evaluate time-driven rules."""
        transition = self._approach.on_tick(self._observation(now))
        self._end_leg_if_stopped()
        return transition

    def _arrive(self, t: float) -> UpdateResult:
        transition = self._approach.arrive(t)
        learned = 0
        if transition is not None:
            if self._historical is not None:
                learned = self._historical.finish_leg(t, self._target.radius_m)
            self._kinematic.reset()
            self._trip_started_at = None
            self._fixes_in_trip = 0
            self._in_ignored = False
            self._leg_glitch_candidate = False
            self._before_move = None
        return UpdateResult(None, transition, learned)

    def _end_leg_if_stopped(self) -> None:
        """A stop ends the leg: time spent stopped is not travel time."""
        historical = self._historical
        if (
            historical is not None
            and historical.leg_open
            and self._approach.state is ApproachState.STATIONARY
        ):
            historical.discard_leg()
            self._leg_glitch_candidate = False

    def _undo_glitch(self, t: float) -> Transition | None:
        """Undo what the last fix, now known to be a glitch, did here."""
        if self._leg_glitch_candidate and self._historical is not None:
            self._historical.remove_last_fix()
        self._leg_glitch_candidate = False
        saved, self._before_move = self._before_move, None
        if saved is None:
            return None
        before = self._approach.state
        self._approach.load(saved)
        if self._approach.state is before:
            return None
        return Transition(before, self._approach.state, self._approach.reason, t)

    def _record_leg(self, t: float, outcome: FixOutcome, was_ignored: bool) -> None:
        historical = self._historical
        self._leg_glitch_candidate = False
        if (
            historical is None
            or self._in_ignored
            or outcome.distance_m is None
            or self._approach.state is ApproachState.STATIONARY
        ):
            return
        # A leg (re)starts on the first fix after leaving the target, an
        # ignored zone or a stop (each of which closed the previous leg).
        if was_ignored or not historical.leg_open:
            historical.start_leg(t)
        recorded = historical.add_fix(t, outcome.distance_m)
        self._leg_glitch_candidate = recorded and outcome.verdict is FixVerdict.MOVED

    def _observation(self, now: float) -> Observation:
        estimate = self._kinematic.estimate(now)
        heard = self.last_heard(now)
        return Observation(
            now=now,
            closing_speed_mps=self._kinematic.closing_speed_mps,
            eta_s=None if estimate is None else estimate.arrival - now,
            distance_m=self._distance_m,
            stopped=self._kinematic.stopped,
            since_fix_s=None if heard is None else now - heard,
            in_other_zone=bool(self._other_zones),
            threshold_s=self._threshold_s,
        )

    # -- output --------------------------------------------------------------

    def snapshot(self, now: float) -> TrackerSnapshot:
        """Return what to publish at ``now``."""
        p = self._p
        state = self._approach.state
        at_home = state is ApproachState.HOME
        kinematic = None if at_home else self._kinematic.estimate(now)
        last_kinematic = (
            None if at_home else self._kinematic.estimate(now, allow_stale=True)
        )
        speed = None if at_home else self._kinematic.closing_speed_mps
        last_fix = self._kinematic.last_fix_time

        historical: HistoricalEstimate | None = None
        trips = 0
        hist = self._historical
        distance_for_history = self._kinematic.distance_m
        if (
            hist is not None
            and state in ON_THE_WAY
            and not self._in_ignored
            and distance_for_history is not None
            and last_fix is not None
            and not self._kinematic.is_stale(now)
        ):
            # Like the kinematic ETA, only meaningful while heading home.
            trips = len(hist.covering(distance_for_history))
            historical = hist.estimate(distance_for_history, last_fix)
            if historical is not None and historical.arrival < now:
                historical = replace(
                    historical,
                    arrival=now,
                    earliest=max(historical.earliest, now),
                    latest=max(historical.latest, now),
                )

        if speed is None:
            direction = Direction.UNKNOWN
        elif state is ApproachState.STATIONARY or abs(speed) < p.min_closing_speed_mps:
            direction = Direction.STATIONARY
        elif speed > 0:
            direction = Direction.TOWARDS
        else:
            direction = Direction.AWAY_FROM

        return TrackerSnapshot(
            now=now,
            state=state,
            reason=self._approach.reason,
            state_since=self._approach.since,
            kinematic=kinematic,
            last_kinematic=last_kinematic,
            historical=historical,
            historical_trips=trips,
            distance_m=self._distance_m,
            closing_speed_mps=speed,
            confidence=None if kinematic is None else kinematic.confidence,
            stale=not at_home and self._kinematic.is_stale(now),
            last_fix_time=last_fix,
            last_heard=self.last_heard(now),
            other_zones=self._other_zones,
            direction=Direction.UNKNOWN if at_home else direction,
            trip_started_at=self._trip_started_at,
            fixes_in_trip=self._fixes_in_trip,
            trips_learned=0 if hist is None else hist.trips_learned,
            last_trip_at=None if hist is None else hist.last_trip_at,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return internal state for diagnostics (no coordinates)."""
        hist = self._historical
        return {
            "kinematic": self._kinematic.diagnostics(),
            "approach": self._approach.diagnostics(),
            "threshold_s": self._threshold_s,
            "other_zones": list(self._other_zones),
            "in_ignored_zone": self._in_ignored,
            "trip_started_at": self._trip_started_at,
            "fixes_in_trip": self._fixes_in_trip,
            "historical": None
            if hist is None
            else {
                "trips_learned": hist.trips_learned,
                "last_trip_at": hist.last_trip_at,
                "stored_trips": hist.stored_trips,
                "leg_open": hist.leg_open,
                "leg_started_at": hist.leg_started_at,
                "leg_fixes": hist.leg_fixes,
            },
        }


NOT_IN_A_ZONE_STATES = frozenset({"not_home", "unknown", "unavailable"})
HOME_STATE = "home"
HOME_ZONE = "zone.home"


def build_report(
    state: str,
    attributes: Mapping[str, Any],
    timestamp: float,
    target_zone: str,
    target_name: str,
    *,
    enclosing_zones: Collection[str] = (),
) -> PersonReport:
    """Turn a person entity's state and attributes into a report.

    Uses the person's ``in_zones`` list. States recorded before that attribute
    existed fall back to the state name: ``home`` for the home zone, or the
    target zone's name, and any other zone name counts as another zone.

    ``enclosing_zones`` are zones that contain the target (a neighbourhood
    around home): being in one is normal on the way in, so it does not count
    as being in another zone.
    """
    in_zones = attributes.get("in_zones")
    if isinstance(in_zones, list):
        zones = tuple(str(zone) for zone in in_zones)
        in_target = target_zone in zones
        other = tuple(
            zone
            for zone in zones
            if zone != target_zone and zone not in enclosing_zones
        )
    else:
        if target_zone == HOME_ZONE:
            in_target = state == HOME_STATE
        else:
            in_target = state == target_name
        if in_target or state in NOT_IN_A_ZONE_STATES:
            other = ()
        elif state == HOME_STATE:
            other = (HOME_ZONE,)
        else:
            other = (f"zone:{state}",)

    def number(key: str) -> float | None:
        value = attributes.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    return PersonReport(
        timestamp=timestamp,
        in_target=in_target,
        other_zones=other,
        latitude=number("latitude"),
        longitude=number("longitude"),
        accuracy_m=number("gps_accuracy"),
    )


def _position(report: PersonReport) -> tuple[float, float] | None:
    lat, lon = report.latitude, report.longitude
    if lat is None or lon is None or not is_valid_coordinate(lat, lon):
        return None
    return lat, lon


def _chain(first: Transition | None, second: Transition | None) -> Transition | None:
    """Combine two consecutive transitions into one from-to transition."""
    if first is None:
        return second
    if second is None:
        return first
    return Transition(first.previous, second.current, second.reason, second.at)
