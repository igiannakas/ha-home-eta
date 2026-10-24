"""Closing-speed accuracy on synthetic journeys.

The filtered closing speed feeds heading home: the progress rule and the
measured half of the expected arrival. It is judged here by the arrival it
implies on its own (remaining distance over the speed) against ground truth,
while at least two minutes remain, over 30 seeded journeys per scenario.
Bounds are the behaviour we promise, with the reasoning beside each.
"""

from __future__ import annotations

from dataclasses import dataclass
import statistics

import pytest

from custom_components.home_eta.core.kinematic import KinematicEstimator
from custom_components.home_eta.core.params import EstimatorParams
from tests.sim import DENSE, IOS, TARGET, FixPolicy, Segment, SimFix, simulate

SEEDS = range(30)
MIN_REMAINING_S = 120.0
PARAMS = EstimatorParams()


def implied_remaining_s(estimator: KinematicEstimator, now: float) -> float | None:
    """Return the time left at the filtered speed, None when it gives none."""
    speed, distance = estimator.closing_speed_mps, estimator.distance_m
    if (
        speed is None
        or distance is None
        or estimator.stopped
        or estimator.is_stale(now)
        or speed < PARAMS.min_closing_speed_mps
    ):
        return None
    return min(max(distance - TARGET.radius_m, 0.0) / speed, PARAMS.max_eta_s)


@dataclass(frozen=True)
class Check:
    """The implied arrival's error at one accepted fix."""

    leg: int
    nth_in_leg: int
    """1-based count of accepted fixes within the leg."""
    nth_overall: int
    error: float | None
    """Relative error, None when the speed gave no arrival."""


def run(
    start: tuple[float, float], segments: list[Segment], policy: FixPolicy, seed: int
) -> list[Check]:
    """Feed one journey through a fresh estimator."""
    fixes: list[SimFix] = simulate(start, segments, policy=policy, seed=seed)
    estimator = KinematicEstimator(PARAMS, TARGET)
    checks: list[Check] = []
    overall = 0
    per_leg: dict[int, int] = {}
    for sim_fix in fixes:
        outcome = estimator.add_fix(sim_fix.fix)
        if not outcome.accepted:
            continue
        overall += 1
        per_leg[sim_fix.leg] = per_leg.get(sim_fix.leg, 0) + 1
        if sim_fix.true_remaining_s < MIN_REMAINING_S:
            continue
        predicted = implied_remaining_s(estimator, sim_fix.fix.timestamp)
        error = None
        if predicted is not None:
            error = (predicted - sim_fix.true_remaining_s) / sim_fix.true_remaining_s
        checks.append(Check(sim_fix.leg, per_leg[sim_fix.leg], overall, error))
    return checks


def errors(checks: list[Check]) -> list[float]:
    """Return absolute errors, failing if the speed gave no arrival anywhere."""
    assert all(c.error is not None for c in checks), "no speed while moving"
    return [abs(c.error) for c in checks if c.error is not None]


STRAIGHT_CAR = ((15_000.0, 0.0), [Segment((0.0, 0.0), 13.0)])
STRAIGHT_WALK = ((2_000.0, 0.0), [Segment((0.0, 0.0), 1.4)])
WINDING_CAR = (
    (15_000.0, 0.0),
    [
        Segment((12_000.0, 1_000.0), 13.0),
        Segment((9_000.0, -1_000.0), 13.0),
        Segment((6_000.0, 1_000.0), 13.0),
        Segment((3_000.0, -1_000.0), 13.0),
        Segment((0.0, 0.0), 13.0),
    ],
)


def train_then_walk(dwell_s: float) -> tuple[tuple[float, float], list[Segment]]:
    """Return 20 km by train at 25 m/s, a platform wait, then a 2 km walk."""
    return (
        (22_000.0, 0.0),
        [Segment((2_000.0, 0.0), 25.0, dwell_s=dwell_s), Segment((0.0, 0.0), 1.4)],
    )


@pytest.mark.parametrize("policy", [IOS, DENSE], ids=["ios", "dense"])
def test_straight_car_within_4_percent(policy: FixPolicy) -> None:
    """A steady radial drive is the estimator's home ground."""
    for seed in SEEDS:
        checks = [c for c in run(*STRAIGHT_CAR, policy, seed) if c.nth_overall >= 3]
        assert checks
        assert max(errors(checks)) <= 0.04, seed


def test_straight_walk_ios_within_20_percent() -> None:
    """Walking fixes are ~6 minutes apart on iOS, each well measured."""
    for seed in SEEDS:
        checks = [c for c in run(*STRAIGHT_WALK, IOS, seed) if c.nth_overall >= 3]
        assert max(errors(checks), default=0.0) <= 0.20, seed


def test_straight_walk_dense_noise_limited() -> None:
    """Fixes a minute apart while walking carry ~16 % noise each.

    Single checks can be off by up to a third, but the typical error is small.
    """
    all_errors: list[float] = []
    for seed in SEEDS:
        checks = [c for c in run(*STRAIGHT_WALK, DENSE, seed) if c.nth_overall >= 3]
        seed_errors = errors(checks)
        assert max(seed_errors) <= 0.35, seed
        all_errors.extend(seed_errors)
    assert statistics.median(all_errors) <= 0.05


@pytest.mark.parametrize("policy", [IOS, DENSE], ids=["ios", "dense"])
def test_winding_route_within_25_percent(policy: FixPolicy) -> None:
    """A 1.2 detour factor mostly cancels; corners near the end cost accuracy."""
    all_errors: list[float] = []
    for seed in SEEDS:
        checks = [c for c in run(*WINDING_CAR, policy, seed) if c.nth_overall >= 3]
        seed_errors = errors(checks)
        assert max(seed_errors) <= 0.25, seed
        all_errors.extend(seed_errors)
    assert statistics.median(all_errors) <= 0.07


@pytest.mark.parametrize("dwell_s", [120.0, 720.0], ids=["short_wait", "long_wait"])
def test_train_then_walk_ios_adapts_by_second_walking_fix(dwell_s: float) -> None:
    """On the train the speed cannot know about the walk ahead.

    The first walking fix straddles the change of mode; from the second the
    speed follows the walk.
    """
    second: list[float] = []
    for seed in SEEDS:
        walk = [c for c in run(*train_then_walk(dwell_s), IOS, seed) if c.leg == 1]
        later = [c for c in walk if c.nth_in_leg >= 3]
        assert max(errors(later), default=0.0) <= 0.25, seed
        second.extend(errors([c for c in walk if c.nth_in_leg == 2]))
    # Typically within 5 % from the second report on foot.
    assert statistics.median(second) <= 0.05


@pytest.mark.parametrize("dwell_s", [120.0, 720.0], ids=["short_wait", "long_wait"])
def test_train_then_walk_dense_adapts_within_five_fixes(dwell_s: float) -> None:
    """With minute-by-minute fixes the walk is learned over a few noisy samples."""
    third: list[float] = []
    for seed in SEEDS:
        walk = [c for c in run(*train_then_walk(dwell_s), DENSE, seed) if c.leg == 1]
        later = [c for c in walk if c.nth_in_leg >= 5]
        assert max(errors(later), default=0.0) <= 0.35, seed
        third.extend(errors([c for c in walk if c.nth_in_leg == 3]))
    assert statistics.median(third) <= 0.20


def test_train_leg_is_optimistic_by_design() -> None:
    """Pin the limit: on the train the speed alone ignores the walk ahead.

    That is why heading home blends it with the typical speed for the distance.
    """
    checks = run(*train_then_walk(120.0), IOS, 0)
    train = [c for c in checks if c.leg == 0 and c.nth_overall >= 3]
    assert train
    assert all(c.error is not None and c.error < -0.5 for c in train)
