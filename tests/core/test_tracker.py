"""Tests for the per-person tracker that ties kinematics and heading home together."""

from __future__ import annotations

import pytest

from custom_components.home_eta.core.homeward import (
    ESTIMATE_WINDOW,
    HomewardReason,
    HomewardRestore,
    HomewardTrip,
    NearHomeConfig,
)
from custom_components.home_eta.core.kinematic import FixVerdict, Target
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.tracker import (
    PersonReport,
    PersonTracker,
    TrackerConfig,
    build_report,
)
from tests.sim import HOME_LAT, HOME_LON, T0, TARGET, to_latlon

PARAMS = EstimatorParams()
STATION = "zone.station"
MIN = 60.0


def tracker(*, at_target: bool = False, **near: float | None) -> PersonTracker:
    """Return a tracker for the test home."""
    config = TrackerConfig(PARAMS, NearHomeConfig(**near))  # type: ignore[arg-type]
    return PersonTracker(config, TARGET, at_target=at_target)


def away(
    east_m: float, t: float, *zones: str, accuracy: float | None = 10.0
) -> PersonReport:
    """Return a report ``east_m`` metres east of home at T0 + t."""
    lat, lon = to_latlon(east_m, 0.0)
    return PersonReport(T0 + t, False, zones, lat, lon, accuracy)


def home(t: float) -> PersonReport:
    """Return a report inside the target zone."""
    return PersonReport(T0 + t, True, (), HOME_LAT, HOME_LON, 5.0)


def drive(
    trk: PersonTracker, points: list[float], *, start_t: float = 0.0, every: float = 60
) -> float:
    """Report each point a fixed interval apart; return the last time."""
    t = start_t
    for east in points:
        trk.update(away(east, t))
        t += every
    return t - every


def test_starts_home_and_departs() -> None:
    trk = tracker(at_target=True)
    snap = trk.snapshot(T0)
    assert snap.at_home
    assert snap.distance_m == 0.0
    assert snap.near_home == 1.0
    assert not snap.heading_home
    assert snap.expected_home is None
    outcome = trk.update(away(500, 10))
    assert outcome is not None
    assert outcome.verdict is FixVerdict.FIRST
    snap = trk.snapshot(T0 + 10)
    assert not snap.at_home
    assert snap.distance_m == pytest.approx(500, rel=1e-3)


def test_drive_out_and_home() -> None:
    trk = tracker(at_target=True)
    trk.update(away(150, 0))
    t = drive(trk, [600.0 * i for i in range(1, 21)], start_t=60)
    assert not trk.snapshot(T0 + t).heading_home
    seen_heading = seen_soon = False
    east = 12_000.0
    while east > 200:
        t += 60
        east -= 600
        trk.update(away(east, t))
        snap = trk.snapshot(T0 + t)
        if snap.heading_home:
            seen_heading = True
            assert snap.expected_home is not None
            assert snap.expected_home >= T0 + t
        elif seen_heading:
            pytest.fail("heading home went off on the way in")
        if snap.arriving_soon:
            seen_soon = True
        elif seen_soon:
            pytest.fail("arriving soon went off on the way in")
    assert seen_heading
    assert seen_soon
    assert trk.update(home(t + 60)) is None
    snap = trk.snapshot(T0 + t + 60)
    assert snap.at_home
    assert not snap.heading_home
    assert not snap.arriving_soon
    assert snap.homeward.reason is HomewardReason.ARRIVED
    assert snap.distance_m == 0.0
    assert snap.near_home == 1.0


def test_other_zone_holds_off_arriving_soon() -> None:
    trk = tracker()
    t = drive(trk, [3_000, 2_400, 1_800, 1_200, 600], every=60)
    assert trk.snapshot(T0 + t).arriving_soon
    trk.update(away(500, t + 60, STATION))
    snap = trk.snapshot(T0 + t + 60)
    assert snap.other_zones == (STATION,)
    assert snap.heading_home
    assert not snap.arriving_soon
    trk.update(away(400, t + 120))
    assert trk.snapshot(T0 + t + 120).arriving_soon


def test_entering_another_zone_without_a_position_holds_it_off_too() -> None:
    trk = tracker()
    t = drive(trk, [3_000, 2_400, 1_800, 1_200, 600])
    assert trk.snapshot(T0 + t).arriving_soon
    assert trk.update(PersonReport(T0 + t + 20, False, (STATION,))) is None
    assert not trk.snapshot(T0 + t + 20).arriving_soon


def test_startup_reports_set_presence_but_are_not_fixes() -> None:
    trk = tracker()
    assert trk.update(away(5_000, 0), use_for_kinematics=False) is None
    snap = trk.snapshot(T0)
    assert snap.distance_m == pytest.approx(5_000, rel=1e-3)
    assert snap.near_home == 0.0
    assert trk.diagnostics()["kinematic"]["last_fix_time"] is None


def test_startup_distance_ignores_poor_accuracy() -> None:
    trk = tracker()
    trk.update(away(500, 0, accuracy=2_000), use_for_kinematics=False)
    assert trk.snapshot(T0).distance_m is None
    assert trk.snapshot(T0).near_home is None
    trk.update(away(5_000, 10, accuracy=20), use_for_kinematics=False)
    assert trk.snapshot(T0 + 10).distance_m == pytest.approx(5_000, rel=1e-3)


def test_missing_accuracy_uses_worst_acceptable() -> None:
    outcome = tracker().update(away(5_000, 0, accuracy=None))
    assert outcome is not None
    assert outcome.verdict is FixVerdict.FIRST


def test_report_without_position_keeps_last_distance() -> None:
    trk = tracker()
    trk.update(away(5_000, 0))
    assert trk.update(PersonReport(T0 + 60, False)) is None
    assert trk.snapshot(T0 + 60).distance_m == pytest.approx(5_000, rel=1e-3)


def test_rejected_fix_does_not_move_distance() -> None:
    trk = tracker()
    drive(trk, [18_000, 17_400, 16_800])
    outcome = trk.update(away(500, 150, accuracy=2_000))
    assert outcome is not None
    assert outcome.verdict is FixVerdict.REJECTED_ACCURACY
    snap = trk.snapshot(T0 + 150)
    assert snap.distance_m == pytest.approx(16_800, rel=1e-3)
    assert not snap.arriving_soon


def test_threshold_is_validated_and_applied_at_the_next_tick() -> None:
    trk = tracker()
    with pytest.raises(ValueError, match="positive"):
        trk.set_threshold(0)
    t = drive(trk, [30_000, 22_000], every=600)
    assert trk.snapshot(T0 + t).heading_home
    assert not trk.snapshot(T0 + t).arriving_soon
    trk.set_threshold(120 * MIN)
    assert trk.threshold_s == 120 * MIN
    trk.tick(T0 + t + 1)
    assert trk.snapshot(T0 + t + 1).arriving_soon


def test_target_change_restarts_tracking() -> None:
    trk = tracker()
    t = drive(trk, [30_000, 22_000], every=600)
    assert trk.snapshot(T0 + t).heading_home
    trk.set_target(Target(HOME_LAT + 0.01, HOME_LON, 100.0), T0 + t)
    snap = trk.snapshot(T0 + t)
    assert snap.distance_m is None
    assert not snap.heading_home
    assert snap.homeward.reason is HomewardReason.ZONE_CHANGED
    trk.set_target(trk.target, T0 + t)  # unchanged: nothing happens
    at_home = tracker(at_target=True)
    at_home.set_target(Target(HOME_LAT + 0.01, HOME_LON, 100.0), T0)
    assert at_home.snapshot(T0).distance_m == 0.0


def test_restore_homeward_then_overdue_at_once() -> None:
    trk = tracker()
    trk.update(away(21_000, 0), use_for_kinematics=False)
    data = HomewardRestore(T0 - 120, HomewardTrip(T0 - 600, 30_000, 20_000))
    assert trk.restore_homeward(data, T0 - 120, T0)
    snap = trk.snapshot(T0)
    assert snap.heading_home
    assert snap.homeward.reason is HomewardReason.RESTORED
    late = tracker()
    late.update(away(21_000, 0), use_for_kinematics=False)
    stale = HomewardRestore(T0 - 7_200, HomewardTrip(T0 - 7_500, 30_000, 20_000))
    assert late.restore_homeward(stale, T0 - 7_200, T0)
    assert not late.snapshot(T0).heading_home
    assert late.snapshot(T0).homeward.reason is HomewardReason.OVERDUE
    assert not tracker(at_target=True).restore_homeward(data, T0 - 120, T0)


@pytest.mark.parametrize(
    ("more", "last_leg_mps"),
    [(0, 13.0), (1, 13.0), (2, 13.0), (3, 1.0)],
    ids=["one", "two", "three", "held-back"],
)
def test_a_reload_with_no_new_report_keeps_the_expected_arrival(
    more: int, last_leg_mps: float
) -> None:
    """As the runtime reloads: a new tracker, the person's unchanged state fed
    as its first fix, then the trip home saved restored. With one to three
    estimates saved, and with the last held back by a crawled leg."""
    before = tracker(at_target=True)
    t = 0.0
    before.update(away(500, t))  # left home
    for east in (10_000, 20_000, 30_000):
        t += 20 * MIN
        before.update(away(east, t))
    t += 40 * MIN
    before.update(away(30_000, t))  # a stay
    east = 30_000.0
    while not before.homeward.heading:
        t += 4 * MIN
        east -= 13.0 * 240
        before.update(away(east, t))
    for leg in range(more):
        t += 4 * MIN
        east -= (last_leg_mps if leg == more - 1 else 13.0) * 240
        before.update(away(east, t))
    published = before.snapshot(T0 + t)
    saved = before.homeward.restore_data()
    assert saved is not None
    assert saved.trip is not None
    assert len(saved.trip.estimates) == min(more + 1, ESTIMATE_WINDOW)

    after = tracker()
    after.update(away(east, t))  # the same report, at the same time
    data = HomewardRestore.from_dict(saved.as_dict())
    assert data is not None
    assert after.restore_homeward(data, data.last_heard, T0 + t)
    snap = after.snapshot(T0 + t)
    assert snap.expected_home == published.expected_home
    assert snap.homeward.speed_source is published.homeward.speed_source
    assert after.homeward.restore_data() == saved


def test_near_home_follows_distance_however_long_the_stay() -> None:
    trk = tracker(radius_m=3_000)
    assert trk.snapshot(T0).near_home is None
    trk.update(away(1_900, 0))
    assert trk.snapshot(T0).near_home == pytest.approx(0.5, abs=0.01)
    for i in range(1, 73):  # six hours there, a fix every 5 minutes
        trk.update(away(1_900 + (5 if i % 2 else -5), 300 * i))
    snap = trk.snapshot(T0 + 360 * MIN)
    assert snap.homeward.still_since == T0
    assert snap.near_home == pytest.approx(0.5, abs=0.01)


@pytest.mark.parametrize(
    ("east_m", "close", "nearby"),
    [
        (500.0, True, True),
        (800.0, True, True),
        (1_500.0, False, True),
        (2_900.0, False, True),
        (3_100.0, False, False),
        (40_000.0, False, False),
    ],
)
def test_close_to_home_and_nearby_follow_the_distance(
    east_m: float, close: bool, nearby: bool
) -> None:
    trk = tracker(radius_m=3_000)
    trk.update(away(east_m, 0))
    snap = trk.snapshot(T0)
    assert snap.close_to_home is close
    assert snap.nearby is nearby


@pytest.mark.parametrize(
    ("east_m", "close", "nearby"),
    [(795.0, True, True), (805.0, False, True), (2_995.0, False, True)],
)
def test_close_to_home_and_nearby_at_their_limits(
    east_m: float, close: bool, nearby: bool
) -> None:
    trk = tracker(radius_m=3_000)
    trk.update(away(east_m, 0))
    snap = trk.snapshot(T0)
    assert snap.close_to_home is close
    assert snap.nearby is nearby


def test_close_to_home_and_nearby_go_off_only_beyond_the_margin() -> None:
    trk = tracker(radius_m=3_000)
    seen: list[tuple[bool | None, bool | None]] = []
    t = 0.0
    for east in (1_500, 790, 940, 960, 900, 795, 2_000, 2_990, 3_140, 3_160, 3_100):
        trk.update(away(east, t))
        snap = trk.snapshot(T0 + t)
        seen.append((snap.close_to_home, snap.nearby))
        t += 300
    on, off = True, False
    assert seen == [
        (off, on),  # 1.5 km
        (on, on),  # 790 m: close
        (on, on),  # 940 m: within the 150 m margin
        (off, on),  # 960 m: beyond it
        (off, on),  # 900 m: not back within 800 m
        (on, on),  # 795 m
        (off, on),  # 2 km
        (off, on),  # 2.99 km
        (off, on),  # 3.14 km: within the margin
        (off, off),  # 3.16 km: beyond it
        (off, off),  # 3.1 km: not back within 3 km
    ]


def test_a_phone_lying_still_at_a_limit_does_not_flap() -> None:
    """Regression: GPS jitter at 800 m switched close to home every few fixes."""
    trk = tracker(radius_m=3_000)
    states = []
    for i in range(30):
        jitter = (-30.0, 10.0, 30.0, -10.0)[i % 4]
        trk.update(away(800 + jitter, 120 * i, accuracy=40))
        states.append(trk.snapshot(T0 + 120 * i).close_to_home)
    assert states.count(True) == 30


def test_leaving_home_with_a_poor_fix_is_unknown_not_home() -> None:
    """Regression: the distance stayed 0, so close to home stayed on 20 km away."""
    trk = tracker(at_target=True)
    trk.update(away(20_000, 60, accuracy=500))
    snap = trk.snapshot(T0 + 60)
    assert not snap.at_home
    assert snap.distance_m is None
    assert snap.near_home is None
    assert snap.close_to_home is None
    assert snap.nearby is None
    trk.tick(T0 + 3_600)
    assert trk.snapshot(T0 + 3_600).close_to_home is None
    trk.update(away(20_000, 3_660))
    snap = trk.snapshot(T0 + 3_660)
    assert snap.distance_m == pytest.approx(20_000, rel=1e-3)
    assert snap.close_to_home is False
    assert snap.nearby is False


def test_leaving_home_without_a_position_is_unknown_not_home() -> None:
    trk = tracker(at_target=True)
    trk.update(PersonReport(T0 + 60, False))
    snap = trk.snapshot(T0 + 60)
    assert snap.distance_m is None
    assert snap.close_to_home is None
    assert snap.nearby is None


def test_leaving_home_keeps_close_within_the_margin() -> None:
    trk = tracker(at_target=True)
    trk.update(away(900, 60))
    assert trk.snapshot(T0 + 60).close_to_home is True
    trk.update(away(1_000, 360))
    assert trk.snapshot(T0 + 360).close_to_home is False
    assert trk.snapshot(T0 + 360).nearby is True


def test_moving_the_zone_forgets_close_and_nearby() -> None:
    trk = tracker(radius_m=3_000)
    trk.update(away(700, 0))
    assert trk.snapshot(T0).close_to_home is True
    trk.set_target(Target(TARGET.latitude, TARGET.longitude, TARGET.radius_m + 10), T0)
    assert trk.snapshot(T0).close_to_home is None
    # Judged afresh: 900 m is not within 800 m (no margin carried over).
    trk.update(away(900, 300))
    assert trk.snapshot(T0 + 300).close_to_home is False


def test_close_to_home_and_nearby_while_home_and_while_unknown() -> None:
    unknown = tracker()
    snap = unknown.snapshot(T0)
    assert snap.close_to_home is None
    assert snap.nearby is None
    home = tracker(at_target=True)
    snap = home.snapshot(T0)
    assert snap.close_to_home is True
    assert snap.nearby is True


def test_a_long_stay_close_by_stays_close_and_nearby() -> None:
    """Six hours at a café 400 m away: no fade, however long."""
    trk = tracker(radius_m=3_000)
    for i in range(73):
        trk.update(away(400 + (5 if i % 2 else -5), 300 * i))
    snap = trk.snapshot(T0 + 360 * MIN)
    assert snap.homeward.still_since == T0
    assert snap.near_home == 1.0
    assert snap.close_to_home is True
    assert snap.nearby is True


def test_zero_accuracy_means_unknown_not_perfect() -> None:
    """Regression: accuracy 0 made every wobble movement, so no stay was seen."""
    trk = tracker()
    for i in range(12):
        trk.update(away(1_500 + (5 if i % 2 else -5), 300 * i, accuracy=0.0))
    assert trk.snapshot(T0 + 3_300).homeward.still_since == T0


def test_an_undone_glitch_puts_the_distance_back() -> None:
    trk = tracker()
    for t in (0, 300, 600, 900):
        trk.update(away(3_000, t, accuracy=40))
    trk.update(away(2_000, 1_200, accuracy=40))
    assert trk.snapshot(T0 + 1_200).distance_m == pytest.approx(2_000, rel=1e-3)
    outcome = trk.update(away(3_000, 1_500, accuracy=40))
    assert outcome is not None
    assert outcome.rolled_back
    assert trk.snapshot(T0 + 1_500).distance_m == pytest.approx(3_000, rel=1e-3)


def test_diagnostics_have_no_coordinates() -> None:
    trk = tracker()
    drive(trk, [3_000, 2_400])
    data = trk.diagnostics()
    assert data["at_home"] is False
    assert "homeward" in data
    assert "kinematic" in data
    assert str(HOME_LAT) not in repr(data)


def test_build_report_from_in_zones() -> None:
    report = build_report(
        "Station",
        {
            "in_zones": ["zone.station", "zone.home"],
            "latitude": 51.5,
            "longitude": -0.1,
            "gps_accuracy": 12,
        },
        T0,
        "zone.home",
        "Home",
    )
    assert report.in_target
    assert report.other_zones == ("zone.station",)
    assert (report.latitude, report.longitude, report.accuracy_m) == (51.5, -0.1, 12.0)


def test_build_report_ignores_non_numeric_position() -> None:
    report = build_report(
        "not_home",
        {"in_zones": [], "latitude": "51.5", "longitude": True},
        T0,
        "zone.home",
        "Home",
    )
    assert not report.in_target
    assert report.latitude is None
    assert report.longitude is None
    assert report.accuracy_m is None


@pytest.mark.parametrize(
    ("state", "target", "name", "in_target", "other"),
    [
        ("home", "zone.home", "Home", True, ()),
        ("not_home", "zone.home", "Home", False, ()),
        ("Work", "zone.home", "Home", False, ("zone:Work",)),
        ("Work", "zone.work", "Work", True, ()),
        ("home", "zone.work", "Work", False, ("zone.home",)),
    ],
)
def test_build_report_legacy_state(
    state: str, target: str, name: str, in_target: bool, other: tuple[str, ...]
) -> None:
    report = build_report(state, {}, T0, target, name)
    assert report.in_target is in_target
    assert report.other_zones == other


def test_build_report_ignores_zones_enclosing_the_target() -> None:
    attributes = {"in_zones": ["zone.station", "zone.neighbourhood"]}
    report = build_report(
        "Station",
        attributes,
        T0,
        "zone.home",
        "Home",
        enclosing_zones={"zone.neighbourhood"},
    )
    assert report.other_zones == ("zone.station",)
