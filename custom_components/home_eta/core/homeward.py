"""Heading home: spot the trip home early and say when it should end.

This is the signal for warming the house on the way home. It answers three
questions:

- **Is the person on their way home?** As early as possible, without being
  fooled by GPS jitter or a drive around town. A trip home starts when the
  person has come at least 20 % (and 500 m) closer than the farthest point
  since they last stayed somewhere, and is still closing; or the moment they
  cross into a zone that encloses home (rings of passive zones around home
  report a crossing within seconds, long before significant-change updates
  show progress). It is latched until they get home, stay somewhere for 20
  minutes, move back out beyond the closest point (500 m or 15 % of the
  distance, whichever is more), or go quiet well past the expected arrival.
- **When will they get home?** Distance over a speed. With few fixes the speed
  is the typical one for a journey home of that length (walking under 1 km, a
  local drive at a few kilometres, a motorway at 100 km), sharpened by what
  was measured: the ring-to-ring speed, or the filtered closing speed. Slow
  and close means on foot, and the measured speed is used as it is. The
  typical speeds are learned from the person's own trips home
  (``core/calibration.py``): each completed trip is handed over for that.
  What is published is the middle of the last three estimates worked out at
  fixes (``ESTIMATE_WINDOW``), so one estimate that disagrees with the two
  before it waits for the next fix to confirm it; a zone crossing whose
  ring-to-ring speed is in use, a speed measured over kilometres, is believed
  at once. Between fixes it stands until it passes, then the one due later.
- **Is the person arriving soon?** On a trip home, once the newest estimate
  (that of the last fix alone, not the one published) is within the person's
  threshold or they are within 800 m, unless they are inside another zone (a
  shop near home). It then stays on until the trip home ends, so an estimate
  that wobbles round the threshold does not flap. The rule that ends a trip
  home gone quiet goes by the newest estimate too. Both are exactly as before
  the published estimate was smoothed (2.3.0): heating that comes on as a
  queue clears must not wait for the next fix to confirm it.

A "place" is where the person stayed: a fix within ``place_radius_m`` (or
``place_accuracy_factor`` times its accuracy) of where they were at least
``place_confirm_s`` earlier, or one at least ``place_still_s`` later followed
by silence. Brief stops (traffic, a train changing) do not count, so they
neither end a trip home nor reset the farthest point.

The thresholds were chosen on 15 everyday absences (strolls, a café 400 m away
for 6 h, car errands, a friend's house, train and car days in London, a
motorway day, a weekend away) on three phone profiles, plus the household's
real trips of 16-23 Oct 2026; none is tuned to one scenario.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
import math

from .calibration import (
    MIN_ELAPSED_S,
    MIN_REMAINING_M,
    LearnedTrip,
    TripSample,
    make_trip,
)
from .geo import haversine_m, is_valid_coordinate
from .kinematic import Fix, Target
from .params import DEFAULT_ARRIVING_SOON_MIN, EstimatorParams
from .speeds import BUILT_IN_CURVES, SpeedCurves

SAMPLE_INTERVAL_S = 60.0
"""Fixes of a trip home are kept for learning at most once a minute..."""

MAX_TRIP_SAMPLES = 360
"""...and at most this many (the latest), six hours' worth."""

ESTIMATE_WINDOW = 3
"""The expected arrival published is the middle one of the last this many.

Only the expected arrival published: arriving soon and the overdue rule go by
the newest estimate, as they did before 2.4.0, so they come on and end at the
same moments as then.

Each fix gives an estimate: the distance over the speed expected for the rest
of the trip. In stop-start traffic one of them is often far off and the next
puts it back: one leg crawled or sped through (the closing speed is measured
leg by leg, and a fix every five minutes is about the filter's own time
constant), a gap in reports that leaves no measured speed (the typical one is
used until the next fix), or a switch between those and the speed between
zones. The middle of three drops such a lone estimate, and follows a change
that the next fix confirms one fix late (a steady drift too: on 27 October
2026 the fix four minutes from home went from spot on to 2.6 minutes off).
Three is the fewest that can tell which of two disagreeing estimates is the
odd one; five left more changes (20 rather than 16) and lags two fixes.
Replayed on the household's trips home of 16-27 October 2026 (ten, two
people): changes of more than ten minutes between fixes 30 to 16, mean error
18.1 to 17.3 minutes, heading home and arriving soon at the same moments.
"""


class HomewardReason(StrEnum):
    """Why heading home started or ended."""

    INITIAL = "initial"
    PROGRESS = "progress"
    """Came well closer than the farthest point and still closing."""
    ZONE_CROSSING = "zone_crossing"
    """Crossed into a zone that encloses home."""
    RESTORED = "restored"
    ARRIVED = "arrived"
    AT_PLACE = "at_place"
    MOVING_AWAY = "moving_away"
    OVERDUE = "overdue"
    ZONE_CHANGED = "zone_changed"


class SpeedSource(StrEnum):
    """Where the speed behind the expected arrival came from."""

    ZONE_CROSSINGS = "zone_crossings"
    MEASURED = "measured"
    ON_FOOT = "on_foot"
    TYPICAL = "typical"


@dataclass(frozen=True, slots=True)
class HomewardEstimate:
    """An expected arrival worked out at a fix, with the speed behind it."""

    arrival: float
    """When the person was expected home then (may since have passed)."""
    speed_mps: float
    source: SpeedSource


@dataclass(frozen=True, slots=True)
class HomewardSnapshot:
    """What is published about the trip home at an instant."""

    heading: bool
    reason: HomewardReason
    since: float | None
    """When heading home started or last ended."""
    arrival: float | None
    """Expected arrival (never in the past), only while heading home.

    The middle of the last ``ESTIMATE_WINDOW`` estimates worked out at fixes;
    once it has passed, the one due later (see ``HomewardTracker._estimate``).
    """
    speed_mps: float | None
    """The speed behind ``arrival`` (that of the estimate published)."""
    speed_source: SpeedSource | None
    farthest_m: float | None
    """Farthest distance since the person last stayed somewhere."""
    still_since: float | None
    """When the person got to where they are staying, if staying somewhere.

    A confirmed place, or, with no report for ``place_dwell_s``, the last fix.
    """
    arriving_soon: bool = False
    """On a trip home and within the threshold or close by (never otherwise)."""


@dataclass(frozen=True, slots=True)
class HomewardTrip:
    """A trip home in progress, as saved for a restart."""

    since: float
    farthest_m: float
    closest_m: float
    arriving_soon: bool = False
    threshold_s: float | None = None
    """The threshold arriving soon was judged with (None in data from 2.1.0)."""
    estimates: tuple[HomewardEstimate, ...] = ()
    """The latest estimates worked out at fixes, oldest first (none before 2.4.0)."""


@dataclass(frozen=True, slots=True)
class HomewardStay:
    """Where and since when the person has been staying, as saved for a restart."""

    since: float
    latitude: float
    longitude: float
    distance_m: float


@dataclass(frozen=True, slots=True)
class HomewardRestore:
    """What survives a restart or reload."""

    last_heard: float
    """When the person was last heard from."""
    trip: HomewardTrip | None = None
    stay: HomewardStay | None = None
    last_report: float | None = None
    """The time of the last report received, whatever became of it.

    A report merged into the fix before it, rejected as a jump or received
    while Home Assistant was starting is no fix, so ``last_heard`` does not
    show it; at a reload the person's state is that report again. None in
    data saved before 2.4.0.
    """

    def as_dict(self) -> dict[str, object]:
        """Return the data as JSON-serialisable values."""
        trip, stay = self.trip, self.stay
        return {
            "last_heard": self.last_heard,
            "last_report": self.last_report,
            "trip": None
            if trip is None
            else {
                "since": trip.since,
                "farthest_m": trip.farthest_m,
                "closest_m": trip.closest_m,
                "arriving_soon": trip.arriving_soon,
                "threshold_s": trip.threshold_s,
                "estimates": [
                    {
                        "arrival": estimate.arrival,
                        "speed_mps": estimate.speed_mps,
                        "source": estimate.source.value,
                    }
                    for estimate in trip.estimates
                ],
            },
            "stay": None
            if stay is None
            else {
                "since": stay.since,
                "latitude": stay.latitude,
                "longitude": stay.longitude,
                "distance_m": stay.distance_m,
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> HomewardRestore | None:
        """Return the data saved by ``as_dict``, or None when it is unusable.

        Saved data comes back from disk, so each part is checked and a bad
        part is dropped; with neither a trip nor a stay left, there is nothing
        to restore.
        """
        if not isinstance(value, dict):
            return None
        last_heard = _number(value.get("last_heard"))
        if last_heard is None:
            return None
        trip: HomewardTrip | None = None
        saved_trip = value.get("trip")
        if isinstance(saved_trip, dict):
            since = _number(saved_trip.get("since"))
            farthest = _number(saved_trip.get("farthest_m"))
            closest = _number(saved_trip.get("closest_m"))
            soon = saved_trip.get("arriving_soon", False)
            threshold = _number(saved_trip.get("threshold_s"))
            if (
                since is not None
                and farthest is not None
                and closest is not None
                and 0 <= closest <= farthest
                and isinstance(soon, bool)
            ):
                trip = HomewardTrip(
                    since,
                    farthest,
                    closest,
                    soon,
                    # A threshold that is no use is dropped, not the trip.
                    threshold if threshold is not None and threshold > 0 else None,
                    # So are estimates that are no use.
                    _estimates_from(saved_trip.get("estimates")),
                )
        stay: HomewardStay | None = None
        saved_stay = value.get("stay")
        if isinstance(saved_stay, dict):
            since = _number(saved_stay.get("since"))
            latitude = _number(saved_stay.get("latitude"))
            longitude = _number(saved_stay.get("longitude"))
            distance = _number(saved_stay.get("distance_m"))
            if (
                since is not None
                and latitude is not None
                and longitude is not None
                and distance is not None
                and is_valid_coordinate(latitude, longitude)
                and distance >= 0
            ):
                stay = HomewardStay(since, latitude, longitude, distance)
        if trip is None and stay is None:
            return None
        return cls(last_heard, trip, stay, _number(value.get("last_report")))


@dataclass(frozen=True, slots=True)
class _Fix:
    t: float
    latitude: float
    longitude: float
    distance_m: float


@dataclass(frozen=True, slots=True)
class _Sample:
    """A fix on the way home, kept to learn typical speeds from on arrival."""

    t: float
    distance_m: float
    measured_mps: float | None
    """The speed measured then (between rings, or closing), if any: the one
    the expected arrival was worked out with."""


@dataclass(frozen=True, slots=True)
class HomewardState:
    """Everything a fix can change, to undo it as a glitch."""

    last: _Fix | None
    anchor: _Fix | None
    anchor_since: float | None
    place_since: float | None
    trip_started: bool
    farthest_m: float | None
    inside: frozenset[Target]
    judged: frozenset[Target]
    spent: frozenset[Target]
    trigger: frozenset[Target]
    inward: tuple[tuple[float, float], ...]
    trip_speed: tuple[float, SpeedSource] | None
    heading: bool
    reason: HomewardReason
    since: float | None
    closest_m: float | None
    soon: bool
    threshold_s: float
    """The threshold ``soon`` was judged with."""
    samples: tuple[_Sample, ...]
    estimates: tuple[HomewardEstimate, ...]


def trip_speed_mps(
    distance_m: float,
    measured_mps: float | None,
    params: EstimatorParams,
    curves: SpeedCurves = BUILT_IN_CURVES,
) -> tuple[float, SpeedSource]:
    """Return the speed to expect for the rest of the trip home, and its source.

    A measured speed alone over-trusts one leg (a stretch of motorway, a queue);
    the typical speed alone ignores how this trip is going. Close and slow is
    on foot, which is steady, so the measurement is used directly. Otherwise
    the two are blended, ``measured ** w * typical ** (1 - w)``, with the
    typical speed and weight learned for that distance; at driving speed,
    those for driving. Above barely closing, closing faster never means a
    later arrival: at driving speed, never slower than just below it
    (walking near home, or a slow drive further out).
    """
    if measured_mps is None or measured_mps <= params.homeward_min_speed_mps:
        return curves.typical_mps(distance_m, driving=False), SpeedSource.TYPICAL
    driving_from = params.on_foot_max_speed_mps
    near = distance_m < params.on_foot_max_distance_m
    if measured_mps < driving_from:
        if near:
            speed = max(measured_mps, params.on_foot_min_speed_mps)
            return speed, SpeedSource.ON_FOOT
        return (
            curves.expected_mps(measured_mps, distance_m, driving=False),
            SpeedSource.MEASURED,
        )
    below = (
        max(driving_from, params.on_foot_min_speed_mps)
        if near
        else curves.expected_mps(driving_from, distance_m, driving=False)
    )
    speed = curves.expected_mps(measured_mps, distance_m, driving=True)
    return max(speed, below), SpeedSource.MEASURED


class HomewardTracker:
    """Detect one person's trip home, estimate its end and say when it is near.

    Fed the fixes the kinematic estimator accepted (so jumps and inaccurate
    fixes are already gone), plus the filtered closing speed when it is fresh.
    """

    def __init__(
        self, params: EstimatorParams, target: Target, *, at_target: bool = False
    ) -> None:
        """Initialise; ``at_target`` starts the person at home."""
        self._p = params
        self._target = target
        self._rings: tuple[Target, ...] = ()
        self._home = at_target
        self._last: _Fix | None = None
        self._anchor: _Fix | None = None
        """Where the person was last seen staying (or first seen after moving)."""
        self._anchor_since: float | None = None
        """When the person may have got to the anchor (at or before its fix)."""
        self._place_since: float | None = None
        """When the person got to the anchor, once a later fix confirmed a stay."""
        self._trip_started = False
        self._farthest_m: float | None = None
        self._inside: frozenset[Target] = frozenset()
        """The enclosing zones the last fix was inside..."""
        self._judged: frozenset[Target] = frozenset()
        """...out of those it was checked against (a zone added since was not)."""
        self._spent: frozenset[Target] = frozenset()
        """Zones entered since leaving the last stay: each starts a trip once."""
        self._trigger: frozenset[Target] = frozenset()
        """The zones whose crossing started the trip home in progress."""
        self._inward: tuple[tuple[float, float], ...] = ()
        """(time, distance) of the last two inward crossings since leaving a place."""
        self._trip_speed: tuple[float, SpeedSource] | None = None
        """The speed expected for the rest of the trip, as of the last fix.

        Fixed between fixes: time passing without news is not information.
        """
        self._heading = False
        self._reason = HomewardReason.INITIAL
        self._since: float | None = None
        self._closest_m: float | None = None
        self._soon = False
        """Arriving soon: latched on a trip home until the trip ends."""
        self._threshold_s = DEFAULT_ARRIVING_SOON_MIN * 60.0
        self._threshold_set = False
        """The threshold was set (until then it is the default, or a restored
        trip's: see ``restore``)."""
        self._in_other_zone = False
        """Inside a zone other than the target and those around it."""
        self._restored_heard: float | None = None
        """When the person was last heard before a restart (no fix this run yet)."""
        self._restored_distance_m: float | None = None
        self._curves: SpeedCurves = BUILT_IN_CURVES
        self._samples: tuple[_Sample, ...] = ()
        """Fixes of the trip home in progress, to learn typical speeds from
        when the person gets home."""
        self._estimates: tuple[HomewardEstimate, ...] = ()
        """The latest estimates of the trip home in progress, oldest first."""
        self._last_report: float | None = None
        """The time of the last report received (``note_report``), as it came.

        Not moved when the clock steps back, nor undone with a glitch: it
        tells a report received from one not received yet.
        """
        self._completed: list[LearnedTrip] = []

    # -- state -----------------------------------------------------------

    @property
    def heading(self) -> bool:
        """Return True while the person is on their way home."""
        return self._heading

    @property
    def reason(self) -> HomewardReason:
        """Return why heading home last started or ended."""
        return self._reason

    @property
    def since(self) -> float | None:
        """Return when heading home last started or ended."""
        return self._since

    @property
    def arriving_soon(self) -> bool:
        """Return True while arriving soon (only ever on a trip home)."""
        return self._soon

    @property
    def threshold_s(self) -> float:
        """Return the arriving-soon threshold in seconds."""
        return self._threshold_s

    @property
    def speed_curves(self) -> SpeedCurves:
        """Return the typical speeds the expected arrival is worked out with."""
        return self._curves

    def take_completed_trips(self) -> list[LearnedTrip]:
        """Return the trips home completed since last asked, to learn from."""
        trips, self._completed = self._completed, []
        return trips

    def restore_data(self) -> HomewardRestore | None:
        """Return what to save for a restart: a trip home, or a stay."""
        last_heard = self._last_time()
        if last_heard is None:
            return None
        trip: HomewardTrip | None = None
        if (
            self._heading
            and self._since is not None
            and self._farthest_m is not None
            and self._closest_m is not None
        ):
            trip = HomewardTrip(
                self._since,
                self._farthest_m,
                self._closest_m,
                self._soon,
                self._threshold_s,
                self._estimates,
            )
        stay: HomewardStay | None = None
        anchor, since = self._anchor, self._place_since
        if not self._home and anchor is not None and since is not None:
            stay = HomewardStay(
                since, anchor.latitude, anchor.longitude, anchor.distance_m
            )
        if trip is None and stay is None:
            return None
        return HomewardRestore(last_heard, trip, stay, self._last_report)

    def save(self) -> HomewardState:
        """Return the state, to put back with ``load`` if a fix was a glitch."""
        return HomewardState(
            last=self._last,
            anchor=self._anchor,
            anchor_since=self._anchor_since,
            place_since=self._place_since,
            trip_started=self._trip_started,
            farthest_m=self._farthest_m,
            inside=self._inside,
            judged=self._judged,
            spent=self._spent,
            trigger=self._trigger,
            inward=self._inward,
            trip_speed=self._trip_speed,
            heading=self._heading,
            reason=self._reason,
            since=self._since,
            closest_m=self._closest_m,
            soon=self._soon,
            threshold_s=self._threshold_s,
            samples=self._samples,
            estimates=self._estimates,
        )

    def load(self, saved: HomewardState) -> None:
        """Put back a state returned by ``save``."""
        self._last = saved.last
        self._anchor = saved.anchor
        self._anchor_since = saved.anchor_since
        self._place_since = saved.place_since
        self._trip_started = saved.trip_started
        self._farthest_m = saved.farthest_m
        self._inside = saved.inside
        self._judged = saved.judged
        self._spent = saved.spent
        self._trigger = saved.trigger
        self._inward = saved.inward
        self._trip_speed = saved.trip_speed
        self._heading = saved.heading
        self._reason = saved.reason
        self._since = saved.since
        self._closest_m = saved.closest_m
        self._samples = saved.samples
        self._estimates = saved.estimates
        # The threshold is not a fix's to undo; an arriving soon judged with
        # an earlier one is judged afresh.
        self._soon = saved.soon and saved.threshold_s == self._threshold_s

    def diagnostics(self) -> dict[str, object]:
        """Return internal state for diagnostics (no coordinates)."""
        return {
            "heading": self._heading,
            "reason": self._reason.value,
            "since": self._since,
            "arriving_soon": self._soon,
            "threshold_s": self._threshold_s,
            "in_other_zone": self._in_other_zone,
            "home": self._home,
            "last_fix_time": None if self._last is None else self._last.t,
            "last_distance_m": None if self._last is None else self._last.distance_m,
            "place_since": self._place_since,
            "farthest_m": self._farthest_m,
            "closest_m": self._closest_m,
            "inward_crossings": [list(c) for c in self._inward],
            "zones_inside": len(self._inside),
            "zones": len(self._rings),
            "trip_samples": len(self._samples),
            "last_report": self._last_report,
            "estimates": [
                {
                    "arrival": estimate.arrival,
                    "speed_mps": estimate.speed_mps,
                    "source": estimate.source.value,
                }
                for estimate in self._estimates
            ],
        }

    # -- configuration ---------------------------------------------------

    def set_target(self, target: Target, now: float) -> None:
        """Move the target (the zone was edited).

        Distances were measured to the old centre, so the trip restarts.
        """
        if target == self._target:
            return
        self._target = target
        self._clear_trip()
        if self._heading:
            self._end(HomewardReason.ZONE_CHANGED, now)

    def set_threshold(self, seconds: float) -> None:
        """Set the arriving-soon threshold; a new one judges arriving soon afresh.

        Applied at the next fix or tick.
        """
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"threshold must be positive, got {seconds}")
        self._threshold_set = True
        if seconds != self._threshold_s:
            self._threshold_s = seconds
            self._soon = False

    def set_other_zone(self, inside: bool) -> None:
        """Say whether the person is in a zone other than home and its rings.

        Applied at the next fix or tick. Not part of what a glitch undoes:
        it comes from the latest report, whatever its position.
        """
        self._in_other_zone = inside

    def set_rings(self, rings: Iterable[Target]) -> None:
        """Set the zones that enclose the target (the rings around home)."""
        rings = tuple(rings)
        if rings == self._rings:
            return
        self._rings = rings
        # A zone that is new or was edited has no "before": no crossing yet.
        kept = frozenset(rings)
        self._inside &= kept
        self._judged &= kept
        self._spent &= kept
        self._trigger &= kept

    def set_speed_curves(self, curves: SpeedCurves) -> None:
        """Use these typical speeds from the next fix on.

        The estimates worked out at fixes stand until the next one. Only a
        trip restored with no fix yet, which has no speed of its own, uses the
        new typical speed at once: for arriving soon and the overdue rule, and
        for the expected arrival when it was saved without estimates (before
        2.4.0).
        """
        self._curves = curves

    def note_report(self, t: float) -> None:
        """Note a report of the person received at ``t``, whatever becomes of it.

        Every report while out, before it is judged: one merged into the last
        fix, rejected as a jump, without a position, or received while Home
        Assistant is starting, as well as each fix. Saved for a reload, which
        feeds the person's state again: a report not newer than this one is
        news already had.
        """
        self._last_report = t

    def discard_samples(self) -> None:
        """Forget what was kept of the trip home in progress, to learn from.

        What happens at a restart (it is not saved), for a replay of the
        recorder's history to do the same.
        """
        self._samples = ()

    def restore(
        self,
        data: HomewardRestore,
        last_heard: float,
        distance_m: float | None,
        *,
        now: float,
    ) -> bool:
        """Resume a stay and a trip home saved before a restart or reload.

        ``distance_m`` is the person's distance now, for when no fix has been
        received in this run (at startup, the person's state is the one saved
        at shutdown, not a fix). After a reload it is a genuine fix: a saved
        stay is kept if that fix is still there, and the trip's farthest and
        closest points are merged with it. Only while nothing has happened to
        the trip home in this run. From then on both end like any other: the
        next fixes, a stay or the overdue rule decide. ``now`` is when this
        happens: a fix of this run reported before the clock stepped back is
        moved to it first, as the next tick would.
        """
        if self._home:
            return False
        last = self._last
        # Saved before 2.4.0: only the last fix is known.
        received = data.last_heard if data.last_report is None else data.last_report
        # After a reload the person's state is a fix of this run. One newer
        # than the last report received is news the saved estimates do not
        # have; that report fed again (a reload with nothing new) is not, even
        # when it was no fix then (merged, rejected as a jump). Judged on the
        # times as reported, before any is moved for the clock.
        news = last is not None and last.t > received
        p = self._p
        if (
            last is not None
            and last.t - now > p.clock_step_back_s
            and data.last_heard - now <= p.clock_step_back_s
        ):
            # The clock stepped back since the person's state was reported,
            # and the saved times were already moved back with it (a tick
            # came between). Move this run's likewise first, as the next tick
            # would, or that tick would move the saved times back again.
            self._shift_times(now - last.t)
            last = self._last
        if last is None:
            if distance_m is None or not math.isfinite(distance_m) or distance_m < 0:
                return False
            current = distance_m
        else:
            current = last.distance_m
        restored = False
        stay = data.stay
        if stay is not None and self._place_since is None:
            place = _Fix(stay.since, stay.latitude, stay.longitude, stay.distance_m)
            if last is None or _ground_m(place, last) <= self._p.place_radius_m:
                self._anchor = place
                self._anchor_since = stay.since
                self._place_since = stay.since
                self._trip_started = True
                restored = True
        trip = data.trip
        if (
            trip is not None
            and not self._heading
            and self._reason is HomewardReason.INITIAL
        ):
            self._trip_started = True
            self._farthest_m = max(trip.farthest_m, current, self._farthest_m or 0.0)
            self._closest_m = min(trip.closest_m, current)
            self._heading = True
            self._reason = HomewardReason.RESTORED
            self._since = trip.since
            self._estimates = self._saved_estimates(trip, data.last_heard)
            if news and last is not None:
                self._note_estimate(last.t, crossing=False)
            self._soon = trip.arriving_soon
            saved = trip.threshold_s
            if not self._threshold_set and saved is not None:
                # The person's own threshold has not arrived yet (it is set up
                # separately, in any order): until it does, the one arriving
                # soon was judged with stands, rather than the default.
                self._threshold_s = saved
            elif saved != self._threshold_s:
                self._soon = False
            restored = True
        if restored and last is None:
            self._restored_heard = last_heard
            self._restored_distance_m = distance_m
        if restored and self._last_report is None:
            # No report in this run yet: the last one received is still that.
            self._last_report = data.last_report
        return restored

    # -- input -----------------------------------------------------------

    def arrive(self, t: float) -> None:
        """The person is home: the trip home is handed over to learn from."""
        if not self._home:
            last_time = self._last_time()
            if last_time is not None and last_time - t > self._p.clock_step_back_s:
                # The clock stepped back since the last fix: how long the rest
                # of the trip took is unknown, so it is not learned from.
                self._samples = ()
            self._complete_trip(t)
        self._clear_trip()
        self._home = True
        if self._heading:
            self._end(HomewardReason.ARRIVED, t)

    def depart(self, t: float) -> None:
        """The person has left home (never heading home then: arriving ends it)."""
        if not self._home:
            return
        self._clear_trip()
        self._home = False

    def add_fix(
        self,
        report: Fix,
        distance_m: float,
        *,
        speed_mps: float | None,
        still: bool = False,
    ) -> None:
        """Process an accepted fix away from home.

        ``distance_m`` is to the target centre; ``speed_mps`` is the filtered
        closing speed when it is fresh, else None. ``still`` is True when the
        kinematic estimator saw no movement in this fix: it learns how far
        this phone's indoor positions wander (Wi-Fi fixes indoors can bounce
        200-300 m), so it is trusted over the place radius.
        """
        if self._home:
            return
        p = self._p
        t, latitude, longitude = report.timestamp, report.latitude, report.longitude
        last_time = self._last_time()
        if last_time is not None and last_time - t > p.clock_step_back_s:
            # The clock stepped back: keep the gaps between what is stored.
            self._shift_times(t - last_time)
        previous = self._last
        accuracy_m = report.accuracy_m
        fix = _Fix(t, latitude, longitude, distance_m)
        self._restored_heard = self._restored_distance_m = None

        # Staying, or moving on?
        anchor = self._anchor
        radius = max(p.place_radius_m, p.place_accuracy_factor * accuracy_m)
        if anchor is not None and (still or _ground_m(anchor, fix) <= radius):
            moved = False
            if self._place_since is None and t - anchor.t >= p.place_confirm_s:
                self._place_since = anchor.t
        else:
            moved = True
            leaving_stay = self._place_since is not None
            if leaving_stay or not self._trip_started:
                # Leaving a stay (or home): distances are measured from here.
                if previous is not None:
                    start = previous.distance_m
                elif anchor is not None:
                    start = anchor.distance_m  # a stay restored after a restart
                else:
                    start = distance_m
                self._farthest_m = max(start, distance_m)
                self._inward = ()
                self._spent = frozenset()
                self._trip_started = True
            self._anchor = fix
            # Back-dated only on a trip home that was already on through the
            # quiet spell; after a stay, the person has just set off.
            self._anchor_since = (
                self._arrived_at(previous, fix)
                if self._heading and not leaving_stay
                else t
            )
            self._place_since = None
        self._farthest_m = (
            distance_m
            if self._farthest_m is None
            else max(self._farthest_m, distance_m)
        )

        # Crossings of the zones around home, judged on the fix's centre and
        # only between fixes that moved: GPS jitter at a stop near a zone's
        # edge is not a crossing.
        # A zone starts a trip home once per outing (since the last stay), so
        # a road weaving along a zone's edge cannot keep restarting it; and
        # leaving the zone that started the trip ends it.
        entered: frozenset[Target] = frozenset()
        left_trigger = False
        if moved:
            inside = frozenset(
                ring
                for ring in self._rings
                if haversine_m(latitude, longitude, ring.latitude, ring.longitude)
                <= ring.radius_m
            )
            crossed = (inside - self._inside) & self._judged
            left_trigger = bool((self._inside - inside) & self._judged & self._trigger)
            entered = crossed - self._spent
            self._spent |= crossed
            self._inside = inside
            self._judged = frozenset(self._rings)
            if entered:
                self._inward = (*self._inward[-1:], (t, distance_m))
        self._last = fix
        self._trip_speed = self._speed_at(t, distance_m, speed_mps)
        self._evaluate(
            t, speed_mps, entered=entered, left_trigger=left_trigger, new_fix=True
        )
        ring = self._ring_speed(t)
        self._keep_sample(
            _Sample(t, distance_m, speed_mps if ring is None else ring),
            crossing=bool(entered),
        )

    def tick(self, now: float, speed_mps: float | None) -> None:
        """Re-evaluate the time-driven rules."""
        if self._home:
            return
        last_time = self._last_time()
        if last_time is not None and last_time - now > self._p.clock_step_back_s:
            # The clock stepped back: keep the gaps between what is stored.
            self._shift_times(now - last_time)
        self._evaluate(now, speed_mps)

    # -- rules -----------------------------------------------------------

    def _evaluate(
        self,
        now: float,
        speed_mps: float | None,
        *,
        entered: frozenset[Target] = frozenset(),
        left_trigger: bool = False,
        new_fix: bool = False,
    ) -> None:
        distance = self._distance()
        if distance is None:
            return
        self._confirm_quiet_stay(now)
        if new_fix and self._heading:
            # A crossing whose ring-to-ring speed is in use (whatever speed it
            # gives: on foot near home, say), a speed measured over
            # kilometres, is believed at once. Whether it is believed must not
            # hang on the speed itself, or closing faster could mean a later
            # arrival.
            crossing = bool(entered) and self._ring_speed(now) is not None
            self._note_estimate(now, crossing=crossing)
        self._judge_heading(now, distance, speed_mps, entered, left_trigger)
        self._judge_soon(now, distance)

    def _judge_heading(
        self,
        now: float,
        distance: float,
        speed_mps: float | None,
        entered: frozenset[Target],
        left_trigger: bool,
    ) -> None:
        p = self._p
        at_place = self._at_place(now)
        if self._heading:
            closest = (
                distance if self._closest_m is None else min(self._closest_m, distance)
            )
            self._closest_m = closest
            arrival = self._newest_arrival()
            last_time = self._last_time()
            grace = (
                p.homeward_overdue_s
                if arrival is None or last_time is None
                else max(
                    p.homeward_overdue_s,
                    p.homeward_overdue_ratio * (arrival - last_time),
                )
            )
            if at_place:
                self._end(HomewardReason.AT_PLACE, now)
            elif left_trigger or distance - closest >= max(
                p.homeward_cancel_m, p.homeward_cancel_ratio * distance
            ):
                self._end(HomewardReason.MOVING_AWAY, now)
                # Turned round: progress is measured from here again, so the
                # lagging speed filter cannot restart the trip at once.
                self._farthest_m = distance
            elif arrival is not None and now - arrival > grace:
                self._end(HomewardReason.OVERDUE, now)
                # Only new progress or a new zone starts it again: the closing
                # speed can stay "fresh" for longer than the grace.
                self._farthest_m = distance
            return
        if at_place:
            return
        if entered:
            self._start(HomewardReason.ZONE_CROSSING, now, distance)
            self._trigger = entered
            return
        farthest = self._farthest_m
        if (
            farthest is not None
            and speed_mps is not None
            and speed_mps > p.homeward_min_speed_mps
            and farthest - distance
            >= max(p.homeward_min_progress_m, p.homeward_min_progress_ratio * farthest)
        ):
            self._start(HomewardReason.PROGRESS, now, distance)

    def _judge_soon(self, now: float, distance: float) -> None:
        """Turn arriving soon on when due; it stays on until the trip ends."""
        if not self._heading or self._in_other_zone:
            self._soon = False
            return
        if self._soon:
            return
        arrival = self._newest_arrival()
        self._soon = distance <= self._p.arriving_soon_distance_m or (
            arrival is not None and arrival - now <= self._threshold_s
        )

    def _start(self, reason: HomewardReason, now: float, distance: float) -> None:
        self._heading = True
        self._reason = reason
        self._since = now
        self._closest_m = distance
        self._trigger = frozenset()
        self._soon = False
        self._samples = ()
        current = self._current_estimate()
        self._estimates = () if current is None else (current,)

    def _end(self, reason: HomewardReason, now: float) -> None:
        self._heading = False
        self._reason = reason
        self._since = now
        self._closest_m = None
        self._trigger = frozenset()
        self._soon = False
        # Arriving hands the trip over first; any other end is not a trip home.
        self._samples = ()
        self._estimates = ()

    def _keep_sample(self, sample: _Sample, *, crossing: bool) -> None:
        """Keep a fix of the trip home, to learn typical speeds from on arrival.

        Only while heading home, which is when the expected arrival is used:
        time spent going out, wandering about or visiting is not travel home.
        A trip home that ends other than by arriving (a stay, turning back,
        going quiet) drops what was kept. At most one fix a minute (fixes can
        come every few seconds), plus every zone crossing.
        """
        samples = self._samples
        if not self._heading:
            self._samples = ()
        elif not samples or crossing or sample.t - samples[-1].t >= SAMPLE_INTERVAL_S:
            self._samples = (*samples, sample)[-MAX_TRIP_SAMPLES:]

    def _complete_trip(self, arrived_at: float) -> None:
        """Hand over the trip home just completed: the speed achieved from each fix.

        From each kept fix, the time actually taken to the edge of the zone.
        Fixes close to the edge, or just before arriving, are left out: there
        the timing of the reports says more than the journey.
        """
        p = self._p
        radius = self._target.radius_m
        samples: list[TripSample] = []
        for sample in self._samples:
            remaining = sample.distance_m - radius
            elapsed = arrived_at - sample.t
            if remaining < MIN_REMAINING_M or elapsed < MIN_ELAPSED_S:
                continue
            speed = remaining / elapsed
            if speed > p.max_plausible_speed_mps:
                continue
            samples.append(TripSample(sample.distance_m, speed, sample.measured_mps))
        trip = make_trip(arrived_at, samples)
        if trip is not None:
            self._completed.append(trip)

    def _shift_times(self, delta: float) -> None:
        """Move every stored time by ``delta`` (after the clock stepped back)."""

        def shift(value: float | None) -> float | None:
            return None if value is None else value + delta

        if self._last is not None:
            self._last = replace(self._last, t=self._last.t + delta)
        if self._anchor is not None:
            self._anchor = replace(self._anchor, t=self._anchor.t + delta)
        self._anchor_since = shift(self._anchor_since)
        self._place_since = shift(self._place_since)
        self._since = shift(self._since)
        self._restored_heard = shift(self._restored_heard)
        self._inward = tuple((t + delta, d) for t, d in self._inward)
        self._samples = tuple(
            replace(sample, t=sample.t + delta) for sample in self._samples
        )
        self._estimates = tuple(
            replace(estimate, arrival=estimate.arrival + delta)
            for estimate in self._estimates
        )

    def _clear_trip(self) -> None:
        self._last = None
        self._anchor = None
        self._anchor_since = None
        self._place_since = None
        self._trip_started = False
        self._farthest_m = None
        self._inside = frozenset()
        self._judged = frozenset()
        self._spent = frozenset()
        self._trigger = frozenset()
        self._inward = ()
        self._trip_speed = None
        self._samples = ()
        self._estimates = ()
        self._restored_heard = self._restored_distance_m = None

    def _distance(self) -> float | None:
        if self._last is not None:
            return self._last.distance_m
        return self._restored_distance_m

    def _last_time(self) -> float | None:
        if self._last is not None:
            return self._last.t
        return self._restored_heard

    def _arrived_at(self, previous: _Fix | None, fix: _Fix) -> float:
        """Return when the person most likely got to where ``fix`` is.

        After a long quiet spell, a fix a few hundred metres from the last
        one means the person got there long ago, not just now: when even a
        slow walk would have covered the distance, the arrival is taken as
        that walk from the previous fix.
        """
        if previous is None:
            return fix.t
        gap = fix.t - previous.t
        walk = _ground_m(previous, fix) / self._p.on_foot_min_speed_mps
        return previous.t + walk if walk < gap else fix.t

    def _at_place(self, now: float) -> bool:
        since = self._place_since
        return since is not None and now - since >= self._p.place_dwell_s

    def _confirm_quiet_stay(self, now: float) -> None:
        """Confirm a stay from a still fix followed by silence.

        A stationary iPhone may report only every 30-80 minutes. A second fix
        at the same spot at least ``place_still_s`` after the first, and no
        news since, is a stay too once ``place_dwell_s`` has passed. Silence
        after a moving fix is not: a phone driving home can be quiet for 25
        minutes.

        On a trip home that went quiet, a first fix at the stop that came
        long after the person must have got there (a few hundred metres on
        from the last fix, a long time later) is taken to mean they stopped
        soon after that last fix. Only when the trip home was already on
        through the quiet spell: from two fixes alone, "stopped soon after
        the first" and "set off just before the second" look the same, and a
        trip home that has just begun must not be ended by it.
        """
        anchor, last = self._anchor, self._last
        if self._place_since is not None or anchor is None or last is None:
            return
        since = anchor.t
        still_for = last.t - anchor.t
        arrived = self._anchor_since
        if self._heading and arrived is not None and arrived < anchor.t:
            since = arrived
            still_for = max(still_for, anchor.t - arrived)
        p = self._p
        if still_for >= p.place_still_s and now - since >= p.place_dwell_s:
            self._place_since = since

    def _ring_speed(self, now: float) -> float | None:
        """Return the closing speed between the last two inward crossings."""
        if len(self._inward) < 2:
            return None
        (t1, d1), (t2, d2) = self._inward
        window = self._p.ring_speed_window_s
        if now - t1 > window or t2 <= t1 or d1 - d2 < self._p.homeward_min_progress_m:
            return None
        return (d1 - d2) / (t2 - t1)

    def _speed_at(
        self, t: float, distance_m: float, speed_mps: float | None
    ) -> tuple[float, SpeedSource]:
        """Return the speed to expect from a fix at ``t``, and its source."""
        ring = self._ring_speed(t)
        if ring is not None:
            speed, source = trip_speed_mps(distance_m, ring, self._p, self._curves)
            if source is SpeedSource.MEASURED:
                source = SpeedSource.ZONE_CROSSINGS
            return speed, source
        return trip_speed_mps(distance_m, speed_mps, self._p, self._curves)

    def _expected_speed(self) -> tuple[float, SpeedSource] | None:
        """Return the speed behind the estimate (typical after a restart)."""
        if self._trip_speed is not None:
            return self._trip_speed
        distance = self._distance()
        if distance is None:
            return None
        return self._curves.typical_mps(distance, driving=False), SpeedSource.TYPICAL

    def _current_estimate(self) -> HomewardEstimate | None:
        """Return the estimate from the last fix (or restored distance) alone."""
        distance = self._distance()
        last_time = self._last_time()
        speed = self._expected_speed()
        if distance is None or last_time is None or speed is None:
            return None
        remaining = max(distance - self._target.radius_m, 0.0)
        return HomewardEstimate(
            last_time + min(remaining / speed[0], self._p.max_eta_s), *speed
        )

    def _note_estimate(self, now: float, *, crossing: bool) -> None:
        """Keep the estimate worked out at the fix just received (at ``now``).

        An estimate whose arrival has passed without the person arriving is
        known to be wrong, so it no longer counts. A zone crossing whose
        ring-to-ring speed is in use starts afresh from its own estimate.
        """
        current = self._current_estimate()
        if current is None:
            return
        kept = () if crossing else tuple(e for e in self._estimates if e.arrival >= now)
        self._estimates = (*kept, current)[-ESTIMATE_WINDOW:]

    def _estimate(self, now: float) -> HomewardEstimate | None:
        """Return the estimate to publish at ``now`` (its arrival may be past).

        The one shown since the last fix (``_shown``) until its time passes
        with the person still out, when it is known to be wrong: then the one
        estimate due later than it, if any. One due earlier passing changes
        nothing. The one shown is the middle of three, or the newer of two,
        so when it passes at most one is due later: it moves on at most once
        between fixes. Once all have passed, the newest (the snapshot shows
        "now", as before 2.4.0). A trip restored without any (saved before
        2.4.0) has only its own until the next fix.

        Moving on keeps a passed estimate from standing at "now" while a later
        one is known, at a cost: closing faster makes the one shown pass
        sooner, so between fixes a faster trip can show the later arrival for
        a while. At a fix, closing faster never means a later arrival.
        """
        estimates = self._estimates
        if not estimates:
            return self._current_estimate()
        shown = _shown(estimates)
        if shown.arrival >= now:
            return shown
        later = [estimate for estimate in estimates if estimate.arrival >= now]
        if later:
            return min(later, key=lambda estimate: estimate.arrival)
        return estimates[-1]

    def _newest_arrival(self) -> float | None:
        """Return the arrival arriving soon and the overdue rule go by.

        The newest estimate, from the last fix alone (or, restored with no fix
        yet, from the distance saved at the typical speed): exactly what both
        went by before 2.4.0. Not the one published, which follows a change
        one fix late: arriving soon would wait a fix after a queue clears
        (heating that should be on is not), and the overdue rule would end a
        trip home by an estimate the newest already shows to be out of date.
        """
        current = self._current_estimate()
        return None if current is None else current.arrival

    def _saved_estimates(
        self, trip: HomewardTrip, last_heard: float
    ) -> tuple[HomewardEstimate, ...]:
        """Return the estimates of a restored trip home, checked.

        Each one this tracker writes is due no earlier than the fix it was
        worked out at (and those that have passed by the next fix are dropped
        then), and no later than ``max_eta_s`` after it: all lie between when
        the person was last heard from and ``max_eta_s`` after. One that does
        not could not have been written by this tracker (a corrupt or edited
        file), so none of the list is trusted: they only make sense together,
        as when one cannot be read at all (``_estimates_from``). The trip then
        has the typical speed until its next fix.
        """
        estimates = trip.estimates[-ESTIMATE_WINDOW:]
        latest = last_heard + self._p.max_eta_s
        if all(last_heard <= e.arrival <= latest for e in estimates):
            return estimates
        return ()

    # -- output ----------------------------------------------------------

    def still_since(self, now: float) -> float | None:
        """Return when the person got to where they are staying, if staying."""
        if self._home:
            return None
        if self._place_since is not None:
            return self._place_since
        last_time = self._last_time()
        if last_time is not None and now - last_time >= self._p.place_dwell_s:
            return last_time
        return None

    def snapshot(self, now: float) -> HomewardSnapshot:
        """Return what to publish at ``now``."""
        arrival: float | None = None
        estimate: HomewardEstimate | None = None
        if self._heading:
            estimate = self._estimate(now)
            arrival = None if estimate is None else max(estimate.arrival, now)
        return HomewardSnapshot(
            heading=self._heading,
            reason=self._reason,
            since=self._since,
            arrival=arrival,
            speed_mps=None if estimate is None else estimate.speed_mps,
            speed_source=None if estimate is None else estimate.source,
            farthest_m=None if self._home else self._farthest_m,
            still_since=self.still_since(now),
            arriving_soon=self._soon,
        )


@dataclass(frozen=True, slots=True)
class NearHomeConfig:
    """How "near home" is measured for a person."""

    radius_m: float = 3_000.0
    """At this distance or beyond, near home is 0; within it, the person is nearby."""
    full_m: float = 800.0
    """At this distance or closer, near home is 1 and the person is close to home."""
    off_margin_m: float = 150.0
    """Close to home and nearby go off only this far beyond their limit.

    About the worst GPS accuracy used, so a phone lying still near a limit
    does not switch them on and off with every report.
    """

    def __post_init__(self) -> None:
        """Reject values that would break the maths."""
        for name in ("radius_m", "full_m"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value}")
        margin = self.off_margin_m
        if not math.isfinite(margin) or margin < 0:
            raise ValueError(f"off_margin_m must be 0 or more, got {margin}")
        # Also keeps close to home within nearby, however the person moves.
        if self.full_m + margin >= self.radius_m:
            raise ValueError("full_m + off_margin_m must be below radius_m")


def within(
    distance_m: float, limit_m: float, margin_m: float, was: bool | None
) -> bool:
    """Return whether ``distance_m`` counts as within ``limit_m``.

    On at the limit or closer; once on, off only beyond the limit by more than
    ``margin_m``.
    """
    return distance_m <= (limit_m + margin_m if was else limit_m)


def near_home(distance_m: float, config: NearHomeConfig) -> float:
    """Return how near home someone out is, from 0 (far) to 1 (close by).

    Falls linearly from 1 at ``full_m`` to 0 at ``radius_m``, in any
    direction, however long the person stays there.
    """
    if not math.isfinite(distance_m):
        return 0.0
    span = config.radius_m - config.full_m
    return min(1.0, max(0.0, (config.radius_m - distance_m) / span))


def _shown(estimates: tuple[HomewardEstimate, ...]) -> HomewardEstimate:
    """Return the estimate shown at the fix that left ``estimates``.

    The middle by arrival of ``ESTIMATE_WINDOW``; the latest until there are
    that many. A fix keeps only estimates that have not passed, so none had
    then.
    """
    if len(estimates) < ESTIMATE_WINDOW:
        return estimates[-1]
    return sorted(estimates, key=lambda estimate: estimate.arrival)[
        ESTIMATE_WINDOW // 2
    ]


def _ground_m(a: _Fix, b: _Fix) -> float:
    return haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)


_SOURCES = frozenset(source.value for source in SpeedSource)


def _estimates_from(value: object) -> tuple[HomewardEstimate, ...]:
    """Return estimates read back from storage; none when any is unusable.

    They only make sense together (the middle one is published), so a list
    with a bad entry is dropped whole, as is one that is not a list (data
    saved before 2.4.0 has none).
    """
    if not isinstance(value, list):
        return ()
    estimates: list[HomewardEstimate] = []
    for item in value[-ESTIMATE_WINDOW:]:
        if not isinstance(item, dict):
            return ()
        arrival = _number(item.get("arrival"))
        speed = _number(item.get("speed_mps"))
        source = item.get("source")
        if (
            arrival is None
            or speed is None
            or speed <= 0
            or not isinstance(source, str)
            or source not in _SOURCES
        ):
            return ()
        estimates.append(HomewardEstimate(arrival, speed, SpeedSource(source)))
    return tuple(estimates)


def _number(value: object) -> float | None:
    """Return a finite number read back from storage, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
