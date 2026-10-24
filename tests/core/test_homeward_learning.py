"""Trips home handed over for learning typical speeds, and the speeds used."""

from __future__ import annotations

from dataclasses import replace
import math

import pytest

from custom_components.home_eta.core.calibration import (
    MIN_ELAPSED_S,
    MIN_REMAINING_M,
    LearnedTrip,
)
from custom_components.home_eta.core.geo import haversine_m
from custom_components.home_eta.core.homeward import (
    SAMPLE_INTERVAL_S,
    HomewardReason,
    HomewardTracker,
    SpeedSource,
    trip_speed_mps,
)
from custom_components.home_eta.core.kinematic import Fix
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.speeds import BUILT_IN_CURVES, SpeedCurves
from custom_components.home_eta.core.tracker import (
    PersonReport,
    TrackerConfig,
    learn_trips,
)
from tests.sim import HOME_LAT, HOME_LON, HOME_RADIUS_M, T0, TARGET, to_latlon

P = EstimatorParams()
MIN = 60.0


class Phone:
    """Feeds a tracker fixes along an east-west line through home."""

    def __init__(self, tracker: HomewardTracker) -> None:
        self.tracker = tracker
        self.t = T0

    def at(
        self,
        east_m: float,
        *,
        after_s: float = 5 * MIN,
        speed: float | None = 8.0,
        still: bool = False,
        north_m: float = 0.0,
    ) -> None:
        self.t += after_s
        lat, lon = to_latlon(east_m, north_m)
        distance = haversine_m(lat, lon, HOME_LAT, HOME_LON)
        self.tracker.add_fix(
            Fix(lat, lon, 10.0, self.t), distance, speed_mps=speed, still=still
        )

    def wait(self, seconds: float) -> None:
        self.t += seconds
        self.tracker.tick(self.t, None)

    def home(self, *, after_s: float = 2 * MIN) -> list[LearnedTrip]:
        self.t += after_s
        self.tracker.arrive(self.t)
        return self.tracker.take_completed_trips()


def setting_off() -> Phone:
    tracker = HomewardTracker(P, TARGET, at_target=True)
    tracker.depart(T0)
    return Phone(tracker)


def distances(trip: LearnedTrip) -> list[float]:
    return [round(s.distance_m, -1) for s in trip.samples]


def test_a_trip_home_is_learned_while_heading_home() -> None:
    phone = setting_off()
    for east in (2_000, 4_000, 6_000, 8_000):
        phone.at(east, speed=-8.0)
    phone.at(6_000, after_s=4 * MIN)  # 25 % closer: heading home from here
    starts = phone.t
    assert phone.tracker.heading
    for east in (4_000, 2_000):
        phone.at(east, after_s=4 * MIN)
    trips = phone.home()
    assert len(trips) == 1
    trip = trips[0]
    assert trip.arrived_at == phone.t
    assert distances(trip) == [6_000, 4_000, 2_000]
    first = trip.samples[0]
    assert first.speed_mps == pytest.approx(
        (first.distance_m - HOME_RADIUS_M) / (phone.t - starts)
    )
    assert all(s.measured_mps == 8.0 for s in trip.samples)
    # Nothing is left over for the next trip.
    assert phone.tracker.take_completed_trips() == []


def test_wandering_about_at_the_far_end_is_not_travel_home() -> None:
    """Regression: three hours in a town centre were learned as the trip home."""
    phone = setting_off()
    phone.at(20_500, after_s=25 * MIN, speed=-12.0)
    for step in range(18):  # never 15 minutes within 150 m: not a stay
        phone.at(
            20_000 + (step % 3) * 300,
            after_s=10 * MIN,
            north_m=step * 200,
            speed=0.1,
        )
    assert not phone.tracker.heading
    for east in (15_000, 9_000, 4_000):
        phone.at(east, after_s=8 * MIN, speed=12.0)
    (trip,) = phone.home(after_s=5 * MIN)
    assert distances(trip) == [15_000, 9_000, 4_000]
    assert all(s.speed_mps > 8 for s in trip.samples)


def test_a_visit_with_a_single_report_is_not_travel_home() -> None:
    """An iPhone reports on arriving at a friend's, then nothing for hours."""
    phone = setting_off()
    phone.at(12_000, after_s=20 * MIN, speed=-10.0)
    phone.at(9_000, after_s=5 * MIN, speed=8.0)  # closer: heading home...
    assert phone.tracker.heading
    phone.wait(3 * 3_600)  # ...but silent for three hours there
    assert not phone.tracker.heading
    assert phone.tracker.reason is HomewardReason.OVERDUE
    assert phone.home(after_s=10 * MIN) == []


def test_turning_back_drops_the_trip_home() -> None:
    phone = setting_off()
    phone.at(8_000, speed=-8.0)
    phone.at(6_000)
    assert phone.tracker.heading
    phone.at(9_000, speed=-8.0)  # back out: not a trip home after all
    assert phone.tracker.reason is HomewardReason.MOVING_AWAY
    phone.at(7_000)  # closer again: a new trip home, from here
    phone.at(3_800)
    (trip,) = phone.home()
    assert distances(trip) == [7_000, 3_800]


def test_a_stay_on_the_way_starts_the_trip_home_again() -> None:
    phone = setting_off()
    phone.at(3_000, speed=-8.0)
    phone.at(5_000, speed=-8.0)
    for _ in range(6):  # 30 minutes at one place
        phone.at(5_000, speed=0.0, still=True)
    phone.at(3_800, after_s=2 * MIN)
    phone.at(2_000, after_s=4 * MIN)
    (trip,) = phone.home()
    assert distances(trip) == [3_800, 2_000]


def test_arriving_straight_from_a_stay_learns_nothing() -> None:
    """When the person set off is unknown: no fix between the stay and home."""
    phone = setting_off()
    phone.at(1_500, speed=-2.0)
    for _ in range(6):
        phone.at(1_500, speed=0.0, still=True)
    assert phone.home(after_s=20 * MIN) == []


def test_fixes_near_the_edge_or_just_before_arriving_are_left_out() -> None:
    phone = setting_off()
    phone.at(5_000, speed=-8.0)
    phone.at(3_800)
    phone.at(HOME_RADIUS_M + MIN_REMAINING_M - 10, after_s=6 * MIN)
    (trip,) = phone.home(after_s=MIN_ELAPSED_S - 1)
    assert distances(trip) == [3_800]


def test_fixes_are_kept_once_a_minute() -> None:
    phone = setting_off()
    phone.at(6_000, speed=-8.0)
    for step in range(1, 16):  # every 20 s for 5 minutes
        phone.at(6_000 - step * 160, after_s=20)
    (trip,) = phone.home(after_s=10 * MIN)
    # Heading home from 20 % closer (the 8th fix), then every third fix.
    assert SAMPLE_INTERVAL_S == 3 * 20
    assert distances(trip) == [4_720, 4_240, 3_760]


def test_walking_samples_keep_their_measured_speed() -> None:
    phone = setting_off()
    phone.at(2_000, speed=-1.2)
    phone.at(1_400, after_s=8 * MIN, speed=1.25)
    (trip,) = phone.home(after_s=15 * MIN)
    assert [s.measured_mps for s in trip.samples] == [1.25]


def test_undoing_a_glitch_undoes_its_effect_on_the_samples() -> None:
    phone = setting_off()
    phone.at(6_000, speed=-8.0)
    phone.at(4_000)
    saved = phone.tracker.save()
    phone.at(9_000)  # a jump away (ending the trip home), then undone
    phone.tracker.load(saved)
    phone.at(2_000)
    (trip,) = phone.home()
    assert distances(trip) == [4_000, 2_000]


def test_a_clock_step_back_before_arriving_learns_nothing() -> None:
    """How long the rest of the trip took is unknown."""
    phone = setting_off()
    phone.at(8_000, speed=-8.0)
    phone.at(6_000)
    phone.at(3_000, after_s=6 * MIN)
    phone.t -= 10 * MIN  # the clock stepped back before the arrival
    assert phone.home(after_s=4 * MIN) == []
    # A small step back (within the tolerance) is just timing.
    phone = setting_off()
    phone.at(8_000, speed=-8.0)
    phone.at(6_000)
    phone.at(3_000, after_s=6 * MIN)
    phone.t -= 30
    assert len(phone.home(after_s=0)) == 1


def test_learned_speeds_are_used_from_the_next_fix() -> None:
    phone = setting_off()
    phone.at(20_000, speed=-8.0)
    phone.at(15_000, speed=8.0)
    snapshot = phone.tracker.snapshot(phone.t)
    assert snapshot.heading
    built_in = snapshot.speed_mps
    faster = SpeedCurves(
        tuple((d, 2 * v) for d, v in BUILT_IN_CURVES.all_trips),
        tuple((d, 2 * v) for d, v in BUILT_IN_CURVES.driving),
    )
    phone.tracker.set_speed_curves(faster)
    assert phone.tracker.speed_curves is faster
    assert phone.tracker.snapshot(phone.t).speed_mps == built_in  # until news
    phone.at(10_000, speed=None, after_s=MIN)
    distance = haversine_m(*to_latlon(10_000, 0), HOME_LAT, HOME_LON)
    assert phone.tracker.snapshot(phone.t).speed_mps == pytest.approx(
        faster.typical_mps(distance, driving=False)
    )


def test_trip_speed_uses_the_learned_table_and_weight() -> None:
    curves = SpeedCurves(
        ((1_000.0, 1.0), (100_000.0, 1.0)),
        ((1_000.0, 9.0), (100_000.0, 9.0)),
        ((1_000.0, 0.5), (100_000.0, 0.5)),
        ((1_000.0, 0.2), (100_000.0, 0.2)),
    )
    assert trip_speed_mps(10_000, None, P, curves) == (1.0, SpeedSource.TYPICAL)
    speed, _ = trip_speed_mps(10_000, 2.0, P, curves)
    assert speed == pytest.approx(math.sqrt(2.0 * 1.0))
    speed, _ = trip_speed_mps(10_000, 4.0, P, curves)
    assert speed == pytest.approx(4.0**0.2 * 9.0**0.8)
    # On foot near home the measured speed is used whatever is learned.
    assert trip_speed_mps(1_500, 1.4, P, curves) == (1.4, SpeedSource.ON_FOOT)


def test_driving_is_never_expected_later_than_just_below_driving_speed() -> None:
    """A learned driving table slower than walking pace near home."""
    curves = SpeedCurves(
        driving=((1_000.0, 1.2), (100_000.0, 20.0)),
        driving_weight=((1_000.0, 0.0), (100_000.0, 0.0)),
    )
    assert trip_speed_mps(1_200, 2.4, P, curves) == (2.4, SpeedSource.ON_FOOT)
    speed, source = trip_speed_mps(1_200, 2.5, P, curves)
    assert source is SpeedSource.MEASURED
    assert speed == P.on_foot_max_speed_mps


def report(t: float, east_m: float | None, *, home: bool = False) -> PersonReport:
    if east_m is None:
        return PersonReport(t, in_target=home)
    lat, lon = to_latlon(east_m, 0.0)
    return PersonReport(t, home, latitude=lat, longitude=lon, accuracy_m=10.0)


def drive(*, repeat_after: float | None = None) -> list[PersonReport]:
    """Home, a drive out to 8 km, some time there, the drive back.

    ``repeat_after``: the report at that distance written again two minutes
    later, as Home Assistant does at a restart.
    """
    reports = [report(T0, 0.0, home=True)]
    t = T0
    for east in (2_000, 4_000, 6_000, 8_000):
        t += 5 * MIN
        reports.append(report(t, east))
    for east in (6_000, 4_000, 2_000):
        t += 24 * MIN if east == 6_000 else 4 * MIN
        reports.append(report(t, east))
        if east == repeat_after:
            t += 2 * MIN
            reports.append(replace(reports[-1], timestamp=t))
    reports.append(report(t + 4 * MIN, 0.0, home=True))
    return reports


def test_replaying_history_learns_its_trips_home() -> None:
    trips = learn_trips(drive(), TARGET, TrackerConfig())
    assert len(trips) == 1
    # The speed is stale after 24 minutes at 8 km: heading home from 4 km.
    assert distances(trips[0]) == [4_000, 2_000]
    # In any order: replayed by time.
    assert learn_trips(reversed(drive()), TARGET, TrackerConfig()) == trips


def test_a_state_written_again_at_a_restart_drops_the_trip_so_far() -> None:
    """As the integration does: what was kept of a trip is not saved."""
    trips = learn_trips(drive(repeat_after=4_000), TARGET, TrackerConfig())
    (trip,) = trips
    assert distances(trip) == [2_000]
