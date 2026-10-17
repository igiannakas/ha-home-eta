"""Per-person approach state machine.

States change only on consecutive evidence so someone wandering around a town
centre does not flip between states:

    home ── leaves zone ──▶ away ── closing on 2 fixes ──▶ approaching
                             ▲                                 │
                             └──── not closing on 2 fixes ─────┤
                                                               ▼
                    ETA ≤ threshold or close by, not in another zone
                                                               │
                                                               ▼
                                                        arriving_soon

Any non-home state becomes ``stationary`` on evidence of a stop: a still fix
long after the last movement, or no report at all for the stale period. Slow
walkers whose phone reports only every 500 m are not mistaken for stopped.
``stationary`` returns to ``away`` on the next movement. ``home`` is entered
from any state the moment the person is inside the target zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .params import EstimatorParams


class ApproachState(StrEnum):
    """Where a person is relative to the target zone."""

    HOME = "home"
    AWAY = "away"
    APPROACHING = "approaching"
    ARRIVING_SOON = "arriving_soon"
    STATIONARY = "stationary"


ON_THE_WAY = frozenset({ApproachState.APPROACHING, ApproachState.ARRIVING_SOON})


class Reason(StrEnum):
    """Why the last transition happened."""

    INITIAL = "initial"
    RESTORED = "restored"
    ARRIVED = "arrived"
    LEFT = "left"
    CLOSING = "closing"
    NOT_CLOSING = "not_closing"
    ETA_WITHIN_THRESHOLD = "eta_within_threshold"
    CLOSE_BY = "close_by"
    IN_OTHER_ZONE = "in_other_zone"
    NO_MOVEMENT = "no_movement"
    NO_REPORTS = "no_reports"
    MOVING_AGAIN = "moving_again"


@dataclass(frozen=True, slots=True)
class Observation:
    """What the machine needs to know at an instant."""

    now: float
    closing_speed_mps: float | None
    eta_s: float | None
    """Seconds until the kinematic arrival estimate, None when undefined or stale."""
    distance_m: float | None
    """Distance to the target centre."""
    stopped: bool
    """A still fix proved a stop (no movement for ``stationary_after_s``)."""
    since_fix_s: float | None
    """Seconds since the last fix (or since tracking restarted), None if unknown."""
    in_other_zone: bool
    """Inside any zone other than the target."""
    threshold_s: float
    """Arriving-soon threshold for this person."""


@dataclass(frozen=True, slots=True)
class MachineState:
    """Everything the machine remembers, to undo a fix."""

    state: ApproachState
    reason: Reason
    since: float | None
    closing_streak: int
    receding_streak: int
    changed: bool


@dataclass(frozen=True, slots=True)
class Transition:
    """A state change, from the state before the call to the state after it."""

    previous: ApproachState
    current: ApproachState
    reason: Reason
    at: float


class ApproachMachine:
    """Track one person's approach state."""

    def __init__(
        self, params: EstimatorParams, initial: ApproachState = ApproachState.AWAY
    ) -> None:
        """Initialise in ``initial``."""
        self._p = params
        self._state = initial
        self._reason = Reason.INITIAL
        self._since: float | None = None
        self._closing_streak = 0
        self._receding_streak = 0
        self._changed = False

    @property
    def state(self) -> ApproachState:
        """Return the current state."""
        return self._state

    @property
    def reason(self) -> Reason:
        """Return why the current state was entered."""
        return self._reason

    @property
    def since(self) -> float | None:
        """Return when the current state was entered, if known."""
        return self._since

    def diagnostics(self) -> dict[str, object]:
        """Return internal state for diagnostics."""
        return {
            "state": self._state.value,
            "reason": self._reason.value,
            "since": self._since,
            "closing_streak": self._closing_streak,
            "receding_streak": self._receding_streak,
        }

    def save(self) -> MachineState:
        """Return the machine's state, to put back with ``load``."""
        return MachineState(
            self._state,
            self._reason,
            self._since,
            self._closing_streak,
            self._receding_streak,
            self._changed,
        )

    def load(self, saved: MachineState) -> None:
        """Put back a state returned by ``save`` (a fix turned out a glitch)."""
        self._state = saved.state
        self._reason = saved.reason
        self._since = saved.since
        self._closing_streak = saved.closing_streak
        self._receding_streak = saved.receding_streak
        self._changed = saved.changed

    def restore(self, state: ApproachState, at: float) -> bool:
        """Restore a state saved before a restart.

        Only applies before anything has happened in this run and never to
        ``home``, which is always derived from the person's zone.
        """
        if self._changed or self._state is not ApproachState.AWAY:
            return False
        if state is ApproachState.HOME or state is ApproachState.AWAY:
            return False
        self._state = state
        self._reason = Reason.RESTORED
        self._since = at
        return True

    def arrive(self, now: float) -> Transition | None:
        """Person is inside the target zone."""
        if self._state is ApproachState.HOME:
            return None
        return self._set(ApproachState.HOME, Reason.ARRIVED, now)

    def depart(self, now: float) -> Transition | None:
        """Person has left the target zone."""
        if self._state is not ApproachState.HOME:
            return None
        return self._set(ApproachState.AWAY, Reason.LEFT, now)

    def on_fix(
        self, obs: Observation, *, moved: bool, new_sample: bool = True
    ) -> Transition | None:
        """Process an accepted fix.

        ``moved`` is True when movement was seen. ``new_sample`` is True when
        the fix gave the speed filter a new sample: only those count towards
        the consecutive fixes needed to enter or leave approaching, so one
        sample is never counted twice.
        """
        if self._state is ApproachState.HOME:
            return None
        start = self._state
        p = self._p
        if self._state is ApproachState.STATIONARY:
            if not moved:
                # Evidence gathered while standing still says nothing about
                # the direction of the next leg.
                self._closing_streak = self._receding_streak = 0
                return None
            self._set(ApproachState.AWAY, Reason.MOVING_AGAIN, obs.now)

        speed = obs.closing_speed_mps
        if speed is None:
            self._closing_streak = self._receding_streak = 0
        elif new_sample:
            closing = speed >= p.min_closing_speed_mps
            receding = speed < p.min_closing_speed_mps / 2
            self._closing_streak = self._closing_streak + 1 if closing else 0
            self._receding_streak = self._receding_streak + 1 if receding else 0

        if (
            self._state is ApproachState.AWAY
            and self._closing_streak >= p.approach_confirm_n
        ):
            self._set(ApproachState.APPROACHING, Reason.CLOSING, obs.now, reset=False)
        elif (
            self._state in ON_THE_WAY and self._receding_streak >= p.approach_confirm_n
        ):
            self._set(ApproachState.AWAY, Reason.NOT_CLOSING, obs.now)
        self._evaluate(obs)
        return self._transition_from(start, obs.now)

    def on_tick(self, obs: Observation) -> Transition | None:
        """Re-evaluate time-driven rules without a new fix."""
        if self._state is ApproachState.HOME:
            return None
        start = self._state
        self._evaluate(obs)
        return self._transition_from(start, obs.now)

    def _evaluate(self, obs: Observation) -> None:
        p = self._p
        silent = obs.since_fix_s is not None and obs.since_fix_s >= p.stale_after_s
        if self._state is not ApproachState.STATIONARY and (obs.stopped or silent):
            reason = Reason.NO_MOVEMENT if obs.stopped else Reason.NO_REPORTS
            self._set(ApproachState.STATIONARY, reason, obs.now)
            return
        if self._state is ApproachState.APPROACHING and not obs.in_other_zone:
            if obs.eta_s is not None and obs.eta_s <= obs.threshold_s:
                self._set(
                    ApproachState.ARRIVING_SOON,
                    Reason.ETA_WITHIN_THRESHOLD,
                    obs.now,
                    reset=False,
                )
            elif (
                obs.distance_m is not None
                and obs.distance_m <= p.arriving_soon_distance_m
            ):
                self._set(
                    ApproachState.ARRIVING_SOON, Reason.CLOSE_BY, obs.now, reset=False
                )
        elif self._state is ApproachState.ARRIVING_SOON and obs.in_other_zone:
            self._set(
                ApproachState.APPROACHING, Reason.IN_OTHER_ZONE, obs.now, reset=False
            )

    def _set(
        self, state: ApproachState, reason: Reason, now: float, *, reset: bool = True
    ) -> Transition:
        previous = self._state
        self._state = state
        self._reason = reason
        self._since = now
        self._changed = True
        if reset:
            self._closing_streak = self._receding_streak = 0
        return Transition(previous, state, reason, now)

    def _transition_from(self, start: ApproachState, now: float) -> Transition | None:
        if self._state is start:
            return None
        return Transition(start, self._state, self._reason, now)
