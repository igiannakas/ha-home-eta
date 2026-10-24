"""Heading home, expected home and near home, through Home Assistant."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.home_eta.const import (
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    CONF_NEAR_HOME_FADE_AFTER_MIN,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_ZONE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
)
from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person, step

HEADING = "binary_sensor.alex_heading_home"
APPROACH = "sensor.alex_approach"
EXPECTED = "sensor.home_eta_expected_home"
NEAR = "sensor.home_eta_near_home"

pytestmark = pytest.mark.usefixtures("zones")


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


async def drive(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    person: str,
    points: list[float],
    *,
    zones_for: dict[float, tuple[str, ...]] | None = None,
) -> None:
    """Report the person at each point east of home, a minute apart."""
    for east in points:
        await step(hass, freezer, person, east, zones=(zones_for or {}).get(east, ()))


async def test_heading_home_follows_the_trip(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(HEADING).state == STATE_OFF
    assert hass.states.get(HEADING).attributes == {"friendly_name": "Alex Heading home"}

    # Out to 10 km at 13 m/s: not heading home.
    await drive(hass, freezer, ALEX, [780.0 * i for i in range(1, 14)])
    state = hass.states.get(HEADING)
    assert state.state == STATE_OFF
    assert state.attributes["reason"] == "initial"
    assert state.attributes["farthest_m"] == pytest.approx(10_140, abs=50)

    # Back 15 %: still not; 21 %: heading home.
    await drive(hass, freezer, ALEX, [9_400.0, 8_620.0])
    assert hass.states.get(HEADING).state == STATE_OFF
    await drive(hass, freezer, ALEX, [7_840.0])
    state = hass.states.get(HEADING)
    assert state.state == STATE_ON
    assert state.attributes["reason"] == "progress"
    assert state.attributes["speed_source"] == "measured"
    expected = dt_util.parse_datetime(state.attributes["expected_at"])
    assert expected is not None
    assert expected > dt_util.utcnow()
    assert 8 <= state.attributes["minutes"] <= 25

    await step(hass, freezer, ALEX, None, zones=("zone.home",))
    state = hass.states.get(HEADING)
    assert state.state == STATE_OFF
    assert state.attributes == {"friendly_name": "Alex Heading home"}


async def test_entering_a_zone_around_home_starts_the_trip_home(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    ring = add_ring(hass, 4_000)
    set_person(hass, ALEX, 6_000)
    await setup_entries(hass, person_entry(ALEX))
    await drive(hass, freezer, ALEX, [5_500.0, 6_000.0])
    assert hass.states.get(HEADING).state == STATE_OFF
    await drive(hass, freezer, ALEX, [3_900.0], zones_for={3_900.0: (ring,)})
    state = hass.states.get(HEADING)
    assert state.state == STATE_ON
    assert state.attributes["reason"] == "zone_crossing"
    # The zone around home is not "another zone" for arriving soon.
    assert hass.states.get(APPROACH).attributes["zones"] == []


async def test_group_expected_home_and_near_home(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 10_000)
    set_person(hass, SAM, None, zones=("zone.home",))
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    assert hass.states.get(EXPECTED).state == STATE_UNKNOWN
    near = hass.states.get(NEAR)
    assert near.state == "100"
    assert near.attributes["person"] == SAM
    assert near.attributes["persons_home"] == [SAM]
    assert near.attributes["radius_m"] == 3_000
    assert near.attributes["fade_after_min"] == 90

    # Sam goes out; nobody is home and Alex is far.
    await step(hass, freezer, SAM, 5_000)
    near = hass.states.get(NEAR)
    assert near.state == "0"
    assert near.attributes["person"] == SAM
    assert near.attributes["distance_m"] == 5_000

    # Alex heads home from 10 km.
    await drive(hass, freezer, ALEX, [9_200.0, 8_400.0, 7_600.0])
    expected = hass.states.get(EXPECTED)
    assert dt_util.parse_datetime(expected.state) is not None
    assert expected.attributes["person"] == ALEX
    assert expected.attributes["person_name"] == "Alex"
    assert expected.attributes["reason"] == "progress"
    assert [c["person"] for c in expected.attributes["heading_home"]] == [ALEX]
    assert expected.attributes["minutes"] >= 1

    # Sam comes within 2 km: 45 % (100 % at 800 m, 0 % at 3 km).
    await step(hass, freezer, SAM, 2_000)
    near = hass.states.get(NEAR)
    assert near.state == "45"
    assert near.attributes["person"] == SAM


async def test_near_home_fades_after_a_long_stay(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_entries(hass, person_entry(ALEX), group_entry([ALEX]))
    await step(hass, freezer, ALEX, 400)
    assert hass.states.get(NEAR).state == "100"

    # At the café, the phone reporting every 10 minutes.
    for minute in range(10, 121, 10):
        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        set_person(hass, ALEX, 400, accuracy=10.0 + minute % 20 / 10)
        await hass.async_block_till_done()
        if minute <= 90:
            assert hass.states.get(NEAR).state == "100", minute
    # Two hours there: half way through the hour-long fade.
    near = hass.states.get(NEAR)
    assert near.state == "50"
    assert near.attributes["staying_since"] is not None
    assert hass.states.get(HEADING).state == STATE_OFF
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(NEAR).state == "0"


async def test_near_home_settings_come_from_the_group_options(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 4_000)
    group = MockConfigEntry(
        domain=DOMAIN,
        title="Home ETA",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_ZONE: "zone.home"},
        options={
            CONF_MEMBERS: [ALEX],
            CONF_NEAR_HOME_RADIUS_M: 8_000,
            CONF_NEAR_HOME_FADE_AFTER_MIN: 0,
        },
    )
    await setup_entries(hass, person_entry(ALEX), group)
    near = hass.states.get(NEAR)
    # (8000 - 4000) / (8000 - 800) = 56 %
    assert near.state == "56"
    assert near.attributes["fade_after_min"] is None


@pytest.mark.parametrize("startup", [True, False], ids=["restart", "reload"])
async def test_heading_home_restored_after_a_restart(
    hass: HomeAssistant, startup: bool
) -> None:
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "away"),
                {
                    "state": "away",
                    "since": now - 1_800,
                    "last_heard": now - 120,
                    "homeward": {
                        "since": now - 600,
                        "farthest_m": 30_000,
                        "closest_m": 20_000,
                    },
                },
            )
        ],
    )
    if startup:
        # The person's state is the one saved at shutdown, not a fix.
        hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX), group_entry([ALEX]))
    state = hass.states.get(HEADING)
    assert state.state == STATE_ON
    assert state.attributes["reason"] == "restored"
    assert dt_util.parse_datetime(state.attributes["since"]) == (
        dt_util.utc_from_timestamp(now - 600)
    )
    # After a restart nothing was measured yet; after a reload the person's
    # state is a genuine fix, but one fix gives no speed either.
    assert state.attributes["speed_source"] == "typical"
    assert state.attributes["farthest_m"] == 30_000
    assert hass.states.get(EXPECTED).attributes["reason"] == "restored"
    # The approach state itself was not one that is restored.
    assert hass.states.get(APPROACH).attributes["reason"] != "restored"


@pytest.mark.parametrize(
    "homeward",
    [
        None,
        "nonsense",
        {"since": "yesterday", "farthest_m": 30_000, "closest_m": 20_000},
        {"since": 1.0, "farthest_m": -5, "closest_m": 20_000},
        {"since": 1.0, "farthest_m": 30_000},
    ],
)
async def test_bad_saved_trip_home_is_ignored(
    hass: HomeAssistant, homeward: object
) -> None:
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "away"),
                {
                    "state": "away",
                    "since": now - 1_800,
                    "last_heard": now - 120,
                    "homeward": homeward,
                },
            )
        ],
    )
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(HEADING).state == STATE_OFF


async def test_old_trip_home_is_not_restored(hass: HomeAssistant) -> None:
    """Home Assistant was down longer than any trip home could take."""
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "away"),
                {
                    "state": "away",
                    "since": now - 30_000,
                    "last_heard": now - 25_000,
                    "homeward": {
                        "since": now - 26_000,
                        "farthest_m": 30_000,
                        "closest_m": 20_000,
                    },
                },
            )
        ],
    )
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(HEADING).state == STATE_OFF


async def test_overdue_restored_trip_ends_at_once(hass: HomeAssistant) -> None:
    """Down for two hours: a trip home from 21 km should be over by now."""
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "away"),
                {
                    "state": "away",
                    "since": now - 9_000,
                    "last_heard": now - 7_200,
                    "homeward": {
                        "since": now - 7_500,
                        "farthest_m": 30_000,
                        "closest_m": 20_000,
                    },
                },
            )
        ],
    )
    # At startup the person's state is the one saved at shutdown, not a fix.
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    state = hass.states.get(HEADING)
    assert state.state == STATE_OFF
    assert state.attributes["reason"] == "overdue"


async def test_trip_home_saved_for_a_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 10_000)
    entry = person_entry(ALEX)
    await setup_entries(hass, entry)
    await drive(hass, freezer, ALEX, [9_200.0, 8_400.0, 7_600.0])
    assert hass.states.get(HEADING).state == STATE_ON
    sensor = next(
        e
        for e in hass.data["sensor"].entities
        if e.entity_id == APPROACH  # type: ignore[attr-defined]
    )
    saved = sensor.extra_restore_state_data.as_dict()
    assert saved["homeward"]["farthest_m"] == pytest.approx(10_000, abs=50)
    assert saved["homeward"]["closest_m"] == pytest.approx(7_600, abs=50)
    assert saved["homeward"]["since"] is not None
