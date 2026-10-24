"""Heading home and its restore across restarts, through Home Assistant."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from tests.helpers import ALEX, person_entry, set_person, step
from tests.sim import to_latlon

STATUS = "sensor.alex_status"
EXPECTED = "sensor.alex_expected_home"
NEAR = "sensor.alex_near_home"

pytestmark = pytest.mark.usefixtures("zones")


ON_THE_WAY = ("heading_home", "arriving_soon")


def heading(hass: HomeAssistant) -> bool:
    """Return True while the status says heading home (arriving soon included)."""
    return hass.states.get(STATUS).state in ON_THE_WAY


def soon(hass: HomeAssistant) -> bool:
    """Return True while the status says arriving soon."""
    return hass.states.get(STATUS).state == "arriving_soon"


async def setup_entries(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    """Add the entries; setting up the first loads the domain and all of them."""
    for entry in entries:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    for entry in entries:
        assert entry.state is ConfigEntryState.LOADED


def add_ring(hass: HomeAssistant, radius_m: float) -> str:
    """Add a passive zone centred on home."""
    home = hass.states.get("zone.home")
    entity_id = f"zone.ring_{radius_m / 1000:g}_km"
    hass.states.async_set(
        entity_id,
        "0",
        {
            "latitude": home.attributes["latitude"],
            "longitude": home.attributes["longitude"],
            "radius": radius_m,
            "passive": True,
            "friendly_name": f"Ring {radius_m / 1000:g} km",
        },
    )
    return entity_id


def restore(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Have Home Assistant hand the status sensor ``data`` on setup."""
    mock_restore_cache_with_extra_data(hass, [(State(STATUS, "away"), data)])


def status_sensor(hass: HomeAssistant) -> Any:
    """Return Alex's status entity."""
    return next(
        e
        for e in hass.data["sensor"].entities
        if e.entity_id == STATUS  # type: ignore[attr-defined]
    )


async def test_heading_home_follows_the_trip(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_entries(hass, person_entry(ALEX))
    state = hass.states.get(STATUS)
    assert state.state not in ON_THE_WAY
    assert state.attributes["reason"] == "initial"
    assert state.attributes["farthest_m"] is None

    # Out to 10 km at 13 m/s: not heading home.
    for east in [780.0 * i for i in range(1, 14)]:
        await step(hass, freezer, ALEX, east)
    state = hass.states.get(STATUS)
    assert state.state not in ON_THE_WAY
    assert state.attributes["farthest_m"] == pytest.approx(10_140, abs=50)

    # Back 15 %: still not; 21 %: heading home.
    for east in (9_400.0, 8_620.0):
        await step(hass, freezer, ALEX, east)
    assert not heading(hass)
    await step(hass, freezer, ALEX, 7_840.0)
    state = hass.states.get(STATUS)
    assert state.state in ON_THE_WAY
    assert state.attributes["reason"] == "progress"
    assert state.attributes["speed_source"] == "measured"
    assert 5 <= state.attributes["speed_mps"] <= 13
    assert dt_util.parse_datetime(state.attributes["since"]) == dt_util.utcnow()

    await step(hass, freezer, ALEX, None, zones=("zone.home",))
    state = hass.states.get(STATUS)
    assert state.state not in ON_THE_WAY
    assert state.attributes["reason"] == "arrived"
    assert state.attributes["speed_mps"] is None


async def test_entering_a_zone_around_home_starts_the_trip_home(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    ring = add_ring(hass, 4_000)
    set_person(hass, ALEX, 6_000)
    await setup_entries(hass, person_entry(ALEX))
    for east in (5_500.0, 6_000.0):
        await step(hass, freezer, ALEX, east)
    assert not heading(hass)
    await step(hass, freezer, ALEX, 3_900.0, zones=(ring,))
    state = hass.states.get(STATUS)
    assert state.state in ON_THE_WAY
    assert state.attributes["reason"] == "zone_crossing"


@pytest.mark.parametrize("startup", [True, False], ids=["restart", "reload"])
async def test_heading_home_restored_after_a_restart(
    hass: HomeAssistant, startup: bool
) -> None:
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 120,
            "trip": {"since": now - 600, "farthest_m": 30_000, "closest_m": 20_000},
            "stay": None,
        },
    )
    if startup:
        # The person's state is the one saved at shutdown, not a fix.
        hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    state = hass.states.get(STATUS)
    assert state.state in ON_THE_WAY
    assert state.attributes["reason"] == "restored"
    assert dt_util.parse_datetime(state.attributes["since"]) == (
        dt_util.utc_from_timestamp(now - 600)
    )
    # After a restart nothing was measured yet; after a reload the person's
    # state is a genuine fix, but one fix gives no speed either.
    assert state.attributes["speed_source"] == "typical"
    assert state.attributes["farthest_m"] == 30_000
    assert dt_util.parse_datetime(hass.states.get(EXPECTED).state) is not None
    assert not soon(hass)


async def test_arriving_soon_restored_with_the_trip(hass: HomeAssistant) -> None:
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 60,
            "trip": {
                "since": now - 900,
                "farthest_m": 3_000,
                "closest_m": 1_500,
                "arriving_soon": True,
            },
        },
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 1_500)
    await setup_entries(hass, person_entry(ALEX))
    assert heading(hass)
    assert soon(hass)


@pytest.mark.parametrize(
    ("saved_soon", "status"), [(True, "arriving_soon"), (False, "heading_home")]
)
async def test_arriving_soon_restored_with_a_personal_threshold(
    hass: HomeAssistant, saved_soon: bool, status: str
) -> None:
    """Regression: a saved threshold other than 15 minutes turned it off.

    About 14 minutes out: on by the default threshold, off by the saved 10.
    """
    now = dt_util.utcnow().timestamp()
    trip = {
        "since": now - 900,
        "farthest_m": 30_000,
        "closest_m": 5_000,
        "arriving_soon": saved_soon,
    }
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State(STATUS, "away"), {"last_heard": now - 60, "trip": trip}),
            (
                State("number.alex_arriving_soon_threshold", "10.0"),
                {
                    "native_max_value": 120,
                    "native_min_value": 1,
                    "native_step": 1,
                    "native_unit_of_measurement": "min",
                    "native_value": 10.0,
                },
            ),
        ],
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 5_000)
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get("number.alex_arriving_soon_threshold").state == "10.0"
    assert hass.states.get(STATUS).state == status


@pytest.mark.parametrize(
    "saved",
    [
        {},
        {
            "last_heard": "yesterday",
            "trip": {"since": 1.0, "farthest_m": 3, "closest_m": 2},
        },
        {"last_heard": 1.0, "trip": {"since": 1.0, "farthest_m": -5, "closest_m": 2}},
        {"last_heard": 1.0, "trip": {"since": 1.0, "farthest_m": 30_000}},
    ],
)
async def test_bad_saved_trip_home_is_ignored(
    hass: HomeAssistant, saved: dict[str, Any]
) -> None:
    restore(hass, saved)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert not heading(hass)


async def test_saved_in_the_future_is_ignored(hass: HomeAssistant) -> None:
    """The clock was wrong when it was saved, or is now."""
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now + 3_600,
            "trip": {"since": now, "farthest_m": 30_000, "closest_m": 20_000},
        },
    )
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert not heading(hass)


async def test_old_trip_home_is_not_restored(hass: HomeAssistant) -> None:
    """Home Assistant was down longer than any trip home could take."""
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 25_000,
            "trip": {"since": now - 26_000, "farthest_m": 30_000, "closest_m": 20_000},
        },
    )
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert not heading(hass)


async def test_overdue_restored_trip_ends_at_once(hass: HomeAssistant) -> None:
    """Down for two hours: a trip home from 21 km should be over by now."""
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 7_200,
            "trip": {"since": now - 7_500, "farthest_m": 30_000, "closest_m": 20_000},
        },
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    state = hass.states.get(STATUS)
    assert state.state not in ON_THE_WAY
    assert state.attributes["reason"] == "overdue"


async def test_trip_home_saved_for_a_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 10_000)
    await setup_entries(hass, person_entry(ALEX))
    assert status_sensor(hass).extra_restore_state_data is None
    for east in (9_200.0, 8_400.0, 7_600.0):
        await step(hass, freezer, ALEX, east)
    assert heading(hass)
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved["last_heard"] == dt_util.utcnow().timestamp()
    assert saved["trip"]["farthest_m"] == pytest.approx(10_000, abs=50)
    assert saved["trip"]["closest_m"] == pytest.approx(7_600, abs=50)
    assert saved["trip"]["arriving_soon"] is True
    assert saved["stay"] is None


async def test_a_stay_survives_a_restart(hass: HomeAssistant) -> None:
    """Three hours at a friend's 500 m away: near home stays faded."""
    now = dt_util.utcnow().timestamp()
    lat, lon = to_latlon(500, 0)
    stay = {
        "since": now - 3 * 3_600,
        "latitude": lat,
        "longitude": lon,
        "distance_m": 500.0,
    }
    restore(hass, {"last_heard": now - 600, "trip": None, "stay": stay})
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 500)
    await setup_entries(hass, person_entry(ALEX))
    near = hass.states.get(NEAR)
    assert near.state == "0"
    assert dt_util.parse_datetime(near.attributes["staying_since"]) == (
        dt_util.utc_from_timestamp(now - 3 * 3_600)
    )
    # Saved again for the next restart, as it was.
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved == {"last_heard": now - 600, "trip": None, "stay": stay}


async def test_a_restored_trip_survives_a_second_restart(hass: HomeAssistant) -> None:
    """Restored, then restarted again before any fix: the trip is kept."""
    now = dt_util.utcnow().timestamp()
    trip = {
        "since": now - 600,
        "farthest_m": 30_000.0,
        "closest_m": 20_000.0,
        "arriving_soon": False,
    }
    restore(hass, {"last_heard": now - 120, "trip": trip, "stay": None})
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(STATUS).attributes["reason"] == "restored"
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved["last_heard"] == now - 120
    assert saved["trip"] == trip


async def test_a_saved_stay_with_bad_coordinates_is_ignored(
    hass: HomeAssistant,
) -> None:
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 600,
            "stay": {
                "since": now - 3 * 3_600,
                "latitude": 123.0,
                "longitude": 0.0,
                "distance_m": 500.0,
            },
        },
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 500)
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(NEAR).state == "100"
    assert hass.states.get(NEAR).attributes["staying_since"] is None


async def test_unknown_person_still_restores_nothing(hass: HomeAssistant) -> None:
    """With no whereabouts, a saved trip has no distance to resume from."""
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 120,
            "trip": {"since": now - 600, "farthest_m": 30_000, "closest_m": 20_000},
        },
    )
    hass.states.async_set(ALEX, STATE_UNKNOWN, {})
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(STATUS).state == STATE_UNKNOWN
