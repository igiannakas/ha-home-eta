"""Rule-by-rule tests for the kinematic estimator."""

from __future__ import annotations

import math

import pytest

from custom_components.home_eta.core.kinematic import (
    Fix,
    FixVerdict,
    KinematicEstimator,
    Target,
)
from custom_components.home_eta.core.params import EstimatorParams
from tests.sim import HOME_LAT, HOME_LON, T0, TARGET, to_latlon


def fix(east_m: float, t: float, accuracy_m: float = 10.0, north_m: float = 0.0) -> Fix:
    """Return a fix ``east_m`` metres east of home, ``t`` seconds after T0."""
    lat, lon = to_latlon(east_m, north_m)
    return Fix(lat, lon, accuracy_m, T0 + t)


def estimator(**overrides: float) -> KinematicEstimator:
    """Return an estimator for the test target."""
    return KinematicEstimator(EstimatorParams(**overrides), TARGET)  # type: ignore[arg-type]


def drive(est: KinematicEstimator, speed: float, start_m: float, *times: float) -> None:
    """Feed noise-free fixes moving towards home at ``speed``."""
    for t in times:
        assert est.add_fix(fix(start_m - speed * t, t)).accepted


class TestFixAcceptance:
    """Which fixes are used, and how."""

    def test_first_fix_sets_anchor_without_speed(self) -> None:
        est = estimator()
        outcome = est.add_fix(fix(5_000, 0))
        assert outcome.verdict is FixVerdict.FIRST
        assert outcome.distance_m == pytest.approx(5_000, rel=1e-3)
        assert est.closing_speed_mps is None

    def test_moving_fix_measures_radial_speed(self) -> None:
        est = estimator()
        est.add_fix(fix(5_000, 0))
        outcome = est.add_fix(fix(4_000, 100))
        assert outcome.verdict is FixVerdict.MOVED
        assert outcome.speed_sample_mps == pytest.approx(10.0, rel=1e-3)
        assert est.closing_speed_mps == pytest.approx(10.0, rel=1e-3)

    @pytest.mark.parametrize(
        ("lat", "lon", "accuracy"),
        [
            (math.nan, HOME_LON, 10.0),
            (HOME_LAT, math.inf, 10.0),
            (91.0, HOME_LON, 10.0),
            (HOME_LAT, -181.0, 10.0),
            (HOME_LAT, HOME_LON, -1.0),
            (HOME_LAT, HOME_LON, 0.0),
            (HOME_LAT, HOME_LON, math.nan),
        ],
    )
    def test_invalid_fix_rejected(
        self, lat: float, lon: float, accuracy: float
    ) -> None:
        est = estimator()
        outcome = est.add_fix(Fix(lat, lon, accuracy, T0))
        assert outcome.verdict is FixVerdict.REJECTED_INVALID
        assert est.last_fix_time is None

    def test_poor_accuracy_rejected_and_good_enough_accepted(self) -> None:
        est = estimator()
        assert est.add_fix(fix(5_000, 0, accuracy_m=400)).verdict is (
            FixVerdict.REJECTED_ACCURACY
        )
        assert est.add_fix(fix(5_000, 0, accuracy_m=60)).verdict is FixVerdict.FIRST

    def test_out_of_order_and_duplicate_time_rejected(self) -> None:
        est = estimator()
        est.add_fix(fix(5_000, 100))
        assert est.add_fix(fix(4_000, 50)).verdict is FixVerdict.REJECTED_OUT_OF_ORDER
        assert est.add_fix(fix(4_000, 100)).verdict is FixVerdict.REJECTED_OUT_OF_ORDER

    def test_gps_jump_rejected_and_state_unchanged(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 0, 60, 120)
        before = (est.closing_speed_mps, est.distance_m, est.last_fix_time)
        assert est.add_fix(fix(5_000 - 1_200 - 3_000, 150)).verdict is (
            FixVerdict.REJECTED_JUMP
        )
        assert (est.closing_speed_mps, est.distance_m, est.last_fix_time) == before

    def test_same_place_moments_apart_merged_keeping_better_fix(self) -> None:
        est = estimator()
        est.add_fix(fix(5_000, 0, accuracy_m=50))
        assert est.add_fix(fix(5_010, 5, accuracy_m=8)).verdict is FixVerdict.MERGED
        assert est.distance_m == pytest.approx(5_010, rel=1e-3)
        assert est.add_fix(fix(5_000, 8, accuracy_m=30)).verdict is FixVerdict.MERGED
        assert est.distance_m == pytest.approx(5_010, rel=1e-3)

    def test_fast_movement_moments_apart_is_not_merged(self) -> None:
        """Motorway fixes can be seconds apart; they are real movement."""
        est = estimator()
        est.add_fix(fix(5_000, 0))
        assert est.add_fix(fix(4_700, 10)).verdict is FixVerdict.MOVED

    def test_long_gap_reinitialises(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 0, 60, 120)
        outcome = est.add_fix(fix(3_000, 120 + 901))
        assert outcome.verdict is FixVerdict.REINITIALISED
        assert est.closing_speed_mps is None


class TestStopsAndStarts:
    """Standing still, setting off and changing mode."""

    def test_still_fix_without_speed_does_not_seed(self) -> None:
        est = estimator()
        est.add_fix(fix(5_000, 0))
        outcome = est.add_fix(fix(5_005, 120))
        assert outcome.verdict is FixVerdict.STATIONARY
        assert est.closing_speed_mps is None

    def test_still_fix_soon_after_moving_is_not_evidence_of_a_stop(self) -> None:
        """A step below the noise at walking pace must not drag the speed down."""
        est = estimator()
        drive(est, 1.4, 2_000, 0, 360, 720)
        speed = est.closing_speed_mps
        # 20 s at 1.4 m/s is 28 m, inside the noise radius.
        outcome = est.add_fix(fix(2_000 - 1.4 * 740, 740))
        assert outcome.verdict is FixVerdict.STATIONARY
        assert est.closing_speed_mps == speed

    def test_stopping_in_traffic_is_followed(self) -> None:
        """Two still fixes where the car should have moved far re-seed to ~0."""
        est = estimator()
        drive(est, 13.0, 10_000, 0, 40, 80, 120, 160)
        assert est.closing_speed_mps == pytest.approx(13.0, rel=0.01)
        position = 10_000 - 13.0 * 160
        est.add_fix(fix(position, 220))
        est.add_fix(fix(position, 280))
        assert est.closing_speed_mps is not None
        assert abs(est.closing_speed_mps) < 0.5

    def test_setting_off_after_short_stop_seeds_from_the_move(self) -> None:
        est = estimator()
        drive(est, 13.0, 10_000, 0, 40, 80, 120, 160)
        position = 10_000 - 13.0 * 160
        est.add_fix(fix(position, 220))
        est.add_fix(fix(position, 280))
        outcome = est.add_fix(fix(position - 1.4 * 120, 400))
        assert outcome.verdict is FixVerdict.MOVED
        assert est.closing_speed_mps == pytest.approx(1.4, rel=0.05)

    def test_moving_after_long_stop_starts_a_fresh_leg(self) -> None:
        est = estimator()
        drive(est, 25.0, 20_000, 0, 30, 60, 90)
        position = 20_000 - 25.0 * 90
        for t in (300, 600, 750):
            assert est.add_fix(fix(position, t)).verdict is FixVerdict.STATIONARY
        assert est.stopped
        # Off on foot: the train's speed is forgotten and the new leg is
        # measured from the last fix at the stop, not from the stop's start.
        outcome = est.add_fix(fix(position - 1.4 * 210, 960))
        assert outcome.verdict is FixVerdict.MOVED
        assert outcome.speed_sample_mps == pytest.approx(1.4, rel=0.05)
        assert est.closing_speed_mps == pytest.approx(1.4, rel=0.05)
        assert not est.stopped
        est.add_fix(fix(position - 1.4 * 510, 1_260))
        assert est.closing_speed_mps == pytest.approx(1.4, rel=0.05)

    def test_mode_change_confirmed_by_two_samples(self) -> None:
        est = estimator()
        drive(est, 25.0, 30_000, 0, 30, 60, 90, 120, 150)
        assert est.closing_speed_mps == pytest.approx(25.0, rel=0.01)
        position = 30_000 - 25.0 * 150
        est.add_fix(fix(position - 1.4 * 360, 510))
        # Held back pending confirmation.
        assert est.closing_speed_mps == pytest.approx(25.0, rel=0.01)
        est.add_fix(fix(position - 1.4 * 720, 870))
        assert est.closing_speed_mps == pytest.approx(1.4, rel=0.05)
        assert est.diagnostics()["mode_changes"] == 1

    def test_lone_outlier_is_dropped(self) -> None:
        est = estimator()
        drive(est, 13.0, 30_000, 0, 60, 120, 180, 240)
        # One sample implies 30 m/s, the next is back to 13 m/s overall.
        est.add_fix(fix(30_000 - 13.0 * 240 - 30.0 * 60, 300))
        assert est.closing_speed_mps == pytest.approx(13.0, rel=0.01)
        est.add_fix(fix(30_000 - 13.0 * 240 - 30.0 * 60 - 13.0 * 60, 360))
        assert est.closing_speed_mps == pytest.approx(13.0, rel=0.02)
        assert est.diagnostics()["mode_changes"] == 0

    def test_young_seed_replaced_by_clear_disagreement(self) -> None:
        est = estimator()
        est.add_fix(fix(5_000, 0))
        est.add_fix(fix(4_400, 60))  # seeds at 10 m/s (straddling a change)
        est.add_fix(fix(4_400 - 1.4 * 360, 420))
        assert est.closing_speed_mps == pytest.approx(1.4, rel=0.05)

    def test_moving_away_gives_negative_speed(self) -> None:
        est = estimator()
        for t in (0, 60, 120, 180):
            est.add_fix(fix(2_000 + 10.0 * t, t))
        assert est.closing_speed_mps == pytest.approx(-10.0, rel=0.01)


class TestTarget:
    """Staleness and a moved target."""

    def test_stale_without_a_recent_fix(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 0, 60, 120)
        assert not est.is_stale(T0 + 120 + 900)
        assert est.is_stale(T0 + 120 + 901)
        assert estimator().is_stale(T0)

    def test_target_change_resets(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 0, 60)
        est.set_target(Target(HOME_LAT + 0.01, HOME_LON, 100.0))
        assert est.last_fix_time is None
        assert est.closing_speed_mps is None

    def test_same_target_keeps_state(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 0, 60)
        est.set_target(TARGET)
        assert est.closing_speed_mps is not None


class TestClockAndTime:
    """Clock steps and arrival times that fall behind the clock."""

    def test_clock_step_back_restarts(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 3_600, 3_660, 3_720)
        outcome = est.add_fix(fix(4_000, 0))
        assert outcome.verdict is FixVerdict.REINITIALISED
        assert est.last_fix_time == T0
        assert est.add_fix(fix(3_400, 60)).verdict is FixVerdict.MOVED

    def test_small_reordering_is_rejected_not_restarted(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 100, 160)
        assert est.add_fix(fix(4_000, 130)).verdict is FixVerdict.REJECTED_OUT_OF_ORDER

    def test_last_fix_in_the_future_is_stale(self) -> None:
        est = estimator()
        drive(est, 10.0, 5_000, 1_000, 1_060)
        assert not est.is_stale(T0 + 1_000)
        assert est.is_stale(T0)


def test_moving_away_is_still_filtered() -> None:
    """Receding speeds are smoothed like closing ones, not re-seeded each fix."""
    est = estimator()
    samples = []
    for i, t in enumerate((0, 60, 120, 180, 240, 300)):
        wobble = 40.0 if i % 2 else -40.0
        samples.append(est.add_fix(fix(1_000 + 10.0 * t + wobble, t)).speed_sample_mps)
    assert est.closing_speed_mps == pytest.approx(-10.0, rel=0.15)
    # Smoothed: nearer the true speed than the last sample was.
    assert samples[-1] is not None
    assert abs(est.closing_speed_mps + 10.0) < abs(samples[-1] + 10.0)


class TestRobustness:
    """Regressions from the final review: noise, glitches and stops."""

    def test_settled_speed_survives_one_red_light_sample(self) -> None:
        """A seed with one agreeing sample is no longer replaced by an outlier."""
        est = estimator()
        drive(est, 13.0, 10_000, 0, 60, 120)
        position = 10_000 - 13.0 * 120
        outcome = est.add_fix(fix(position - 3.0 * 60, 180))
        assert outcome.verdict is FixVerdict.MOVED
        assert not outcome.speed_updated
        assert est.closing_speed_mps == pytest.approx(13.0, rel=1e-3)
        est.add_fix(fix(position - 3.0 * 60 - 13.0 * 60, 240))
        assert est.closing_speed_mps == pytest.approx(13.0, rel=0.05)

    def test_warm_up_is_a_noise_weighted_mean(self) -> None:
        """A poor fix gets its own weight, not the seed's."""
        est = estimator()
        est.add_fix(fix(10_000, 0, accuracy_m=10))
        est.add_fix(fix(10_000 - 600, 60, accuracy_m=10))  # 10 m/s, full weight
        est.add_fix(fix(10_000 - 600 - 720, 120, accuracy_m=80))  # 12 m/s
        noise = math.hypot(10, 80) / 1.51 / 60
        weight = 0.3 / noise
        expected = 10.0 + weight / (1 + weight) * 2.0
        assert est.closing_speed_mps == pytest.approx(expected, rel=1e-3)

    def test_slow_walker_with_poor_accuracy_is_not_stopped(self) -> None:
        """At 0.6 m/s and 140 m accuracy, 10 minutes is too short to see a move."""
        est = estimator()
        for t in (0, 833, 1_666):
            est.add_fix(fix(5_000 - 0.6 * t, t, accuracy_m=140))
        # A background fetch 614 s after the last movement: 368 m walked,
        # inside the 393 m noise radius.
        outcome = est.add_fix(fix(5_000 - 0.6 * 2_280, 2_280, accuracy_m=140))
        assert outcome.verdict is FixVerdict.STATIONARY
        assert not est.stopped
        for t in (2_499, 3_332):
            outcome = est.add_fix(fix(5_000 - 0.6 * t, t, accuracy_m=140))
            assert outcome.verdict is FixVerdict.MOVED
        assert est.closing_speed_mps == pytest.approx(0.6, rel=0.05)

    def test_long_wait_proves_a_stop_when_barely_moving(self) -> None:
        """Below the closing speed, ten still minutes prove a stop (platform wait).

        At 0.34 m/s and 100 m accuracy no still fix could show the creep, but
        such a person is not travelling anywhere useful either.
        """
        est = estimator()
        est.add_fix(fix(5_000, 0, accuracy_m=100))
        est.add_fix(fix(4_700, 890, accuracy_m=100))
        assert est.closing_speed_mps == pytest.approx(300 / 890, rel=1e-3)
        for t in (1_090, 1_290):
            est.add_fix(fix(4_700, t, accuracy_m=100))
            assert not est.stopped
        est.add_fix(fix(4_700, 1_490, accuracy_m=100))
        assert est.stopped

    def test_position_glitch_that_returns_is_undone(self) -> None:
        """Indoor Wi-Fi: one fix jumps 300 m away and the next is back."""
        est = estimator()
        est.add_fix(fix(1_500, 0, accuracy_m=40))
        est.add_fix(fix(1_500, 300, accuracy_m=40))
        jump = est.add_fix(fix(1_300, 600, accuracy_m=40))
        assert jump.verdict is FixVerdict.MOVED
        assert jump.speed_updated
        back = est.add_fix(fix(1_500, 900, accuracy_m=40))
        assert back.rolled_back
        assert back.verdict is FixVerdict.STATIONARY
        assert est.closing_speed_mps is None
        assert est.diagnostics()["glitches"] == 1
        # The same bounce again is now within the learned jitter.
        assert est.add_fix(fix(1_300, 1_200, accuracy_m=40)).verdict is (
            FixVerdict.STATIONARY
        )

    def test_driving_is_never_mistaken_for_a_glitch(self) -> None:
        est = estimator()
        drive(est, 13.0, 10_000, *range(0, 600, 40))
        assert est.diagnostics()["glitches"] == 0

    def test_jitter_is_capped_and_cleared_by_real_movement(self) -> None:
        est = estimator()
        est.add_fix(fix(5_000, 0, accuracy_m=20))
        est.add_fix(fix(3_000, 60, accuracy_m=20))  # a 2 km glitch...
        assert est.add_fix(fix(5_000, 120, accuracy_m=20)).rolled_back  # ...undone
        assert est.diagnostics()["jitter_m"] == pytest.approx(2_500, rel=1e-3)
        # The cap keeps a real 200 m move visible (noise radius ~56 m).
        assert est.add_fix(fix(4_800, 300, accuracy_m=20)).verdict is FixVerdict.MOVED
        est.add_fix(fix(4_600, 480, accuracy_m=20))
        assert est.diagnostics()["jitter_m"] == 0.0

    def test_speed_updated_only_when_the_filter_took_a_sample(self) -> None:
        est = estimator()
        assert not est.add_fix(fix(2_000, 0)).speed_updated
        assert est.add_fix(fix(2_000 - 1.4 * 360, 360)).speed_updated
        # 20 s later at walking pace, below the noise: not evidence, no sample.
        assert not est.add_fix(fix(2_000 - 1.4 * 380, 380)).speed_updated

    def test_trip_after_a_bounce_while_parked_is_seen(self) -> None:
        """Regression: after one undone bounce the whole drive home went unseen.

        Fixes 500 m apart at 100 m accuracy sit between one and two noise
        radii, inside the raised threshold; the anchor must stay put so the
        steps add up.
        """
        est = estimator()
        for t in (0, 300, 600, 900, 1_200):
            est.add_fix(fix(6_000, t, accuracy_m=100))
        assert est.stopped
        est.add_fix(fix(5_000, 1_500, accuracy_m=100))
        assert est.add_fix(fix(6_000, 1_800, accuracy_m=100)).rolled_back
        verdicts = [
            est.add_fix(fix(6_000 - 500 * i, 1_800 + 42 * i, accuracy_m=100)).verdict
            for i in range(1, 6)
        ]
        assert FixVerdict.MOVED in verdicts[:2]
        assert est.closing_speed_mps == pytest.approx(12.0, rel=0.1)

    def test_seed_replacement_is_bounded(self) -> None:
        """Stop-and-go samples must not make the filter echo the last sample."""
        est = estimator()
        position, t = 20_000.0, 0.0
        est.add_fix(fix(position, t))
        speeds = []
        for i in range(8):
            t += 60
            position -= (13.0 if i % 2 == 0 else 3.0) * 60
            est.add_fix(fix(position, t))
            speeds.append(est.closing_speed_mps)
        # Two replacements at most, then disagreement needs confirmation:
        # the filter stops echoing each sample.
        assert speeds[:3] == pytest.approx([13.0, 3.0, 13.0], rel=1e-3)
        assert all(v == pytest.approx(speeds[3], abs=0.01) for v in speeds[3:])

    def test_no_movement_for_the_stale_period_is_a_stop(self) -> None:
        """Even when the believed speed is too slow to have shown movement."""
        est = estimator()
        for t in (0, 833, 1_666):
            est.add_fix(fix(5_000 - 0.6 * t, t, accuracy_m=140))
        position = 5_000 - 0.6 * 1_666
        for t in (1_966, 2_266):
            est.add_fix(fix(position, t, accuracy_m=140))
            assert not est.stopped
        est.add_fix(fix(position, 1_666 + 900, accuracy_m=140))
        assert est.stopped
