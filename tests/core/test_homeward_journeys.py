"""Heading home on synthetic journeys, through the person tracker.

Each journey is replayed with the integration's minute tick. The checks are
the ones that matter for warming the house: the trip home is spotted early
and stays on until arrival; an afternoon nearby or a drive round town is not
taken for one.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
import math

import pytest

from custom_components.home_eta.core.homeward import HomewardReason
from custom_components.home_eta.core.kinematic import Target
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.tracker import (
    PersonReport,
    PersonTracker,
    TrackerConfig,
)
from tests.sim import (
    HOME_LAT,
    HOME_LON,
    IOS,
    T0,
    TARGET,
    FixPolicy,
    Segment,
    SimFix,
    simulate,
    to_latlon,
)

SEEDS = range(20)
TICK_S = 60.0
RINGS = tuple(Target(HOME_LAT, HOME_LON, km * 1000.0) for km in (1, 2, 4, 8, 16, 32))
FINE = FixPolicy(significant_m=None, interval_s=10.0, background_mean_s=None)


@dataclass(frozen=True, slots=True)
class Minute:
    """What was published at one minute of a journey."""

    t: float
    heading: bool
    reason: HomewardReason
    arrival: float | None
    remaining_s: float


def zone_reports(
    start: tuple[float, float],
    segments: list[Segment],
    rings: tuple[Target, ...],
    seed: int,
) -> list[SimFix]:
    """Return the reports iOS sends on crossing each zone, within ~10 s."""
    fine = simulate(start, segments, policy=FINE, seed=seed + 10_000)
    return [
        b
        for a, b in pairwise(fine)
        for ring in rings
        if (a.true_distance_m - ring.radius_m) * (b.true_distance_m - ring.radius_m) < 0
    ]


def replay(
    start: tuple[float, float],
    segments: list[Segment],
    *,
    policy: FixPolicy = IOS,
    seed: int = 0,
    rings: tuple[Target, ...] = (),
    accuracy_m: float = 15.0,
) -> list[Minute]:
    """Leave home for ``start``, then follow ``segments`` home, minute by minute.

    With ``rings``, the phone also reports on crossing each of them, as the
    Companion app does for every zone.
    """
    fixes = simulate(start, segments, policy=policy, seed=seed, accuracy_m=accuracy_m)
    if rings:
        fixes = sorted(
            fixes + zone_reports(start, segments, rings, seed),
            key=lambda f: f.fix.timestamp,
        )
    trk = PersonTracker(TrackerConfig(EstimatorParams()), TARGET, at_target=True)
    trk.set_rings(rings)
    first = fixes[0].fix.timestamp
    # Leaving home: a fix just outside the zone a minute before the journey.
    lat, lon = to_latlon(TARGET.radius_m + 50.0, 0.0)
    trk.update(PersonReport(first - TICK_S, False, (), lat, lon, accuracy_m))
    arrival = fixes[-1].fix.timestamp + fixes[-1].true_remaining_s
    out: list[Minute] = []
    tick = first

    def record(t: float) -> None:
        homeward = trk.snapshot(t).homeward
        assert homeward is not None
        out.append(
            Minute(t, homeward.heading, homeward.reason, homeward.arrival, arrival - t)
        )

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


def warning_s(minutes: list[Minute]) -> float:
    """Return how long before arrival heading home came on for good (0: never)."""
    if not minutes or not minutes[-1].heading:
        return 0.0
    start = len(minutes) - 1
    while start > 0 and minutes[start - 1].heading:
        start -= 1
    return minutes[start].remaining_s


def false_starts(minutes: list[Minute]) -> int:
    """Return how many times heading home came on and went off again."""
    return sum(1 for a, b in pairwise(minutes) if a.heading and not b.heading)


def test_drive_home_after_a_stay_is_spotted_early_and_held() -> None:
    """20 km out, 2 h there, 13 m/s back: about 25 minutes of driving."""
    segments = [
        Segment((20_000.0, 0.0), 13.0, dwell_s=7_200.0),
        Segment((0.0, 0.0), 13.0),
    ]
    for seed in SEEDS:
        minutes = replay((300.0, 0.0), segments, seed=seed)
        trip = [m for m in minutes if m.remaining_s <= 25 * 60]
        assert false_starts(trip) == 0, seed
        # 20 % of 20 km is 4 km, about five minutes of driving.
        assert warning_s(minutes) >= 15 * 60, seed
        assert minutes[-1].heading, seed


def test_zones_around_home_spot_the_trip_home_sooner() -> None:
    """A quiet phone on the motorway home: only the zone crossings speak.

    60 km at 25 m/s (40 minutes), the phone reporting by background fetch
    every half hour or so, like a phone sitting in a bag.
    """
    segments = [
        Segment((60_000.0, 0.0), 25.0, dwell_s=7_200.0),
        Segment((0.0, 0.0), 25.0),
    ]
    quiet = FixPolicy(significant_m=None, interval_s=None, background_mean_s=1_800.0)
    crossings = 0
    for seed in SEEDS:
        without = replay((300.0, 0.0), segments, policy=quiet, seed=seed)
        with_rings = replay(
            (300.0, 0.0), segments, policy=quiet, seed=seed, rings=RINGS
        )
        assert warning_s(with_rings) >= warning_s(without), seed
        # The 32 km zone is crossed 20 minutes before arrival.
        assert warning_s(with_rings) >= 19 * 60, seed
        assert false_starts([m for m in with_rings if m.remaining_s < 40 * 60]) == 0
        crossings += any(m.reason is HomewardReason.ZONE_CROSSING for m in with_rings)
    assert crossings >= len(SEEDS) // 2


def test_walk_home_from_a_friends_house() -> None:
    """1.6 km away for 4 h, then a walk home at 1.35 m/s (20 minutes)."""
    segments = [
        Segment((1_600.0, 0.0), 1.35, dwell_s=4 * 3_600.0),
        Segment((0.0, 0.0), 1.35),
    ]
    for seed in SEEDS:
        minutes = replay((300.0, 0.0), segments, seed=seed)
        at_friends = [m for m in minutes if m.remaining_s > 20 * 60 + 60]
        # On the way there it is never a trip home; staying there ends nothing.
        assert not any(m.heading for m in at_friends), seed
        assert minutes[-1].heading, seed
        assert warning_s(minutes) >= 5 * 60, seed


def test_six_hours_at_a_cafe_close_to_home_is_never_a_trip_home() -> None:
    segments = [
        Segment((400.0, 0.0), 1.35, dwell_s=6 * 3_600.0),
        Segment((0.0, 0.0), 1.35),
    ]
    for seed in SEEDS:
        minutes = replay((300.0, 0.0), segments, seed=seed)
        assert false_starts(minutes) == 0, seed
        assert not any(m.heading for m in minutes if m.remaining_s > 10 * 60), seed


def test_driving_round_to_park_far_from_home_is_not_a_trip_home() -> None:
    """23 Oct 2026: 2.5 km of driving round central London at a steady 35 km."""
    radius = 35_000.0
    points = [
        (radius * math.cos(a), radius * math.sin(a))
        for a in (i * 0.02 for i in range(1, 12))
    ]
    segments = [Segment((radius, 0.0), 25.0, dwell_s=600.0)]
    segments += [Segment(point, 4.0) for point in points]
    segments += [
        Segment(points[-1], 4.0, dwell_s=3 * 3_600.0),
        Segment((0.0, 0.0), 20.0),
    ]
    for seed in SEEDS:
        minutes = replay((300.0, 0.0), segments, seed=seed, rings=RINGS)
        before_trip_home = [m for m in minutes if m.remaining_s > 35_000 / 20.0 + 60]
        assert not any(m.heading for m in before_trip_home), seed
        assert minutes[-1].heading, seed


def test_out_and_back_and_out_again() -> None:
    """Out to 10 km, back to 4 km for an hour, out to 12 km, then home."""
    segments = [
        Segment((10_000.0, 0.0), 13.0, dwell_s=3_600.0),
        Segment((4_000.0, 0.0), 13.0, dwell_s=3_600.0),
        Segment((12_000.0, 0.0), 13.0, dwell_s=7_200.0),
        Segment((0.0, 0.0), 13.0),
    ]
    for seed in SEEDS:
        minutes = replay((300.0, 0.0), segments, seed=seed, rings=RINGS)
        # Heading home on the way back to 4 km is fair. It must end during
        # the stay there, and not come back on the way out to 12 km.
        home_leg = 12_000 / 13.0
        outbound = [
            m
            for m in minutes
            if home_leg + 7_200 + 60 < m.remaining_s < home_leg + 7_200 + 6_500 / 13.0
        ]
        assert not any(m.heading for m in outbound), seed
        stay_end = home_leg + 7_200 + 8_000 / 13.0
        # With the quietest phone (one fix there, then nothing for the hour)
        # it ends 15 minutes after the expected arrival: within 40 minutes.
        stay = [m for m in minutes if stay_end < m.remaining_s < stay_end + 1_200]
        assert not any(m.heading for m in stay), seed
        assert minutes[-1].heading, seed


def test_expected_arrival_on_a_drive_home() -> None:
    """25 km by road, the last 3 km through town: the estimate stays close.

    Within half of the time left (more than five minutes out). Deliberately
    not tighter: the measured speed is blended with the typical door-to-door
    speed for the distance, which allows for town streets and parking.
    """
    segments = [
        Segment((25_000.0, 0.0), 13.0, dwell_s=3_600.0),
        Segment((3_000.0, 0.0), 13.0),
        Segment((0.0, 0.0), 7.0),
    ]
    errors: list[float] = []
    for seed in SEEDS:
        minutes = replay((300.0, 0.0), segments, seed=seed, rings=RINGS)
        for m in minutes:
            if m.heading and m.arrival is not None and m.remaining_s > 5 * 60:
                predicted = m.arrival - m.t
                assert predicted == pytest.approx(m.remaining_s, rel=0.5), (seed, m)
                errors.append(predicted / m.remaining_s - 1.0)
    errors.sort()
    assert abs(errors[len(errors) // 2]) < 0.2  # median


def test_a_glitch_into_a_zone_is_undone() -> None:
    """A fix that jumps into a zone around home and straight back starts nothing."""
    trk = PersonTracker(TrackerConfig(EstimatorParams()), TARGET, at_target=True)
    trk.set_rings(RINGS)
    t = T0

    def report(east_m: float, accuracy: float = 20.0) -> None:
        lat, lon = to_latlon(east_m, 0.0)
        trk.update(PersonReport(t, False, (), lat, lon, accuracy))

    report(TARGET.radius_m + 50.0)
    for east in (5_000.0, 5_010.0, 4_990.0):
        t += 600.0
        report(east)
    t += 60.0
    report(3_600.0)  # into the 4 km zone
    homeward = trk.snapshot(t).homeward
    assert homeward is not None
    assert homeward.heading
    t += 60.0
    report(5_005.0)  # back: the jump was a glitch
    homeward = trk.snapshot(t).homeward
    assert homeward is not None
    assert not homeward.heading
