"""Tunable parameters for the Home ETA estimators.

Every default lives here as a module constant so the integration's options
flow, the tests and the backtest script all share one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import math

# --- Fix acceptance ---------------------------------------------------------
MAX_GPS_ACCURACY_M = 150.0
"""Fixes reporting a worse accuracy than this are discarded."""

ACCURACY_MOVE_FACTOR = 3.0
"""Movement counts when displacement exceeds this many combined position sigmas.

Two fixes of one spot differ by more than 3 combined sigmas about 1 % of the
time (Rayleigh tail), so a still phone is rarely mistaken for a moving one.
"""

ACCURACY_TO_SIGMA = 1.0 / 1.51
"""Phones report horizontal accuracy as a ~68 % radius, which is 1.51 sigma."""

MIN_FIX_INTERVAL_S = 20.0
"""Fixes closer in time than this, at the same place, are merged."""

MAX_PLAUSIBLE_SPEED_MPS = 90.0
"""Ground speed (beyond accuracy) above this is treated as a GPS jump."""

# --- Closing-speed filter ---------------------------------------------------
SPEED_FILTER_TAU_S = 300.0
"""Time constant of the exponential filter on closing speed."""

SPEED_NOISE_REF_MPS = 0.3
"""Expected sample noise at which a speed sample gets full weight."""

MIN_SAMPLE_WEIGHT = 0.25
"""Floor on the weight of a noisy speed sample."""

SPEED_WINDOW_N = 6
"""Number of recent speed samples used for spread and confidence."""

MODE_CHANGE_RATIO = 0.5
"""A sample differing from the filtered speed by more than this fraction..."""

MODE_CHANGE_MIN_DELTA_MPS = 0.5
"""...and by at least this much..."""

MODE_CHANGE_NOISE_SIGMAS = 3.0
"""...and by this many times its own expected noise is divergent."""

MODE_CHANGE_CONFIRM_N = 2
"""Consecutive divergent samples (same direction) that re-seed the filter."""

SEED_SETTLE_N = 2
"""While a seed has fewer samples than this, one clearly divergent sample may
replace it (once): the seed may straddle a change of mode."""

MAX_SEED_REPLACEMENTS = 2
"""How many times in a row a lone seed can be replaced before disagreement
must be confirmed like any other."""

GLITCH_JITTER_MARGIN = 1.25
"""After a fix jumps away and the next returns, movement must exceed the jump
by this factor before it is believed (the phone's accuracy was optimistic)."""

MAX_JITTER_FACTOR = 2.0
"""The learned jitter never raises the movement threshold beyond this many
noise radii, so a single wild jump cannot blind the estimator."""

STOP_EVIDENCE_FACTOR = 3.0
"""A still fix counts as slowing down only when, at the filtered speed, the
person should have moved this many noise radii since the anchor."""

MIN_CLOSING_SPEED_MPS = 0.5
"""Below this closing speed the ETA is undefined."""

STOPPED_SPEED_MPS = 0.2
"""Below this speed (either direction) the filter treats the person as stopped."""

CLOCK_STEP_BACK_S = 60.0
"""A fix this much older than the previous one means the clock stepped back."""

MAX_ETA_S = 21_600.0
"""ETAs are clamped to this many seconds."""

STALE_AFTER_S = 900.0
"""With no fix for this long the estimate is stale."""

# --- Confidence -------------------------------------------------------------
CONFIDENCE_HIGH_MIN_SAMPLES = 4
CONFIDENCE_HIGH_MAX_SPREAD = 0.3
"""High confidence needs speed spread / speed below this."""

CONFIDENCE_MEDIUM_MIN_SAMPLES = 2

# --- Approach state machine -------------------------------------------------
APPROACH_CONFIRM_N = 2
"""Consecutive accepted fixes needed to enter or leave approaching."""

ARRIVING_SOON_DISTANCE_M = 800.0
"""Within this distance of the zone centre, approaching becomes arriving soon."""

DEFAULT_ARRIVING_SOON_MIN = 15
MIN_ARRIVING_SOON_MIN = 1
MAX_ARRIVING_SOON_MIN = 120

STATIONARY_AFTER_S = 600.0
"""No movement for this long marks the person stationary."""

# --- Heading home (core/homeward.py) ----------------------------------------
PLACE_RADIUS_M = 150.0
"""A fix this close to where the person was is the same place..."""

PLACE_ACCURACY_FACTOR = 2.5
"""...or within this many times its reported accuracy, whichever is larger."""

PLACE_CONFIRM_S = 900.0
"""A fix at the same place this long after getting there confirms a stay."""

PLACE_DWELL_S = 1_200.0
"""This long at a confirmed place ends the trip home (and prevents one)."""

PLACE_STILL_S = 60.0
"""A fix still at the same place this long after getting there, then silence
until ``place_dwell_s``, also confirms a stay (a still phone reports rarely)."""

HOMEWARD_MIN_PROGRESS_M = 500.0
"""Heading home needs at least this much closing from the farthest point..."""

HOMEWARD_MIN_PROGRESS_RATIO = 0.2
"""...and at least this fraction of that farthest distance (scale-invariant)."""

HOMEWARD_MIN_SPEED_MPS = 0.3
"""...while still closing at least this fast."""

HOMEWARD_CANCEL_M = 500.0
"""Moving back out this far from the closest point ends the trip home..."""

HOMEWARD_CANCEL_RATIO = 0.15
"""...or this fraction of the current distance, whichever is larger."""

HOMEWARD_OVERDUE_S = 900.0
"""With no news, the trip home ends this long after the expected arrival...

or ``homeward_overdue_ratio`` of the trip time expected at the last fix,
whichever is longer. Fresh fixes keep moving the expected arrival, so this
only ends trips that went silent (a stay with a quiet phone).
"""

HOMEWARD_OVERDUE_RATIO = 0.5

RING_SPEED_WINDOW_S = 5_400.0
"""Two inward zone crossings this recent give a ring-to-ring speed."""

ON_FOOT_MAX_SPEED_MPS = 2.5
"""Closing slower than this within ``on_foot_max_distance_m`` is on foot..."""

ON_FOOT_MAX_DISTANCE_M = 3_000.0
"""...and the measured speed is used as it is (at least ``on_foot_min_speed_mps``)."""

ON_FOOT_MIN_SPEED_MPS = 1.1
"""Slowest walking speed assumed for someone heading home on foot."""

# --- Historical estimator ---------------------------------------------------
MIN_TRIP_DURATION_S = 300.0
MIN_TRIP_FIXES = 4
HIST_MAX_TRIPS = 50
"""Learned trips kept; the oldest is forgotten first."""
HIST_MAX_TRIP_POINTS = 100
"""Points kept per learned trip (thinned evenly beyond this)."""
MIN_HIST_TRIPS = 5
"""Past trips that must cover a distance before a historical ETA is given."""
HIST_LOW_QUANTILE = 0.2
HIST_HIGH_QUANTILE = 0.8
HIST_RESET_MOVE_M = 200.0
"""Moving the target zone's centre further than this forgets learned trips."""


@dataclass(frozen=True, slots=True)
class EstimatorParams:
    """Parameters for one person's estimators.

    Validated on construction so a bad options entry fails loudly at setup
    instead of producing nonsense ETAs.
    """

    max_gps_accuracy_m: float = MAX_GPS_ACCURACY_M
    accuracy_move_factor: float = ACCURACY_MOVE_FACTOR
    min_fix_interval_s: float = MIN_FIX_INTERVAL_S
    max_plausible_speed_mps: float = MAX_PLAUSIBLE_SPEED_MPS
    speed_filter_tau_s: float = SPEED_FILTER_TAU_S
    speed_noise_ref_mps: float = SPEED_NOISE_REF_MPS
    min_sample_weight: float = MIN_SAMPLE_WEIGHT
    speed_window_n: int = SPEED_WINDOW_N
    mode_change_ratio: float = MODE_CHANGE_RATIO
    mode_change_min_delta_mps: float = MODE_CHANGE_MIN_DELTA_MPS
    mode_change_noise_sigmas: float = MODE_CHANGE_NOISE_SIGMAS
    mode_change_confirm_n: int = MODE_CHANGE_CONFIRM_N
    seed_settle_n: int = SEED_SETTLE_N
    max_seed_replacements: int = MAX_SEED_REPLACEMENTS
    stop_evidence_factor: float = STOP_EVIDENCE_FACTOR
    min_closing_speed_mps: float = MIN_CLOSING_SPEED_MPS
    stopped_speed_mps: float = STOPPED_SPEED_MPS
    clock_step_back_s: float = CLOCK_STEP_BACK_S
    max_eta_s: float = MAX_ETA_S
    stale_after_s: float = STALE_AFTER_S
    confidence_high_min_samples: int = CONFIDENCE_HIGH_MIN_SAMPLES
    confidence_high_max_spread: float = CONFIDENCE_HIGH_MAX_SPREAD
    confidence_medium_min_samples: int = CONFIDENCE_MEDIUM_MIN_SAMPLES
    approach_confirm_n: int = APPROACH_CONFIRM_N
    arriving_soon_distance_m: float = ARRIVING_SOON_DISTANCE_M
    stationary_after_s: float = STATIONARY_AFTER_S
    min_trip_duration_s: float = MIN_TRIP_DURATION_S
    min_trip_fixes: int = MIN_TRIP_FIXES
    hist_max_trips: int = HIST_MAX_TRIPS
    hist_max_trip_points: int = HIST_MAX_TRIP_POINTS
    min_hist_trips: int = MIN_HIST_TRIPS
    hist_low_quantile: float = HIST_LOW_QUANTILE
    hist_high_quantile: float = HIST_HIGH_QUANTILE
    hist_reset_move_m: float = HIST_RESET_MOVE_M
    glitch_jitter_margin: float = GLITCH_JITTER_MARGIN
    max_jitter_factor: float = MAX_JITTER_FACTOR
    place_radius_m: float = PLACE_RADIUS_M
    place_accuracy_factor: float = PLACE_ACCURACY_FACTOR
    place_confirm_s: float = PLACE_CONFIRM_S
    place_dwell_s: float = PLACE_DWELL_S
    place_still_s: float = PLACE_STILL_S
    homeward_min_progress_m: float = HOMEWARD_MIN_PROGRESS_M
    homeward_min_progress_ratio: float = HOMEWARD_MIN_PROGRESS_RATIO
    homeward_min_speed_mps: float = HOMEWARD_MIN_SPEED_MPS
    homeward_cancel_m: float = HOMEWARD_CANCEL_M
    homeward_cancel_ratio: float = HOMEWARD_CANCEL_RATIO
    homeward_overdue_s: float = HOMEWARD_OVERDUE_S
    homeward_overdue_ratio: float = HOMEWARD_OVERDUE_RATIO
    ring_speed_window_s: float = RING_SPEED_WINDOW_S
    on_foot_max_speed_mps: float = ON_FOOT_MAX_SPEED_MPS
    on_foot_max_distance_m: float = ON_FOOT_MAX_DISTANCE_M
    on_foot_min_speed_mps: float = ON_FOOT_MIN_SPEED_MPS

    def __post_init__(self) -> None:
        """Reject values that would break the maths."""
        for item in fields(self):
            value = getattr(self, item.name)
            if item.type in ("int", int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{item.name} must be an integer, got {value!r}")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{item.name} must be a number, got {value!r}")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{item.name} must be a positive number, got {value}")
        if not 0 < self.min_sample_weight <= 1:
            raise ValueError("min_sample_weight must be in (0, 1]")
        if not 0 < self.hist_low_quantile < self.hist_high_quantile < 1:
            raise ValueError("historical quantiles must satisfy 0 < low < high < 1")
        if self.stale_after_s < self.stationary_after_s:
            raise ValueError(
                "stale_after_s must not be shorter than stationary_after_s"
            )
        if self.stopped_speed_mps >= self.min_closing_speed_mps:
            raise ValueError("stopped_speed_mps must be below min_closing_speed_mps")
        if self.speed_window_n < 2:
            raise ValueError("speed_window_n must be at least 2")
        if self.hist_max_trip_points < 2:
            raise ValueError("hist_max_trip_points must be at least 2")
        if self.glitch_jitter_margin < 1 or self.max_jitter_factor < 1:
            raise ValueError("jitter margin and factor must be at least 1")
        if self.homeward_min_progress_ratio >= 1 or self.homeward_cancel_ratio >= 1:
            raise ValueError("heading-home ratios must be below 1")
        if not self.place_still_s <= self.place_confirm_s <= self.place_dwell_s:
            raise ValueError(
                "place times must satisfy place_still_s <= place_confirm_s"
                " <= place_dwell_s"
            )
        if self.on_foot_min_speed_mps >= self.on_foot_max_speed_mps:
            raise ValueError(
                "on_foot_min_speed_mps must be below on_foot_max_speed_mps"
            )
