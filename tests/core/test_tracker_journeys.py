"""The approach state on synthetic journeys, with the integration's minute tick."""

from __future__ import annotations

import pytest

from custom_components.home_eta.core.approach import ApproachState
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.tracker import (
    PersonReport,
    PersonTracker,
    TrackerConfig,
)
from tests.sim import HOME_LAT, HOME_LON, IOS, TARGET, FixPolicy, Segment, simulate

SEEDS = range(30)
TICK_S = 60.0


def replay(
    start: tuple[float, float], segments: list[Segment], policy: FixPolicy, seed: int
) -> list[ApproachState]:
    """Feed a journey and minute ticks; return every state seen before arrival."""
    fixes = simulate(start, segments, policy=policy, seed=seed)
    trk = PersonTracker(TrackerConfig(EstimatorParams()), TARGET)
    states: list[ApproachState] = []
    last = fixes[0].fix.timestamp
    for sim_fix in fixes:
        fix = sim_fix.fix
        tick = last + TICK_S
        while tick < fix.timestamp:
            trk.tick(tick)
            states.append(trk.state)
            tick += TICK_S
        last = fix.timestamp
        trk.update(
            PersonReport(
                fix.timestamp, False, (), fix.latitude, fix.longitude, fix.accuracy_m
            )
        )
        states.append(trk.state)
    return states


@pytest.mark.parametrize("speed", [0.7, 1.0, 1.4], ids=["slow", "steady", "brisk"])
def test_walkers_with_sparse_reports_stay_on_the_way(speed: float) -> None:
    """At 0.7 m/s an iPhone reports only every ~12 minutes; that is not a stop."""
    for seed in SEEDS:
        states = replay((3_000.0, 0.0), [Segment((0.0, 0.0), speed)], IOS, seed)
        assert ApproachState.STATIONARY not in states, seed
        assert states[-1] is ApproachState.ARRIVING_SOON, seed


def test_drive_reaches_arriving_soon() -> None:
    for seed in SEEDS:
        states = replay((20_000.0, 0.0), [Segment((0.0, 0.0), 13.0)], IOS, seed)
        assert ApproachState.APPROACHING in states, seed
        assert ApproachState.STATIONARY not in states, seed
        assert states[-1] is ApproachState.ARRIVING_SOON, seed


def test_long_stop_on_the_way_is_stationary_then_resumes() -> None:
    """A 30-minute stop with a still background report, then the walk home."""
    segments = [
        Segment((4_000.0, 0.0), 13.0, dwell_s=1_800.0),
        Segment((0.0, 0.0), 1.4),
    ]
    policy = FixPolicy(significant_m=500.0, interval_s=None, background_mean_s=600.0)
    for seed in SEEDS:
        states = replay((10_000.0, 0.0), segments, policy, seed)
        assert ApproachState.STATIONARY in states, seed
        last_stop = len(states) - 1 - states[::-1].index(ApproachState.STATIONARY)
        assert ApproachState.APPROACHING in states[last_stop:], seed
        assert states[-1] is ApproachState.ARRIVING_SOON, seed


@pytest.mark.parametrize(
    ("start_m", "speed"), [(3_000.0, 1.4), (20_000.0, 13.0)], ids=["walk", "drive"]
)
def test_learned_eta_matches_past_trips(start_m: float, speed: float) -> None:
    """After 12 similar trips the learned ETA is within 10 % at every distance.

    Regression: pooling log-spaced buckets made it a step function, off by up
    to +120 % near home.
    """
    trk = PersonTracker(
        TrackerConfig(EstimatorParams(), historical=True), TARGET, at_target=True
    )
    errors: list[float] = []
    for seed in range(15):
        fixes = simulate(
            (start_m, 0.0),
            [Segment((0.0, 0.0), speed)],
            policy=IOS,
            seed=seed,
            t0=1.76e9 + seed * 86_400,
        )
        first = fixes[0].fix
        trk.update(PersonReport(first.timestamp - 60, True, (), HOME_LAT, HOME_LON, 5))
        for sim_fix in fixes:
            fix = sim_fix.fix
            trk.update(
                PersonReport(
                    fix.timestamp,
                    False,
                    (),
                    fix.latitude,
                    fix.longitude,
                    fix.accuracy_m,
                )
            )
            snap = trk.snapshot(fix.timestamp)
            if seed >= 12 and snap.historical is not None:
                predicted = snap.historical.arrival - fix.timestamp
                if sim_fix.true_remaining_s > 60:
                    errors.append(
                        abs(predicted - sim_fix.true_remaining_s)
                        / sim_fix.true_remaining_s
                    )
        arrival = first.timestamp + fixes[0].true_remaining_s
        trk.update(PersonReport(arrival, True, (), HOME_LAT, HOME_LON, 5))
    assert len(errors) >= 10
    assert max(errors) <= 0.10
