"""Kinematic ETA: remaining distance over filtered radial closing speed.

The estimator only sees sparse, irregular position fixes (iOS reports on
significant location change, zone events and background fetch). It measures
the radial closing speed, the rate at which the straight-line distance to the
target falls, between an *anchor* fix and each new fix, and filters it.
Using radial distance for both the remaining distance and the speed makes a
route's detour factor largely cancel on a steady leg, so no map is needed.

Noise model: phones report horizontal accuracy as a ~68 % radius (1.51 sigma).
Movement is only believed when two fixes differ by more than 3 combined sigmas,
and every speed sample carries its expected noise (position sigma over its
baseline), which sets its weight in the filter. A fix that jumps away and is
followed by one back at the earlier spot is undone as a position glitch, and
the size of the jump raises the movement threshold until real movement is
confirmed (indoor Wi-Fi positions often bounce further than their accuracy).

Filter: an exponential moving average over time (``speed_filter_tau_s``),
warmed up as a noise-weighted running mean after each seed. Three things
re-seed it rather than letting it drift slowly: setting off after a stop
(measured from the last fix at the stop), a clear disagreement with a seed
that has no other sample yet (at most twice in a row), and two consecutive
samples that diverge the same way beyond their noise (a change of transport
mode). A lone divergent sample is held back and dropped if not confirmed.

The anchor stays where movement was last seen, so steps each below the noise
add up. A stop is proven by a still fix ten minutes after the last movement
when, at the believed speed, the movement would have shown (or that speed is
below the closing speed), and by no movement at all for the stale period.
While stopped there is no ETA.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import math
from statistics import stdev

from .geo import haversine_m, is_valid_coordinate
from .params import ACCURACY_TO_SIGMA, EstimatorParams


@dataclass(frozen=True, slots=True)
class Fix:
    """One position report."""

    latitude: float
    longitude: float
    accuracy_m: float
    timestamp: float
    """UTC epoch seconds."""


@dataclass(frozen=True, slots=True)
class Target:
    """The zone being approached."""

    latitude: float
    longitude: float
    radius_m: float


class FixVerdict(StrEnum):
    """What the estimator did with a fix."""

    FIRST = "first"
    """First fix: it becomes the anchor, no speed yet."""
    REINITIALISED = "reinitialised"
    """Previous data was stale; the estimator restarted from this fix."""
    MOVED = "moved"
    """Moved beyond the accuracy since the anchor; it becomes the new anchor."""
    STATIONARY = "stationary"
    """Within the accuracy of the anchor; anchor kept."""
    MERGED = "merged"
    """Same place as the previous fix moments ago; the better of the two kept."""
    REJECTED_INVALID = "rejected_invalid"
    REJECTED_ACCURACY = "rejected_accuracy"
    REJECTED_OUT_OF_ORDER = "rejected_out_of_order"
    REJECTED_JUMP = "rejected_jump"


ACCEPTED_VERDICTS = frozenset(
    {
        FixVerdict.FIRST,
        FixVerdict.REINITIALISED,
        FixVerdict.MOVED,
        FixVerdict.STATIONARY,
    }
)


class Confidence(StrEnum):
    """How much to trust an ETA."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """Result of offering a fix to the estimator."""

    verdict: FixVerdict
    distance_m: float | None
    """Distance from the fix to the target centre, when computable."""
    speed_sample_mps: float | None
    """Radial closing speed measured from the anchor, when one was taken."""
    speed_updated: bool = False
    """The filtered speed took a new sample from this fix."""
    rolled_back: bool = False
    """The previous fix was undone as a position glitch before this one."""

    @property
    def accepted(self) -> bool:
        """Return True when the fix updated the estimator."""
        return self.verdict in ACCEPTED_VERDICTS


@dataclass(frozen=True, slots=True)
class KinematicEstimate:
    """An arrival estimate. Times are UTC epoch seconds."""

    arrival: float
    earliest: float
    latest: float
    remaining_m: float
    closing_speed_mps: float
    speed_spread_mps: float | None
    samples: int
    confidence: Confidence
    fix_time: float


@dataclass(slots=True)
class _Point:
    latitude: float
    longitude: float
    accuracy_m: float
    timestamp: float
    distance_m: float


@dataclass(frozen=True, slots=True)
class _Saved:
    """Everything a moving fix can change, to undo it as a glitch."""

    anchor: _Point | None
    last: _Point | None
    speed: float | None
    last_feed: float | None
    window: tuple[float, ...]
    last_movement: float | None
    stopped: bool
    divergent: tuple[float, ...]
    since_seed: int
    weight_sum: float
    replacements: int
    mode_changes: int


class KinematicEstimator:
    """Estimate arrival time from position fixes for one person."""

    def __init__(self, params: EstimatorParams, target: Target) -> None:
        """Initialise with parameters and the target zone."""
        self._p = params
        self._target = target
        self._anchor: _Point | None = None
        self._last: _Point | None = None
        self._speed: float | None = None
        self._last_feed: float | None = None
        self._window: deque[float] = deque(maxlen=params.speed_window_n)
        self._last_movement: float | None = None
        self._stopped = False
        self._divergent: list[float] = []
        self._mode_changes = 0
        self._since_seed = 0
        self._weight_sum = 0.0
        self._replacements = 0
        self._pending: tuple[_Saved, _Point] | None = None
        """State before the latest moving fix, and that fix, until confirmed."""
        self._jitter_m = 0.0
        self._glitches = 0

    # -- state -----------------------------------------------------------

    @property
    def target(self) -> Target:
        """Return the target zone."""
        return self._target

    @property
    def closing_speed_mps(self) -> float | None:
        """Return the filtered closing speed, positive towards the target."""
        return self._speed

    @property
    def distance_m(self) -> float | None:
        """Return the distance to the target centre at the latest fix."""
        return None if self._last is None else self._last.distance_m

    @property
    def last_fix_time(self) -> float | None:
        """Return the time of the latest accepted fix."""
        return None if self._last is None else self._last.timestamp

    @property
    def last_movement_time(self) -> float | None:
        """Return when movement was last seen (or tracking started)."""
        return self._last_movement

    @property
    def samples(self) -> int:
        """Return how many speed samples are in the spread window."""
        return len(self._window)

    @property
    def stopped(self) -> bool:
        """Return True after a still fix proved a stop of ``stationary_after_s``."""
        return self._stopped

    def reset(self) -> None:
        """Forget everything, e.g. on arrival or when the target moves."""
        self._anchor = None
        self._last = None
        self._speed = None
        self._last_feed = None
        self._window.clear()
        self._last_movement = None
        self._stopped = False
        self._divergent.clear()
        self._since_seed = 0
        self._weight_sum = 0.0
        self._replacements = 0
        self._pending = None
        self._jitter_m = 0.0

    def set_target(self, target: Target) -> None:
        """Change the target. Distances change, so the filter restarts."""
        if target != self._target:
            self._target = target
            self.reset()

    def is_stale(self, now: float) -> bool:
        """Return True when there is no fix within the stale window.

        A last fix in the future (the clock stepped back) also counts as stale.
        """
        if self._last is None:
            return True
        age = now - self._last.timestamp
        return age > self._p.stale_after_s or age < -self._p.clock_step_back_s

    def diagnostics(self) -> dict[str, object]:
        """Return internal state for diagnostics (coordinates excluded)."""
        return {
            "closing_speed_mps": self._speed,
            "speed_window_mps": list(self._window),
            "anchor_time": None if self._anchor is None else self._anchor.timestamp,
            "anchor_distance_m": None
            if self._anchor is None
            else self._anchor.distance_m,
            "last_fix_time": self.last_fix_time,
            "last_distance_m": self.distance_m,
            "last_movement_time": self._last_movement,
            "stopped": self._stopped,
            "mode_changes": self._mode_changes,
            "glitches": self._glitches,
            "jitter_m": self._jitter_m,
        }

    # -- input -------------------------------------------------------------

    def add_fix(self, fix: Fix) -> FixOutcome:
        """Offer a fix and return what was done with it.

        ``accuracy_m`` must be positive; callers map an unknown accuracy to a
        conservative value.
        """
        p = self._p
        if (
            not is_valid_coordinate(fix.latitude, fix.longitude)
            or not math.isfinite(fix.accuracy_m)
            or fix.accuracy_m <= 0
            or not math.isfinite(fix.timestamp)
        ):
            return FixOutcome(FixVerdict.REJECTED_INVALID, None, None)

        point = _Point(
            fix.latitude,
            fix.longitude,
            fix.accuracy_m,
            fix.timestamp,
            haversine_m(
                fix.latitude,
                fix.longitude,
                self._target.latitude,
                self._target.longitude,
            ),
        )
        if fix.accuracy_m > p.max_gps_accuracy_m:
            return FixOutcome(FixVerdict.REJECTED_ACCURACY, point.distance_m, None)

        last = self._last
        if last is None:
            self._start(point)
            return FixOutcome(FixVerdict.FIRST, point.distance_m, None)

        dt = point.timestamp - last.timestamp
        if dt < -p.clock_step_back_s:
            # The clock stepped back: everything stored is now "in the future"
            # and would block every fix until the clock caught up.
            self._start(point)
            return FixOutcome(FixVerdict.REINITIALISED, point.distance_m, None)
        if dt <= 0:
            return FixOutcome(FixVerdict.REJECTED_OUT_OF_ORDER, point.distance_m, None)
        if dt > p.stale_after_s:
            self._start(point)
            return FixOutcome(FixVerdict.REINITIALISED, point.distance_m, None)

        rolled_back = self._undo_glitch(point)
        last = self._last
        assert last is not None
        dt = point.timestamp - last.timestamp

        step = _ground_m(last, point)
        if dt < p.min_fix_interval_s and step <= self._noise_radius(last, point):
            self._merge(point)
            return FixOutcome(
                FixVerdict.MERGED, point.distance_m, None, rolled_back=rolled_back
            )

        if (step - self._noise_radius(last, point)) / dt > p.max_plausible_speed_mps:
            return FixOutcome(
                FixVerdict.REJECTED_JUMP,
                point.distance_m,
                None,
                rolled_back=rolled_back,
            )

        if self._pending is not None:
            # The last move was followed by a fix that did not return: it was
            # real, so the phone's accuracy can be trusted again.
            self._pending = None
            self._jitter_m = 0.0

        anchor = self._anchor
        assert anchor is not None  # set together with _last
        baseline = point.timestamp - anchor.timestamp
        noise = _combined_sigma(anchor.accuracy_m, point.accuracy_m) / baseline
        movement_radius = self._movement_radius(anchor, point)
        moved = _ground_m(anchor, point) > movement_radius
        sample = (anchor.distance_m - point.distance_m) / baseline
        saved = self._save()
        self._last = point

        if moved:
            self._pending = (saved, point)
            self._anchor = point
            self._last_movement = point.timestamp
            stopped = self._stopped
            self._stopped = False
            if stopped or (
                self._speed is not None and abs(self._speed) < p.stopped_speed_mps
            ):
                # Setting off: the speed before the stop (often another mode of
                # transport) says nothing about this leg. Start fresh, measured
                # from the latest fix, still at the stop, so as little waiting
                # as possible counts as travel; a seed that still includes some
                # is replaced by the next clear sample.
                span = point.timestamp - last.timestamp
                sample = (last.distance_m - point.distance_m) / span
                noise = _combined_sigma(last.accuracy_m, point.accuracy_m) / span
                self._seed(sample, point.timestamp, noise)
                updated = True
            else:
                updated = self._feed(sample, point.timestamp, noise)
            return FixOutcome(
                FixVerdict.MOVED,
                point.distance_m,
                sample,
                speed_updated=updated,
                rolled_back=rolled_back,
            )

        # No detectable movement. It says something only when movement would
        # have been detectable: the person is already believed stopped, or at
        # the believed speed they should clearly have moved by now. A still fix
        # soon after a moving one just means the step was below the noise; the
        # next moving fix, measured from the same anchor, carries that interval.
        speed = self._speed
        evidence = (
            speed is None
            or abs(speed) < p.stopped_speed_mps
            or abs(speed) * baseline >= p.stop_evidence_factor * movement_radius
        )
        updated = False
        if speed is not None and evidence:
            updated = self._feed(sample, point.timestamp, noise)
        # A stop is proven by a long time without movement, unless the person
        # is believed to be travelling (at least the closing speed) too slowly
        # for that to have shown at this accuracy. Nothing at all for the stale
        # period is a stop whatever the speed, as is silence for that long.
        still_for = (
            None
            if self._last_movement is None
            else point.timestamp - self._last_movement
        )
        if still_for is not None and (
            still_for >= p.stale_after_s
            or (
                still_for >= p.stationary_after_s
                and (evidence or abs(self._speed or 0.0) < p.min_closing_speed_mps)
            )
        ):
            # The next movement starts a new leg.
            self._stopped = True
        # The anchor stays where movement was last seen, stopped or not: steps
        # each below the noise still add up to movement over the baseline, and
        # a wrong speed shows once it should have moved the person by now.
        return FixOutcome(
            FixVerdict.STATIONARY,
            point.distance_m,
            sample,
            speed_updated=updated,
            rolled_back=rolled_back,
        )

    def _undo_glitch(self, point: _Point) -> bool:
        """Undo the last moving fix if ``point`` is back where it came from.

        A position that jumps away and comes straight back is far more likely
        a bad fix than a trip there and back between two reports.
        """
        pending = self._pending
        if pending is None:
            return False
        saved, glitch = pending
        if glitch is not self._last or saved.anchor is None:
            self._pending = None
            return False
        before = saved.anchor
        returned = _ground_m(before, point) <= self._movement_radius(before, point)
        left_glitch = _ground_m(glitch, point) > self._movement_radius(glitch, point)
        if not (returned and left_glitch):
            return False
        jump = _ground_m(before, glitch)
        self._restore(saved)
        self._pending = None
        self._jitter_m = max(self._jitter_m, jump * self._p.glitch_jitter_margin)
        self._glitches += 1
        return True

    def _save(self) -> _Saved:
        return _Saved(
            anchor=self._anchor,
            last=self._last,
            speed=self._speed,
            last_feed=self._last_feed,
            window=tuple(self._window),
            last_movement=self._last_movement,
            stopped=self._stopped,
            divergent=tuple(self._divergent),
            since_seed=self._since_seed,
            weight_sum=self._weight_sum,
            replacements=self._replacements,
            mode_changes=self._mode_changes,
        )

    def _restore(self, saved: _Saved) -> None:
        self._anchor = saved.anchor
        self._last = saved.last
        self._speed = saved.speed
        self._last_feed = saved.last_feed
        self._window.clear()
        self._window.extend(saved.window)
        self._last_movement = saved.last_movement
        self._stopped = saved.stopped
        self._divergent = list(saved.divergent)
        self._since_seed = saved.since_seed
        self._weight_sum = saved.weight_sum
        self._replacements = saved.replacements
        self._mode_changes = saved.mode_changes

    def _start(self, point: _Point) -> None:
        self.reset()
        self._anchor = point
        self._last = point
        self._last_movement = point.timestamp

    def _merge(self, point: _Point) -> None:
        last = self._last
        assert last is not None
        if point.accuracy_m >= last.accuracy_m:
            return
        if self._anchor is last:
            self._anchor = point
        if self._pending is not None and self._pending[1] is last:
            self._pending = (self._pending[0], point)
        self._last = point

    def _noise_radius(self, a: _Point, b: _Point) -> float:
        """Return the displacement two fixes of one spot can plausibly show."""
        return self._p.accuracy_move_factor * _combined_sigma(
            a.accuracy_m, b.accuracy_m
        )

    def _movement_radius(self, a: _Point, b: _Point) -> float:
        """Return the displacement that counts as movement.

        The noise radius, raised by recently seen glitches but never beyond
        ``max_jitter_factor`` noise radii.
        """
        noise = self._noise_radius(a, b)
        return max(noise, min(self._jitter_m, self._p.max_jitter_factor * noise))

    def _weight(self, noise: float) -> float:
        """Return a sample's weight from its expected noise (m/s, 1 sigma)."""
        p = self._p
        weight = 1.0 if noise <= 0 else min(1.0, p.speed_noise_ref_mps / noise)
        return max(p.min_sample_weight, weight)

    def _feed(self, sample: float, timestamp: float, noise: float) -> bool:
        """Blend one speed sample with expected noise ``noise`` (m/s, 1 sigma).

        Returns True when the filtered speed took the sample.
        """
        p = self._p
        speed = self._speed
        last_feed = self._last_feed
        if speed is None or last_feed is None:
            self._seed(sample, timestamp, noise)
            return True

        delta = sample - speed
        # A sample far from the filtered speed, beyond its own noise, is held
        # back. If the next one(s) diverge the same way the transport mode has
        # changed (train to foot, motorway to jam), so the filter re-seeds
        # instead of taking several time constants to follow. Otherwise the
        # held sample was a glitch and is dropped.
        divergent = (
            abs(delta)
            > max(p.mode_change_ratio * abs(speed), p.mode_change_min_delta_mps)
            and abs(delta) > p.mode_change_noise_sigmas * noise
        )
        if divergent:
            if (
                self._since_seed < p.seed_settle_n
                and self._replacements < p.max_seed_replacements
            ):
                # The seed has no other sample yet and may straddle the change
                # (half train, half platform); a clear disagreement this early
                # means the newer sample is the better seed. A limited number
                # of times: a change can pass through a stop (train, platform,
                # walk), but endless replacement would just echo each sample.
                self._seed(sample, timestamp, noise, replacement=True)
                return True
            if self._divergent and (self._divergent[-1] - speed) * delta <= 0:
                self._divergent.clear()
            self._divergent.append(sample)
            if len(self._divergent) >= p.mode_change_confirm_n:
                self._mode_changes += 1
                self._seed(sample, timestamp, noise)
                return True
            return False
        self._divergent.clear()

        weight = self._weight(noise)
        self._weight_sum += weight
        # Right after seeding, average like a noise-weighted running mean so a
        # noisy seed does not linger; once settled the time constant governs.
        decay = 1.0 - math.exp(-(timestamp - last_feed) / p.speed_filter_tau_s)
        alpha = max(decay * weight, weight / self._weight_sum)
        self._since_seed += 1
        self._speed = speed + alpha * delta
        self._last_feed = timestamp
        self._window.append(sample)
        return True

    def _seed(
        self,
        sample: float,
        timestamp: float,
        noise: float,
        *,
        replacement: bool = False,
    ) -> None:
        """Restart the filter from one sample."""
        self._speed = sample
        self._since_seed = 1
        self._weight_sum = self._weight(noise)
        self._replacements = self._replacements + 1 if replacement else 0
        self._last_feed = timestamp
        self._window.clear()
        self._window.append(sample)
        self._divergent.clear()

    # -- output ------------------------------------------------------------

    def estimate(
        self, now: float, *, allow_stale: bool = False
    ) -> KinematicEstimate | None:
        """Return the arrival estimate, or None when it is undefined."""
        p = self._p
        last = self._last
        speed = self._speed
        if (
            last is None
            or speed is None
            or self._stopped
            or speed < p.min_closing_speed_mps
        ):
            return None
        if not allow_stale and self.is_stale(now):
            return None

        remaining = max(last.distance_m - self._target.radius_m, 0.0)
        spread = stdev(self._window) if len(self._window) >= 2 else None

        def arrive_at(speed_mps: float) -> float:
            return last.timestamp + min(remaining / speed_mps, p.max_eta_s)

        arrival = arrive_at(speed)
        if spread:
            earliest = arrive_at(speed + spread)
            latest = arrive_at(max(speed - spread, p.min_closing_speed_mps))
        else:
            earliest = latest = arrival
        # Between sparse fixes the projection can fall behind the clock; never
        # publish an arrival in the past, and say it is a guess.
        overdue = arrival < now
        arrival, earliest, latest = (max(t, now) for t in (arrival, earliest, latest))

        samples = len(self._window)
        if overdue:
            confidence = Confidence.LOW
        elif (
            samples >= p.confidence_high_min_samples
            and spread is not None
            and spread / speed < p.confidence_high_max_spread
        ):
            confidence = Confidence.HIGH
        elif samples >= p.confidence_medium_min_samples:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW

        return KinematicEstimate(
            arrival=arrival,
            earliest=earliest,
            latest=latest,
            remaining_m=remaining,
            closing_speed_mps=speed,
            speed_spread_mps=spread,
            samples=samples,
            confidence=confidence,
            fix_time=last.timestamp,
        )


def _combined_sigma(accuracy_a_m: float, accuracy_b_m: float) -> float:
    """Return the 1-sigma error of the difference between two fixes, per axis."""
    return ACCURACY_TO_SIGMA * math.hypot(accuracy_a_m, accuracy_b_m)


def _ground_m(a: _Point, b: _Point) -> float:
    return haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
