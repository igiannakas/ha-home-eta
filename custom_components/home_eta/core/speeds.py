"""Typical speeds of a trip home by distance: the built-in tables and learned ones.

Two curves give the speed to expect for the rest of a trip home from a
straight-line distance: one for any trip (whatever the means, as the built-in
table is), and one for when the person is measured at driving speed. Each is
a table of (distance, speed) knots, interpolated linearly in speed over log
distance and flat beyond its ends. With each goes how much a measured speed
is worth at that distance: the expected speed is
``measured ** weight * typical ** (1 - weight)``, 0.5 (the geometric mean)
built in. ``core/calibration.py`` learns both from the person's own trips
home, starting from the built-in values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
import math
from typing import TypeAlias

# A plain alias, not a ``type`` statement: the core runs on Python 3.11.
SpeedTable: TypeAlias = tuple[tuple[float, float], ...]
"""(distance in metres, value) knots, distances strictly increasing."""

TYPICAL_SPEED_MPS: SpeedTable = (
    (300.0, 1.2),
    (1_000.0, 1.3),
    (2_000.0, 3.0),
    (5_000.0, 6.0),
    (15_000.0, 9.0),
    (35_000.0, 11.0),
    (100_000.0, 20.0),
)
"""Built-in typical door-to-door closing speed for a trip home, by distance.

Interpolated on log distance. Under ~1 km people walk; a few kilometres is a
local drive with parking; 15-35 km is London by train or car (measured 4.7-9.6
m/s by car, ~11 by train); 100 km is mostly motorway. A starting point: each
person's trips home refine it (``core/calibration.py``).
"""

VEHICLE_TYPICAL_MIN_MPS = 4.5
"""Floor on the built-in typical speed when travelling faster than walking.

The table is the typical speed for a trip home of a given length, whatever
the means; under a few kilometres most of those are walked. Someone measured
at vehicle speed is not walking the last kilometre: their typical speed is
that of the last stretch of a drive, junctions and parking included. Eight
drives' first or last 1-2 km from home (16-24 Oct 2026) closed at 2.2 to
8.9 m/s, 4.6 m/s on average (geometric, as the blend is). Without this, a car
1 km out at 6-10 m/s was expected home at about 3 m/s, 2-3 minutes late. It
applies within about 3 km, where the table is slower than this.
"""

MEASURED_WEIGHT = 0.5
"""Built-in weight of a measured speed against the typical one: their
geometric mean, so one fast stretch of motorway or one queue does not swing
the expected arrival."""

VEHICLE_MIN_SPEED_MPS = 1.0
"""Floor on a measured speed before it is blended with the typical one."""


class Curve(StrEnum):
    """Which typical speed: for any trip home, or when driving."""

    ALL_TRIPS = "all_trips"
    DRIVING = "driving"


def interpolate(table: SpeedTable, distance_m: float) -> float:
    """Return a table's speed at ``distance_m``: linear over log distance."""
    if distance_m <= table[0][0]:
        return table[0][1]
    for (d0, v0), (d1, v1) in pairwise(table):
        if distance_m <= d1:
            f = (math.log(distance_m) - math.log(d0)) / (math.log(d1) - math.log(d0))
            return v0 + (v1 - v0) * f
    return table[-1][1]


def typical_speed_mps(distance_m: float) -> float:
    """Return the built-in typical closing speed for a trip home from ``distance_m``."""
    return interpolate(TYPICAL_SPEED_MPS, distance_m)


def _driving_table() -> SpeedTable:
    """Return the built-in table at no less than ``VEHICLE_TYPICAL_MIN_MPS``.

    With a knot where the table reaches the floor, so the result is the same
    as taking the larger of the two at every distance.
    """
    floor = VEHICLE_TYPICAL_MIN_MPS
    knots: list[tuple[float, float]] = []
    for (d0, v0), (d1, v1) in pairwise(TYPICAL_SPEED_MPS):
        knots.append((d0, max(v0, floor)))
        if v0 < floor < v1:
            crossing = d0 * (d1 / d0) ** ((floor - v0) / (v1 - v0))
            knots.append((crossing, floor))
    last_distance, last_speed = TYPICAL_SPEED_MPS[-1]
    knots.append((last_distance, max(last_speed, floor)))
    return tuple(knots)


DRIVING_SPEED_MPS: SpeedTable = _driving_table()
"""Built-in typical speed for the rest of a trip home when driving."""

BUILT_IN_TABLES: dict[Curve, SpeedTable] = {
    Curve.ALL_TRIPS: TYPICAL_SPEED_MPS,
    Curve.DRIVING: DRIVING_SPEED_MPS,
}

BUILT_IN_WEIGHTS: dict[Curve, SpeedTable] = {
    curve: tuple((distance, MEASURED_WEIGHT) for distance, _ in table)
    for curve, table in BUILT_IN_TABLES.items()
}
"""The built-in weight of a measured speed, at the same distances."""


def validate_table(table: SpeedTable, *, weights: bool = False) -> None:
    """Raise ValueError unless ``table`` is usable.

    Distances must increase; speeds must be positive, weights from 0 to 1.
    """
    if not table:
        raise ValueError("a table needs at least one knot")
    previous = 0.0
    for distance, value in table:
        if not (math.isfinite(distance) and distance > previous):
            raise ValueError(f"distances must increase, got {distance} m")
        if weights:
            if not (math.isfinite(value) and 0.0 <= value <= 1.0):
                raise ValueError(f"weights must lie in [0, 1], got {value}")
        elif not (math.isfinite(value) and value > 0):
            raise ValueError(f"speeds must be positive, got {value} m/s")
        previous = distance


def blend(measured_mps: float, typical_mps: float, weight: float) -> float:
    """Return the speed to expect from a measured and a typical one.

    ``measured ** weight * typical ** (1 - weight)``, the measured speed taken
    as at least ``VEHICLE_MIN_SPEED_MPS``. Never slower for a faster
    measurement.
    """
    measured = max(measured_mps, VEHICLE_MIN_SPEED_MPS)
    return math.exp(
        weight * math.log(measured) + (1.0 - weight) * math.log(typical_mps)
    )


@dataclass(frozen=True, slots=True)
class SpeedCurves:
    """The typical speeds a person's expected arrival is worked out with.

    And, with each, the weight of a measured speed against it.
    """

    all_trips: SpeedTable = TYPICAL_SPEED_MPS
    driving: SpeedTable = DRIVING_SPEED_MPS
    all_trips_weight: SpeedTable = BUILT_IN_WEIGHTS[Curve.ALL_TRIPS]
    driving_weight: SpeedTable = BUILT_IN_WEIGHTS[Curve.DRIVING]

    def __post_init__(self) -> None:
        """Reject tables that would break the maths."""
        validate_table(self.all_trips)
        validate_table(self.driving)
        validate_table(self.all_trips_weight, weights=True)
        validate_table(self.driving_weight, weights=True)

    def table(self, curve: Curve) -> SpeedTable:
        """Return one of the two speed tables."""
        return self.driving if curve is Curve.DRIVING else self.all_trips

    def weights(self, curve: Curve) -> SpeedTable:
        """Return one of the two weight tables."""
        return self.driving_weight if curve is Curve.DRIVING else self.all_trips_weight

    def typical_mps(self, distance_m: float, *, driving: bool) -> float:
        """Return the typical speed for the rest of a trip home from ``distance_m``."""
        return interpolate(self.driving if driving else self.all_trips, distance_m)

    def measured_weight(self, distance_m: float, *, driving: bool) -> float:
        """Return the weight of a measured speed at ``distance_m``."""
        weights = self.driving_weight if driving else self.all_trips_weight
        return interpolate(weights, distance_m)

    def expected_mps(
        self, measured_mps: float, distance_m: float, *, driving: bool
    ) -> float:
        """Return the speed to expect from a measured speed at ``distance_m``."""
        return blend(
            measured_mps,
            self.typical_mps(distance_m, driving=driving),
            self.measured_weight(distance_m, driving=driving),
        )


BUILT_IN_CURVES = SpeedCurves()
"""The built-in tables, before anything is learned."""
