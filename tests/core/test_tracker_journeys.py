"""Arriving soon on synthetic journeys, through the person tracker and its tick."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from custom_components.home_eta.core.homeward import HomewardReason
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.tracker import (
    PersonReport,
    PersonTracker,
    TrackerConfig,
)
from tests.sim import IOS, TARGET, FixPolicy, Segment, simulate, to_latlon

SEEDS = range(30)
TICK_S = 60.0
MIN = 60.0


@dataclass(frozen=True, slots=True)
class Minute:
    """What was published at one moment of a journey."""

    heading: bool
    soon: bool
    reason: HomewardReason
    remaining_s: float


def replay(
    start: tuple[float, float], segments: list[Segment], policy: FixPolicy, seed: int
) -> list[Minute]:
    """Leave home, then follow the journey home with minute ticks between fixes."""
    fixes = simulate(start, segments, policy=policy, seed=seed)
    trk = PersonTracker(TrackerConfig(EstimatorParams()), TARGET, at_target=True)
    first = fixes[0].fix.timestamp
    lat, lon = to_latlon(TARGET.radius_m + 50.0, 0.0)
    trk.update(PersonReport(first - TICK_S, False, (), lat, lon, 15.0))
    arrival = first + fixes[0].true_remaining_s
    out: list[Minute] = []

    def record(t: float) -> None:
        snap = trk.snapshot(t)
        out.append(
            Minute(
                snap.heading_home,
                snap.arriving_soon,
                snap.homeward.reason,
                arrival - t,
            )
        )

    tick = first
    for sim_fix in fixes:
        fix = sim_fix.fix
        while tick < fix.timestamp:
            trk.tick(tick)
            record(tick)
            tick += TICK_S
        trk.update(
            PersonReport(
                fix.timestamp, False, (), fix.latitude, fix.longitude, fix.accuracy_m
            )
        )
        record(fix.timestamp)
    while tick < arrival:
        trk.tick(tick)
        record(tick)
        tick += TICK_S
    return out


def lead_s(minutes: list[Minute]) -> float:
    """Return how long before arrival arriving soon came on (it must have)."""
    first = next(m for m in minutes if m.soon)
    return first.remaining_s


def stays_on(minutes: list[Minute], attribute: str) -> bool:
    """Return True when ``attribute`` never goes off once on."""
    seen = False
    for minute in minutes:
        if getattr(minute, attribute):
            seen = True
        elif seen:
            return False
    return seen


@pytest.mark.parametrize("speed", [0.7, 1.0, 1.4], ids=["slow", "steady", "brisk"])
def test_walk_home_with_sparse_reports(speed: float) -> None:
    """At 0.7 m/s an iPhone reports only every ~12 minutes; that is not a stop.

    Arriving soon comes on by the 15-minute threshold, or within 800 m: for the
    slowest walker that is 19 minutes out, plus up to one report interval.
    """
    for seed in SEEDS:
        minutes = replay((3_000.0, 0.0), [Segment((0.0, 0.0), speed)], IOS, seed)
        assert stays_on(minutes, "heading"), seed
        assert stays_on(minutes, "soon"), seed
        assert 10 * MIN <= lead_s(minutes) <= 31 * MIN, seed


def test_drive_home() -> None:
    """20 km at a constant 13 m/s, which the typical speed for the distance tempers.

    Real drives slow down near home (the household's own did); on this
    idealised one arriving soon comes on a few minutes after the threshold.
    """
    for seed in SEEDS:
        minutes = replay((20_000.0, 0.0), [Segment((0.0, 0.0), 13.0)], IOS, seed)
        assert stays_on(minutes, "heading"), seed
        assert stays_on(minutes, "soon"), seed
        assert 9 * MIN <= lead_s(minutes) <= 16 * MIN, seed


def test_a_long_stop_on_the_way_ends_the_trip_home_until_moving_on() -> None:
    """A 30-minute stop with background reports every ~10 minutes, then a walk.

    The stop ends the trip home: as a stay, or, when the phone says nothing
    there for long, as overdue. Walking on starts it again.
    """
    segments = [
        Segment((4_000.0, 0.0), 13.0, dwell_s=1_800.0),
        Segment((0.0, 0.0), 1.4),
    ]
    policy = FixPolicy(significant_m=500.0, interval_s=None, background_mean_s=600.0)
    ended = {HomewardReason.AT_PLACE, HomewardReason.OVERDUE}
    for seed in SEEDS:
        minutes = replay((10_000.0, 0.0), segments, policy, seed)
        stopped = [m for m in minutes if not m.heading and m.reason in ended]
        assert stopped, seed
        assert not any(m.soon for m in stopped), seed
        assert minutes[-1].heading, seed
        assert minutes[-1].soon, seed
