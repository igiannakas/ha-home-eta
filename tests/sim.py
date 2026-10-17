"""Synthetic journeys for testing the estimators.

A journey is a list of straight segments in a local east/north frame with the
target at the origin. The phone reports the way iOS does: on significant
location change, on a fixed interval when the app is open, and on random
background fetches, with Gaussian position noise.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random

from custom_components.home_eta.core.geo import offset_m
from custom_components.home_eta.core.kinematic import Fix, Target

HOME_LAT = 51.4779
HOME_LON = -0.0015
HOME_RADIUS_M = 100.0
TARGET = Target(HOME_LAT, HOME_LON, HOME_RADIUS_M)
T0 = 1_760_000_000.0


@dataclass(frozen=True, slots=True)
class Segment:
    """Travel in a straight line to ``to`` (east, north metres), then wait."""

    to: tuple[float, float]
    speed_mps: float
    dwell_s: float = 0.0


@dataclass(frozen=True, slots=True)
class FixPolicy:
    """When the phone reports."""

    significant_m: float | None = 500.0
    """Report after moving this far since the last report (iOS significant change)."""
    interval_s: float | None = None
    """Report at this fixed interval (app in the foreground)."""
    background_mean_s: float | None = 900.0
    """Mean of random background-fetch reports."""


IOS = FixPolicy()
DENSE = FixPolicy(significant_m=None, interval_s=60.0, background_mean_s=None)


@dataclass(frozen=True, slots=True)
class SimFix:
    """A reported fix and the ground truth at that moment."""

    fix: Fix
    true_distance_m: float
    true_remaining_s: float
    leg: int
    """Index of the segment being travelled (or dwelt at)."""


def to_latlon(east_m: float, north_m: float) -> tuple[float, float]:
    """Convert local metres to latitude/longitude."""
    return offset_m(HOME_LAT, HOME_LON, north_m, east_m)


def simulate(
    start: tuple[float, float],
    segments: list[Segment],
    *,
    policy: FixPolicy = IOS,
    noise_m: float = 10.0,
    accuracy_m: float = 15.0,
    seed: int = 0,
    step_s: float = 1.0,
    t0: float = T0,
) -> list[SimFix]:
    """Run a journey and return the fixes the phone would have sent.

    Stops when the true position enters the target radius or the segments
    run out. ``true_remaining_s`` is measured to that first entry.
    """
    rng = random.Random(seed)
    timeline = _timeline(start, segments, step_s)
    arrival = next(
        (t for t, x, y, _ in timeline if math.hypot(x, y) <= HOME_RADIUS_M),
        timeline[-1][0],
    )

    def background_gap() -> float:
        assert policy.background_mean_s is not None
        return rng.expovariate(1.0 / policy.background_mean_s)

    fixes: list[SimFix] = []
    last_reported: tuple[float, float] | None = None
    next_interval = policy.interval_s
    next_background = (
        background_gap() if policy.background_mean_s is not None else math.inf
    )
    for t, x, y, leg in timeline:
        if t > arrival:
            break
        report = last_reported is None
        if (
            not report
            and policy.significant_m is not None
            and last_reported is not None
            and math.hypot(x - last_reported[0], y - last_reported[1])
            >= policy.significant_m
        ):
            report = True
        if next_interval is not None and t >= next_interval:
            report = True
            next_interval = t + (policy.interval_s or 0.0)
        if t >= next_background:
            report = True
            next_background = t + background_gap()
        if not report:
            continue
        last_reported = (x, y)
        lat, lon = to_latlon(x + rng.gauss(0, noise_m), y + rng.gauss(0, noise_m))
        fixes.append(
            SimFix(
                fix=Fix(lat, lon, accuracy_m, t0 + t),
                true_distance_m=math.hypot(x, y),
                true_remaining_s=arrival - t,
                leg=leg,
            )
        )
    return fixes


def _timeline(
    start: tuple[float, float], segments: list[Segment], step_s: float
) -> list[tuple[float, float, float, int]]:
    """Return (t, east, north, segment index) samples along the journey."""
    samples: list[tuple[float, float, float, int]] = []
    t = 0.0
    x, y = start
    samples.append((t, x, y, 0))
    for index, segment in enumerate(segments):
        tx, ty = segment.to
        length = math.hypot(tx - x, ty - y)
        duration = length / segment.speed_mps
        steps = max(1, math.ceil(duration / step_s))
        sx, sy = x, y
        for i in range(1, steps + 1):
            fraction = i / steps
            samples.append(
                (
                    t + duration * fraction,
                    sx + (tx - sx) * fraction,
                    sy + (ty - sy) * fraction,
                    index,
                )
            )
        t += duration
        x, y = tx, ty
        dwell_steps = math.ceil(segment.dwell_s / step_s)
        samples.extend((t + i * step_s, x, y, index) for i in range(1, dwell_steps + 1))
        t += dwell_steps * step_s
    return samples
