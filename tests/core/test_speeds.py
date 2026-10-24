"""Unit tests for the built-in typical-speed tables."""

from __future__ import annotations

import math

import pytest

from custom_components.home_eta.core.speeds import (
    BUILT_IN_CURVES,
    DRIVING_SPEED_MPS,
    MEASURED_WEIGHT,
    TYPICAL_SPEED_MPS,
    VEHICLE_MIN_SPEED_MPS,
    VEHICLE_TYPICAL_MIN_MPS,
    Curve,
    SpeedCurves,
    blend,
    interpolate,
    typical_speed_mps,
)


def test_interpolation_is_linear_over_log_distance_and_flat_beyond() -> None:
    table = ((1_000.0, 2.0), (4_000.0, 6.0))
    assert interpolate(table, 500.0) == 2.0
    assert interpolate(table, 2_000.0) == pytest.approx(4.0)
    assert interpolate(table, 9_000.0) == 6.0


def test_driving_table_is_the_table_at_no_less_than_the_floor() -> None:
    """The same as taking the larger of the two at every distance (2.1.2)."""
    for distance in range(100, 150_000, 37):
        assert interpolate(DRIVING_SPEED_MPS, distance) == pytest.approx(
            max(typical_speed_mps(distance), VEHICLE_TYPICAL_MIN_MPS)
        )
    distances = [d for d, _ in DRIVING_SPEED_MPS]
    assert {d for d, _ in TYPICAL_SPEED_MPS} <= set(distances)
    # One knot more: where the table reaches the floor (about 3.2 km).
    assert len(distances) == len(TYPICAL_SPEED_MPS) + 1
    assert (
        3_000 < next(d for d in distances if d not in dict(TYPICAL_SPEED_MPS)) < 3_300
    )


def test_built_in_curves() -> None:
    assert BUILT_IN_CURVES.table(Curve.ALL_TRIPS) == TYPICAL_SPEED_MPS
    assert BUILT_IN_CURVES.table(Curve.DRIVING) == DRIVING_SPEED_MPS
    assert BUILT_IN_CURVES.typical_mps(1_000, driving=False) == pytest.approx(1.3)
    assert BUILT_IN_CURVES.typical_mps(1_000, driving=True) == pytest.approx(4.5)


@pytest.mark.parametrize(
    "table",
    [
        (),
        ((1_000.0, 2.0), (1_000.0, 3.0)),
        ((2_000.0, 2.0), (1_000.0, 3.0)),
        ((0.0, 2.0),),
        ((1_000.0, 0.0),),
        ((1_000.0, float("nan")),),
        ((float("inf"), 2.0),),
    ],
)
def test_unusable_tables_are_refused(table: tuple[tuple[float, float], ...]) -> None:
    with pytest.raises(ValueError, match=r"knot|distances|speeds"):
        SpeedCurves(all_trips=table)
    with pytest.raises(ValueError, match=r"knot|distances|speeds"):
        SpeedCurves(driving=table)


@pytest.mark.parametrize("weight", [-0.1, 1.1, float("nan")])
def test_unusable_weights_are_refused(weight: float) -> None:
    with pytest.raises(ValueError, match="weights"):
        SpeedCurves(driving_weight=((1_000.0, weight),))
    with pytest.raises(ValueError, match="weights"):
        SpeedCurves(all_trips_weight=((1_000.0, weight),))


def test_weights_of_nothing_and_everything_are_usable() -> None:
    curves = SpeedCurves(
        all_trips_weight=((1_000.0, 0.0),), driving_weight=((1_000.0, 1.0),)
    )
    assert curves.expected_mps(20.0, 5_000, driving=False) == pytest.approx(
        curves.typical_mps(5_000, driving=False)
    )
    assert curves.expected_mps(20.0, 5_000, driving=True) == pytest.approx(20.0)


def test_the_built_in_blend_is_the_geometric_mean() -> None:
    assert MEASURED_WEIGHT == 0.5
    for curve in Curve:
        assert {w for _, w in BUILT_IN_CURVES.weights(curve)} == {0.5}
        assert [d for d, _ in BUILT_IN_CURVES.weights(curve)] == [
            d for d, _ in BUILT_IN_CURVES.table(curve)
        ]
    assert blend(16.0, 4.0, 0.5) == pytest.approx(8.0)
    assert BUILT_IN_CURVES.expected_mps(16.0, 20_000, driving=True) == pytest.approx(
        math.sqrt(16.0 * typical_speed_mps(20_000))
    )


def test_a_measured_speed_below_the_vehicle_floor_counts_as_the_floor() -> None:
    assert blend(0.4, 9.0, 0.5) == pytest.approx(math.sqrt(VEHICLE_MIN_SPEED_MPS * 9.0))
    assert blend(0.4, 9.0, 0.5) == blend(0.9, 9.0, 0.5)
