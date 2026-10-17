"""Great-circle geometry."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8
"""Mean Earth radius (IUGG)."""


def is_valid_coordinate(latitude: float, longitude: float) -> bool:
    """Return True for a finite latitude/longitude pair inside its range."""
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def offset_m(
    latitude: float, longitude: float, north_m: float, east_m: float
) -> tuple[float, float]:
    """Return the point ``north_m``/``east_m`` metres from a point.

    Uses a local flat-earth approximation, accurate to well under a metre for
    offsets of a few tens of kilometres. Used by tests and the backtest tooling.
    """
    d_lat = math.degrees(north_m / EARTH_RADIUS_M)
    d_lon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(latitude))))
    return latitude + d_lat, longitude + d_lon
