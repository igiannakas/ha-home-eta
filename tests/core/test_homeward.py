"""Unit tests for heading home and near home."""

from __future__ import annotations

import math

import pytest

from custom_components.home_eta.core.geo import haversine_m
from custom_components.home_eta.core.homeward import (
    HomewardReason,
    HomewardRestore,
    HomewardStay,
    HomewardTracker,
    HomewardTrip,
    NearHomeConfig,
    SpeedSource,
    near_home,
    trip_speed_mps,
    within,
)
from custom_components.home_eta.core.kinematic import Fix, Target
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.speeds import (
    TYPICAL_SPEED_MPS,
    VEHICLE_TYPICAL_MIN_MPS,
    typical_speed_mps,
)
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


def test_a_vehicle_near_home_is_not_expected_at_walking_pace() -> None:
    """Regression: a car 1 km out at 6.5 m/s was expected at 2.9 m/s."""
    speed, source = trip_speed_mps(1_000, 6.5, P)
    assert source is SpeedSource.MEASURED
    assert speed == pytest.approx(math.sqrt(6.5 * VEHICLE_TYPICAL_MIN_MPS))
    # Beyond a local drive the table is already at least that fast.
    assert typical_speed_mps(5_000) >= VEHICLE_TYPICAL_MIN_MPS
    for distance in (5_000, 15_000, 60_000):
        speed, _ = trip_speed_mps(distance, 8.0, P)
        assert speed == pytest.approx(math.sqrt(8.0 * typical_speed_mps(distance)))


@pytest.mark.parametrize("distance", [400, 1_000, 2_000, 2_999, 3_000, 4_000, 8_000])
def test_closing_faster_never_expects_a_later_arrival(distance: float) -> None:
    """Above barely closing (no measurement: the typical speed), across modes."""
    measured = [P.homeward_min_speed_mps + i / 20 for i in range(1, 800)]
    speeds = [trip_speed_mps(distance, m, P)[0] for m in measured]
    assert speeds == sorted(speeds)


# -- near home ---------------------------------------------------------------


def test_near_home_falls_with_distance() -> None:
    config = NearHomeConfig(radius_m=3_000, full_m=800)
    assert near_home(0, config) == 1.0
    assert near_home(800, config) == 1.0
    assert near_home(1_900, config) == pytest.approx(0.5)
    assert near_home(3_000, config) == 0.0
    assert near_home(30_000, config) == 0.0
    assert near_home(math.inf, config) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"radius_m": 0},
        {"radius_m": math.nan},
        {"full_m": 3_000},
        {"radius_m": 500},
        {"full_m": 0},
        {"full_m": math.inf},
        {"off_margin_m": -1},
        {"off_margin_m": math.nan},
        {"radius_m": 950},
    ],
)
def test_near_home_config_rejects_bad_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must"):
        NearHomeConfig(**kwargs)


@pytest.mark.parametrize(
    ("distance_m", "was", "expected"),
    [
        (800.0, None, True),
        (800.1, None, False),
        (800.1, False, False),
        (800.1, True, True),
        (950.0, True, True),
        (950.1, True, False),
    ],
)
def test_within_switches_off_only_beyond_the_margin(
    distance_m: float, was: bool | None, expected: bool
) -> None:
    assert within(distance_m, 800.0, 150.0, was) is expected


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
    """A zone added between two fixes has no "before": entering it is unknown."""
    tracker, phone = out_of_home()
    phone.at(9_000)
    tracker.set_rings([ring(8_000)])  # added while the person is outside it
    phone.at(7_000)
    assert not tracker.heading
    phone.at(9_000)
    phone.at(7_000)  # now a crossing
    assert tracker.heading


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
    """Two zones 300 m apart give no speed worth trusting."""
    tracker, phone = out_of_home()
    tracker.set_rings([ring(8_000), ring(8_300)])
    phone.at(8_500)
    phone.at(8_200)  # into the 8.3 km zone: heading home
    phone.at(7_900)  # into the 8 km zone, 300 m on
    snap = tracker.snapshot(phone.t)
    assert snap.heading
    assert snap.speed_source is SpeedSource.TYPICAL


def test_a_road_along_a_zone_edge_does_not_keep_starting_trips() -> None:
    """Weaving in and out of the 8 km zone: one short start, then nothing."""
    tracker, phone = out_of_home()
    tracker.set_rings([ring(8_000)])
    phone.at(8_500)
    phone.at(7_700, after_s=2 * MIN)  # in: a trip home, as far as anyone knows
    assert tracker.heading
    phone.at(8_300, after_s=2 * MIN)  # out of the zone that started it
    assert not tracker.heading
    assert tracker.reason is HomewardReason.MOVING_AWAY
    for east in (7_700, 8_300, 7_700, 8_300, 7_700):
        phone.at(east, after_s=2 * MIN)
        assert not tracker.heading
    # Another zone, further in, is new evidence.
    tracker.set_rings([ring(8_000), ring(4_000)])
    phone.at(5_000, after_s=5 * MIN)
    phone.at(3_900, after_s=2 * MIN)
    assert tracker.heading
    assert tracker.reason is HomewardReason.ZONE_CROSSING


def test_zones_start_trips_again_after_a_stay() -> None:
    """The once-per-outing rule resets when the person stays somewhere."""
    tracker, phone = out_of_home()
    tracker.set_rings([ring(8_000)])
    phone.at(9_000)
    phone.at(7_500)  # in
    phone.at(9_000)  # out: ends
    assert not tracker.heading
    phone.at(9_010, after_s=16 * MIN)  # a stay
    phone.wait(5 * MIN)
    phone.at(7_500, after_s=30 * MIN)  # in again, after the stay
    assert tracker.heading
    assert tracker.reason is HomewardReason.ZONE_CROSSING


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


def trip(since: float, farthest: float, closest: float) -> HomewardRestore:
    """Return saved data for a trip home only."""
    return HomewardRestore(since, HomewardTrip(since, farthest, closest))


def test_restore_resumes_and_then_ends_like_any_trip() -> None:
    tracker = HomewardTracker(P, TARGET)
    assert tracker.restore(trip(T0, 30_000, 20_000), T0 + 10 * MIN, 21_000)
    assert tracker.heading
    assert tracker.reason is HomewardReason.RESTORED
    snap = tracker.snapshot(T0 + 11 * MIN)
    assert snap.arrival is not None
    assert snap.farthest_m == 30_000
    # Saved again as it was, with when the person was last heard from: a
    # second restart before any fix keeps it.
    assert tracker.restore_data() == HomewardRestore(
        T0 + 10 * MIN, HomewardTrip(T0, 30_000, 20_000, threshold_s=15 * MIN)
    )
    trip_s = snap.arrival - (T0 + 10 * MIN)
    grace = max(P.homeward_overdue_s, P.homeward_overdue_ratio * trip_s)
    tracker.tick(snap.arrival + grace - MIN, None)
    assert tracker.heading
    tracker.tick(snap.arrival + grace + MIN, None)
    assert not tracker.heading
    assert tracker.reason is HomewardReason.OVERDUE


def test_restore_merges_with_a_fix_already_received() -> None:
    """After a reload the person's state is a genuine fix: kept, and merged."""
    tracker, phone = out_of_home()
    phone.at(19_000)
    assert tracker.restore(trip(T0 - 30 * MIN, 30_000, 20_000), T0 - MIN, None)
    assert tracker.heading
    saved = tracker.restore_data()
    assert saved is not None
    assert saved.last_heard == phone.t
    assert saved.trip is not None
    assert saved.trip.since == T0 - 30 * MIN
    assert saved.trip.farthest_m == 30_000
    assert saved.trip.closest_m == pytest.approx(19_000, abs=1)
    assert tracker.snapshot(phone.t).farthest_m == 30_000


def test_restore_is_refused_at_home_after_a_change_or_without_a_distance() -> None:
    data = trip(T0, 30_000, 20_000)
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


def test_a_stay_survives_a_restart() -> None:
    """Three hours at a friend's 500 m away: the stay comes back after a restart."""
    tracker, phone = out_of_home()
    phone.at(500, after_s=MIN)
    phone.at(505, after_s=20 * MIN)
    saved = tracker.restore_data()
    assert saved is not None
    assert saved.trip is None
    assert saved.stay == HomewardStay(
        T0 + MIN, *to_latlon(500, 0), pytest.approx(500, abs=1)
    )
    # At startup nothing has been received yet; the distance is known.
    restarted = HomewardTracker(P, TARGET)
    assert restarted.restore(saved, phone.t, 505)
    assert restarted.snapshot(phone.t + 3 * 60 * MIN).still_since == T0 + MIN
    # The next fix there carries the stay on; one elsewhere ends it.
    restarted.add_fix(
        Fix(*to_latlon(510, 0), 10, phone.t + 30 * MIN), 510, speed_mps=None
    )
    assert restarted.snapshot(phone.t + 30 * MIN).still_since == T0 + MIN
    restarted.add_fix(
        Fix(*to_latlon(2_000, 0), 10, phone.t + 40 * MIN), 2_000, speed_mps=None
    )
    assert restarted.snapshot(phone.t + 40 * MIN).still_since is None


def test_a_saved_stay_is_dropped_if_a_reload_fix_is_elsewhere() -> None:
    saved = HomewardRestore(T0, stay=HomewardStay(T0, *to_latlon(500, 0), 500.0))
    tracker, phone = out_of_home()
    phone.at(3_000)
    assert not tracker.restore(saved, T0, None)
    assert tracker.snapshot(phone.t).still_since is None


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


# -- arriving soon -------------------------------------------------------------


def heading_home_from(tracker: HomewardTracker, phone: Phone, far: float) -> None:
    """Drive out to ``far`` metres, then a quarter of the way back."""
    phone.at(far, speed=-13.0)
    phone.at(far * 0.75, speed=13.0)
    assert tracker.heading


def due_in(tracker: HomewardTracker, now: float) -> float:
    """Return seconds until the expected arrival."""
    arrival = tracker.snapshot(now).arrival
    assert arrival is not None
    return arrival - now


def test_arriving_soon_once_due_within_the_threshold() -> None:
    tracker, phone = out_of_home()
    tracker.set_threshold(10 * MIN)
    heading_home_from(tracker, phone, 30_000)
    east = 22_500.0
    while not tracker.arriving_soon:
        assert due_in(tracker, phone.t) > 10 * MIN
        east -= 1_000
        phone.at(east, after_s=77.0, speed=13.0)
    assert due_in(tracker, phone.t) <= 10 * MIN
    assert east > P.arriving_soon_distance_m
    assert tracker.snapshot(phone.t).arriving_soon


def test_arriving_soon_as_the_time_runs_down_between_fixes() -> None:
    tracker, phone = out_of_home()
    heading_home_from(tracker, phone, 30_000)
    due = due_in(tracker, phone.t)
    assert due > 16 * MIN
    phone.wait(due - 15 * MIN - MIN, speed=13.0)
    assert not tracker.arriving_soon
    phone.wait(MIN, speed=13.0)
    assert tracker.arriving_soon


def test_close_by_is_arriving_soon_whatever_the_time() -> None:
    tracker, phone = out_of_home()
    tracker.set_threshold(MIN)
    phone.at(2_000, speed=-1.3)
    phone.at(1_200, after_s=10 * MIN, speed=1.3)
    assert tracker.heading
    assert not tracker.arriving_soon
    phone.at(790, after_s=5 * MIN, speed=1.3)
    assert tracker.arriving_soon
    assert due_in(tracker, phone.t) > MIN


def test_arriving_soon_holds_until_the_trip_home_ends() -> None:
    tracker, phone = out_of_home()
    heading_home_from(tracker, phone, 30_000)
    phone.wait(due_in(tracker, phone.t), speed=13.0)
    assert tracker.arriving_soon
    # A jam: the estimate goes back past the threshold, but it stays on.
    phone.at(6_000, after_s=MIN, speed=1.5)
    assert due_in(tracker, phone.t) > 15 * MIN
    assert tracker.arriving_soon
    # Turning back ends the trip home, and arriving soon with it.
    phone.at(9_000, after_s=5 * MIN, speed=-10.0)
    assert not tracker.heading
    assert not tracker.arriving_soon
    assert not tracker.snapshot(phone.t).arriving_soon


def test_arriving_home_ends_arriving_soon() -> None:
    tracker, phone = out_of_home()
    phone.at(2_000, speed=-1.3)
    phone.at(700, after_s=15 * MIN, speed=1.4)
    assert tracker.arriving_soon
    tracker.arrive(phone.t + 5 * MIN)
    assert not tracker.arriving_soon
    assert tracker.reason is HomewardReason.ARRIVED


def test_another_zone_holds_arriving_soon_off() -> None:
    """A shop 600 m from home is not arriving home."""
    tracker, phone = out_of_home()
    phone.at(2_000, speed=-1.3)
    tracker.set_other_zone(True)
    phone.at(600, after_s=15 * MIN, speed=1.4)
    assert tracker.heading
    assert not tracker.arriving_soon
    tracker.set_other_zone(False)
    assert not tracker.arriving_soon  # judged at the next fix or tick
    phone.wait(MIN, speed=1.4)
    assert tracker.arriving_soon
    tracker.set_other_zone(True)
    phone.wait(MIN, speed=1.4)
    assert not tracker.arriving_soon


def test_a_new_threshold_is_judged_afresh() -> None:
    tracker, phone = out_of_home()
    heading_home_from(tracker, phone, 30_000)
    phone.wait(due_in(tracker, phone.t) - 10 * MIN, speed=13.0)
    assert tracker.arriving_soon
    tracker.set_threshold(5 * MIN)
    phone.wait(0.0, speed=13.0)
    assert not tracker.arriving_soon
    tracker.set_threshold(5 * MIN)  # unchanged: nothing to judge again
    tracker.set_threshold(20 * MIN)
    phone.wait(0.0, speed=13.0)
    assert tracker.arriving_soon
    assert tracker.threshold_s == 20 * MIN


@pytest.mark.parametrize("seconds", [0.0, -60.0, math.nan, math.inf])
def test_threshold_must_be_positive(seconds: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        HomewardTracker(P, TARGET).set_threshold(seconds)


def test_undoing_a_fix_undoes_arriving_soon() -> None:
    tracker, phone = out_of_home()
    tracker.set_threshold(5 * MIN)
    phone.at(2_000, speed=-1.3)
    phone.at(1_200, after_s=10 * MIN, speed=1.3)
    assert not tracker.arriving_soon
    saved = tracker.save()
    phone.at(700, after_s=5 * MIN, speed=1.3)
    assert tracker.arriving_soon
    tracker.load(saved)
    assert not tracker.arriving_soon


def test_arriving_soon_survives_a_restart() -> None:
    tracker, phone = out_of_home()
    phone.at(2_000, speed=-1.3)
    phone.at(700, after_s=15 * MIN, speed=1.4)
    saved = tracker.restore_data()
    assert saved is not None
    assert saved.trip is not None
    assert saved.trip.arriving_soon
    restarted = HomewardTracker(P, TARGET)
    assert restarted.restore(saved, phone.t, 700)
    restarted.tick(phone.t + MIN, None)
    assert restarted.arriving_soon


# -- saved format --------------------------------------------------------------


def test_saved_data_round_trips() -> None:
    data = HomewardRestore(
        T0,
        HomewardTrip(T0 - 600, 30_000, 20_000, arriving_soon=True, threshold_s=600),
        HomewardStay(T0 - 3_600, *to_latlon(500, 0), 500.0),
    )
    assert HomewardRestore.from_dict(data.as_dict()) == data
    trip_only = HomewardRestore(T0, HomewardTrip(T0 - 600, 30_000, 20_000))
    assert HomewardRestore.from_dict(trip_only.as_dict()) == trip_only


GOOD_TRIP = {"since": T0, "farthest_m": 30_000, "closest_m": 20_000}
GOOD_STAY = {"since": T0, "latitude": HOME_LAT, "longitude": HOME_LON, "distance_m": 5}


@pytest.mark.parametrize(
    "value",
    [
        None,
        "nonsense",
        {"trip": GOOD_TRIP},
        {"last_heard": "yesterday", "trip": GOOD_TRIP},
        {"last_heard": True, "trip": GOOD_TRIP},
        {"last_heard": math.nan, "trip": GOOD_TRIP},
        {"last_heard": T0},
        {"last_heard": T0, "trip": "x", "stay": None},
        {"last_heard": T0, "trip": {**GOOD_TRIP, "since": "yesterday"}},
        {"last_heard": T0, "trip": {**GOOD_TRIP, "farthest_m": -5}},
        {"last_heard": T0, "trip": {**GOOD_TRIP, "closest_m": 40_000}},
        {"last_heard": T0, "trip": {"since": T0, "farthest_m": 30_000}},
        {"last_heard": T0, "trip": {**GOOD_TRIP, "arriving_soon": "yes"}},
        {"last_heard": T0, "stay": {**GOOD_STAY, "latitude": 123.0}},
        {"last_heard": T0, "stay": {**GOOD_STAY, "distance_m": -1}},
        {"last_heard": T0, "stay": {**GOOD_STAY, "longitude": None}},
    ],
)
def test_unusable_saved_data_restores_nothing(value: object) -> None:
    assert HomewardRestore.from_dict(value) is None


@pytest.mark.parametrize("threshold", [-60, 0, "soon", math.nan, True])
def test_an_unusable_saved_threshold_is_dropped(threshold: object) -> None:
    data = HomewardRestore.from_dict(
        {"last_heard": T0, "trip": {**GOOD_TRIP, "threshold_s": threshold}}
    )
    assert data is not None
    assert data.trip == HomewardTrip(T0, 30_000, 20_000)


def test_a_bad_part_of_saved_data_is_dropped() -> None:
    data = HomewardRestore.from_dict(
        {"last_heard": T0, "trip": {**GOOD_TRIP, "since": None}, "stay": GOOD_STAY}
    )
    assert data == HomewardRestore(T0, None, HomewardStay(T0, HOME_LAT, HOME_LON, 5))


def restore_at_5_km(
    tracker: HomewardTracker, soon: bool, threshold_s: float | None = 10 * MIN
) -> None:
    """Restore a trip home 5 km out (about 14 minutes left at a typical speed)."""
    trip = HomewardTrip(T0 - 600, 30_000, 5_000, soon, threshold_s)
    assert tracker.restore(HomewardRestore(T0, trip), T0, 5_000)
    tracker.tick(T0 + 1, None)


@pytest.mark.parametrize("soon", [True, False])
def test_a_restored_trip_keeps_its_threshold_until_the_persons_arrives(
    soon: bool,
) -> None:
    """Regression: judged with the default first, it showed the wrong state.

    The person's threshold is set up separately, after or before the trip.
    """
    tracker = HomewardTracker(P, TARGET)
    restore_at_5_km(tracker, soon)
    assert tracker.threshold_s == 10 * MIN
    assert due_in(tracker, T0 + 1) > 10 * MIN
    assert tracker.arriving_soon is soon
    tracker.set_threshold(10 * MIN)  # the person's own, the same
    tracker.tick(T0 + 2, None)
    assert tracker.arriving_soon is soon


@pytest.mark.parametrize("soon", [True, False])
def test_the_persons_threshold_set_first_stands(soon: bool) -> None:
    tracker = HomewardTracker(P, TARGET)
    tracker.set_threshold(10 * MIN)
    restore_at_5_km(tracker, soon)
    assert tracker.arriving_soon is soon


def test_a_different_threshold_judges_a_restored_trip_afresh() -> None:
    """Changed meanwhile (or saved by 2.1.0 without one): judged again."""
    for threshold_s in (10 * MIN, None):
        tracker = HomewardTracker(P, TARGET)
        tracker.set_threshold(12 * MIN)
        restore_at_5_km(tracker, True, threshold_s)
        assert not tracker.arriving_soon
    tracker = HomewardTracker(P, TARGET)
    restore_at_5_km(tracker, True)
    tracker.set_threshold(12 * MIN)
    tracker.tick(T0 + 2, None)
    assert not tracker.arriving_soon


def test_undoing_a_fix_after_a_new_threshold_judges_afresh() -> None:
    """Regression: the undo brought back an arriving soon judged with the old one."""
    tracker, phone = out_of_home()
    phone.at(2_000, speed=-1.3)
    phone.at(1_200, after_s=10 * MIN, speed=1.3)
    assert tracker.arriving_soon
    saved = tracker.save()
    tracker.set_threshold(5 * MIN)
    phone.wait(0.0, speed=1.3)
    assert not tracker.arriving_soon
    tracker.load(saved)
    assert not tracker.arriving_soon
    assert tracker.threshold_s == 5 * MIN
