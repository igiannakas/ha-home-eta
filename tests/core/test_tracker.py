"""Tests for the per-person tracker that ties kinematics and heading home together."""

from __future__ import annotations

import pytest

from custom_components.home_eta.core.homeward import (
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


def test_near_home_follows_distance_and_fades_after_a_stay() -> None:
    trk = tracker(radius_m=3_000, fade_after_s=90 * MIN, fade_s=60 * MIN)
    assert trk.snapshot(T0).near_home is None
    trk.update(away(1_900, 0))
    assert trk.snapshot(T0).near_home == pytest.approx(0.5, abs=0.01)
    for i in range(1, 25):  # two hours there, a fix every 5 minutes
        trk.update(away(1_900 + (5 if i % 2 else -5), 300 * i))
    snap = trk.snapshot(T0 + 120 * MIN)
    assert snap.homeward.still_since == T0
    # 30 minutes into the hour-long fade.
    assert snap.near_home == pytest.approx(0.25, abs=0.01)


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
