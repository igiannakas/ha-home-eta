"""Learn a person's typical speeds of a trip home from their own trips.

Every trip home that ends at home leaves samples: at each report while the
person was heading home, the distance, the speed they actually achieved from
there to the edge of the zone, and the speed measured then (between rings, or
closing), if any. At each distance (knot) of the built-in tables
(``core/speeds.py``), for any trip home and when driving, two things are
learned:

- **The typical speed.** Each trip votes once per knot: the mean log ratio of
  its achieved speeds near the knot to the built-in speed, each counted
  within a factor of ``MAX_RATIO`` and weighted by how near it is (linearly
  over log distance between neighbouring knots). Votes are averaged with
  ``PRIOR_TRIPS`` trips' worth of the built-in speed, so one trip moves a
  knot a little and a handful most of the way. The learned speeds never fall
  with distance (longer trips use faster roads), and driving is never slower
  than any trip: keeping to that can move a knot no trip was near, along
  with its neighbours.
- **How much a measured speed is worth.** The expected speed is
  ``measured ** w * typical ** (1 - w)``. Each trip votes the ``w`` that
  would have predicted its achieved speeds best (least squares on log
  speeds, both within a factor of ``MAX_RATIO``, from 0 to 1), counted by
  how far its measured speeds were from the typical one, not by how many
  there were: a measurement equal to the typical speed says nothing about
  its worth. Averaged with ``PRIOR_WEIGHT_TRIPS`` trips' worth of 0.5.

Older trips count for less: their weight halves every ``HALF_LIFE_DAYS``.
Which table a sample trains follows the rules the expected arrival uses
(``EstimatorParams``): measured at driving speed, the driving table; on foot
close to home, no weight at all (the measured speed is used as it is).

Trips are held exactly as they are saved (rounded, see ``_stored_trip``), so what
is learned after a restart is what was learned before it. The place they
were learned at (the zone's centre and radius) is kept with them, to notice
the zone moving.

Calibrators never change: learning returns a new one, so the work can be
done away from the event loop and whatever reads one sees it whole.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import copy
from dataclasses import dataclass
import math

from .geo import haversine_m, is_valid_coordinate
from .kinematic import Target
from .params import EstimatorParams
from .speeds import (
    BUILT_IN_TABLES,
    MEASURED_WEIGHT,
    VEHICLE_MIN_SPEED_MPS,
    Curve,
    SpeedCurves,
    SpeedTable,
    interpolate,
)

PRIOR_TRIPS = 3.0
"""The built-in speed counts as this many trips at every knot."""

PRIOR_WEIGHT_TRIPS = 3.0
"""The built-in weight of a measured speed counts as this many trips."""

INFORMATIVE_LOG_RATIO = math.log(2.0)
"""A trip whose measured speeds near a knot were this far from the typical
one (a factor of 2, root mean square over log speeds) counts fully towards
the weight there; nearer, proportionally less."""

HALF_LIFE_DAYS = 60.0
"""A trip's weight halves every this many days."""

MAX_RATIO = 3.0
"""An achieved or measured speed counts as at most this many times off the
typical one."""

MIN_REMAINING_M = 300.0
"""Samples closer than this to the zone's edge are not used (timing noise)."""

MIN_ELAPSED_S = 60.0
"""Samples taken less than this before arriving are not used."""

MAX_SAMPLES_PER_TRIP = 30
"""A trip keeps at most this many samples, spread evenly over it."""

MAX_TRIPS = 200
"""At most this many trips are kept, the most recent."""

MIN_TRIP_WEIGHT = 0.02
"""Trips that have aged below this weight are forgotten (after ~11 months)."""

SAME_ARRIVAL_S = 60.0
"""Trips arriving this close together are the same trip (learned twice)."""

RECENT_TRIPS = 5
"""How many of the latest trips' changes are reported."""

MIN_REPORTED_CHANGE = 0.005
"""A change smaller than this (in a factor or a weight) is not reported."""

DECIMALS = 6
"""Learned speeds and weights are kept to a millionth: with nothing learned,
the built-in values come back exactly rather than as floating-point noise."""

# Kept for learning, rounded: enough to learn from, no more.
STORED_ARRIVAL_S = 60.0
STORED_DISTANCE_DECIMALS = -1
STORED_SPEED_DECIMALS = 2
"""Achieved and measured speeds alike. A centimetre a second is far finer
than any measurement: only a sample measured within half of one of a
threshold (driving at 2.5 m/s, say) can change table, and that is noise."""

_DEFAULT_PARAMS = EstimatorParams()
_MAX_STORED_SPEED_MPS = _DEFAULT_PARAMS.max_plausible_speed_mps
"""Achieved speeds above this are not kept (the tracker drops them anyway)."""


@dataclass(frozen=True, slots=True)
class TripSample:
    """One report of a trip home, seen from the arrival."""

    distance_m: float
    """Distance from the target's centre at the report."""
    speed_mps: float
    """Speed achieved from there to the edge of the zone."""
    measured_mps: float | None = None
    """Speed measured at the time (between rings, or closing), if any."""


@dataclass(frozen=True, slots=True)
class LearnedTrip:
    """A completed trip home, as kept for learning (no coordinates)."""

    arrived_at: float
    samples: tuple[TripSample, ...]

    @property
    def from_m(self) -> float:
        """Return the farthest distance the trip was sampled at."""
        return max(sample.distance_m for sample in self.samples)


def make_trip(arrived_at: float, samples: Sequence[TripSample]) -> LearnedTrip | None:
    """Return a trip from its samples, thinned evenly; None without samples."""
    if not samples:
        return None
    if len(samples) > MAX_SAMPLES_PER_TRIP:
        last = len(samples) - 1
        keep = MAX_SAMPLES_PER_TRIP - 1
        samples = [samples[round(i * last / keep)] for i in range(keep + 1)]
    return LearnedTrip(arrived_at, tuple(samples))


def _stored_trip(trip: LearnedTrip) -> LearnedTrip | None:
    """Return ``trip`` as it is saved; None when none of its samples can be kept.

    Rounded: arrival to the minute, distances to 10 m, speeds to 0.01 m/s.
    """
    samples = [
        stored
        for sample in trip.samples
        if (
            stored := _stored_sample(
                sample.distance_m, sample.speed_mps, sample.measured_mps
            )
        )
        is not None
    ]
    arrived_at = round(trip.arrived_at / STORED_ARRIVAL_S) * STORED_ARRIVAL_S
    return make_trip(float(arrived_at), samples)


def _stored_sample(
    distance_m: float, speed_mps: float, measured_mps: float | None
) -> TripSample | None:
    """Return a sample as it is saved, or None when it cannot be kept.

    Any finite measured speed is kept, zero or below too (the road bending
    away, a queue): leaving those out would leave the learned speeds faster.
    """
    if not math.isfinite(distance_m) or not math.isfinite(speed_mps):
        return None
    if measured_mps is not None and not math.isfinite(measured_mps):
        return None
    distance = float(round(distance_m, STORED_DISTANCE_DECIMALS))
    speed = float(round(speed_mps, STORED_SPEED_DECIMALS))
    if distance <= 0 or not 0 < speed <= _MAX_STORED_SPEED_MPS:
        return None
    measured = (
        None
        if measured_mps is None
        else float(round(measured_mps, STORED_SPEED_DECIMALS))
    )
    return TripSample(distance, speed, measured)


@dataclass(frozen=True, slots=True)
class KnotFit:
    """One knot of a learned table."""

    distance_m: float
    built_in_mps: float
    learned_mps: float
    trips: float
    """The weight of the trips behind the speed, after ageing."""
    weight: float = MEASURED_WEIGHT
    """The weight of a measured speed here."""
    weight_trips: float = 0.0
    """The weight of the trips behind that, after ageing."""

    @property
    def factor(self) -> float:
        """Return the correction: learned over built-in speed."""
        return self.learned_mps / self.built_in_mps


@dataclass(frozen=True, slots=True)
class CurveFit:
    """A learned table."""

    curve: Curve
    knots: tuple[KnotFit, ...]

    def table(self) -> SpeedTable:
        """Return the learned (distance, speed) knots."""
        return tuple((knot.distance_m, knot.learned_mps) for knot in self.knots)

    def weights(self) -> SpeedTable:
        """Return the learned (distance, weight of a measured speed) knots."""
        return tuple((knot.distance_m, knot.weight) for knot in self.knots)


@dataclass(frozen=True, slots=True)
class Calibration:
    """Both learned tables, as of a moment."""

    at: float
    all_trips: CurveFit
    driving: CurveFit

    @property
    def curves(self) -> SpeedCurves:
        """Return the tables for the expected arrival."""
        return SpeedCurves(
            self.all_trips.table(),
            self.driving.table(),
            self.all_trips.weights(),
            self.driving.weights(),
        )

    def fit(self, curve: Curve) -> CurveFit:
        """Return one of the two learned tables."""
        return self.driving if curve is Curve.DRIVING else self.all_trips


@dataclass(frozen=True, slots=True)
class KnotChange:
    """How one trip moved one knot: its speed's factor and its weight."""

    curve: Curve
    distance_m: float
    factor_before: float
    factor_after: float
    weight_before: float
    weight_after: float


@dataclass(frozen=True, slots=True)
class TripChange:
    """A trip and what learning it changed."""

    trip: LearnedTrip
    changes: tuple[KnotChange, ...]


def trip_weight(arrived_at: float, now: float) -> float:
    """Return how much a trip counts at ``now``: 1 when new, halving with age."""
    age_days = max(0.0, now - arrived_at) / 86_400.0
    return math.exp(-math.log(2.0) * age_days / HALF_LIFE_DAYS)


def driving_sample(sample: TripSample, params: EstimatorParams) -> bool:
    """Return True when the sample was measured at driving speed."""
    measured = sample.measured_mps
    return measured is not None and measured >= params.on_foot_max_speed_mps


def blended_curve(sample: TripSample, params: EstimatorParams) -> Curve | None:
    """Return the table whose measured-speed weight the sample was estimated with.

    None when the expected arrival used no weight: nothing measured, barely
    closing, or on foot close to home (the measured speed used as it is).
    The same rules as ``homeward.trip_speed_mps``.
    """
    measured = sample.measured_mps
    if measured is None or measured <= params.homeward_min_speed_mps:
        return None
    if measured >= params.on_foot_max_speed_mps:
        return Curve.DRIVING
    if sample.distance_m < params.on_foot_max_distance_m:
        return None
    return Curve.ALL_TRIPS


@dataclass(frozen=True, slots=True)
class _Votes:
    """A trip's say on the typical speeds: (knot, vote, coverage) per table.

    It does not depend on when it is counted or on other trips, so it is
    worked out once per trip.
    """

    all_trips: tuple[tuple[int, float, float], ...]
    driving: tuple[tuple[int, float, float], ...]

    def of(self, curve: Curve) -> tuple[tuple[int, float, float], ...]:
        return self.driving if curve is Curve.DRIVING else self.all_trips


def _speed_votes(trip: LearnedTrip, params: EstimatorParams) -> _Votes:
    limit = math.log(MAX_RATIO)
    result: dict[Curve, tuple[tuple[int, float, float], ...]] = {}
    for curve in Curve:
        table = BUILT_IN_TABLES[curve]
        sums = [0.0] * len(table)
        near = [0.0] * len(table)
        for sample in trip.samples:
            if curve is Curve.DRIVING and not driving_sample(sample, params):
                continue
            ratio = math.log(sample.speed_mps / interpolate(table, sample.distance_m))
            ratio = min(limit, max(-limit, ratio))
            for index, share in _knot_shares(table, sample.distance_m):
                sums[index] += share * ratio
                near[index] += share
        result[curve] = tuple(
            (index, sums[index] / near[index], min(1.0, near[index]))
            for index in range(len(table))
            if near[index] > 0
        )
    return _Votes(result[Curve.ALL_TRIPS], result[Curve.DRIVING])


def fit(
    trips: Iterable[LearnedTrip],
    now: float,
    params: EstimatorParams = _DEFAULT_PARAMS,
) -> Calibration:
    """Return the tables learned from ``trips``, weighted as of ``now``."""
    trips = tuple(trips)
    return _fit(trips, [_speed_votes(trip, params) for trip in trips], now, params)


def _fit(
    trips: Sequence[LearnedTrip],
    votes: Sequence[_Votes],
    now: float,
    params: EstimatorParams,
) -> Calibration:
    ages = [trip_weight(trip.arrived_at, now) for trip in trips]
    all_speeds, all_evidence = _fit_speeds(Curve.ALL_TRIPS, votes, ages)
    all_table = tuple(
        zip((d for d, _ in BUILT_IN_TABLES[Curve.ALL_TRIPS]), all_speeds, strict=True)
    )
    driving_speeds, driving_evidence = _fit_speeds(Curve.DRIVING, votes, ages)
    # Both tables are linear between the driving table's knots (it has every
    # knot of the other), so a floor at each of them holds everywhere.
    driving_speeds = [
        max(speed, round(interpolate(all_table, distance), DECIMALS))
        for speed, (distance, _) in zip(
            driving_speeds, BUILT_IN_TABLES[Curve.DRIVING], strict=True
        )
    ]
    driving_table = tuple(
        zip((d for d, _ in BUILT_IN_TABLES[Curve.DRIVING]), driving_speeds, strict=True)
    )
    weights = _fit_weights(trips, ages, all_table, driving_table, params)
    fits = {}
    for curve, speeds, evidence in (
        (Curve.ALL_TRIPS, all_speeds, all_evidence),
        (Curve.DRIVING, driving_speeds, driving_evidence),
    ):
        curve_weights, weight_evidence = weights[curve]
        fits[curve] = CurveFit(
            curve,
            tuple(
                KnotFit(
                    distance,
                    built_in,
                    speeds[index],
                    evidence[index],
                    curve_weights[index],
                    weight_evidence[index],
                )
                for index, (distance, built_in) in enumerate(BUILT_IN_TABLES[curve])
            ),
        )
    return Calibration(now, fits[Curve.ALL_TRIPS], fits[Curve.DRIVING])


def _fit_speeds(
    curve: Curve, votes: Sequence[_Votes], ages: Sequence[float]
) -> tuple[list[float], list[float]]:
    table = BUILT_IN_TABLES[curve]
    sums = [0.0] * len(table)
    evidence = [0.0] * len(table)
    for trip_votes, age in zip(votes, ages, strict=True):
        for index, vote, coverage in trip_votes.of(curve):
            sums[index] += age * coverage * vote
            evidence[index] += age * coverage
    learned = [
        math.log(speed) + sums[index] / (PRIOR_TRIPS + evidence[index])
        for index, (_, speed) in enumerate(table)
    ]
    learned = _non_decreasing(learned, [PRIOR_TRIPS + e for e in evidence])
    return [round(math.exp(value), DECIMALS) for value in learned], evidence


def _fit_weights(
    trips: Sequence[LearnedTrip],
    ages: Sequence[float],
    all_table: SpeedTable,
    driving_table: SpeedTable,
    params: EstimatorParams,
) -> dict[Curve, tuple[list[float], list[float]]]:
    """Return, per table, each knot's weight of a measured speed and its evidence.

    Against the learned typical speeds: the weight the expected arrival
    should give a measurement, given what it now takes as typical. Achieved
    and measured speeds alike count within a factor of ``MAX_RATIO``, so a
    glitch neither dominates a trip's vote nor pulls it towards 0. A trip
    counts by how far its measured speeds were from the typical one (root
    mean square), and fully only where it was sampled as much as for the
    typical speed: many reports near the typical speed say no more than one.
    """
    learned = {Curve.ALL_TRIPS: all_table, Curve.DRIVING: driving_table}
    limit = math.log(MAX_RATIO)
    sums = {curve: [0.0] * len(BUILT_IN_TABLES[curve]) for curve in Curve}
    evidence = {curve: [0.0] * len(BUILT_IN_TABLES[curve]) for curve in Curve}
    for trip, age in zip(trips, ages, strict=True):
        num = {curve: [0.0] * len(BUILT_IN_TABLES[curve]) for curve in Curve}
        den = {curve: [0.0] * len(BUILT_IN_TABLES[curve]) for curve in Curve}
        mass = {curve: [0.0] * len(BUILT_IN_TABLES[curve]) for curve in Curve}
        for sample in trip.samples:
            curve = blended_curve(sample, params)
            if curve is None:
                continue
            assert sample.measured_mps is not None
            typical = interpolate(learned[curve], sample.distance_m)
            measured = math.log(
                max(sample.measured_mps, VEHICLE_MIN_SPEED_MPS) / typical
            )
            measured = min(limit, max(-limit, measured))
            achieved = math.log(sample.speed_mps / typical)
            achieved = min(limit, max(-limit, achieved))
            for index, share in _knot_shares(BUILT_IN_TABLES[curve], sample.distance_m):
                num[curve][index] += share * measured * achieved
                den[curve][index] += share * measured * measured
                mass[curve][index] += share
        for curve in Curve:
            for index, spread in enumerate(den[curve]):
                if spread <= 0:
                    continue
                vote = min(1.0, max(0.0, num[curve][index] / spread))
                shares = mass[curve][index]
                coverage = min(1.0, spread / shares / INFORMATIVE_LOG_RATIO**2) * min(
                    1.0, shares
                )
                sums[curve][index] += age * coverage * vote
                evidence[curve][index] += age * coverage
    return {
        curve: (
            [
                round(
                    (PRIOR_WEIGHT_TRIPS * MEASURED_WEIGHT + sums[curve][index])
                    / (PRIOR_WEIGHT_TRIPS + evidence[curve][index]),
                    DECIMALS,
                )
                for index in range(len(BUILT_IN_TABLES[curve]))
            ],
            evidence[curve],
        )
        for curve in Curve
    }


def _knot_shares(table: SpeedTable, distance_m: float) -> list[tuple[int, float]]:
    """Return the knots a sample at ``distance_m`` informs, and by how much."""
    if distance_m <= table[0][0]:
        return [(0, 1.0)]
    for index in range(len(table) - 1):
        d0, d1 = table[index][0], table[index + 1][0]
        if distance_m <= d1:
            f = math.log(distance_m / d0) / math.log(d1 / d0)
            return [(index, 1.0 - f), (index + 1, f)]
    return [(len(table) - 1, 1.0)]


def _non_decreasing(values: list[float], weights: list[float]) -> list[float]:
    """Return the weighted least-squares fit to ``values`` that never falls."""
    blocks: list[tuple[float, float, int]] = []  # mean, weight, length
    for value, weight in zip(values, weights, strict=True):
        mean, total, length = value, weight, 1
        while blocks and blocks[-1][0] > mean:
            prev_mean, prev_total, prev_length = blocks.pop()
            mean = (prev_mean * prev_total + mean * total) / (prev_total + total)
            total += prev_total
            length += prev_length
        blocks.append((mean, total, length))
    result: list[float] = []
    for mean, _, length in blocks:
        result.extend([mean] * length)
    return result


class SpeedCalibrator:
    """A person's learned trips, the tables learned from them and recent changes.

    Never changes: ``adding``, ``relearned``, ``refitted`` and ``forgotten``
    return a new calibrator (or None when nothing changed).
    """

    __slots__ = (
        "_calibration",
        "_params",
        "_recent",
        "_trips",
        "_votes",
        "history_learned",
        "learn_after",
        "place",
    )

    def __init__(
        self,
        trips: Iterable[LearnedTrip] = (),
        now: float = 0.0,
        *,
        params: EstimatorParams = _DEFAULT_PARAMS,
        history_learned: bool = False,
        learn_after: float | None = None,
        place: Target | None = None,
    ) -> None:
        """Hold ``trips``, as they are saved, and fit the tables as of ``now``."""
        self._setup(
            _merge((), _stored(trips)),
            now,
            params=params,
            history_learned=history_learned,
            learn_after=learn_after,
            place=place,
        )

    def _setup(
        self,
        trips: tuple[LearnedTrip, ...],
        now: float,
        *,
        params: EstimatorParams,
        history_learned: bool,
        learn_after: float | None,
        place: Target | None,
        known: dict[LearnedTrip, _Votes] | None = None,
        recent: tuple[TripChange, ...] | None = None,
    ) -> None:
        """Fit ``trips``; ``known`` votes and ``recent`` changes are reused."""
        self._params = params
        self.history_learned = history_learned
        """The recorder's history has been learned from."""
        self.learn_after = learn_after
        """Only what happened after this is learned from (the zone moved then)."""
        self.place = place
        """The zone the trips ended at, its centre and radius (None: not known)."""
        self._trips = _kept(trips, now)
        known = known or {}
        self._votes = tuple(
            known.get(trip) or _speed_votes(trip, params) for trip in self._trips
        )
        self._calibration = _fit(self._trips, self._votes, now, params)
        if recent is None:
            self._recent = self._changes_of_latest()
        else:
            kept = set(self._trips)
            self._recent = tuple(change for change in recent if change.trip in kept)

    def _derived(
        self,
        trips: tuple[LearnedTrip, ...],
        now: float,
        *,
        history_learned: bool,
        keep_recent: bool = False,
    ) -> SpeedCalibrator:
        """Return a calibrator like this one holding ``trips``."""
        derived = SpeedCalibrator.__new__(SpeedCalibrator)
        # Another instance of this class: its own setup, not someone else's.
        derived._setup(  # noqa: SLF001
            trips,
            now,
            params=self._params,
            history_learned=history_learned,
            learn_after=self.learn_after,
            place=self.place,
            known=dict(zip(self._trips, self._votes, strict=True)),
            recent=self._recent if keep_recent else None,
        )
        return derived

    @property
    def trips(self) -> tuple[LearnedTrip, ...]:
        """Return the trips learned from, oldest first."""
        return self._trips

    @property
    def calibration(self) -> Calibration:
        """Return the learned tables, as of the last fit."""
        return self._calibration

    @property
    def curves(self) -> SpeedCurves:
        """Return the tables for the expected arrival."""
        return self._calibration.curves

    @property
    def recent_changes(self) -> tuple[TripChange, ...]:
        """Return the latest trips, newest first, and what learning each changed.

        Each is the change it made when learned: the tables just before and
        just after it, both weighted as of its arrival.
        """
        return self._recent

    def adding(
        self,
        trips: Iterable[LearnedTrip],
        now: float,
        *,
        history_learned: bool | None = None,
    ) -> SpeedCalibrator | None:
        """Return a calibrator that has learned from ``trips`` too.

        None when all were known already (and ``history_learned`` is not
        changed): there is nothing to save.
        """
        merged = _merge(self._trips, self._after(_stored(trips)))
        learned = self.history_learned if history_learned is None else history_learned
        if len(merged) == len(self._trips) and learned == self.history_learned:
            return None
        return self._derived(merged, now, history_learned=learned)

    def relearned(
        self, trips: Iterable[LearnedTrip], now: float, *, keep_after: float
    ) -> SpeedCalibrator:
        """Return a calibrator learned afresh from ``trips`` (the history).

        Trips learned after ``keep_after`` are kept: the recorder writes
        states with a delay, so a trip learned moments ago may not be in the
        history yet.
        """
        recent = [trip for trip in self._trips if trip.arrived_at > keep_after]
        return self._derived(
            _merge((), [*self._after(_stored(trips)), *recent]),
            now,
            history_learned=True,
        )

    def refitted(self, now: float) -> SpeedCalibrator:
        """Return the same trips fitted as of ``now`` (they have aged)."""
        return self._derived(
            self._trips, now, history_learned=self.history_learned, keep_recent=True
        )

    def forgotten(self, now: float, place: Target) -> SpeedCalibrator:
        """Return a calibrator that has forgotten everything before ``now``.

        For when the zone has moved to ``place``: what was learned was about
        the old place, and so is the history before now.
        """
        return SpeedCalibrator(
            (),
            now,
            params=self._params,
            history_learned=True,
            learn_after=now,
            place=place,
        )

    def at_place(self, place: Target) -> SpeedCalibrator:
        """Return this calibrator, its trips known to have ended at ``place``."""
        located = copy.copy(self)
        located.place = place
        return located

    def has_moved(self, zone: Target) -> bool:
        """Return True when ``zone`` is no longer the place the trips ended at.

        Moved further than its radius (the larger, old or new). Changes within
        it, and a radius changing, keep what was learned: it still applies.
        """
        place = self.place
        if place is None:
            return False
        moved = haversine_m(
            place.latitude, place.longitude, zone.latitude, zone.longitude
        )
        return moved > max(place.radius_m, zone.radius_m)

    def as_dict(self) -> dict[str, object]:
        """Return what was learned as JSON-serialisable values.

        The trips are held as they are saved (rounded); no coordinates but
        the zone's own centre.
        """
        place = self.place
        return {
            "history_learned": self.history_learned,
            "learn_after": self.learn_after,
            "place": None
            if place is None
            else {
                "latitude": place.latitude,
                "longitude": place.longitude,
                "radius_m": place.radius_m,
            },
            "trips": [
                {
                    "arrived_at": trip.arrived_at,
                    "samples": [
                        [sample.distance_m, sample.speed_mps, sample.measured_mps]
                        for sample in trip.samples
                    ],
                }
                for trip in self._trips
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        now: float,
        *,
        params: EstimatorParams = _DEFAULT_PARAMS,
    ) -> SpeedCalibrator:
        """Return the calibrator saved by ``as_dict``; unusable parts are dropped.

        Saved data comes back from disk, so every trip and sample is checked.
        """
        if not isinstance(value, dict):
            return cls(now=now, params=params)
        trips: list[LearnedTrip] = []
        saved = value.get("trips")
        for item in saved if isinstance(saved, list) else ():
            trip = _trip_from_dict(item)
            if trip is not None:
                trips.append(trip)
        return cls(
            trips,
            now,
            params=params,
            history_learned=value.get("history_learned") is True,
            learn_after=_number(value.get("learn_after")),
            place=_place_from_dict(value.get("place")),
        )

    def _after(self, trips: Iterable[LearnedTrip]) -> list[LearnedTrip]:
        after = self.learn_after
        return [trip for trip in trips if after is None or trip.arrived_at > after]

    def _changes_of_latest(self) -> tuple[TripChange, ...]:
        changes: list[TripChange] = []
        trips, votes = self._trips, self._votes
        for index in range(max(0, len(trips) - RECENT_TRIPS), len(trips)):
            trip = trips[index]
            before = _fit(trips[:index], votes[:index], trip.arrived_at, self._params)
            after = _fit(
                trips[: index + 1], votes[: index + 1], trip.arrived_at, self._params
            )
            changes.append(TripChange(trip, _changes(before, after)))
        return tuple(reversed(changes))


def _kept(trips: tuple[LearnedTrip, ...], now: float) -> tuple[LearnedTrip, ...]:
    kept = tuple(
        trip for trip in trips if trip_weight(trip.arrived_at, now) >= MIN_TRIP_WEIGHT
    )
    return kept[-MAX_TRIPS:]


def _merge(
    known: tuple[LearnedTrip, ...], new: Iterable[LearnedTrip]
) -> tuple[LearnedTrip, ...]:
    """Return ``known`` with the trips of ``new`` not already in it, by arrival."""
    merged = list(known)
    for trip in sorted(new, key=lambda trip: trip.arrived_at):
        if all(
            abs(trip.arrived_at - other.arrived_at) > SAME_ARRIVAL_S for other in merged
        ):
            merged.append(trip)
    merged.sort(key=lambda trip: trip.arrived_at)
    return tuple(merged)


def _changes(before: Calibration, after: Calibration) -> tuple[KnotChange, ...]:
    changes: list[KnotChange] = []
    for curve in Curve:
        for old, new in zip(
            before.fit(curve).knots, after.fit(curve).knots, strict=True
        ):
            if (
                abs(new.factor - old.factor) >= MIN_REPORTED_CHANGE
                or abs(new.weight - old.weight) >= MIN_REPORTED_CHANGE
            ):
                changes.append(
                    KnotChange(
                        curve,
                        old.distance_m,
                        old.factor,
                        new.factor,
                        old.weight,
                        new.weight,
                    )
                )
    return tuple(changes)


def _trip_from_dict(value: object) -> LearnedTrip | None:
    if not isinstance(value, dict):
        return None
    arrived_at = _number(value.get("arrived_at"))
    saved = value.get("samples")
    if arrived_at is None or not isinstance(saved, list):
        return None
    samples: list[TripSample] = []
    for item in saved:
        if not isinstance(item, list) or len(item) != 3:
            continue
        distance, speed = _number(item[0]), _number(item[1])
        measured = None if item[2] is None else _number(item[2])
        unreadable = item[2] is not None and measured is None
        if distance is None or speed is None or unreadable:
            continue
        # Checked and rounded as any trip learned: see ``_stored_trip``.
        sample = _stored_sample(distance, speed, measured)
        if sample is not None:
            samples.append(sample)
    return make_trip(arrived_at, samples)


def _place_from_dict(value: object) -> Target | None:
    if not isinstance(value, dict):
        return None
    latitude = _number(value.get("latitude"))
    longitude = _number(value.get("longitude"))
    radius = _number(value.get("radius_m"))
    if (
        latitude is None
        or longitude is None
        or radius is None
        or not is_valid_coordinate(latitude, longitude)
        or radius <= 0
    ):
        return None
    return Target(latitude, longitude, radius)


def _stored(trips: Iterable[LearnedTrip]) -> list[LearnedTrip]:
    return [stored for trip in trips if (stored := _stored_trip(trip)) is not None]


def _number(value: object) -> float | None:
    """Return a finite number read back from storage, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
