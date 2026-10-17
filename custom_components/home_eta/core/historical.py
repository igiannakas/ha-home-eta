"""Historical ETA: how long it took from this distance on past trips.

Each completed leg that ends inside the target zone is kept as a profile of
(distance from the target, seconds until arrival), from the last time the
person was at their farthest. For a distance, every past trip that started at
least that far out says how long it took from there: its time remaining is
interpolated where it last crossed that distance inwards. The estimate is the
median over those trips, with the 20th and 80th percentiles as the band.

It is deliberately simple and model-free; it is exposed beside the kinematic
ETA, never blended with it. Each trip counts once, however often the phone
reported during it.

A *leg* is the travel that ends at the target. It (re)starts when the person
leaves the target, leaves an ignored zone, or moves again after a stop, so a
day at work or an afternoon at a friend's never counts as travel time.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
import logging
import math
from typing import Any

from .params import EstimatorParams

_LOGGER = logging.getLogger(__name__)

DATA_VERSION = 2

MAX_STORED_DISTANCE_M = 20_037_508.0
"""Half the Earth's circumference: no distance on Earth is larger."""

MAX_STORED_SECONDS = 7 * 86_400.0
"""No learned trip lasts longer than a week."""


@dataclass(frozen=True, slots=True)
class HistoricalEstimate:
    """A history-based arrival estimate. Times are UTC epoch seconds."""

    arrival: float
    earliest: float
    latest: float
    trips: int
    """Past trips the estimate is based on."""


@dataclass(frozen=True, slots=True)
class LearnedTrip:
    """One completed leg, as distance against time remaining."""

    arrived_at: float
    profile: tuple[tuple[float, float], ...]
    """(distance m, seconds remaining) in time order; the first point is the
    farthest, the last is the arrival at the zone edge."""

    @property
    def max_distance_m(self) -> float:
        """Return the farthest distance the trip covers."""
        return self.profile[0][0]

    def remaining_from(self, distance_m: float) -> float | None:
        """Return the seconds it took from ``distance_m``, None if not covered.

        Uses the last inward crossing of that distance, so a wiggle in the
        route counts from where the person was last that far out.
        """
        profile = self.profile
        if distance_m > profile[0][0]:
            return None
        for (far_d, far_s), (near_d, near_s) in reversed(list(pairwise(profile))):
            if far_d >= distance_m >= near_d:
                if far_d == near_d:
                    return near_s
                fraction = (far_d - distance_m) / (far_d - near_d)
                return far_s + (near_s - far_s) * fraction
        return 0.0


class HistoricalEstimator:
    """Learn arrival times from completed legs for one person."""

    def __init__(
        self, params: EstimatorParams, stored: Mapping[str, Any] | None = None
    ) -> None:
        """Initialise, loading previously learned trips if given."""
        self._p = params
        self._trips: deque[LearnedTrip] = deque(maxlen=params.hist_max_trips)
        self._trips_learned = 0
        self._last_trip_at: float | None = None
        self._leg_start: float | None = None
        self._leg: list[tuple[float, float]] = []
        if stored is not None:
            self._load(stored)

    # -- learned data ------------------------------------------------------

    @property
    def trips_learned(self) -> int:
        """Return how many legs have been learned in total."""
        return self._trips_learned

    @property
    def stored_trips(self) -> int:
        """Return how many learned trips are kept."""
        return len(self._trips)

    @property
    def last_trip_at(self) -> float | None:
        """Return when the last learned leg arrived."""
        return self._last_trip_at

    def reset(self) -> None:
        """Forget everything learned and any leg in progress."""
        self._trips.clear()
        self._trips_learned = 0
        self._last_trip_at = None
        self.discard_leg()

    def as_dict(self) -> dict[str, Any]:
        """Return the learned data in its storage form."""
        return {
            "version": DATA_VERSION,
            "trips_learned": self._trips_learned,
            "last_trip_at": self._last_trip_at,
            "trips": [
                {
                    "arrived_at": trip.arrived_at,
                    "profile": [[round(d, 1), round(s, 1)] for d, s in trip.profile],
                }
                for trip in self._trips
            ],
        }

    def export(self) -> dict[str, Any]:
        """Return the learned data for inspection."""
        return {
            "trips_learned": self._trips_learned,
            "last_trip_at": self._last_trip_at,
            "trips": [
                {
                    "arrived_at": trip.arrived_at,
                    "max_distance_m": round(trip.max_distance_m),
                    "duration_s": round(trip.profile[0][1]),
                    "profile": [[round(d, 1), round(s, 1)] for d, s in trip.profile],
                }
                for trip in self._trips
            ],
        }

    # -- legs ----------------------------------------------------------------

    @property
    def leg_open(self) -> bool:
        """Return True while a leg is being recorded."""
        return self._leg_start is not None

    @property
    def leg_started_at(self) -> float | None:
        """Return when the current leg started."""
        return self._leg_start

    @property
    def leg_fixes(self) -> int:
        """Return the number of fixes in the current leg."""
        return len(self._leg)

    def start_leg(self, now: float) -> None:
        """Start (or restart) recording a leg."""
        self._leg_start = now
        self._leg = []

    def discard_leg(self) -> None:
        """Drop the leg in progress without learning from it."""
        self._leg_start = None
        self._leg = []

    def add_fix(self, timestamp: float, distance_m: float) -> bool:
        """Record an accepted fix in the current leg; return True if recorded.

        A fix earlier than the last one (the clock stepped back) makes the
        leg's times meaningless, so the leg starts again from it.
        """
        if self._leg_start is None or not math.isfinite(distance_m):
            return False
        if self._leg and timestamp <= self._leg[-1][0]:
            self.start_leg(timestamp)
        self._leg.append((timestamp, distance_m))
        return True

    def remove_last_fix(self) -> None:
        """Forget the latest fix of the leg (it turned out to be a glitch)."""
        if self._leg:
            self._leg.pop()

    def finish_leg(self, arrival: float, arrival_distance_m: float) -> int:
        """Close the leg on arrival at ``arrival_distance_m`` and learn from it.

        Returns the number of profile points learned; zero when the leg was
        too short or too sparse to trust.
        """
        start = self._leg_start
        leg = self._leg
        self.discard_leg()
        p = self._p
        if start is None or arrival - start < p.min_trip_duration_s:
            return 0
        leg = [(t, d) for t, d in leg if t <= arrival]
        if len(leg) < p.min_trip_fixes:
            return 0
        # Learn only the final approach: from the last time the distance was
        # at its largest, so an outward stretch does not count as travel home.
        peak = max(d for _, d in leg)
        first = max(i for i, (_, d) in enumerate(leg) if d == peak)
        points = [(d, arrival - t) for t, d in leg[first:]]
        edge = min(arrival_distance_m, points[-1][0])
        points.append((edge, 0.0))
        if len(points) < 2 or points[0][0] <= edge:
            return 0
        trip = LearnedTrip(arrival, tuple(_thin(points, p.hist_max_trip_points)))
        self._trips.append(trip)
        self._trips_learned += 1
        self._last_trip_at = arrival
        return len(trip.profile)

    # -- estimate ------------------------------------------------------------

    def covering(self, distance_m: float) -> list[float]:
        """Return the seconds each covering past trip took from a distance."""
        values: list[float] = []
        for trip in self._trips:
            remaining = trip.remaining_from(distance_m)
            if remaining is not None:
                values.append(remaining)
        return values

    def estimate(self, distance_m: float, fix_time: float) -> HistoricalEstimate | None:
        """Return the estimate at a distance, or None with too few trips."""
        values = self.covering(distance_m)
        if len(values) < self._p.min_hist_trips:
            return None
        values.sort()
        return HistoricalEstimate(
            arrival=fix_time + _quantile(values, 0.5),
            earliest=fix_time + _quantile(values, self._p.hist_low_quantile),
            latest=fix_time + _quantile(values, self._p.hist_high_quantile),
            trips=len(values),
        )

    # -- internals -----------------------------------------------------------

    def _load(self, stored: Mapping[str, Any]) -> None:
        """Load stored data, skipping anything malformed."""
        if stored.get("version") != DATA_VERSION:
            _LOGGER.warning(
                "Ignoring learned history with unsupported version %s",
                stored.get("version"),
            )
            return
        trips = stored.get("trips_learned")
        if isinstance(trips, int) and not isinstance(trips, bool) and trips >= 0:
            self._trips_learned = trips
        last = _number(stored.get("last_trip_at"))
        if last is not None:
            self._last_trip_at = last
        items = stored.get("trips")
        if not isinstance(items, list):
            return
        skipped = 0
        for item in items:
            trip = self._parse_trip(item)
            if trip is None:
                skipped += 1
            else:
                self._trips.append(trip)
        self._trips_learned = max(self._trips_learned, len(self._trips))
        if skipped:
            _LOGGER.warning("Skipped %d malformed learned trips", skipped)

    def _parse_trip(self, item: object) -> LearnedTrip | None:
        if not isinstance(item, Mapping):
            return None
        arrived_at = _number(item.get("arrived_at"))
        raw = item.get("profile")
        if arrived_at is None or not isinstance(raw, list) or len(raw) < 2:
            return None
        points: list[tuple[float, float]] = []
        for point in raw:
            if not isinstance(point, Sequence) or len(point) != 2:
                return None
            distance, seconds = _number(point[0]), _number(point[1])
            if (
                distance is None
                or seconds is None
                or not 0 <= distance <= MAX_STORED_DISTANCE_M
                or not 0 <= seconds <= MAX_STORED_SECONDS
            ):
                return None
            points.append((distance, seconds))
        if points[-1][1] != 0 or points[0][0] <= points[-1][0]:
            return None
        if any(later[1] > earlier[1] for earlier, later in pairwise(points)):
            return None
        return LearnedTrip(
            arrived_at, tuple(_thin(points, self._p.hist_max_trip_points))
        )


def _number(value: object) -> float | None:
    """Return a finite float, or None for anything else (bool included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and abs(number) < 1e12 else None


def _thin(points: list[tuple[float, float]], limit: int) -> list[tuple[float, float]]:
    """Keep at most ``limit`` points, evenly spread, always the first and last."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def _quantile(sorted_values: list[float], q: float) -> float:
    """Return the q-quantile of sorted values by linear interpolation."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction
