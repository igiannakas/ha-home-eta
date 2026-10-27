"""Heading home and its restore across restarts, through Home Assistant."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import timedelta
from types import ModuleType
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.home_eta import (
    number as number_platform,
    sensor as sensor_platform,
)
from tests.helpers import ALEX, group_entry, person_entry, set_person, step
from tests.sim import to_latlon

STATUS = "sensor.alex_status"
THRESHOLD = "number.alex_arriving_soon_threshold"
SOMEONE_SOON = "binary_sensor.home_eta_someone_arriving_soon"
THRESHOLD_DATA = {
    "native_max_value": 120,
    "native_min_value": 1,
    "native_step": 1,
    "native_unit_of_measurement": "min",
}
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
        "threshold_s": 600.0,
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
    """Three hours at a friend's 500 m away: the stay comes back after a restart."""
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
    assert near.state == "100"  # near home does not fade, however long the stay
    assert dt_util.parse_datetime(near.attributes["staying_since"]) == (
        dt_util.utc_from_timestamp(now - 3 * 3_600)
    )
    # Saved again for the next restart, as it was, with the report received
    # while starting (the person's restored state: no fix).
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved == {
        "last_heard": now - 600,
        "last_report": hass.states.get(ALEX).last_updated.timestamp(),
        "trip": None,
        "stay": stay,
    }


async def test_a_restored_trip_survives_a_second_restart(hass: HomeAssistant) -> None:
    """Restored, then restarted again before any fix: the trip is kept."""
    now = dt_util.utcnow().timestamp()
    trip = {
        "since": now - 600,
        "farthest_m": 30_000.0,
        "closest_m": 20_000.0,
        "arriving_soon": False,
        "threshold_s": 900.0,
    }
    restore(hass, {"last_heard": now - 120, "trip": trip, "stay": None})
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    await setup_entries(hass, person_entry(ALEX))
    assert hass.states.get(STATUS).attributes["reason"] == "restored"
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved["last_heard"] == now - 120
    # Saved before 2.4.0: no estimates, and none worked out since (no fix).
    assert saved["trip"] == {**trip, "estimates": []}


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


def record(hass: HomeAssistant, *entity_ids: str) -> list[tuple[str, str]]:
    """Return a list that fills with every state the entities publish."""
    seen: list[tuple[str, str]] = []

    @callback
    def listener(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if event.data["entity_id"] in entity_ids and new is not None:
            seen.append((event.data["entity_id"], new.state))

    hass.bus.async_listen(EVENT_STATE_CHANGED, listener)
    return seen


def set_up_late(platform: ModuleType) -> Any:
    """Return a patch that makes a platform set up after the others."""
    original = platform.async_setup_entry

    async def late(*args: Any, **kwargs: Any) -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        await original(*args, **kwargs)

    return patch.object(platform, "async_setup_entry", late)


@pytest.mark.parametrize("late", [None, number_platform, sensor_platform])
async def test_no_wrong_status_while_setting_up_after_a_restart(
    hass: HomeAssistant, late: ModuleType | None
) -> None:
    """Regression: judged with the default threshold, arriving soon flashed on.

    About 14 minutes out with a saved threshold of 10, whichever of the
    threshold and the trip home is set up first.
    """
    now = dt_util.utcnow().timestamp()
    trip = {
        "since": now - 900,
        "farthest_m": 30_000,
        "closest_m": 5_000,
        "arriving_soon": False,
        "threshold_s": 600.0,
    }
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State(STATUS, "heading_home"), {"last_heard": now - 60, "trip": trip}),
            (State(THRESHOLD, "10.0"), THRESHOLD_DATA | {"native_value": 10.0}),
        ],
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 5_000)
    seen = record(hass, STATUS, SOMEONE_SOON)
    with set_up_late(late) if late is not None else nullcontext():
        await setup_entries(hass, person_entry(ALEX), group_entry([ALEX]))
    assert hass.states.get(STATUS).state == "heading_home"
    assert (STATUS, "arriving_soon") not in seen
    assert (SOMEONE_SOON, STATE_ON) not in seen


async def test_no_wrong_status_while_reloading(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The same at a reload, when automations are running."""
    set_person(hass, ALEX, 20_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, group_entry([ALEX]))
    await hass.services.async_call(
        "number", "set_value", {"entity_id": THRESHOLD, "value": 5}, blocking=True
    )
    for east in (17_000.0, 14_000.0, 11_000.0, 8_000.0, 5_000.0):
        await step(hass, freezer, ALEX, east, seconds=240)
    await step(hass, freezer, ALEX, 4_000.0, seconds=80)
    # About 8 minutes at the measured speed. The expected arrival keeps it
    # across the reload, but arriving soon goes by the newest estimate, as
    # before 2.4.0: after the reload that is the one report fed again, which
    # gives no speed, so the typical one for 4 km, about 12 minutes: within
    # the default 15, not within 5.
    minutes = hass.states.get("sensor.alex_minutes_to_home").state
    assert 5 < int(minutes) <= 15
    assert hass.states.get(STATUS).state == "heading_home"
    seen = record(hass, STATUS, SOMEONE_SOON)
    with set_up_late(number_platform):
        await hass.config_entries.async_reload(alex.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "heading_home"
    assert hass.states.get(STATUS).attributes["speed_source"] == "measured"
    assert hass.states.get("sensor.alex_minutes_to_home").state == minutes
    assert (STATUS, "arriving_soon") not in seen
    assert (SOMEONE_SOON, STATE_ON) not in seen


@pytest.mark.parametrize("held_back", [False, True], ids=["steady", "held-back"])
async def test_a_reload_with_no_new_report_keeps_the_expected_arrival(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, held_back: bool
) -> None:
    """A reload (an options change, say) is not a report: nothing moves."""
    set_person(hass, ALEX, 30_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, group_entry([ALEX]))
    east = 30_000.0
    for _ in range(6):
        east -= 12.5 * 240
        await step(hass, freezer, ALEX, east, seconds=240)
    if held_back:
        east -= 300  # one leg in a queue: an estimate the next report may drop
        await step(hass, freezer, ALEX, east, seconds=240)
    assert heading(hass)
    watched = (EXPECTED, STATUS, "sensor.home_eta_expected_home")
    before = {entity_id: hass.states.get(entity_id) for entity_id in watched}
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    await hass.config_entries.async_reload(alex.entry_id)
    await hass.async_block_till_done()
    for entity_id, state in before.items():
        assert hass.states.get(entity_id).state == state.state, entity_id
    status = hass.states.get(STATUS).attributes
    assert status["speed_mps"] == before[STATUS].attributes["speed_mps"]
    assert status["speed_source"] == before[STATUS].attributes["speed_source"]
    assert status_sensor(hass).extra_restore_state_data.as_dict() == saved


async def test_a_reload_after_a_merged_report_keeps_the_expected_arrival(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The phone sends the same place again 10 s later, more accurately: merged
    into the fix before it, so no fix itself, but the report the reload feeds
    again. It is no news, and nothing is counted twice."""
    set_person(hass, ALEX, 30_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, group_entry([ALEX]))
    east = 30_000.0
    for _ in range(6):
        east -= 12.5 * 240
        await step(hass, freezer, ALEX, east, seconds=240)
    east -= 300
    await step(hass, freezer, ALEX, east, seconds=240)  # one leg in a queue
    freezer.tick(timedelta(seconds=10))
    set_person(hass, ALEX, east, accuracy=5.0)
    await hass.async_block_till_done()
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved["last_report"] == saved["last_heard"] + 10
    watched = (EXPECTED, "sensor.home_eta_expected_home")
    before = {entity_id: hass.states.get(entity_id).state for entity_id in watched}
    await hass.config_entries.async_reload(alex.entry_id)
    await hass.async_block_till_done()
    for entity_id, state in before.items():
        assert hass.states.get(entity_id).state == state, entity_id
    after = status_sensor(hass).extra_restore_state_data.as_dict()
    assert after["trip"]["estimates"] == saved["trip"]["estimates"]


async def test_a_restart_keeps_the_saved_estimate_until_the_first_fix(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The person drove on while Home Assistant was down: their state at
    startup carries the restore time and is no fix, so the estimate saved is
    shown; the first fix after startup joins the saved ones."""
    now = dt_util.utcnow().timestamp()
    estimates = [
        {"arrival": now + 1_500, "speed_mps": 12.0, "source": "measured"},
        {"arrival": now + 1_200, "speed_mps": 12.5, "source": "measured"},
        {"arrival": now + 1_300, "speed_mps": 12.2, "source": "measured"},
    ]
    restore(
        hass,
        {
            "last_heard": now - 300,
            "last_report": now - 300,
            "trip": {
                "since": now - 1_800,
                "farthest_m": 30_000,
                "closest_m": 18_000,
                "arriving_soon": False,
                "threshold_s": 900,
                "estimates": estimates,
            },
            "stay": None,
        },
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 15_000)  # 3 km on since the last report heard of
    await setup_entries(hass, person_entry(ALEX))
    expected = dt_util.parse_datetime(hass.states.get(EXPECTED).state)
    assert expected is not None
    assert expected.timestamp() == pytest.approx(now + 1_300, abs=1)  # the middle
    assert hass.states.get(STATUS).attributes["speed_source"] == "measured"
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await step(hass, freezer, ALEX, 14_200, seconds=60)  # the first fix since
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved["trip"]["estimates"][:-1] == estimates[1:]
    assert saved["trip"]["estimates"][-1] not in estimates


async def test_a_saved_estimate_no_trip_home_could_take_is_dropped(
    hass: HomeAssistant,
) -> None:
    """Finite but absurd, read back from disk: not shown, and nothing breaks."""
    now = dt_util.utcnow().timestamp()
    restore(
        hass,
        {
            "last_heard": now - 120,
            "trip": {
                "since": now - 600,
                "farthest_m": 30_000,
                "closest_m": 20_000,
                "arriving_soon": False,
                "threshold_s": 900,
                "estimates": [
                    {"arrival": 1e12, "speed_mps": 10.0, "source": "measured"}
                ],
            },
            "stay": None,
        },
    )
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 21_000)
    group = group_entry([ALEX])
    await setup_entries(hass, person_entry(ALEX), group)
    assert group.state is ConfigEntryState.LOADED
    state = hass.states.get(STATUS)
    assert state.attributes["reason"] == "restored"
    assert state.attributes["speed_source"] == "typical"
    expected = dt_util.parse_datetime(hass.states.get(EXPECTED).state)
    assert expected is not None
    assert expected.timestamp() < now + 4 * 3_600
    group_expected = hass.states.get("sensor.home_eta_expected_home").state
    assert dt_util.parse_datetime(group_expected) == expected
    saved = status_sensor(hass).extra_restore_state_data.as_dict()
    assert saved["trip"]["estimates"] == []
