"""Tests for the per-person tracker that ties the estimators together."""

from __future__ import annotations

import pytest

from custom_components.home_eta.core.approach import ApproachState, Reason
from custom_components.home_eta.core.kinematic import FixVerdict, Target
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.tracker import (
    Direction,
    PersonReport,
    PersonTracker,
    TrackerConfig,
    build_report,
)
from tests.sim import HOME_LAT, HOME_LON, T0, TARGET, to_latlon

PARAMS = EstimatorParams()
WORK = "zone.work"
STATION = "zone.station"


def tracker(*, historical: bool = True, at_target: bool = False) -> PersonTracker:
    """Return a tracker with history enabled and work as an ignored zone."""
    config = TrackerConfig(PARAMS, frozenset({WORK}), historical=historical)
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


def walk_home(
    trk: PersonTracker, start_t: float = 0.0, start_m: float = 2_000.0
) -> float:
    """Walk home at 1.4 m/s with a fix every 5 minutes; return the arrival time."""
    t, east = start_t, start_m
    while east > 150:
        trk.update(away(east, t))
        t += 300
        east -= 1.4 * 300
    trk.update(home(t))
    return t


def test_starts_home_and_departs() -> None:
    trk = tracker(at_target=True)
    assert trk.state is ApproachState.HOME
    assert trk.snapshot(T0).distance_m == 0.0
    result = trk.update(away(500, 10))
    assert result.transition is not None
    assert result.transition.current is ApproachState.AWAY
    assert result.outcome is not None
    assert result.outcome.verdict is FixVerdict.FIRST
    snap = trk.snapshot(T0 + 10)
    assert snap.trip_started_at == T0 + 10
    assert snap.fixes_in_trip == 1


def test_full_journey_publishes_and_learns() -> None:
    trk = tracker()
    trk.set_threshold(300)
    states = []
    t, east = 0.0, 8_000.0
    while east > 150:
        trk.update(away(east, t))
        states.append(trk.state)
        t += 60
        east -= 10.0 * 60
    assert ApproachState.APPROACHING in states
    assert states[-1] is ApproachState.ARRIVING_SOON
    snap = trk.snapshot(T0 + t - 60)
    assert snap.kinematic is not None
    assert snap.direction is Direction.TOWARDS
    assert snap.eta_seconds is not None
    result = trk.update(home(t))
    assert result.transition is not None
    assert result.transition.current is ApproachState.HOME
    assert result.learned_samples > 0
    snap = trk.snapshot(T0 + t)
    assert snap.kinematic is None
    assert snap.trips_learned == 1
    assert snap.distance_m == 0.0
    assert snap.direction is Direction.UNKNOWN
    assert not snap.stale


def learn_trips(trk: PersonTracker, count: int, start_m: float = 2_000.0) -> float:
    """Walk home ``count`` times, an hour apart; return the time after."""
    t = 0.0
    for _ in range(count):
        trk.update(away(start_m + 500, t))
        t = walk_home(trk, t + 60, start_m) + 3_600
    return t


def test_historical_estimate_after_enough_trips() -> None:
    trk = tracker()
    t = learn_trips(trk, PARAMS.min_hist_trips)
    trk.update(away(2_000, t))
    trk.update(away(1_580, t + 300))
    trk.update(away(1_160, t + 600))
    assert trk.state in (ApproachState.APPROACHING, ApproachState.ARRIVING_SOON)
    snap = trk.snapshot(T0 + t + 600)
    assert snap.historical is not None
    assert snap.historical_trips == PARAMS.min_hist_trips
    # Every learned walk reached home 900 s after passing 1 160 m.
    assert snap.historical.arrival - (T0 + t + 600) == pytest.approx(900.0, abs=1.0)


def test_history_disabled() -> None:
    trk = tracker(historical=False)
    assert trk.historical is None
    walk_home(trk)
    snap = trk.snapshot(T0)
    assert snap.trips_learned == 0
    assert snap.historical is None


def test_ignored_zone_restarts_the_leg() -> None:
    trk = tracker()
    trk.update(away(8_000, 0))
    trk.update(away(8_000, 3_600, WORK))
    hist = trk.historical
    assert hist is not None
    assert not hist.leg_open
    trk.update(away(8_000, 7_200, WORK))
    assert not hist.leg_open
    trk.update(away(7_400, 7_300))
    assert hist.leg_open
    assert hist.leg_started_at == T0 + 7_300


def test_other_zone_holds_off_arriving_soon() -> None:
    trk = tracker()
    for t in (0, 60, 120):
        trk.update(away(1_500 - 10 * t, t, STATION))
    assert trk.state is ApproachState.APPROACHING
    snap = trk.snapshot(T0 + 120)
    assert snap.in_other_zone
    assert snap.other_zones == (STATION,)
    trk.update(away(1_500 - 10 * 180, 180))
    assert trk.state is ApproachState.ARRIVING_SOON


def test_entering_other_zone_without_position_steps_back() -> None:
    trk = tracker()
    for t in (0, 60, 120, 180):
        trk.update(away(1_500 - 10 * t, t))
    assert trk.state is ApproachState.ARRIVING_SOON
    result = trk.update(PersonReport(T0 + 200, False, (STATION,)))
    assert result.outcome is None
    assert trk.state is ApproachState.APPROACHING


def test_startup_reports_set_presence_but_not_speed() -> None:
    trk = tracker()
    result = trk.update(away(5_000, 0), use_for_kinematics=False)
    assert result.outcome is None
    snap = trk.snapshot(T0)
    assert snap.distance_m == pytest.approx(5_000, rel=1e-3)
    assert snap.last_fix_time is None
    trk.update(away(4_400, 60))
    assert trk.snapshot(T0 + 60).closing_speed_mps is None


def test_missing_accuracy_uses_worst_acceptable() -> None:
    trk = tracker()
    result = trk.update(away(5_000, 0, accuracy=None))
    assert result.outcome is not None
    assert result.outcome.verdict is FixVerdict.FIRST


def test_report_without_position_keeps_last_distance() -> None:
    trk = tracker()
    trk.update(away(5_000, 0))
    trk.update(PersonReport(T0 + 60, False))
    assert trk.snapshot(T0 + 60).distance_m == pytest.approx(5_000, rel=1e-3)


def test_stale_and_stationary_on_tick() -> None:
    trk = tracker()
    for t in (0, 60, 120):
        trk.update(away(20_000 - 10 * t, t))
    assert trk.state is ApproachState.APPROACHING
    # A long gap alone is not a stop (iPhones report every ~500 m)...
    assert trk.tick(T0 + 120 + 600) is None
    # ...but no report for the stale period is.
    transition = trk.tick(T0 + 120 + 900)
    assert transition is not None
    assert transition.current is ApproachState.STATIONARY
    assert transition.reason is Reason.NO_REPORTS
    snap = trk.snapshot(T0 + 120 + 901)
    assert snap.stale
    assert snap.kinematic is None
    assert snap.last_kinematic is not None
    assert snap.direction is Direction.STATIONARY


def test_moving_away_direction() -> None:
    trk = tracker()
    for t in (0, 60, 120):
        trk.update(away(1_000 + 10 * t, t))
    assert trk.snapshot(T0 + 120).direction is Direction.AWAY_FROM


def test_threshold_validation() -> None:
    trk = tracker()
    with pytest.raises(ValueError, match="positive"):
        trk.set_threshold(0)
    trk.set_threshold(120)
    assert trk.threshold_s == 120


def test_target_change_restarts_kinematics() -> None:
    trk = tracker()
    for t in (0, 60):
        trk.update(away(5_000 - 10 * t, t))
    trk.set_target(Target(HOME_LAT + 0.01, HOME_LON, 100.0), T0 + 60)
    snap = trk.snapshot(T0 + 60)
    assert snap.closing_speed_mps is None
    assert snap.distance_m is None
    trk.set_target(trk.target, T0 + 60)


def test_restore_state() -> None:
    trk = tracker()
    assert trk.restore_state(ApproachState.APPROACHING, T0 - 200, T0 - 30)
    snap = trk.snapshot(T0)
    assert snap.state is ApproachState.APPROACHING
    assert snap.reason is Reason.RESTORED
    assert snap.state_since == T0 - 200
    assert snap.last_heard == T0 - 30


def test_diagnostics_have_no_coordinates() -> None:
    trk = tracker()
    walk_home(trk)
    data = trk.diagnostics()
    assert data["historical"]["trips_learned"] == 1
    text = repr(data)
    assert str(HOME_LAT) not in text


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


def test_restored_state_ends_after_silence() -> None:
    trk = tracker()
    # The startup report carries the restore time, not news of the person:
    # silence is measured from when they were last heard before the restart.
    trk.update(away(8_000, 0), use_for_kinematics=False)
    assert trk.restore_state(ApproachState.ARRIVING_SOON, T0 - 300, T0 - 30)
    assert trk.tick(T0 - 30 + PARAMS.stale_after_s - 1) is None
    transition = trk.tick(T0 - 30 + PARAMS.stale_after_s)
    assert transition is not None
    assert transition.current is ApproachState.STATIONARY


def test_target_change_restarts_silence_timer() -> None:
    trk = tracker()
    for t in (0, 60, 120):
        trk.update(away(20_000 - 10 * t, t))
    trk.set_target(Target(HOME_LAT + 0.001, HOME_LON, 100.0), T0 + 500)
    assert trk.tick(T0 + 500 + PARAMS.stale_after_s - 1) is None
    assert trk.tick(T0 + 500 + PARAMS.stale_after_s) is not None


def test_rejected_fix_does_not_move_distance() -> None:
    trk = tracker()
    for t in (0, 60, 120):
        trk.update(away(18_000 - 10 * t, t))
    assert trk.state is ApproachState.APPROACHING
    result = trk.update(away(500, 150, accuracy=2_000))
    assert result.outcome is not None
    assert result.outcome.verdict is FixVerdict.REJECTED_ACCURACY
    snap = trk.snapshot(T0 + 150)
    assert snap.distance_m == pytest.approx(16_800, rel=1e-3)
    assert snap.state is ApproachState.APPROACHING


def test_startup_distance_ignores_poor_accuracy() -> None:
    trk = tracker()
    trk.update(away(500, 0, accuracy=2_000), use_for_kinematics=False)
    assert trk.snapshot(T0).distance_m is None
    trk.update(away(5_000, 10, accuracy=20), use_for_kinematics=False)
    assert trk.snapshot(T0 + 10).distance_m == pytest.approx(5_000, rel=1e-3)


def test_historical_only_while_on_the_way() -> None:
    trk = tracker()
    t = learn_trips(trk, PARAMS.min_hist_trips)
    # Sitting 2 km away: stationary, no historical ETA.
    trk.update(away(2_000, t))
    trk.update(away(2_000, t + 700))
    trk.tick(T0 + t + 700 + PARAMS.stale_after_s)
    assert trk.state is ApproachState.STATIONARY
    assert trk.snapshot(T0 + t + 700 + PARAMS.stale_after_s).historical is None
    # On the way home again but inside an ignored zone: still none.
    t += 3_000
    for i in range(3):
        trk.update(away(2_000 - 400 * i, t + 300 * i, WORK))
    assert trk.state in (ApproachState.APPROACHING, ApproachState.ARRIVING_SOON)
    assert trk.snapshot(T0 + t + 600).historical is None


def test_stop_on_the_way_is_not_learned_as_travel() -> None:
    """Regression: a visit 450 m away was learned as a 55-minute walk home."""
    trk = tracker()
    hist = trk.historical
    assert hist is not None
    for t, east in ((0, 120), (180, 330), (330, 450)):
        trk.update(away(east, t))
    trk.tick(T0 + 330 + PARAMS.stale_after_s)
    assert trk.state is ApproachState.STATIONARY
    assert not hist.leg_open
    for t in (1_500, 2_700, 3_900, 4_500):
        trk.update(away(450, t))
        assert not hist.leg_open
    result = trk.update(home(4_800))
    assert result.learned_samples == 0
    assert hist.trips_learned == 0


def test_leg_restarts_when_moving_again_after_a_stop() -> None:
    trk = tracker()
    hist = trk.historical
    assert hist is not None
    trk.update(away(3_000, 0))
    for t in (300, 600, 900):
        trk.update(away(3_000, t))
    assert trk.state is ApproachState.STATIONARY
    assert not hist.leg_open
    walk_home(trk, 1_200, 2_600)
    assert hist.trips_learned == 1
    trip = hist.export()["trips"][0]
    assert trip["max_distance_m"] <= 2_610
    assert trip["duration_s"] < 2_600 / 1.4


def test_undone_glitch_is_dropped_from_the_leg() -> None:
    trk = tracker()
    hist = trk.historical
    assert hist is not None
    trk.update(away(1_500, 0, accuracy=40))
    trk.update(away(1_500, 120, accuracy=40))
    trk.update(away(1_300, 240, accuracy=40))
    fixes = hist.leg_fixes
    result = trk.update(away(1_500, 360, accuracy=40))
    assert result.outcome is not None
    assert result.outcome.rolled_back
    assert hist.leg_fixes == fixes  # the glitch out, the return in


def test_zero_accuracy_means_unknown_not_perfect() -> None:
    """Regression: accuracy 0 made every wobble movement, so no stop was seen."""
    trk = tracker()
    for i in range(12):
        trk.update(away(1_500 + (5 if i % 2 else -5), 300 * i, accuracy=0.0))
    assert trk.state is ApproachState.STATIONARY


def test_clock_step_back_keeps_silence_detection() -> None:
    """Regression: a reference left in the future disabled the silence rule."""
    trk = tracker()
    trk.set_target(Target(HOME_LAT + 0.0001, HOME_LON, 100.0), T0 + 7_200)
    for t in (0, 60, 120):
        trk.update(away(10_000 - 13 * t, t))
    assert trk.snapshot(T0 + 120).last_heard == T0 + 120
    transition = trk.tick(T0 + 120 + PARAMS.stale_after_s)
    assert transition is not None
    assert transition.current is ApproachState.STATIONARY


def test_moving_the_zone_far_forgets_history() -> None:
    trk = tracker()
    learn_trips(trk, 2)
    hist = trk.historical
    assert hist is not None
    trk.update(away(3_000, 30_000))
    assert hist.leg_open
    assert not trk.set_target(Target(HOME_LAT + 0.0005, HOME_LON, 100.0), T0)
    assert not hist.leg_open
    assert hist.stored_trips == 2
    assert trk.set_target(Target(HOME_LAT + 0.01, HOME_LON, 100.0), T0)
    assert hist.stored_trips == 0


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


def test_undone_glitch_restores_the_approach_state() -> None:
    """Regression: a bounce flipped stationary -> away and left it there."""
    trk = tracker()
    for t in (0, 300, 600, 900):
        trk.update(away(3_000, t, accuracy=40))
    assert trk.state is ApproachState.STATIONARY
    since = trk.snapshot(T0 + 900).state_since
    jump = trk.update(away(2_000, 1_200, accuracy=40))
    assert jump.transition is not None
    assert jump.transition.current is ApproachState.AWAY
    back = trk.update(away(3_000, 1_500, accuracy=40))
    assert back.outcome is not None
    assert back.outcome.rolled_back
    assert back.transition is not None
    assert back.transition.current is ApproachState.STATIONARY
    snap = trk.snapshot(T0 + 1_500)
    assert snap.state is ApproachState.STATIONARY
    assert snap.state_since == since


def test_clock_step_back_restarts_the_leg() -> None:
    """Regression: a leg with times out of order was stored, then dropped."""
    trk = tracker()
    hist = trk.historical
    assert hist is not None
    for t in (0, 300):
        trk.update(away(3_000 - 2 * t, t))
    trk.update(away(2_000, -3_600))  # the clock stepped back an hour
    assert hist.leg_fixes == 1
    assert hist.leg_started_at == T0 - 3_600
