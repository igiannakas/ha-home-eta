"""The expected arrival on a drive home in stop-start traffic (27 October 2026).

Two phones in one car, 35 km out: 90 minutes of queues from 31 to 20 km, then
the last 20 km in 36 minutes. Each report is replayed at its recorded time and
distance from home (positions were not kept: here every report lies due east
of home), through the person tracker and its minute tick, with the zones
around home the household uses. Before 2.4.0 one estimate in a queue often
jumped and the next report put it back: on the day, +36 then -38 minutes on
one phone, -18 then +27 on the other, which started and stopped a heating
plan within half a minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import pytest

from custom_components.home_eta.core.kinematic import Target
from custom_components.home_eta.core.params import EstimatorParams
from custom_components.home_eta.core.tracker import (
    PersonReport,
    PersonTracker,
    TrackerConfig,
)
from tests.sim import HOME_LAT, HOME_LON, T0, to_latlon

TICK_S = 60.0
MIN = 60.0
HOME = Target(HOME_LAT, HOME_LON, 309.0)
RINGS = tuple(
    Target(HOME_LAT, HOME_LON, km * 1000.0) for km in (1, 2, 4, 8, 16, 24, 32)
)

# (seconds, metres from home, reported accuracy in metres[, home])
IOANNIS = [
    (916, 35180, 17), (1818, 34910, 9), (2042, 34680, 10), (2343, 33410, 10),
    (2644, 31830, 10), (2757, 31420, 10), (2944, 30980, 10), (3244, 30080, 10),
    (3415, 29530, 10), (3545, 29440, 10), (3845, 28730, 10), (4146, 28180, 10),
    (4322, 28100, 10), (4446, 27720, 10), (4747, 26970, 10), (5201, 26480, 10),
    (5232, 26430, 10), (5502, 25740, 10), (6152, 25470, 10), (6679, 25250, 10),
    (6980, 24060, 10), (7070, 23640, 10), (7096, 23450, 10), (7280, 22620, 10),
    (7580, 21790, 10), (7880, 21000, 10), (8048, 20840, 10), (8180, 17770, 10),
    (8257, 15460, 10), (8481, 11650, 10), (8781, 8210, 10), (8864, 7460, 10),
    (8950, 6980, 10), (9082, 6430, 10), (9252, 3970, 10), (9383, 3400, 10),
    (9683, 2010, 10), (9692, 1980, 10), (9876, 1060, 10), (9923, 990, 10),
    (9984, 850, 10), (10226, 300, 10, True),
]  # fmt: skip
NANCY = [
    (2098, 34600, 90), (2398, 33320, 40), (2709, 31680, 11), (2756, 31420, 40),
    (3012, 30940, 14), (3332, 29650, 40), (3638, 29260, 73), (3945, 28730, 37),
    (4267, 28130, 14), (4590, 27100, 23), (5184, 26590, 40), (5484, 25840, 22),
    (6640, 25350, 30), (6957, 24220, 40), (7098, 23410, 37), (7278, 22930, 26),
    (7724, 21530, 40), (8042, 20800, 15), (8558, 10080, 65), (8566, 9990, 4),
    (8874, 7500, 59), (8895, 7330, 8), (9165, 5160, 5), (9253, 3950, 45),
    (9482, 2800, 38), (9769, 1430, 40), (10328, 70, 21, True),
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Estimate:
    """The expected arrival published at a report."""

    t: float
    distance_m: float
    arrival: float


@dataclass(frozen=True, slots=True)
class Drive:
    """What was published on the way home."""

    arrived_at: float
    estimates: list[Estimate]
    heading_since: float | None
    soon_since: float | None


def drive(reports: list[tuple[int, ...]]) -> Drive:
    """Replay one phone's reports, ticking every minute between them."""
    tracker = PersonTracker(TrackerConfig(EstimatorParams()), HOME, at_target=True)
    tracker.set_rings(RINGS)
    # Left home an hour before the first report.
    tracker.update(PersonReport(T0 - 3_600, False, (), *to_latlon(500.0, 0.0), 10.0))
    estimates: list[Estimate] = []
    heading_since = soon_since = None
    tick: float | None = None

    def follow(now: float) -> None:
        nonlocal heading_since, soon_since
        snap = tracker.snapshot(now)
        if snap.heading_home and heading_since is None:
            heading_since = now
        if not snap.heading_home:
            heading_since = None
        if snap.arriving_soon and soon_since is None:
            soon_since = now

    for t, distance, accuracy, *home in reports:
        now = T0 + t
        while tick is not None and tick < now:
            tracker.tick(tick)
            follow(tick)
            tick += TICK_S
        tick = now + TICK_S
        if home:
            return Drive(now, estimates, heading_since, soon_since)
        latitude, longitude = to_latlon(float(distance), 0.0)
        tracker.update(
            PersonReport(now, False, (), latitude, longitude, float(accuracy))
        )
        follow(now)
        expected = tracker.snapshot(now).expected_home
        if expected is not None:
            estimates.append(Estimate(now, float(distance), expected))
    raise AssertionError("the replay never got home")


PHONES = {"ioannis": IOANNIS, "nancy": NANCY}


@pytest.mark.parametrize("phone", PHONES)
def test_no_estimate_jumps_and_comes_straight_back(phone: str) -> None:
    """A jump of over ten minutes that the next report undoes is a lone estimate."""
    estimates = drive(PHONES[phone]).estimates
    for a, b, c in zip(estimates, estimates[1:], estimates[2:], strict=False):
        first, second = b.arrival - a.arrival, c.arrival - b.arrival
        assert not (
            abs(first) > 10 * MIN and abs(second) > 10 * MIN and first * second < 0
        ), (phone, b.t - T0)


@pytest.mark.parametrize("phone", PHONES)
def test_the_trip_home_and_arriving_soon_come_on_as_before(phone: str) -> None:
    """Heading home from the 32 km zone on; arriving soon 15-30 minutes out."""
    result = drive(PHONES[phone])
    assert result.heading_since is not None
    assert result.arrived_at - result.heading_since > 120 * MIN
    assert result.soon_since is not None
    assert 15 * MIN <= result.arrived_at - result.soon_since <= 30 * MIN


@pytest.mark.parametrize("phone", PHONES)
def test_the_zone_crossing_after_the_queues_is_believed_at_once(phone: str) -> None:
    """Into the 16 km zone, 9 km covered in minutes: the estimate catches up."""
    result = drive(PHONES[phone])
    crossing = next(e for e in result.estimates if e.distance_m < 16_000)
    before = result.estimates[result.estimates.index(crossing) - 1]
    assert before.arrival - result.arrived_at > 15 * MIN  # still the queues
    assert abs(crossing.arrival - result.arrived_at) < 11 * MIN


def test_no_change_between_reports_is_over_half_an_hour() -> None:
    """On the phone reporting every few minutes (before 2.4.0: +51, then -62)."""
    estimates = drive(IOANNIS).estimates
    for a, b in pairwise(estimates):
        if abs(b.arrival - a.arrival) > 30 * MIN:
            pytest.fail(f"{(b.arrival - a.arrival) / MIN:+.0f} min at {b.t - T0:.0f} s")
