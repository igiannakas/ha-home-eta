"""Tests for the historical estimator."""

from __future__ import annotations

import statistics

import pytest

from custom_components.home_eta.core.historical import (
    DATA_VERSION,
    HistoricalEstimator,
    LearnedTrip,
)
from custom_components.home_eta.core.params import EstimatorParams

PARAMS = EstimatorParams()
EDGE_M = 100.0
"""The target zone's radius: legs end where the person enters it."""


def learn_walk(
    est: HistoricalEstimator,
    start: float = 0.0,
    *,
    speed: float = 1.4,
    from_m: float = 2_000.0,
    every_s: float = 300.0,
) -> int:
    """Learn one straight walk home that reaches the zone edge on arrival."""
    est.start_leg(start)
    arrival = start + (from_m - EDGE_M) / speed
    t = start
    while t < arrival:
        est.add_fix(t, from_m - speed * (t - start))
        t += every_s
    return est.finish_leg(arrival, EDGE_M)


def test_leg_learns_one_trip() -> None:
    est = HistoricalEstimator(PARAMS)
    learned = learn_walk(est)
    # Fixes at 0, 300, ... 1 200 s plus the arrival at the zone edge.
    assert learned == 6
    assert est.trips_learned == 1
    assert est.stored_trips == 1
    assert est.last_trip_at == pytest.approx(1_900 / 1.4)
    assert not est.leg_open


def test_short_or_sparse_legs_are_not_learned() -> None:
    est = HistoricalEstimator(PARAMS)
    est.start_leg(0)
    for t in (0, 60, 120, 180):
        est.add_fix(t, 2_000 - t)
    assert est.finish_leg(250, EDGE_M) == 0  # under five minutes

    est.start_leg(0)
    for t in (0, 300, 600):
        est.add_fix(t, 2_000 - t)
    assert est.finish_leg(900, EDGE_M) == 0  # three fixes
    assert est.trips_learned == 0
    assert est.stored_trips == 0


def test_only_final_approach_after_farthest_point_is_learned() -> None:
    est = HistoricalEstimator(PARAMS)
    est.start_leg(0)
    for t, distance in (
        (0, 500),
        (300, 1_500),
        (600, 3_000),
        (900, 2_000),
        (1_200, 1_000),
    ):
        est.add_fix(t, distance)
    assert est.finish_leg(1_500, EDGE_M) == 4
    profile = est.export()["trips"][0]["profile"]
    assert profile == [
        [3_000.0, 900.0],
        [2_000.0, 600.0],
        [1_000.0, 300.0],
        [EDGE_M, 0.0],
    ]


def test_fixes_outside_a_leg_are_ignored() -> None:
    est = HistoricalEstimator(PARAMS)
    assert not est.add_fix(0, 1_000)
    assert est.finish_leg(600, EDGE_M) == 0
    est.start_leg(0)
    assert est.add_fix(0, 1_000)
    est.discard_leg()
    assert not est.leg_open
    assert est.leg_fixes == 0


def test_restarting_a_leg_drops_earlier_fixes() -> None:
    est = HistoricalEstimator(PARAMS)
    est.start_leg(0)
    est.add_fix(0, 10_000)
    est.start_leg(5_000)
    assert est.leg_started_at == 5_000
    assert est.leg_fixes == 0


def test_remove_last_fix() -> None:
    est = HistoricalEstimator(PARAMS)
    est.start_leg(0)
    est.add_fix(0, 1_000)
    est.add_fix(60, 900)
    est.remove_last_fix()
    assert est.leg_fixes == 1
    est.remove_last_fix()
    est.remove_last_fix()  # nothing left: no error
    assert est.leg_fixes == 0


def test_estimate_needs_enough_trips_covering_the_distance() -> None:
    est = HistoricalEstimator(PARAMS)
    for i in range(PARAMS.min_hist_trips - 1):
        learn_walk(est, start=i * 10_000.0)
    assert est.estimate(1_000, 0.0) is None
    learn_walk(est, start=99_000.0)
    result = est.estimate(1_000, 0.0)
    assert result is not None
    assert result.trips == PARAMS.min_hist_trips
    assert result.earliest <= result.arrival <= result.latest
    # Trips that started closer than the query cannot say how long it takes.
    assert est.estimate(2_500, 0.0) is None


def test_estimate_is_unbiased_at_every_distance() -> None:
    """Regression: bucket pooling made the ETA a step function, +120 % at 500 m."""
    est = HistoricalEstimator(PARAMS)
    for i in range(20):
        learn_walk(est, start=i * 10_000.0, from_m=3_000.0)
    for distance in (150.0, 500.0, 900.0, 1_200.0, 1_700.0, 2_500.0):
        result = est.estimate(distance, 0.0)
        assert result is not None
        truth = (distance - EDGE_M) / 1.4
        assert result.arrival == pytest.approx(truth, rel=0.01), distance


def test_each_trip_counts_once_however_dense() -> None:
    """Regression: one trip with the app open used to fill a whole bucket."""
    est = HistoricalEstimator(PARAMS)
    for i in range(15):
        learn_walk(est, start=i * 10_000.0)
    before = est.estimate(1_500, 0.0)
    learn_walk(est, start=200_000.0, speed=0.9, every_s=30.0)
    after = est.estimate(1_500, 0.0)
    assert before is not None
    assert after is not None
    assert after.trips == before.trips + 1
    # The median moves by at most one trip's share, not to the dense trip.
    assert after.arrival == pytest.approx(before.arrival, rel=0.02)


def test_band_spans_trip_to_trip_variation() -> None:
    est = HistoricalEstimator(PARAMS)
    speeds = [1.0, 1.2, 1.4, 1.6, 1.8]
    for i, speed in enumerate(speeds):
        learn_walk(est, start=i * 10_000.0, speed=speed)
    result = est.estimate(1_100, 0.0)
    assert result is not None
    times = sorted(1_000 / s for s in speeds)
    assert result.arrival == pytest.approx(statistics.median(times), rel=0.01)
    assert result.earliest == pytest.approx(times[0] + 0.8 * (times[1] - times[0]))
    assert result.latest == pytest.approx(times[3] + 0.2 * (times[4] - times[3]))


def test_last_inward_crossing_is_used() -> None:
    trip = LearnedTrip(
        0.0,
        (
            (3_000.0, 1_000.0),
            (1_000.0, 700.0),
            (1_500.0, 600.0),
            (500.0, 200.0),
            (100.0, 0.0),
        ),
    )
    # 1 200 m was crossed inward twice; the later crossing (1 500 -> 500) counts.
    assert trip.remaining_from(1_200.0) == pytest.approx(600.0 - 0.3 * 400.0)
    assert trip.remaining_from(3_000.0) == 1_000.0
    assert trip.remaining_from(3_001.0) is None
    assert trip.remaining_from(50.0) == 0.0


def test_stored_trips_are_a_bounded_ring() -> None:
    est = HistoricalEstimator(EstimatorParams(hist_max_trips=3))
    for i in range(4):
        learn_walk(est, start=i * 10_000.0)
    assert est.stored_trips == 3
    assert est.trips_learned == 4


def test_long_trips_are_thinned_keeping_both_ends() -> None:
    est = HistoricalEstimator(EstimatorParams(hist_max_trip_points=10))
    learn_walk(est, every_s=10.0)
    profile = est.export()["trips"][0]["profile"]
    assert len(profile) == 10
    assert profile[0][0] == 2_000.0
    assert profile[-1] == [EDGE_M, 0.0]


def test_round_trip_and_reset() -> None:
    est = HistoricalEstimator(PARAMS)
    for i in range(6):
        learn_walk(est, start=i * 10_000.0)
    data = est.as_dict()
    assert data["version"] == DATA_VERSION
    restored = HistoricalEstimator(PARAMS, data)
    assert restored.trips_learned == 6
    assert restored.stored_trips == 6
    assert restored.last_trip_at == est.last_trip_at
    original = est.estimate(1_000, 0.0)
    again = restored.estimate(1_000, 0.0)
    assert original is not None
    assert again is not None
    assert again.arrival == pytest.approx(original.arrival, abs=0.5)
    restored.reset()
    assert restored.trips_learned == 0
    assert restored.stored_trips == 0
    assert restored.last_trip_at is None


def test_malformed_store_is_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    good = {"arrived_at": 1_000.0, "profile": [[1_000.0, 300.0], [100.0, 0.0]]}
    data = {
        "version": DATA_VERSION,
        "trips_learned": True,
        "last_trip_at": 10**400,
        "trips": [
            good,
            "junk",
            {"arrived_at": 1.0},
            {"arrived_at": 1.0, "profile": [[1_000.0, 300.0]]},
            {"arrived_at": 1.0, "profile": [[1_000.0, 300.0], [100.0, 5.0]]},
            {"arrived_at": 1.0, "profile": [[100.0, 300.0], [1_000.0, 0.0]]},
            {"arrived_at": 1.0, "profile": [[1_000.0, 10.0], [500.0, 20.0], [1, 0]]},
            {"arrived_at": 1.0, "profile": [[10**400, 300.0], [100.0, 0.0]]},
            {"arrived_at": 1.0, "profile": [[1_000.0, 1e300], [100.0, 0.0]]},
            {"arrived_at": 1.0, "profile": [[1_000.0, float("nan")], [100.0, 0.0]]},
            {"arrived_at": True, "profile": [[1_000.0, 300.0], [100.0, 0.0]]},
            {"arrived_at": 1.0, "profile": [[True, 300.0], [100.0, 0.0]]},
            {"arrived_at": 1.0, "profile": [[-5.0, 300.0], [100.0, 0.0]]},
        ],
    }
    est = HistoricalEstimator(PARAMS, data)
    assert est.stored_trips == 1
    assert est.trips_learned == 1
    assert est.last_trip_at is None
    assert "malformed" in caplog.text


def test_unknown_or_old_version_ignored(caplog: pytest.LogCaptureFixture) -> None:
    est = HistoricalEstimator(PARAMS, {"version": 1, "buckets": [[[1.0, 2.0]]]})
    assert est.stored_trips == 0
    assert "unsupported version" in caplog.text
