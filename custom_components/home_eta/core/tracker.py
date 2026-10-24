"""One person's tracking: zone membership, kinematics and the trip home.

This is everything between "the person's state changed" and "here are the
numbers to publish", kept free of Home Assistant so the integration and the
backtest replay exactly the same logic.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .geo import haversine_m, is_valid_coordinate
from .homeward import (
    HomewardRestore,
    HomewardSnapshot,
    HomewardState,
    HomewardTracker,
    NearHomeConfig,
    near_home,
)
from .kinematic import Fix, FixOutcome, FixVerdict, KinematicEstimator, Target
from .params import EstimatorParams


@dataclass(frozen=True, slots=True)
class PersonReport:
    """A person's state as reported by Home Assistant (or a recorder export)."""

    timestamp: float
    """When the state was last updated, UTC epoch seconds."""
    in_target: bool
    """Inside the target zone."""
    other_zones: tuple[str, ...] = ()
    """Zone entity ids the person is in, other than the target and its rings."""
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    """Reported horizontal accuracy; None when the source gives none."""


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Static configuration of one tracker."""

    params: EstimatorParams = field(default_factory=EstimatorParams)
    near_home: NearHomeConfig = field(default_factory=NearHomeConfig)


class PersonStatus(StrEnum):
    """Where a person is on the way home, most home first.

    Arriving soon is part of a trip home, so a short trip can go from away
    straight to arriving soon.
    """

    HOME = "home"
    ARRIVING_SOON = "arriving_soon"
    HEADING_HOME = "heading_home"
    AWAY = "away"


STATUS_ORDER: tuple[PersonStatus, ...] = tuple(PersonStatus)
"""Most home first: a group shows the first of these any member is in."""


@dataclass(frozen=True, slots=True)
class TrackerSnapshot:
    """Everything published for one person at an instant."""

    now: float
    at_home: bool
    distance_m: float | None
    """Distance to the target centre; 0 at home, None while unknown."""
    other_zones: tuple[str, ...]
    homeward: HomewardSnapshot
    near_home: float | None
    """0 (far, or staying somewhere for long) to 1 (home or close by)."""

    @property
    def heading_home(self) -> bool:
        """Return True while on the way home."""
        return self.homeward.heading

    @property
    def expected_home(self) -> float | None:
        """Return the expected arrival (never in the past) while heading home."""
        return self.homeward.arrival if self.homeward.heading else None

    @property
    def arriving_soon(self) -> bool:
        """Return True while arriving soon."""
        return self.homeward.arriving_soon

    @property
    def status(self) -> PersonStatus:
        """Return the stage of the way home the person is at."""
        if self.at_home:
            return PersonStatus.HOME
        if self.arriving_soon:
            return PersonStatus.ARRIVING_SOON
        if self.heading_home:
            return PersonStatus.HEADING_HOME
        return PersonStatus.AWAY


class PersonTracker:
    """Track one person against one target zone."""

    def __init__(
        self, config: TrackerConfig, target: Target, *, at_target: bool = False
    ) -> None:
        """Initialise; ``at_target`` starts the person at home."""
        self._config = config
        self._p = config.params
        self._target = target
        self._kinematic = KinematicEstimator(config.params, target)
        self._homeward = HomewardTracker(config.params, target, at_target=at_target)
        self._homeward_before_move: HomewardState | None = None
        """The heading-home state before the latest move, while it may be undone."""
        self._at_home = at_target
        self._other_zones: tuple[str, ...] = ()
        self._distance_m: float | None = 0.0 if at_target else None

    # -- configuration -------------------------------------------------------

    @property
    def target(self) -> Target:
        """Return the target zone."""
        return self._target

    @property
    def at_home(self) -> bool:
        """Return True while the person is inside the target zone."""
        return self._at_home

    @property
    def threshold_s(self) -> float:
        """Return the arriving-soon threshold in seconds."""
        return self._homeward.threshold_s

    @property
    def homeward(self) -> HomewardTracker:
        """Return the heading-home tracker."""
        return self._homeward

    def set_rings(self, rings: Iterable[Target]) -> None:
        """Set the zones that enclose the target (rings around home)."""
        self._homeward.set_rings(rings)

    def set_threshold(self, seconds: float, *, saved: bool = False) -> None:
        """Set the arriving-soon threshold (applied at the next tick or fix).

        ``saved``: the value saved before a restart (see HomewardTracker).
        """
        self._homeward.set_threshold(seconds, saved=saved)

    def set_target(self, target: Target, now: float) -> None:
        """Move the target (the zone was edited).

        Distances were measured to the old centre: kinematics restart, and a
        trip home in progress ends.
        """
        if target == self._target:
            return
        self._target = target
        self._kinematic.set_target(target)
        self._homeward.set_target(target, now)
        self._homeward_before_move = None
        self._distance_m = 0.0 if self._at_home else None

    def restore_homeward(
        self, data: HomewardRestore, last_heard: float, now: float
    ) -> bool:
        """Resume a stay and a trip home saved before a restart or reload.

        They end like any other: the first fixes, a stay or the overdue rule,
        which is applied at once.
        """
        if not self._homeward.restore(data, last_heard, self._distance_m):
            return False
        self._homeward.tick(now, self._fresh_speed(now))
        return True

    # -- input ---------------------------------------------------------------

    def update(
        self, report: PersonReport, *, use_for_kinematics: bool = True
    ) -> FixOutcome | None:
        """Process a report of the person's state; return what became of its fix.

        None when the report carried no usable position or was not used.
        ``use_for_kinematics`` is False while Home Assistant is still starting:
        restored states carry the restore time, not the time of the fix, so
        they may set presence and distance but are not used as fixes.
        """
        t = report.timestamp
        if report.in_target:
            self._arrive(t)
            return None

        self._other_zones = report.other_zones
        if self._at_home:
            self._at_home = False
            self._kinematic.reset()
            self._homeward.depart(t)
            self._homeward_before_move = None
        self._homeward.set_other_zone(bool(report.other_zones))

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
            fix = Fix(position[0], position[1], accuracy, t)
            outcome = self._kinematic.add_fix(fix)
            # Only fixes the estimator trusts move the published distance; a
            # wild indoor fix must not count as close by. After an undone
            # glitch the estimator is back at the fix before it.
            if (
                outcome.accepted
                or outcome.verdict is FixVerdict.MERGED
                or outcome.rolled_back
            ):
                self._distance_m = self._kinematic.distance_m
            if outcome.rolled_back:
                self._undo_homeward_glitch()
            # An accepted fix always has its distance.
            if outcome.accepted and outcome.distance_m is not None:
                self._feed_homeward(fix, outcome.distance_m, outcome.verdict)
                return outcome
        elif position is not None and accuracy <= self._p.max_gps_accuracy_m:
            self._distance_m = haversine_m(
                position[0],
                position[1],
                self._target.latitude,
                self._target.longitude,
            )
        self._homeward.tick(t, self._fresh_speed(t))
        return outcome

    def tick(self, now: float) -> None:
        """Re-evaluate the time-driven rules."""
        self._homeward.tick(now, self._fresh_speed(now))

    def _arrive(self, t: float) -> None:
        self._other_zones = ()
        self._distance_m = 0.0
        if self._at_home:
            return
        self._at_home = True
        self._kinematic.reset()
        self._homeward.arrive(t)
        self._homeward_before_move = None

    def _feed_homeward(self, fix: Fix, distance_m: float, verdict: FixVerdict) -> None:
        """Give an accepted fix to the heading-home tracker."""
        homeward = self._homeward
        # Kept until the next fix shows the move was real; only a moving fix
        # can turn out to be a glitch.
        self._homeward_before_move = (
            homeward.save() if verdict is FixVerdict.MOVED else None
        )
        homeward.add_fix(
            fix,
            distance_m,
            speed_mps=self._fresh_speed(fix.timestamp),
            still=verdict is FixVerdict.STATIONARY,
        )

    def _undo_homeward_glitch(self) -> None:
        """Undo the last moving fix in heading home too (it was a glitch).

        Whatever the fix that showed it (accepted, merged or rejected as a
        jump): the kinematic estimator has already put its state back.
        """
        saved, self._homeward_before_move = self._homeward_before_move, None
        if saved is not None:
            self._homeward.load(saved)

    def _fresh_speed(self, now: float) -> float | None:
        """Return the filtered closing speed, unless stopped or stale."""
        kinematic = self._kinematic
        if kinematic.stopped or kinematic.is_stale(now):
            return None
        return kinematic.closing_speed_mps

    # -- output --------------------------------------------------------------

    def snapshot(self, now: float) -> TrackerSnapshot:
        """Return what to publish at ``now``."""
        homeward = self._homeward.snapshot(now)
        distance = self._distance_m
        nearness: float | None
        if self._at_home:
            nearness = 1.0
        elif distance is None:
            nearness = None
        else:
            nearness = near_home(
                distance, homeward.still_since, now, self._config.near_home
            )
        return TrackerSnapshot(
            now=now,
            at_home=self._at_home,
            distance_m=distance,
            other_zones=self._other_zones,
            homeward=homeward,
            near_home=nearness,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return internal state for diagnostics (no coordinates)."""
        return {
            "at_home": self._at_home,
            "distance_m": self._distance_m,
            "other_zones": list(self._other_zones),
            "kinematic": self._kinematic.diagnostics(),
            "homeward": self._homeward.diagnostics(),
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
