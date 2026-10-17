"""Tests for the approach state machine."""

from __future__ import annotations

import pytest

from custom_components.home_eta.core.approach import (
    ApproachMachine,
    ApproachState,
    Observation,
    Reason,
)
from custom_components.home_eta.core.params import EstimatorParams

PARAMS = EstimatorParams()
THRESHOLD_S = 900.0


def obs(
    now: float = 0.0,
    *,
    speed: float | None = 10.0,
    eta_s: float | None = 3_600.0,
    distance_m: float | None = 30_000.0,
    stopped: bool = False,
    since_fix_s: float | None = 0.0,
    in_other_zone: bool = False,
    threshold_s: float = THRESHOLD_S,
) -> Observation:
    """Return an observation with sensible defaults (far away, closing fast)."""
    return Observation(
        now=now,
        closing_speed_mps=speed,
        eta_s=eta_s,
        distance_m=distance_m,
        stopped=stopped,
        since_fix_s=since_fix_s,
        in_other_zone=in_other_zone,
        threshold_s=threshold_s,
    )


def approaching() -> ApproachMachine:
    """Return a machine that has just entered approaching."""
    machine = ApproachMachine(PARAMS)
    machine.on_fix(obs(1), moved=True)
    machine.on_fix(obs(2), moved=True)
    assert machine.state is ApproachState.APPROACHING
    return machine


def test_home_and_departure() -> None:
    machine = ApproachMachine(PARAMS, ApproachState.HOME)
    assert machine.on_fix(obs(), moved=True) is None
    assert machine.on_tick(obs(since_fix_s=10_000)) is None
    transition = machine.depart(5)
    assert transition is not None
    assert transition.current is ApproachState.AWAY
    assert transition.reason is Reason.LEFT
    assert machine.depart(6) is None


def test_arrive_from_any_state() -> None:
    machine = approaching()
    transition = machine.arrive(10)
    assert transition is not None
    assert (transition.previous, transition.current) == (
        ApproachState.APPROACHING,
        ApproachState.HOME,
    )
    assert machine.arrive(11) is None


def test_approaching_needs_two_closing_fixes() -> None:
    machine = ApproachMachine(PARAMS)
    assert machine.on_fix(obs(1), moved=True) is None
    transition = machine.on_fix(obs(2), moved=True)
    assert transition is not None
    assert transition.current is ApproachState.APPROACHING
    assert transition.reason is Reason.CLOSING


def test_interrupted_closing_streak_restarts() -> None:
    machine = ApproachMachine(PARAMS)
    machine.on_fix(obs(1), moved=True)
    machine.on_fix(obs(2, speed=0.4), moved=True)
    machine.on_fix(obs(3), moved=True)
    assert machine.state is ApproachState.AWAY
    machine.on_fix(obs(4), moved=True)
    assert machine.state is ApproachState.APPROACHING


def test_unknown_speed_breaks_streak() -> None:
    machine = ApproachMachine(PARAMS)
    machine.on_fix(obs(1), moved=True)
    machine.on_fix(obs(2, speed=None), moved=True)
    machine.on_fix(obs(3), moved=True)
    assert machine.state is ApproachState.AWAY


def test_hysteresis_between_half_and_full_minimum_speed() -> None:
    """Speeds between v_min/2 and v_min neither enter nor leave approaching."""
    machine = approaching()
    for i in range(10):
        machine.on_fix(obs(10 + i, speed=0.3 if i % 2 else 0.45), moved=True)
    assert machine.state is ApproachState.APPROACHING

    away = ApproachMachine(PARAMS)
    for i in range(10):
        away.on_fix(obs(10 + i, speed=0.3 if i % 2 else 0.45), moved=True)
    assert away.state is ApproachState.AWAY


def test_leaves_approaching_after_two_receding_fixes() -> None:
    machine = approaching()
    assert machine.on_fix(obs(5, speed=0.1), moved=True) is None
    transition = machine.on_fix(obs(6, speed=-3.0), moved=True)
    assert transition is not None
    assert transition.current is ApproachState.AWAY
    assert transition.reason is Reason.NOT_CLOSING


def test_arriving_soon_by_eta_or_distance() -> None:
    machine = approaching()
    transition = machine.on_fix(obs(5, eta_s=THRESHOLD_S), moved=True)
    assert transition is not None
    assert transition.current is ApproachState.ARRIVING_SOON
    assert transition.reason is Reason.ETA_WITHIN_THRESHOLD

    machine = approaching()
    transition = machine.on_fix(obs(5, eta_s=None, distance_m=800.0), moved=True)
    assert transition is not None
    assert transition.reason is Reason.CLOSE_BY


def test_arriving_soon_on_tick_as_eta_counts_down() -> None:
    machine = approaching()
    assert machine.on_tick(obs(5, eta_s=THRESHOLD_S + 60)) is None
    transition = machine.on_tick(obs(65, eta_s=THRESHOLD_S))
    assert transition is not None
    assert transition.current is ApproachState.ARRIVING_SOON


def test_threshold_is_per_call() -> None:
    machine = approaching()
    machine.on_tick(obs(5, eta_s=1_200, threshold_s=600))
    assert machine.state is ApproachState.APPROACHING
    machine.on_tick(obs(6, eta_s=1_200, threshold_s=1_800))
    assert machine.state is ApproachState.ARRIVING_SOON


def test_other_zone_holds_off_arriving_soon_until_left() -> None:
    machine = approaching()
    machine.on_fix(obs(5, eta_s=60, in_other_zone=True), moved=True)
    assert machine.state is ApproachState.APPROACHING
    machine.on_tick(obs(6, eta_s=60, in_other_zone=True))
    assert machine.state is ApproachState.APPROACHING
    transition = machine.on_tick(obs(7, eta_s=60, in_other_zone=False))
    assert transition is not None
    assert transition.current is ApproachState.ARRIVING_SOON


def test_entering_other_zone_steps_back_to_approaching() -> None:
    machine = approaching()
    machine.on_fix(obs(5, eta_s=60), moved=True)
    assert machine.state is ApproachState.ARRIVING_SOON
    transition = machine.on_fix(obs(6, eta_s=60, in_other_zone=True), moved=True)
    assert transition is not None
    assert transition.current is ApproachState.APPROACHING
    assert transition.reason is Reason.IN_OTHER_ZONE


def test_arriving_soon_keeps_state_when_eta_rises() -> None:
    machine = approaching()
    machine.on_fix(obs(5, eta_s=60), moved=True)
    machine.on_fix(obs(6, eta_s=5_000, distance_m=5_000), moved=True)
    assert machine.state is ApproachState.ARRIVING_SOON


@pytest.mark.parametrize(
    "state",
    [ApproachState.AWAY, ApproachState.APPROACHING, ApproachState.ARRIVING_SOON],
)
def test_stationary_on_proven_stop(state: ApproachState) -> None:
    machine = approaching()
    if state is ApproachState.ARRIVING_SOON:
        machine.on_fix(obs(5, eta_s=60), moved=True)
    elif state is ApproachState.AWAY:
        machine = ApproachMachine(PARAMS)
    assert machine.state is state
    transition = machine.on_fix(obs(11, stopped=True), moved=False)
    assert transition is not None
    assert transition.current is ApproachState.STATIONARY
    assert transition.reason is Reason.NO_MOVEMENT


def test_stationary_after_silence() -> None:
    machine = approaching()
    assert machine.on_tick(obs(10, since_fix_s=PARAMS.stale_after_s - 1)) is None
    transition = machine.on_tick(obs(11, since_fix_s=PARAMS.stale_after_s))
    assert transition is not None
    assert transition.current is ApproachState.STATIONARY
    assert transition.reason is Reason.NO_REPORTS


def test_long_gap_between_moving_fixes_is_not_a_stop() -> None:
    """iPhones report every ~500 m; a slow walker can go 10+ minutes between."""
    machine = approaching()
    assert machine.on_tick(obs(10, since_fix_s=PARAMS.stationary_after_s + 60)) is None
    assert machine.state is ApproachState.APPROACHING


def test_stationary_ignores_still_fixes_then_resumes() -> None:
    machine = approaching()
    machine.on_tick(obs(10, since_fix_s=PARAMS.stale_after_s))
    for t in (20, 30, 40):
        assert machine.on_fix(obs(t, stopped=True), moved=False) is None
    transition = machine.on_fix(obs(50), moved=True)
    assert transition is not None
    assert transition.current is ApproachState.AWAY
    assert transition.reason is Reason.MOVING_AGAIN
    # Evidence from the stop did not carry over: one more closing fix needed.
    transition = machine.on_fix(obs(60), moved=True)
    assert transition is not None
    assert transition.current is ApproachState.APPROACHING


def test_unknown_report_time_never_stationary() -> None:
    machine = approaching()
    assert machine.on_tick(obs(10, since_fix_s=None)) is None


def test_restore_rules() -> None:
    machine = ApproachMachine(PARAMS)
    assert machine.restore(ApproachState.APPROACHING, 5)
    assert machine.state is ApproachState.APPROACHING
    assert machine.reason is Reason.RESTORED
    assert machine.since == 5

    for state in (ApproachState.HOME, ApproachState.AWAY):
        assert not ApproachMachine(PARAMS).restore(state, 5)

    moved_on = ApproachMachine(PARAMS)
    moved_on.on_fix(obs(1), moved=True)
    moved_on.on_fix(obs(2), moved=True)
    moved_on.on_fix(obs(3, speed=-1), moved=True)
    moved_on.on_fix(obs(4, speed=-1), moved=True)
    assert moved_on.state is ApproachState.AWAY
    assert not moved_on.restore(ApproachState.ARRIVING_SOON, 5)

    at_home = ApproachMachine(PARAMS, ApproachState.HOME)
    assert not at_home.restore(ApproachState.APPROACHING, 5)


def test_diagnostics() -> None:
    machine = approaching()
    data = machine.diagnostics()
    assert data["state"] == "approaching"
    assert data["closing_streak"] == 2


def test_fix_without_a_new_sample_does_not_confirm() -> None:
    """Regression: a still fix repeated the last sample as a second confirmation."""
    machine = ApproachMachine(PARAMS)
    machine.on_fix(obs(1), moved=True, new_sample=True)
    machine.on_fix(obs(2), moved=False, new_sample=False)
    assert machine.state is ApproachState.AWAY
    machine.on_fix(obs(3), moved=True, new_sample=True)
    assert machine.state is ApproachState.APPROACHING


def test_fix_without_a_new_sample_does_not_break_a_streak() -> None:
    machine = approaching()
    machine.on_fix(obs(5, speed=-2.0), moved=True, new_sample=True)
    machine.on_fix(obs(6, speed=-2.0), moved=False, new_sample=False)
    assert machine.state is ApproachState.APPROACHING
    machine.on_fix(obs(7, speed=-2.0), moved=True, new_sample=True)
    assert machine.state is ApproachState.AWAY
