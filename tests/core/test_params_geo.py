"""Tests for parameter validation and geometry."""

from __future__ import annotations

import math

import pytest

from custom_components.home_eta.core.geo import (
    haversine_m,
    is_valid_coordinate,
    offset_m,
)
from custom_components.home_eta.core.params import EstimatorParams


def test_defaults_are_valid() -> None:
    EstimatorParams()


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_gps_accuracy_m": 0},
        {"speed_filter_tau_s": -1},
        {"min_closing_speed_mps": math.nan},
        {"stale_after_s": math.inf},
        {"min_sample_weight": 1.5},
        {"mode_change_confirm_n": 0},
        {"place_still_s": 1_000.0},
        {"place_confirm_s": 1_300.0},
        {"homeward_min_progress_ratio": 1.0},
        {"homeward_cancel_ratio": 1.5},
        {"on_foot_min_speed_mps": 3.0},
        {"glitch_jitter_margin": 0.9},
        {"max_jitter_factor": 0.5},
        {"stale_after_s": 300.0},
        {"stopped_speed_mps": 0.6},
        {"min_closing_speed_mps": 0.2},
    ],
)
def test_invalid_values_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must"):
        EstimatorParams(**overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["1", None, True])
def test_non_numbers_rejected(value: object) -> None:
    with pytest.raises(TypeError):
        EstimatorParams(max_gps_accuracy_m=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode_change_confirm_n": 2.5},
        {"seed_settle_n": 2.0},
        {"max_seed_replacements": True},
    ],
)
def test_integer_fields_need_integers(overrides: dict[str, object]) -> None:
    """Regression: 4.5 passed validation and then broke a count."""
    with pytest.raises(TypeError, match="integer"):
        EstimatorParams(**overrides)  # type: ignore[arg-type]


def test_float_fields_accept_integers() -> None:
    assert EstimatorParams(max_gps_accuracy_m=100).max_gps_accuracy_m == 100


def test_haversine_known_distance() -> None:
    # One degree of latitude is ~111.2 km.
    assert haversine_m(51.0, 0.0, 52.0, 0.0) == pytest.approx(111_195, rel=1e-4)
    assert haversine_m(51.0, 0.0, 51.0, 0.0) == 0.0


def test_offset_round_trip() -> None:
    lat, lon = offset_m(51.4779, -0.0015, 3_000.0, 4_000.0)
    assert haversine_m(51.4779, -0.0015, lat, lon) == pytest.approx(5_000.0, rel=1e-3)


@pytest.mark.parametrize(
    ("lat", "lon", "valid"),
    [
        (51.5, -0.1, True),
        (90.0, 180.0, True),
        (90.1, 0.0, False),
        (0.0, -180.1, False),
        (math.nan, 0.0, False),
    ],
)
def test_coordinate_validation(lat: float, lon: float, valid: bool) -> None:
    assert is_valid_coordinate(lat, lon) is valid
