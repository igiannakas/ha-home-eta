"""Unit tests for heading home and near home."""

from __future__ import annotations

import math

import pytest

from custom_components.home_eta.core.geo import haversine_m
from custom_components.home_eta.core.homeward import (
    TYPICAL_SPEED_MPS,
    HomewardReason,
    HomewardRestore,
    HomewardTracker,
    NearHomeConfig,
    SpeedSource,
    near_home,
    trip_speed_mps,
    typical_speed_mps,
)
from custom_components.home_eta.core.kinematic import Fix, Target
from custom_components.home_eta.core.params import EstimatorParams
from tests.sim import HOME_LAT, HOME_LON, T0, TARGET, to_latlon

P = EstimatorParams()
MIN = 60.0


def ring(radius_m: float) -> Target:
    """Return a zone around home."""
    return Target(HOME_LAT, HOME_LON, radius_m)


class Phone:
    """Feeds a tracker fixes along an east-west line through home."""

    def __init__(self, tracker: HomewardTracker, t: float = T0) -> None:
        self.tracker = tracker
        self.t = t

    def at(
        self,
        east_m: float,
        *,
        after_s: float = 5 * MIN,
        speed: float | None = None,
        accuracy: float = 10.0,
        north_m: float = 0.0,
    ) -> None:
        """Report a fix ``after_s`` after the previous one."""
        self.t += after_s
        lat, lon = to_latlon(east_m, north_m)
        distance = haversine_m(lat, lon, HOME_LAT, HOME_LON)
        self.tracker.add_fix(Fix(lat, lon, accuracy, self.t), distance, speed_mps=speed)

    def wait(self, seconds: float, speed: float | None = None) -> None:
        """Let time pass with no report."""
        self.t += seconds
        self.tracker.tick(self.t, speed)


def out_of_home() -> tuple[HomewardTracker, Phone]:
    """Return a tracker for someone who has just left home, and their phone."""
    tracker = HomewardTracker(P, TARGET, at_target=True)
    tracker.depart(T0)
    return tracker, Phone(tracker)


# -- speeds ------------------------------------------------------------------


def test_typical_speed_follows_the_table_and_clamps() -> None:
    for distance, speed in TYPICAL_SPEED_MPS:
        assert typical_speed_mps(distance) == pytest.approx(speed)
    assert typical_speed_mps(0.0) == TYPICAL_SPEED_MPS[0][1]
    assert typical_speed_mps(1_000_000.0) == TYPICAL_SPEED_MPS[-1][1]
    speeds = [typical_speed_mps(d) for d in range(100, 200_000, 500)]
    assert speeds == sorted(speeds)


def test_trip_speed_rules() -> None:
    # Nothing measured, or barely closing: the typical speed.
    assert trip_speed_mps(10_000, None, P) == (
        typical_speed_mps(10_000),
        SpeedSource.TYPICAL,
    )
    assert trip_speed_mps(10_000, 0.2, P)[1] is SpeedSource.TYPICAL
    # Close and slow: on foot, at no less than a slow walk.
    assert trip_speed_mps(1_500, 1.4, P) == (1.4, SpeedSource.ON_FOOT)
    assert trip_speed_mps(1_500, 0.6, P) == (
        P.on_foot_min_speed_mps,
        SpeedSource.ON_FOOT,
    )
    # Slow but far is a vehicle in traffic: blended with the typical speed.
    speed, source = trip_speed_mps(20_000, 2.0, P)
    assert source is SpeedSource.MEASURED
    assert speed == pytest.approx(math.sqrt(2.0 * typical_speed_mps(20_000)))
    # Fast: blended, so one fast stretch does not promise an early arrival.
    speed, _ = trip_speed_mps(30_000, 30.0, P)
    assert typical_speed_mps(30_000) < speed < 30.0


# -- near home ---------------------------------------------------------------


def test_near_home_falls_with_distance() -> None:
    config = NearHomeConfig(radius_m=3_000, full_m=800, fade_after_s=None)
    assert near_home(0, None, T0, config) == 1.0
    assert near_home(800, None, T0, config) == 1.0
    assert near_home(1_900, None, T0, config) == pytest.approx(0.5)
    assert near_home(3_000, None, T0, config) == 0.0
    assert near_home(30_000, None, T0, config) == 0.0
    assert near_home(math.inf, None, T0, config) == 0.0


def test_near_home_fades_after_a_long_stay() -> None:
    config = NearHomeConfig(radius_m=3_000, fade_after_s=90 * MIN, fade_s=60 * MIN)
    arrived = T0
    assert near_home(400, arrived, arrived + 90 * MIN, config) == 1.0
    assert near_home(400, arrived, arrived + 120 * MIN, config) == pytest.approx(0.5)
    assert near_home(400, arrived, arrived + 150 * MIN, config) == 0.0
    assert near_home(400, arrived, arrived + 600 * MIN, config) == 0.0
    # Not staying anywhere: no fade.
    assert near_home(400, None, arrived + 600 * MIN, config) == 1.0
    # Fading off.
    never = NearHomeConfig(fade_after_s=None)
    assert near_home(400, arrived, arrived + 600 * MIN, never) == 1.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"radius_m": 0},
        {"radius_m": math.nan},
        {"full_m": 3_000},
        {"radius_m": 500},
        {"fade_s": 0},
        {"fade_after_s": -1},
        {"fade_after_s": math.inf},
    ],
)
def test_near_home_config_rejects_bad_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must"):
        NearHomeConfig(**kwargs)


# -- heading home ------------------------------------------------------------


def test_progress_from_the_farthest_point_starts_the_trip_home() -> None:
    tracker, phone = out_of_home()
    for east in (2_000, 6_000, 10_000):
        phone.at(east, speed=-13.0)
    assert not tracker.heading
    phone.at(8_500, speed=5.0)  # 15 %: not yet
    assert not tracker.heading
    phone.at(7_900, speed=5.0)  # 21 %
    assert tracker.heading
    assert tracker.reason is HomewardReason.PROGRESS
    snap = tracker.snapshot(phone.t)
    assert snap.heading
    assert snap.farthest_m == pytest.approx(10_000, abs=1)
    assert snap.arrival is not None
    assert snap.arrival > phone.t


def test_progress_needs_a_fresh_closing_speed() -> None:
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(7_000, speed=None)  # far closer, but the speed is stale
    assert not tracker.heading
    phone.wait(MIN, speed=0.2)
    assert not tracker.heading
    phone.wait(MIN, speed=4.0)
    assert tracker.heading


def test_progress_needs_at_least_500_m() -> None:
    """Close to home, 20 % is a few steps of GPS jitter: not a trip home."""
    tracker, phone = out_of_home()
    phone.at(1_500, speed=-1.3)
    phone.at(1_150, speed=1.3)  # 23 % but 350 m
    assert not tracker.heading
    phone.at(950, speed=1.3)
    assert tracker.heading


def test_entering_a_zone_around_home_starts_the_trip_home_at_once() -> None:
    tracker, phone = out_of_home()
    tracker.set_rings([ring(4_000), ring(8_000)])
    for east in (3_000, 6_000, 12_000):
        phone.at(east)
    assert not tracker.heading  # crossing outwards
    phone.at(11_000)  # no speed known, little progress
    assert not tracker.heading
    phone.at(7_900)  # into the 8 km zone
    assert tracker.heading
    assert tracker.reason is HomewardReason.ZONE_CROSSING


def test_a_new_zone_is_not_a_crossing() -> None:
    tracker, phone = out_of_home()
    phone.at(6_000)
    tracker.set_rings([ring(8_000)])  # added while the person is inside it
    phone.at(5_900)
    assert not tracker.heading


def test_staying_somewhere_ends_and_prevents_the_trip_home() -> None:
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(4_000, speed=10.0)
    assert tracker.heading
    # A friend's house: a fix there 15 minutes later confirms the stay.
    phone.at(4_020, after_s=15 * MIN, speed=0.0)
    assert tracker.heading
    assert tracker.snapshot(phone.t).still_since == pytest.approx(phone.t - 15 * MIN)
    phone.wait(5 * MIN)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.AT_PLACE
    # Still there an hour later, closer than the farthest point: no trip.
    phone.at(4_010, after_s=60 * MIN, speed=0.6)
    assert not tracker.heading


def test_leaving_a_stay_measures_progress_from_there() -> None:
    tracker, phone = out_of_home()
    phone.at(10_000)
    phone.at(10_010, after_s=30 * MIN)  # stay at 10 km
    phone.at(10_005, after_s=30 * MIN)
    phone.at(14_000, speed=-10.0)  # on to 14 km
    phone.at(12_500, speed=8.0)  # 11 % back from 14 km: not yet
    assert not tracker.heading
    phone.at(11_000, speed=8.0)  # 21 %
    assert tracker.heading


def test_a_brief_stop_does_not_reset_the_farthest_point() -> None:
    tracker, phone = out_of_home()
    phone.at(20_000, speed=-13.0)
    phone.at(19_900, after_s=4 * MIN)  # a station stop
    phone.at(15_500, after_s=4 * MIN, speed=15.0)
    assert tracker.heading


def test_moving_back_out_ends_the_trip_home_without_flapping() -> None:
    tracker, phone = out_of_home()
    phone.at(30_000, speed=-13.0)
    phone.at(22_000, speed=13.0)
    assert tracker.heading
    phone.at(20_000, speed=13.0)
    phone.at(22_000, speed=13.0)  # 2 km back out: within 15 % of 22 km
    assert tracker.heading
    phone.at(24_000, speed=6.0)  # 4 km: ends, though the filter still says closing
    assert not tracker.heading
    assert tracker.reason is HomewardReason.MOVING_AWAY
    phone.wait(MIN, speed=6.0)
    assert not tracker.heading
    phone.at(25_000, speed=-8.0)
    phone.at(20_500, speed=10.0)  # 18 % closer than 25 km: not yet
    assert not tracker.heading
    phone.at(19_900, speed=10.0)  # 20 % closer than 25 km
    assert tracker.heading


def test_no_news_long_after_the_expected_arrival_ends_the_trip_home() -> None:
    tracker, phone = out_of_home()
    phone.at(20_000, speed=-13.0)
    phone.at(15_000, speed=10.0)
    snap = tracker.snapshot(phone.t)
    assert snap.arrival is not None
    grace = max(
        P.homeward_overdue_s, P.homeward_overdue_ratio * (snap.arrival - phone.t)
    )
    overdue_at = snap.arrival + grace
    # The speed filter goes stale; the estimate made at the last fix stands.
    phone.wait(20 * MIN)
    assert tracker.snapshot(phone.t).arrival == snap.arrival
    phone.wait(overdue_at - phone.t - MIN)
    assert tracker.heading
    published = tracker.snapshot(phone.t).arrival
    assert published == phone.t  # never in the past
    phone.wait(2 * MIN)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.OVERDUE


def test_arriving_ends_the_trip_and_forgets_it() -> None:
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(7_000, speed=10.0)
    assert tracker.heading
    tracker.arrive(phone.t + 10 * MIN)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.ARRIVED
    tracker.depart(phone.t + 20 * MIN)
    phone.t += 20 * MIN
    # Closing at 3 km would be well inside the old farthest point (10 km),
    # but that belonged to the last trip.
    phone.at(3_000, speed=5.0)
    assert not tracker.heading
    assert tracker.snapshot(phone.t).farthest_m == pytest.approx(3_000, abs=1)


def test_ring_to_ring_speed_drives_the_estimate() -> None:
    tracker, phone = out_of_home()
    tracker.set_rings([ring(4_000), ring(8_000), ring(16_000)])
    phone.at(20_000, after_s=30 * MIN)
    phone.at(15_900, after_s=10 * MIN)
    phone.at(7_900, after_s=10 * MIN)  # 8 km in 10 min: 13.3 m/s
    snap = tracker.snapshot(phone.t)
    assert snap.speed_source is SpeedSource.ZONE_CROSSINGS
    expected = math.sqrt(8_000 / 600 * typical_speed_mps(7_900))
    assert snap.speed_mps == pytest.approx(expected, rel=0.01)
    assert snap.arrival == pytest.approx(
        phone.t + (7_900 - TARGET.radius_m) / expected, abs=5
    )


def test_ring_speed_needs_real_progress_between_crossings() -> None:
    tracker, phone = out_of_home()
    tracker.set_rings([ring(8_000)])
    phone.at(8_100)
    phone.at(7_900)  # in
    phone.at(8_100)  # out
    phone.at(7_950)  # in again, no progress between the two
    snap = tracker.snapshot(phone.t)
    assert snap.speed_source is SpeedSource.TYPICAL


def test_snapshot_away_from_a_trip() -> None:
    tracker, phone = out_of_home()
    phone.at(5_000)
    snap = tracker.snapshot(phone.t)
    assert not snap.heading
    assert snap.arrival is None
    assert snap.speed_source is None
    assert snap.still_since is None
    # Silence: the last fix counts as where the person is staying.
    phone.wait(P.place_dwell_s)
    assert tracker.snapshot(phone.t).still_since == pytest.approx(
        phone.t - P.place_dwell_s
    )


def test_save_and_load_undo_a_fix() -> None:
    tracker, phone = out_of_home()
    tracker.set_rings([ring(4_000)])
    phone.at(5_000)
    saved = tracker.save()
    before = tracker.snapshot(phone.t)
    phone.at(3_500)  # a jump into the zone
    assert tracker.heading
    tracker.load(saved)
    assert not tracker.heading
    assert tracker.snapshot(phone.t - 5 * MIN) == before


def test_restore_resumes_and_then_ends_like_any_trip() -> None:
    tracker = HomewardTracker(P, TARGET)
    data = HomewardRestore(since=T0, farthest_m=30_000, closest_m=20_000)
    assert tracker.restore(data, T0 + 10 * MIN, 21_000)
    assert tracker.heading
    assert tracker.reason is HomewardReason.RESTORED
    snap = tracker.snapshot(T0 + 11 * MIN)
    assert snap.arrival is not None
    assert snap.farthest_m == 30_000
    # The restored closest point still counts for moving away.
    assert tracker.restore_data() == HomewardRestore(T0, 30_000, 20_000)
    trip = snap.arrival - (T0 + 10 * MIN)
    grace = max(P.homeward_overdue_s, P.homeward_overdue_ratio * trip)
    tracker.tick(snap.arrival + grace - MIN, None)
    assert tracker.heading
    tracker.tick(snap.arrival + grace + MIN, None)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.OVERDUE


def test_restore_merges_with_a_fix_already_received() -> None:
    """After a reload the person's state is a genuine fix: kept, and merged."""
    data = HomewardRestore(since=T0 - 30 * MIN, farthest_m=30_000, closest_m=20_000)
    tracker, phone = out_of_home()
    phone.at(19_000)
    assert tracker.restore(data, T0 - MIN, None)
    assert tracker.heading
    saved = tracker.restore_data()
    assert saved is not None
    assert saved.since == T0 - 30 * MIN
    assert saved.farthest_m == 30_000
    assert saved.closest_m == pytest.approx(19_000, abs=1)
    assert tracker.snapshot(phone.t).farthest_m == 30_000


def test_restore_is_refused_at_home_after_a_change_or_without_a_distance() -> None:
    data = HomewardRestore(since=T0, farthest_m=30_000, closest_m=20_000)
    home = HomewardTracker(P, TARGET, at_target=True)
    assert not home.restore(data, T0, 21_000)
    assert not HomewardTracker(P, TARGET).restore(data, T0, None)
    # A trip home that already started and ended in this run is not undone.
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(7_000, speed=10.0)
    phone.at(11_000, speed=-13.0)
    assert tracker.reason is HomewardReason.MOVING_AWAY
    assert not tracker.restore(data, T0, 11_000)


def test_moving_the_target_ends_the_trip() -> None:
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(7_000, speed=10.0)
    assert tracker.heading
    moved = Target(HOME_LAT + 0.01, HOME_LON, TARGET.radius_m)
    tracker.set_target(moved, phone.t)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.ZONE_CHANGED
    assert tracker.restore_data() is None


def test_diagnostics_have_no_coordinates() -> None:
    tracker, phone = out_of_home()
    tracker.set_rings([ring(4_000)])
    phone.at(3_000)
    diag = tracker.diagnostics()
    assert "latitude" not in str(diag)
    assert diag["zones"] == 1
    assert diag["zones_inside"] == 1


def test_a_still_fix_then_silence_is_a_stay() -> None:
    """A stationary iPhone may say nothing for an hour after arriving."""
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(4_000, speed=10.0)
    assert tracker.heading
    phone.at(4_010, after_s=2 * MIN, speed=0.0)  # still, two minutes on
    phone.wait(17 * MIN)
    assert tracker.heading
    phone.wait(2 * MIN)  # 21 minutes since getting there
    assert not tracker.heading
    assert tracker.reason is HomewardReason.AT_PLACE


def test_silence_after_a_moving_fix_is_not_a_stay() -> None:
    """A phone in a bag on the motorway home: quiet, but still travelling."""
    tracker, phone = out_of_home()
    phone.at(60_000, speed=-25.0)
    phone.at(40_000, speed=25.0)
    assert tracker.heading
    phone.wait(25 * MIN)
    assert tracker.heading


def test_a_late_first_fix_at_a_stop_ends_a_quiet_trip_home() -> None:
    """Heading home, then 40 minutes of silence and a fix 430 m on: stopped."""
    tracker, phone = out_of_home()
    phone.at(10_000, speed=-13.0)
    phone.at(4_430, speed=13.0)
    assert tracker.heading
    phone.at(4_000, after_s=40 * MIN, speed=None)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.AT_PLACE


def test_leaving_after_a_quiet_stay_starts_the_trip_home() -> None:
    """Four hours at a friend's with a quiet phone, then the walk home.

    The first fix on the way is far from the last one at the friend's and
    hours later; that must not be read as having stopped there long ago.
    """
    tracker, phone = out_of_home()
    phone.at(1_600, after_s=20 * MIN, speed=-1.3)
    phone.at(1_100, after_s=4 * 60 * MIN, speed=None)
    assert not tracker.heading
    phone.at(950, after_s=2 * MIN, speed=1.3)  # 650 m (41 %) from 1.6 km
    assert tracker.heading
    assert tracker.reason is HomewardReason.PROGRESS


def test_jitter_at_a_stop_on_a_zone_edge_is_not_a_crossing() -> None:
    tracker, phone = out_of_home()
    tracker.set_rings([ring(4_000)])
    phone.at(10_000, speed=-13.0)
    phone.at(4_020, speed=13.0)
    # The trip home ends at the stop, on the edge of the 4 km zone.
    phone.at(4_015, after_s=16 * MIN, speed=0.0)
    phone.wait(5 * MIN)
    assert not tracker.heading
    for i in range(6):
        phone.at(3_990 if i % 2 else 4_010, after_s=10 * MIN, speed=0.0)
        assert not tracker.heading


def test_overdue_grace_grows_with_the_trip() -> None:
    """Half the expected trip, at least 15 minutes, past the expected arrival."""
    tracker, phone = out_of_home()
    phone.at(100_000, speed=-25.0)
    phone.at(75_000, speed=25.0)
    snap = tracker.snapshot(phone.t)
    assert snap.arrival is not None
    trip = snap.arrival - phone.t
    assert P.homeward_overdue_ratio * trip > P.homeward_overdue_s
    phone.wait(trip + P.homeward_overdue_ratio * trip - MIN)
    assert tracker.heading
    phone.wait(2 * MIN)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.OVERDUE
