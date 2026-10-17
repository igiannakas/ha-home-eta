"""Shared fixtures for the Home Assistant tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from tests.helpers import HOME_LAT, HOME_LON, HOME_RADIUS_M


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Let Home Assistant load the integration from custom_components."""
    return


@pytest.fixture
async def zones(hass: HomeAssistant) -> None:
    """Place home at the test location and add a station and a work zone."""
    hass.config.latitude = HOME_LAT
    hass.config.longitude = HOME_LON
    hass.config.radius = HOME_RADIUS_M
    config: dict[str, Any] = {
        "zone": [
            {
                "name": "Station",
                "latitude": HOME_LAT,
                "longitude": HOME_LON + 0.02,
                "radius": 150,
                "passive": True,
            },
            {
                "name": "Work",
                "latitude": HOME_LAT + 0.2,
                "longitude": HOME_LON,
                "radius": 200,
            },
        ]
    }
    assert await async_setup_component(hass, "zone", config)
    await hass.async_block_till_done()
