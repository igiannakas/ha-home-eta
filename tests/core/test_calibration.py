"""Unit tests for learning typical speeds from trips home."""

from __future__ import annotations

from itertools import pairwise
import json
import math
import random

import pytest

from custom_components.home_eta.core.calibration import (
    MAX_RATIO,
    MAX_SAMPLES_PER_TRIP,
    MAX_TRIPS,
    PRIOR_TRIPS,
    RECENT_TRIPS,
    KnotFit,
    LearnedTrip,
    SpeedCalibrator,
    TripSample,
    blended_curve,
    driving_sample,
    fit,
    make_trip,
    trip_weight,
)
from custom_components.home_eta.core.homeward import trip_speed_mps
from custom_components.home_eta.core.kinematic import Target
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.speeds import (
    BUILT_IN_CURVES,
    DRIVING_SPEED_MPS,
    MEASURED_WEIGHT,
    TYPICAL_SPEED_MPS,
    Curve,
    interpolate,
)

DAY = 86_400.0
T0 = 1_760_000_040.0
"""A whole minute: trips are kept to the minute."""
P = EstimatorParams()
KNOT_15KM = 4
"""Index of the 15 km knot in the all-trips table."""
DRIVING_15KM = 5
"""...and in the driving table (it has a knot at about 3.2 km more)."""
HOME = Target(51.5, -0.1, 100.0)
MOVED = Target(51.51, -0.1, 100.0)
"""About 1.1 km north of ``HOME``."""


def trip(at: float, *samples: tuple[float, float, float | None]) -> LearnedTrip:
    """A trip of (distance, achieved speed, measured speed or None) samples."""
    made = make_trip(at, [TripSample(d, v, m) for d, v, m in samples])
    assert made is not None
    return made


def at_15km(
    factor: float, *, at: float = T0, measured: float | None = None
) -> LearnedTrip:
    """A trip with one sample at 15 km, ``factor`` times the built-in speed.

    Measured at that speed unless given: driving, and measured right.
    """
    speed = 9.0 * factor
    return trip(at, (15_000.0, speed, speed if measured is None else measured))


def walked(distance: float, speed: float, *, at: float = T0) -> LearnedTrip:
    return trip(at, (distance, speed, None))


def knot(calibrator: SpeedCalibrator, curve: Curve, index: int) -> KnotFit:
    return calibrator.calibration.fit(curve).knots[index]


def test_nothing_learned_is_the_built_in_tables() -> None:
    calibration = fit((), T0)
    assert calibration.curves == BUILT_IN_CURVES
    for curve in Curve:
        for k in calibration.fit(curve).knots:
            assert k.factor == pytest.approx(1.0)
            assert k.trips == 0
            assert k.weight == MEASURED_WEIGHT
            assert k.weight_trips == 0


def test_one_trip_moves_a_knot_part_of_the_way() -> None:
    calibrator = SpeedCalibrator([walked(15_000.0, 9.0 * 1.2)], T0)
    expected = 1.2 ** (1 / (1 + PRIOR_TRIPS))
    assert knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).factor == pytest.approx(
        expected
    )
    assert knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).trips == pytest.approx(1)
    # Knots it was not near are untouched; driving learned nothing from a
    # walk, and is only lifted to no slower than any trip.
    assert knot(calibrator, Curve.ALL_TRIPS, 1).factor == pytest.approx(1.0)
    driving = knot(calibrator, Curve.DRIVING, DRIVING_15KM)
    assert driving.trips == 0
    assert (
        driving.learned_mps == knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).learned_mps
    )


def test_many_trips_move_it_most_of_the_way() -> None:
    now = T0 + 30 * 3_600
    trips = [walked(15_000.0, 10.8, at=T0 + i * 3_600) for i in range(30)]
    calibrator = SpeedCalibrator(trips, now)
    weight = sum(trip_weight(t.arrived_at, now) for t in trips)
    learned = knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).factor
    assert learned == pytest.approx(1.2 ** (weight / (weight + PRIOR_TRIPS)))
    assert learned > 1.18


def test_a_sample_counts_within_a_factor_of_max_ratio() -> None:
    wild = SpeedCalibrator([walked(15_000.0, 9.0 * 9)], T0)
    capped = SpeedCalibrator([walked(15_000.0, 9.0 * MAX_RATIO)], T0)
    assert knot(wild, Curve.ALL_TRIPS, KNOT_15KM).factor == pytest.approx(
        knot(capped, Curve.ALL_TRIPS, KNOT_15KM).factor
    )


def test_a_trip_votes_once_however_many_samples() -> None:
    one = SpeedCalibrator([walked(15_000.0, 10.8)], T0)
    many = SpeedCalibrator([trip(T0, *[(15_000.0, 10.8, None)] * 10)], T0)
    assert knot(many, Curve.ALL_TRIPS, KNOT_15KM).factor == pytest.approx(
        knot(one, Curve.ALL_TRIPS, KNOT_15KM).factor
    )


def test_a_sample_between_knots_informs_both_by_nearness() -> None:
    # About two thirds of the way from 5 km to 15 km on a log scale.
    distance = 10_400.0
    near_15km = math.log(distance / 5_000.0) / math.log(3.0)
    built_in = interpolate(TYPICAL_SPEED_MPS, distance)
    speed = round(1.1 * built_in, 2)  # as it is kept
    calibrator = SpeedCalibrator([walked(distance, speed)], T0)
    knots = calibrator.calibration.all_trips.knots
    assert knots[3].trips == pytest.approx(1 - near_15km)
    assert knots[4].trips == pytest.approx(near_15km)
    for index in (3, 4):
        share = knots[index].trips
        assert knots[index].factor == pytest.approx(
            math.exp(share * math.log(speed / built_in) / (PRIOR_TRIPS + share))
        )


def test_samples_beyond_the_ends_inform_the_end_knots() -> None:
    calibrator = SpeedCalibrator(
        [trip(T0, (150.0, 1.2 * 1.05, None), (250_000.0, 40.0, None))], T0
    )
    near = calibrator.calibration.all_trips.knots[0]
    assert near.trips == pytest.approx(1.0)
    assert near.factor == pytest.approx(1.05 ** (1 / (1 + PRIOR_TRIPS)))
    far = calibrator.calibration.all_trips.knots[-1]
    assert far.trips == pytest.approx(1.0)
    assert far.factor == pytest.approx(2 ** (1 / (1 + PRIOR_TRIPS)))


def test_older_trips_count_for_less() -> None:
    assert trip_weight(T0, T0) == 1.0
    assert trip_weight(T0, T0 + 60 * DAY) == pytest.approx(0.5)
    assert trip_weight(T0 + DAY, T0) == 1.0  # from the future: as new
    fresh = SpeedCalibrator([walked(15_000.0, 10.8)], T0)
    aged = SpeedCalibrator([walked(15_000.0, 10.8)], T0 + 60 * DAY)
    assert knot(aged, Curve.ALL_TRIPS, KNOT_15KM).trips == pytest.approx(0.5)
    assert (
        1
        < knot(aged, Curve.ALL_TRIPS, KNOT_15KM).factor
        < knot(fresh, Curve.ALL_TRIPS, KNOT_15KM).factor
    )


def test_learned_speeds_never_fall_with_distance() -> None:
    # Trips that say 15 km is much slower than built in, slower than 5 km.
    trips = [trip(T0 + i * 600, (15_000.0, 2.0, None)) for i in range(20)]
    calibrator = SpeedCalibrator(trips, T0 + 20 * 600)
    for curve in Curve:
        speeds = [s for _, s in calibrator.curves.table(curve)]
        assert speeds == sorted(speeds), curve


def test_driving_is_learned_from_driving_samples_only() -> None:
    calibrator = SpeedCalibrator([walked(15_000.0, 4.5)], T0)
    assert knot(calibrator, Curve.DRIVING, DRIVING_15KM).factor == pytest.approx(1.0)
    assert knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).factor < 1
    calibrator = SpeedCalibrator([at_15km(1.15)], T0)
    assert knot(calibrator, Curve.DRIVING, DRIVING_15KM).factor > 1
    assert knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).factor > 1


def test_driving_is_never_slower_than_any_trip() -> None:
    # Fast walks, and drives near home in a queue.
    trips = [
        trip(T0 + i * 600, (1_000.0, 3.9, None), (1_200.0, 1.0, 3.0)) for i in range(40)
    ]
    curves = SpeedCalibrator(trips, T0 + 40 * 600).curves
    for distance in range(300, 120_000, 50):
        assert (
            curves.typical_mps(distance, driving=True)
            >= curves.typical_mps(distance, driving=False) - 1e-9
        )


def test_which_table_a_sample_trains_follows_the_estimate() -> None:
    assert not driving_sample(TripSample(5_000.0, 5.0, None), P)
    assert not driving_sample(TripSample(5_000.0, 5.0, 2.4), P)
    assert driving_sample(TripSample(5_000.0, 5.0, 2.5), P)
    assert blended_curve(TripSample(5_000.0, 5.0, None), P) is None
    assert blended_curve(TripSample(5_000.0, 5.0, 0.3), P) is None  # barely closing
    assert blended_curve(TripSample(1_000.0, 1.3, 1.3), P) is None  # on foot
    assert blended_curve(TripSample(5_000.0, 2.0, 2.0), P) is Curve.ALL_TRIPS
    assert blended_curve(TripSample(1_000.0, 6.0, 6.0), P) is Curve.DRIVING


def test_a_measured_speed_that_overshoots_is_trusted_less() -> None:
    """Fast on the road behind, slower ahead: the typical speed was nearer."""
    trips = [
        trip(T0 + i * 600, (15_000.0, 9.0, 20.0), (20_000.0, 9.5, 22.0))
        for i in range(10)
    ]
    calibrator = SpeedCalibrator(trips, T0 + 10 * 600)
    learned = knot(calibrator, Curve.DRIVING, DRIVING_15KM)
    assert learned.weight < 0.2
    assert learned.weight_trips > 5
    # Walks taught nothing about it.
    assert knot(calibrator, Curve.ALL_TRIPS, KNOT_15KM).weight == MEASURED_WEIGHT


def test_a_measured_speed_that_predicts_is_trusted_more() -> None:
    """Queues some days, a clear road others: only the measurement knew which."""
    trips = [
        trip(T0 + i * 600, (15_000.0, speed, speed), (20_000.0, speed, speed))
        for i, speed in enumerate([4.0, 14.0] * 6)
    ]
    calibrator = SpeedCalibrator(trips, T0 + 12 * 600)
    assert knot(calibrator, Curve.DRIVING, DRIVING_15KM).weight > 0.8


def test_a_measurement_equal_to_the_typical_speed_says_little() -> None:
    same = SpeedCalibrator([at_15km(1.0, measured=9.0)], T0)
    differs = SpeedCalibrator([at_15km(1.0, measured=18.0)], T0)
    assert knot(same, Curve.DRIVING, DRIVING_15KM).weight_trips < 0.01
    assert knot(differs, Curve.DRIVING, DRIVING_15KM).weight_trips == pytest.approx(1)


def test_many_reports_near_the_typical_speed_say_no_more_than_one() -> None:
    """Regression: a trip counted by the sum over its reports, so a phone that
    reports often had its say from deviations that tell nothing."""
    one = SpeedCalibrator([at_15km(1.0, measured=9.0 * 1.13)], T0)
    many = SpeedCalibrator([trip(T0, *[(15_000.0, 9.0, 9.0 * 1.13)] * 30)], T0)
    few = knot(one, Curve.DRIVING, DRIVING_15KM).weight_trips
    assert few < 0.05
    assert knot(many, Curve.DRIVING, DRIVING_15KM).weight_trips == pytest.approx(few)


@pytest.mark.parametrize(("slow", "fast"), [(4.0, 14.0), (3.0, 40.0), (2.6, 60.0)])
def test_a_measurement_that_predicts_is_trusted_however_far_off(
    slow: float, fast: float
) -> None:
    """Regression: only the achieved speed was capped, so the further off a
    measurement that was right, the less it was trusted."""
    trips = [
        trip(T0 + i * 600, (15_000.0, speed, speed), (20_000.0, speed, speed))
        for i, speed in enumerate([slow, fast] * 6)
    ]
    calibrator = SpeedCalibrator(trips, T0 + 12 * 600)
    assert knot(calibrator, Curve.DRIVING, DRIVING_15KM).weight > 0.85


def _random_trips(seed: int, count: int) -> list[LearnedTrip]:
    rng = random.Random(seed)
    return [
        trip(
            T0 + i * 600,
            *[
                (
                    rng.uniform(400, 80_000),
                    rng.uniform(0.6, 30),
                    rng.choice([None, rng.uniform(0.2, 40)]),
                )
                for _ in range(10)
            ],
        )
        for i in range(count)
    ]


def test_weights_stay_between_nothing_and_everything() -> None:
    curves = SpeedCalibrator(_random_trips(7, 60), T0 + 60 * 600).curves
    for curve in Curve:
        assert all(0.0 <= w <= 1.0 for _, w in curves.weights(curve))


@pytest.mark.parametrize("seed", range(8))
def test_closing_faster_never_expects_a_later_arrival_whatever_is_learned(
    seed: int,
) -> None:
    curves = SpeedCalibrator(_random_trips(seed, 40), T0 + 40 * 600).curves
    measured = [P.homeward_min_speed_mps + i / 20 for i in range(1, 700)]
    for distance in (500, 1_000, 2_000, 2_999, 3_000, 4_000, 9_000, 30_000):
        speeds = [trip_speed_mps(distance, m, P, curves)[0] for m in measured]
        assert all(b >= a - 1e-9 for a, b in pairwise(speeds))


def test_adding_a_trip_twice_learns_it_once() -> None:
    calibrator = SpeedCalibrator()
    added = calibrator.adding([at_15km(1.1)], T0)
    assert added is not None
    assert added.adding([at_15km(1.1)], T0) is None
    assert added.adding([at_15km(1.05, at=T0 + 30)], T0) is None  # same arrival
    again = added.adding([at_15km(1.05, at=T0 + 3_600)], T0 + 3_600)
    assert again is not None
    assert len(again.trips) == 2
    assert len(calibrator.trips) == 0  # never changes


def test_marking_the_history_learned_is_a_change() -> None:
    calibrator = SpeedCalibrator()
    learned = calibrator.adding([], T0, history_learned=True)
    assert learned is not None
    assert learned.history_learned
    assert learned.adding([], T0, history_learned=True) is None


def test_trips_are_kept_in_arrival_order_and_old_ones_forgotten() -> None:
    calibrator = SpeedCalibrator(
        [at_15km(1.1, at=T0 + 2 * DAY), at_15km(1.1, at=T0)], T0 + 2 * DAY
    )
    assert [t.arrived_at for t in calibrator.trips] == [T0, T0 + 2 * DAY]
    later = calibrator.adding([at_15km(1.1, at=T0 + 400 * DAY)], T0 + 400 * DAY)
    assert later is not None
    assert [t.arrived_at for t in later.trips] == [T0 + 400 * DAY]


def test_at_most_max_trips_are_kept() -> None:
    trips = [at_15km(1.1, at=T0 + i * 600) for i in range(MAX_TRIPS + 10)]
    calibrator = SpeedCalibrator(trips, T0 + MAX_TRIPS * 600)
    assert len(calibrator.trips) == MAX_TRIPS
    assert calibrator.trips[0].arrived_at == T0 + 10 * 600


def test_relearning_keeps_only_the_given_trips_and_very_recent_ones() -> None:
    calibrator = SpeedCalibrator(
        [at_15km(1.1, at=T0), at_15km(1.1, at=T0 + DAY)], T0 + DAY
    )
    history = [at_15km(1.05, at=T0 - DAY), at_15km(1.05, at=T0 + 20)]
    relearned = calibrator.relearned(history, T0 + DAY, keep_after=T0 + DAY - 600)
    # The history's trips (its copy of T0 replaces the old one) plus the
    # trip learned after it was read.
    assert [t.arrived_at for t in relearned.trips] == [T0 - DAY, T0, T0 + DAY]
    assert relearned.trips[1].samples[0].speed_mps == pytest.approx(9.0 * 1.05)
    assert relearned.history_learned


def test_refitting_ages_the_trips_and_keeps_what_they_changed() -> None:
    calibrator = SpeedCalibrator([walked(15_000.0, 10.8)], T0)
    later = calibrator.refitted(T0 + 60 * DAY)
    assert later.trips == calibrator.trips
    assert knot(later, Curve.ALL_TRIPS, KNOT_15KM).trips == pytest.approx(0.5)
    assert later.calibration.at == T0 + 60 * DAY
    assert later.recent_changes == calibrator.recent_changes


def test_forgetting_drops_everything_and_learns_only_what_comes_after() -> None:
    calibrator = SpeedCalibrator([at_15km(1.1)], T0, place=HOME)
    forgotten = calibrator.forgotten(T0 + DAY, MOVED)
    assert forgotten.trips == ()
    assert forgotten.curves == BUILT_IN_CURVES
    assert forgotten.history_learned
    assert forgotten.learn_after == T0 + DAY
    assert forgotten.place == MOVED
    assert not forgotten.has_moved(MOVED)
    assert forgotten.adding([at_15km(1.1, at=T0 + DAY / 2)], T0 + DAY) is None
    assert forgotten.adding([at_15km(1.1, at=T0 + 2 * DAY)], T0 + 2 * DAY)
    relearned = forgotten.relearned(
        [at_15km(1.1, at=T0 + DAY / 2)], T0 + 2 * DAY, keep_after=T0 + 2 * DAY
    )
    assert relearned.trips == ()


def test_recent_changes_say_what_each_trip_changed() -> None:
    trips = [at_15km(1.3, at=T0 + i * DAY) for i in range(RECENT_TRIPS + 2)]
    calibrator = SpeedCalibrator(trips, T0 + 10 * DAY)
    recent = calibrator.recent_changes
    assert len(recent) == RECENT_TRIPS
    assert recent[0].trip is calibrator.trips[-1]  # newest first
    moved = {(c.curve, round(c.distance_m)) for c in recent[0].changes}
    assert (Curve.ALL_TRIPS, 15_000) in moved
    assert (Curve.DRIVING, 15_000) in moved
    for change in recent[0].changes:
        assert change.factor_after > change.factor_before
    added = calibrator.adding([at_15km(1.3, at=T0 + 20 * DAY)], T0 + 20 * DAY)
    assert added is not None
    assert added.recent_changes[0].trip.arrived_at == T0 + 20 * DAY


def test_make_trip_thins_samples_evenly() -> None:
    samples = [TripSample(1_000.0 + i, 5.0, 5.0) for i in range(100)]
    made = make_trip(T0, samples)
    assert made is not None
    assert len(made.samples) == MAX_SAMPLES_PER_TRIP
    assert made.samples[0] is samples[0]
    assert made.samples[-1] is samples[-1]
    assert make_trip(T0, []) is None
    assert made.from_m == 1_099.0


def test_trips_are_held_rounded_as_they_are_saved() -> None:
    calibrator = SpeedCalibrator(
        [
            trip(T0 + 12.3, (20_004.0, 11.123456, 13.374), (2_000.0, 4.0, None)),
            at_15km(1.1, at=T0 + DAY),
        ],
        T0 + DAY,
        history_learned=True,
        learn_after=T0 - DAY,
    )
    first = calibrator.trips[0]
    assert first.arrived_at == T0  # to the minute
    assert first.samples == (
        TripSample(20_000.0, 11.12, 13.37),
        TripSample(2_000.0, 4.0, None),
    )
    saved = json.loads(json.dumps(calibrator.as_dict()))
    assert "latitude" not in json.dumps(saved)  # no place known: no coordinates
    restored = SpeedCalibrator.from_dict(saved, T0 + DAY)
    assert restored.history_learned
    assert restored.learn_after == T0 - DAY
    assert restored.place is None
    assert restored.trips == calibrator.trips
    assert restored.calibration == calibrator.calibration


def test_a_restart_learns_exactly_what_was_learned_before_it() -> None:
    """Regression: closing at or below 0.05 m/s, or just under driving speed,
    was dropped or moved to another table when loaded, so the speeds learned
    after a restart were faster."""
    rng = random.Random(3)
    trips = [
        trip(
            T0 + i * 3_600 + rng.uniform(0, 59),
            *[
                (
                    rng.uniform(3_000, 30_000),
                    rng.uniform(4, 14),
                    rng.choice([-1.234, -0.04, 0.0, 0.049, 2.449, 2.4951, 9.876]),
                )
                for _ in range(20)
            ],
        )
        for i in range(20)
    ]
    calibrator = SpeedCalibrator(trips, T0 + DAY, place=HOME)
    assert all(len(t.samples) == 20 for t in calibrator.trips)
    restored = SpeedCalibrator.from_dict(
        json.loads(json.dumps(calibrator.as_dict())), T0 + DAY
    )
    assert restored.trips == calibrator.trips
    assert restored.calibration == calibrator.calibration
    assert restored.place == HOME


def test_the_place_is_saved_and_unusable_places_are_unknown() -> None:
    saved = SpeedCalibrator(place=HOME).as_dict()
    assert saved["place"] == {"latitude": 51.5, "longitude": -0.1, "radius_m": 100.0}
    for place in (
        "x",
        {"latitude": 91.0, "longitude": 0.0, "radius_m": 100.0},
        {"latitude": 51.5, "longitude": "x", "radius_m": 100.0},
        {"latitude": 51.5, "longitude": -0.1, "radius_m": 0},
        {"latitude": 51.5, "longitude": -0.1},
    ):
        assert SpeedCalibrator.from_dict({**saved, "place": place}, T0).place is None


def test_the_zone_has_moved_once_further_than_its_radius() -> None:
    assert not SpeedCalibrator().has_moved(MOVED)  # nowhere known yet
    calibrator = SpeedCalibrator([at_15km(1.1)], T0, place=HOME)
    assert not calibrator.has_moved(HOME)
    assert not calibrator.has_moved(Target(51.5005, -0.1, 100.0))  # about 56 m
    assert not calibrator.has_moved(Target(51.5005, -0.1, 30.0))  # smaller too
    assert calibrator.has_moved(Target(51.5015, -0.1, 100.0))  # about 167 m
    assert not calibrator.has_moved(Target(51.5015, -0.1, 200.0))  # grown to cover it
    assert calibrator.has_moved(MOVED)


def test_a_place_can_be_given_to_what_was_learned() -> None:
    calibrator = SpeedCalibrator([at_15km(1.1)], T0)
    located = calibrator.at_place(HOME)
    assert located.place == HOME
    assert calibrator.place is None  # never changes
    assert located.trips == calibrator.trips
    assert located.calibration == calibrator.calibration
    assert located.recent_changes == calibrator.recent_changes
    added = located.adding([at_15km(1.1, at=T0 + DAY)], T0 + DAY)
    assert added is not None
    assert added.place == HOME


@pytest.mark.parametrize(
    "value", [None, [], "x", {"trips": "x"}, {"trips": [1, "x", {"arrived_at": "x"}]}]
)
def test_unusable_saved_data_learns_nothing(value: object) -> None:
    calibrator = SpeedCalibrator.from_dict(value, T0)
    assert calibrator.trips == ()
    assert not calibrator.history_learned
    assert calibrator.learn_after is None
    assert calibrator.curves == BUILT_IN_CURVES


def test_bad_samples_and_trips_are_dropped() -> None:
    good = [5_000.0, 6.0, 6.0]
    saved = {
        "history_learned": True,
        "learn_after": "x",
        "trips": [
            {"arrived_at": T0, "samples": [good, [5_000.0, -1.0, None]]},
            {"arrived_at": T0 + 960, "samples": [[0, 5.0, None], [1.0, 5.0, "y"]]},
            {"arrived_at": float("nan"), "samples": [good]},
            {
                "arrived_at": T0 + 1_980,
                "samples": [good, [1, 2], "z", [1e3, 1e3, None]],
            },
        ],
    }
    calibrator = SpeedCalibrator.from_dict(saved, T0 + 1_980)
    assert calibrator.history_learned
    assert calibrator.learn_after is None
    assert [t.arrived_at for t in calibrator.trips] == [T0, T0 + 1_980]
    assert all(len(t.samples) == 1 for t in calibrator.trips)


def test_closing_at_zero_or_below_is_kept() -> None:
    """The road bending away, or a queue: still a report of the trip home."""
    samples = [[1e3, 5.0, -2.0], [2e3, 5.0, 0]]
    saved = {"trips": [{"arrived_at": T0, "samples": samples}]}
    calibrator = SpeedCalibrator.from_dict(saved, T0)
    assert calibrator.trips[0].samples == (
        TripSample(1_000.0, 5.0, -2.0),
        TripSample(2_000.0, 5.0, 0.0),
    )
    # They teach the typical speed, but not the weight (barely closing).
    assert all(blended_curve(s, P) is None for s in calibrator.trips[0].samples)


def test_samples_that_cannot_be_kept_are_left_out_when_learned() -> None:
    made = trip(
        T0,
        (5_000.0, 6.0, 6.0),
        (5_000.0, 0.004, None),  # 0 m/s to the centimetre
        (5_000.0, 95.0, None),  # implausible
        (5_000.0, 6.0, math.nan),
        (math.inf, 6.0, None),
    )
    calibrator = SpeedCalibrator([made], T0)
    assert calibrator.trips[0].samples == (TripSample(5_000.0, 6.0, 6.0),)
    assert SpeedCalibrator([trip(T0, (5_000.0, 0.004, None))], T0).trips == ()


def test_built_in_tables_are_what_is_learned_from() -> None:
    calibrator = SpeedCalibrator()
    assert [k.distance_m for k in calibrator.calibration.all_trips.knots] == [
        d for d, _ in TYPICAL_SPEED_MPS
    ]
    assert [k.distance_m for k in calibrator.calibration.driving.knots] == [
        d for d, _ in DRIVING_SPEED_MPS
    ]
