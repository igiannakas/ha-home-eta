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
  and close means on foot, and the measured speed is used as it is.
- **Is the person arriving soon?** On a trip home, once the expected arrival
  is within the person's threshold or they are within 800 m, unless they are
  inside another zone (a shop near home). It then stays on until the trip
  home ends, so an estimate that wobbles round the threshold does not flap.

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
from itertools import pairwise
import math

from .geo import haversine_m, is_valid_coordinate
from .kinematic import Fix, Target
from .params import DEFAULT_ARRIVING_SOON_MIN, EstimatorParams

TYPICAL_SPEED_MPS: tuple[tuple[float, float], ...] = (
    (300.0, 1.2),
    (1_000.0, 1.3),
    (2_000.0, 3.0),
    (5_000.0, 6.0),
    (15_000.0, 9.0),
    (35_000.0, 11.0),
    (100_000.0, 20.0),
)
"""Typical door-to-door closing speed for a trip home from a straight-line distance.

Interpolated on log distance. Under ~1 km people walk; a few kilometres is a
local drive with parking; 15-35 km is London by train or car (measured 4.7-9.6
m/s by car, ~11 by train); 100 km is mostly motorway.
"""

VEHICLE_MIN_SPEED_MPS = 1.0
"""Floor on a measured speed before it is blended with the typical one."""


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
class HomewardSnapshot:
    """What is published about the trip home at an instant."""

    heading: bool
    reason: HomewardReason
    since: float | None
    """When heading home started or last ended."""
    arrival: float | None
    """Expected arrival (never in the past), only while heading home."""
    speed_mps: float | None
    """The speed behind ``arrival``."""
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

    def as_dict(self) -> dict[str, object]:
        """Return the data as JSON-serialisable values."""
        trip, stay = self.trip, self.stay
        return {
            "last_heard": self.last_heard,
            "trip": None
            if trip is None
            else {
                "since": trip.since,
                "farthest_m": trip.farthest_m,
                "closest_m": trip.closest_m,
                "arriving_soon": trip.arriving_soon,
                "threshold_s": trip.threshold_s,
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
        return cls(last_heard, trip, stay)


@dataclass(frozen=True, slots=True)
class _Fix:
    t: float
    latitude: float
    longitude: float
    distance_m: float


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


def typical_speed_mps(distance_m: float) -> float:
    """Return the typical closing speed for a trip home from ``distance_m``."""
    table = TYPICAL_SPEED_MPS
    if distance_m <= table[0][0]:
        return table[0][1]
    for (d0, v0), (d1, v1) in pairwise(table):
        if distance_m <= d1:
            f = (math.log(distance_m) - math.log(d0)) / (math.log(d1) - math.log(d0))
            return v0 + (v1 - v0) * f
    return table[-1][1]


def trip_speed_mps(
    distance_m: float, measured_mps: float | None, params: EstimatorParams
) -> tuple[float, SpeedSource]:
    """Return the speed to expect for the rest of the trip home, and its source.

    A measured speed alone over-trusts one leg (a stretch of motorway, a queue);
    the typical speed alone ignores how this trip is going. Close and slow is
    on foot, which is steady, so the measurement is used directly. Otherwise
    the two are blended geometrically.
    """
    typical = typical_speed_mps(distance_m)
    if measured_mps is None or measured_mps <= params.homeward_min_speed_mps:
        return typical, SpeedSource.TYPICAL
    if (
        measured_mps < params.on_foot_max_speed_mps
        and distance_m < params.on_foot_max_distance_m
    ):
        return max(measured_mps, params.on_foot_min_speed_mps), SpeedSource.ON_FOOT
    return (
        math.sqrt(max(measured_mps, VEHICLE_MIN_SPEED_MPS) * typical),
        SpeedSource.MEASURED,
    )


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
            )
        stay: HomewardStay | None = None
        anchor, since = self._anchor, self._place_since
        if not self._home and anchor is not None and since is not None:
            stay = HomewardStay(
                since, anchor.latitude, anchor.longitude, anchor.distance_m
            )
        if trip is None and stay is None:
            return None
        return HomewardRestore(last_heard, trip, stay)

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

    def restore(
        self, data: HomewardRestore, last_heard: float, distance_m: float | None
    ) -> bool:
        """Resume a stay and a trip home saved before a restart or reload.

        ``distance_m`` is the person's distance now, for when no fix has been
        received in this run (at startup, the person's state is the one saved
        at shutdown, not a fix). After a reload it is a genuine fix: a saved
        stay is kept if that fix is still there, and the trip's farthest and
        closest points are merged with it. Only while nothing has happened to
        the trip home in this run. From then on both end like any other: the
        next fixes, a stay or the overdue rule decide.
        """
        if self._home:
            return False
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
        return restored

    # -- input -----------------------------------------------------------

    def arrive(self, t: float) -> None:
        """The person is home."""
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
        self._evaluate(t, speed_mps, entered=entered, left_trigger=left_trigger)

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
    ) -> None:
        distance = self._distance()
        if distance is None:
            return
        self._confirm_quiet_stay(now)
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
            arrival = self._raw_arrival()
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
        arrival = self._raw_arrival()
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

    def _end(self, reason: HomewardReason, now: float) -> None:
        self._heading = False
        self._reason = reason
        self._since = now
        self._closest_m = None
        self._trigger = frozenset()
        self._soon = False

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
            speed, source = trip_speed_mps(distance_m, ring, self._p)
            if source is SpeedSource.MEASURED:
                source = SpeedSource.ZONE_CROSSINGS
            return speed, source
        return trip_speed_mps(distance_m, speed_mps, self._p)

    def _expected_speed(self) -> tuple[float, SpeedSource] | None:
        """Return the speed behind the estimate (typical after a restart)."""
        if self._trip_speed is not None:
            return self._trip_speed
        distance = self._distance()
        if distance is None:
            return None
        return typical_speed_mps(distance), SpeedSource.TYPICAL

    def _raw_arrival(self) -> float | None:
        """Return the expected arrival, which may be in the past."""
        distance = self._distance()
        last_time = self._last_time()
        speed = self._expected_speed()
        if distance is None or last_time is None or speed is None:
            return None
        remaining = max(distance - self._target.radius_m, 0.0)
        return last_time + min(remaining / speed[0], self._p.max_eta_s)

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
        speed: tuple[float, SpeedSource] | None = None
        if self._heading:
            raw = self._raw_arrival()
            speed = self._expected_speed()
            arrival = None if raw is None else max(raw, now)
        return HomewardSnapshot(
            heading=self._heading,
            reason=self._reason,
            since=self._since,
            arrival=arrival,
            speed_mps=None if speed is None else speed[0],
            speed_source=None if speed is None else speed[1],
            farthest_m=None if self._home else self._farthest_m,
            still_since=self.still_since(now),
            arriving_soon=self._soon,
        )


@dataclass(frozen=True, slots=True)
class NearHomeConfig:
    """How "near home" is measured for a person."""

    radius_m: float = 3_000.0
    """At this distance or beyond, near home is 0."""
    full_m: float = 800.0
    """At this distance or closer, near home is 1."""
    fade_after_s: float | None = 5_400.0
    """After staying this long at one place, near home fades; None never fades."""
    fade_s: float = 3_600.0
    """...to 0 over this long."""

    def __post_init__(self) -> None:
        """Reject values that would break the maths."""
        for name in ("radius_m", "full_m", "fade_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value}")
        if self.full_m >= self.radius_m:
            raise ValueError("full_m must be below radius_m")
        fade_after = self.fade_after_s
        if fade_after is not None and (not math.isfinite(fade_after) or fade_after < 0):
            raise ValueError(f"fade_after_s must be 0 or more, got {fade_after}")


def near_home(
    distance_m: float, still_since: float | None, now: float, config: NearHomeConfig
) -> float:
    """Return how near home someone out is, from 0 (far) to 1 (close by).

    Falls linearly from 1 at ``full_m`` to 0 at ``radius_m``, in any
    direction. A long stay at one place (a friend's next door) fades it back
    to 0, so a nearby afternoon out does not keep the house warm throughout.
    """
    if not math.isfinite(distance_m):
        return 0.0
    span = config.radius_m - config.full_m
    nearness = min(1.0, max(0.0, (config.radius_m - distance_m) / span))
    fade_after = config.fade_after_s
    if nearness > 0 and fade_after is not None and still_since is not None:
        stayed = now - still_since
        if stayed > fade_after:
            nearness *= max(0.0, 1.0 - (stayed - fade_after) / config.fade_s)
    return nearness


def _ground_m(a: _Fix, b: _Fix) -> float:
    return haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)


def _number(value: object) -> float | None:
    """Return a finite number read back from storage, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
