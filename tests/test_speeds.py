"""Typical speeds learned from a person's trips home, in Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import math
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.home_eta.const import (
    SPEEDS_REFIT_INTERVAL_S,
    SPEEDS_SAVE_DELAY_S,
)
from custom_components.home_eta.core.calibration import (
    HALF_LIFE_DAYS,
    MIN_TRIP_WEIGHT,
    LearnedTrip,
    SpeedCalibrator,
    TripSample,
    make_trip,
)
from custom_components.home_eta.core.kinematic import Target
from custom_components.home_eta.core.speeds import (
    BUILT_IN_CURVES,
    DRIVING_SPEED_MPS,
    TYPICAL_SPEED_MPS,
)
from custom_components.home_eta.runtime import PersonRuntime
from tests.helpers import (
    ALEX,
    HOME_LAT,
    HOME_LON,
    HOME_RADIUS_M,
    person_entry,
    set_person,
    step,
)

SPEEDS = "sensor.alex_typical_speeds"
RELEARN = "button.alex_relearn_typical_speeds"
HOME = Target(HOME_LAT, HOME_LON, HOME_RADIUS_M)

pytestmark = pytest.mark.usefixtures("zones")


def store_key(entry: MockConfigEntry) -> str:
    return f"home_eta.speeds.{entry.entry_id}"


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def set_up(
    hass: HomeAssistant, entry: MockConfigEntry | None = None
) -> MockConfigEntry:
    entry = entry or person_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    return entry


def runtime(entry: MockConfigEntry) -> PersonRuntime:
    assert isinstance(entry.runtime_data, PersonRuntime)
    return entry.runtime_data


async def round_trip(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """A drive out to 8 km and back home, reported every few minutes.

    Heading home from 5 km (more than 20 % closer than 8 km). Back by other
    points: a fix right back where an earlier one was would be undone as a
    position glitch.
    """
    for east in (2_000, 4_000, 6_000, 8_000):
        await step(hass, freezer, ALEX, east, seconds=300)
    for east in (7_000, 5_000, 3_000, 1_500):
        await step(hass, freezer, ALEX, east, seconds=180)
    await step(hass, freezer, ALEX, None, seconds=180, zones=("zone.home",))
    await settle(hass)


async def save_now(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=SPEEDS_SAVE_DELAY_S + 1))
    async_fire_time_changed(hass)
    await settle(hass)


def built_in_rows(table: tuple[tuple[float, float], ...]) -> list[dict[str, float]]:
    return [
        {
            "distance_m": round(distance),
            "built_in_mps": round(speed, 2),
            "learned_mps": round(speed, 2),
            "factor": 1.0,
            "trips": 0.0,
            "measured_weight": 0.5,
            "weight_trips": 0.0,
        }
        for distance, speed in table
    ]


def seeded(
    entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    count: int,
    *,
    place: Target | None = HOME,
) -> None:
    """Save ``count`` drives home from 20 km, faster than built in."""
    now = dt_util.utcnow().timestamp()
    saved = SpeedCalibrator(
        [_trip(now - 3_600 * i, 20_000.0, 14.0) for i in range(1, count + 1)],
        now,
        history_learned=True,
        place=place,
    )
    hass_storage[store_key(entry)] = {
        "version": 1,
        "key": store_key(entry),
        "data": saved.as_dict(),
    }


async def test_starts_from_the_built_in_tables(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    state = hass.states.get(SPEEDS)
    assert state.state == "0"
    assert state.attributes["unit_of_measurement"] == "trips"
    assert state.attributes["all_trips"] == built_in_rows(TYPICAL_SPEED_MPS)
    assert state.attributes["driving"] == built_in_rows(DRIVING_SPEED_MPS)
    assert state.attributes["recent_trips"] == []
    assert state.attributes["first_trip"] is None
    assert state.attributes["fitted_at"] is None
    assert state.attributes["prior_trips"] == 3.0
    assert state.attributes["half_life_days"] == 60.0
    # No recorder: nothing learned from history, tried again next time.
    assert state.attributes["learned_from_history"] is False
    assert store_key(entry) not in hass_storage
    assert runtime(entry).tracker.homeward.speed_curves == BUILT_IN_CURVES
    assert hass.states.get(RELEARN) is not None


async def test_a_trip_home_is_learned_and_saved(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await round_trip(hass, freezer)

    state = hass.states.get(SPEEDS)
    assert state.state == "1"
    (recent,) = state.attributes["recent_trips"]
    now = dt_util.utcnow().timestamp()
    arrived = dt_util.utc_from_timestamp(round(now / 60) * 60)  # to the minute
    assert recent["arrived_at"] == arrived.isoformat()
    assert 4_900 < recent["from_m"] < 5_100
    assert recent["samples"] == 3
    assert recent["changes"]
    for change in recent["changes"]:
        assert change["curve"] in ("all_trips", "driving")
        assert "factor" in change or "measured_weight" in change
        if "factor" in change:
            before, after = change["factor"]
            assert before == 1.0
            assert after != 1.0
    learned = runtime(entry).speeds
    assert runtime(entry).tracker.homeward.speed_curves == learned.curves
    assert learned.curves != BUILT_IN_CURVES
    assert state.attributes["first_trip"] == recent["arrived_at"]
    assert state.attributes["fitted_at"] == dt_util.utcnow().isoformat()

    await save_now(hass, freezer)
    saved = hass_storage[store_key(entry)]["data"]
    assert len(saved["trips"]) == 1
    # No coordinates but the zone's own centre.
    place = saved.pop("place")
    assert place == {
        "latitude": HOME_LAT,
        "longitude": HOME_LON,
        "radius_m": HOME_RADIUS_M,
    }
    assert "latitude" not in str(saved)


async def test_what_was_learned_survives_a_reload(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Saved at once on unload, not only after the delay."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await round_trip(hass, freezer)
    learned = runtime(entry).speeds
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert hass.states.get(SPEEDS).state == "1"
    reloaded = runtime(entry).speeds
    assert len(reloaded.trips) == 1
    # Held as saved: exactly what was learned.
    assert reloaded.trips == learned.trips
    assert reloaded.curves == learned.curves
    assert reloaded.place == learned.place == HOME
    assert runtime(entry).tracker.homeward.speed_curves == reloaded.curves


async def test_saved_trips_are_used_from_the_start(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = person_entry()
    seeded(entry, hass_storage, 5)
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)
    assert hass.states.get(SPEEDS).state == "5"
    curves = runtime(entry).tracker.homeward.speed_curves
    assert curves == runtime(entry).speeds.curves
    assert curves.typical_mps(20_000, driving=True) > BUILT_IN_CURVES.typical_mps(
        20_000, driving=True
    )


async def test_unusable_saved_data_starts_afresh(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = person_entry()
    hass_storage[store_key(entry)] = {
        "version": 1,
        "key": store_key(entry),
        "data": {"trips": "nonsense", "history_learned": "yes"},
    }
    set_person(hass, ALEX, None, zones=("zone.home",))
    await set_up(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SPEEDS).state == "0"


async def test_speeds_saved_by_a_newer_version_are_learned_afresh(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a downgrade: not a failed setup."""
    entry = person_entry()
    hass_storage[store_key(entry)] = {
        "version": 99,
        "minor_version": 1,
        "key": store_key(entry),
        "data": {"anything": True},
    }
    set_person(hass, ALEX, None, zones=("zone.home",))
    await set_up(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert "saved by a newer version of Home ETA" in caplog.text
    assert hass.states.get(SPEEDS).state == "0"


async def test_relearning_without_the_recorder_is_refused(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    """It would only forget: what was learned is kept."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await round_trip(hass, freezer)
    await save_now(hass, freezer)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button", "press", {"entity_id": RELEARN}, blocking=True
        )
    assert err.value.translation_key == "relearn_no_recorder"
    assert hass.states.get(SPEEDS).state == "1"
    assert len(hass_storage[store_key(entry)]["data"]["trips"]) == 1


async def test_learned_speeds_are_refitted_daily_as_trips_age(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await round_trip(hass, freezer)
    fitted = runtime(entry).speeds.calibration
    evidence = max(k.trips for k in fitted.all_trips.knots)
    freezer.tick(timedelta(seconds=SPEEDS_REFIT_INTERVAL_S - 120))
    async_fire_time_changed(hass)
    await settle(hass)
    assert runtime(entry).speeds.calibration is fitted
    freezer.tick(timedelta(seconds=180))
    async_fire_time_changed(hass)
    await settle(hass)
    refitted = runtime(entry).speeds.calibration
    assert refitted.at > fitted.at
    assert max(k.trips for k in refitted.all_trips.knots) < evidence
    state = hass.states.get(SPEEDS)
    assert (
        state.attributes["fitted_at"]
        == dt_util.utc_from_timestamp(refitted.at).isoformat()
    )
    assert runtime(entry).tracker.homeward.speed_curves == refitted.curves


async def test_moving_the_zone_far_forgets_what_was_learned(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = person_entry()
    seeded(entry, hass_storage, 4)
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)
    assert hass.states.get(SPEEDS).state == "4"

    # Nudged within its radius: still the same place.
    await move_home(hass, HOME_LAT + 0.0005, HOME_LON)
    assert hass.states.get(SPEEDS).state == "4"

    await move_home(hass, HOME_LAT + 0.05, HOME_LON)
    state = hass.states.get(SPEEDS)
    assert state.state == "0"
    assert state.attributes["learned_from_history"] is True
    saved = hass_storage[store_key(entry)]["data"]
    assert saved["trips"] == []
    assert saved["learn_after"] is not None
    assert runtime(entry).tracker.homeward.speed_curves == BUILT_IN_CURVES


async def move_home(hass: HomeAssistant, latitude: float, longitude: float) -> None:
    await hass.config.async_update(latitude=latitude, longitude=longitude)
    await settle(hass)


async def test_removing_the_person_deletes_what_was_learned(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await round_trip(hass, freezer)
    await save_now(hass, freezer)
    assert store_key(entry) in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert store_key(entry) not in hass_storage


def _trip(at: float, distance: float, speed: float) -> LearnedTrip:
    trip = make_trip(at, [TripSample(distance, speed, speed)])
    assert trip is not None
    return trip


async def test_a_trip_already_learned_changes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await round_trip(hass, freezer)
    learned = runtime(entry).speeds
    runtime(entry)._learn(list(learned.trips))
    await settle(hass)
    assert runtime(entry).speeds is learned


async def test_trips_that_age_out_at_a_refit_leave_the_disk(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    entry = person_entry()
    now = dt_util.utcnow().timestamp()
    # Just above the weight at which a trip is forgotten, then just below.
    limit_days = HALF_LIFE_DAYS * math.log2(1 / MIN_TRIP_WEIGHT)
    old = _trip(now - (limit_days - 0.5) * 86_400, 20_000.0, 14.0)
    recent = _trip(now - 3_600, 20_000.0, 14.0)
    hass_storage[store_key(entry)] = {
        "version": 1,
        "key": store_key(entry),
        "data": SpeedCalibrator(
            [old, recent], now, history_learned=True, place=HOME
        ).as_dict(),
    }
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)
    assert hass.states.get(SPEEDS).state == "2"
    freezer.tick(timedelta(seconds=SPEEDS_REFIT_INTERVAL_S + 60))
    async_fire_time_changed(hass)
    await settle(hass)
    assert hass.states.get(SPEEDS).state == "1"
    await save_now(hass, freezer)
    assert len(hass_storage[store_key(entry)]["data"]["trips"]) == 1


async def test_moving_the_zone_twice_while_learning_forgets_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    """The second move is within the first place's radius: nothing more to forget."""
    entry = person_entry()
    seeded(entry, hass_storage, 4)
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)
    person = runtime(entry)
    holding, release = asyncio.Event(), asyncio.Event()

    async def learning_a_trip() -> None:
        async with person._learning:
            holding.set()
            await release.wait()

    person._start_task(learning_a_trip())
    await holding.wait()
    zone = hass.states.get("zone.home")
    first = HOME_LAT + 0.05
    moved_at = dt_util.utcnow().timestamp()
    attributes = {**zone.attributes, "latitude": first}
    hass.states.async_set("zone.home", zone.state, attributes)
    freezer.tick(timedelta(minutes=1))
    attributes = {**zone.attributes, "latitude": first + 0.0005}  # 56 m on
    hass.states.async_set("zone.home", zone.state, attributes)
    release.set()
    await settle(hass)
    assert person.speeds.learn_after == moved_at
    assert person.speeds.place is not None
    assert person.speeds.place.latitude == first


async def test_a_trip_learned_while_unloading_is_saved(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Regression: learning that finished while stopping was not saved."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    await hold_learning(runtime(entry))
    # A trip home ends: learning it waits for the learning under way.
    now = dt_util.utcnow().timestamp()
    runtime(entry)._learn([_trip(now - 60, 20_000.0, 14.0)])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert len(hass_storage[store_key(entry)]["data"]["trips"]) == 1


async def hold_learning(person: PersonRuntime) -> None:
    """Learn from the history, as it were, until stopping cancels it."""
    holding = asyncio.Event()

    async def learning_from_history() -> None:
        async with person._learning:
            holding.set()
            await asyncio.Event().wait()

    person._start_task(learning_from_history(), history=True)
    await holding.wait()


async def test_a_zone_move_waiting_for_learning_is_saved_when_unloading(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Regression: forgetting that waited for learning to finish was not
    saved when stopping, so the old place's trips came back at the next start."""
    entry = person_entry()
    seeded(entry, hass_storage, 4)
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)
    await hold_learning(runtime(entry))
    # The zone moves: forgetting waits for the learning under way.
    zone = hass.states.get("zone.home")
    moved = HOME_LAT + 0.05
    attributes = {**zone.attributes, "latitude": moved}
    hass.states.async_set("zone.home", zone.state, attributes)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    saved = hass_storage[store_key(entry)]["data"]
    assert saved["trips"] == []
    assert saved["learn_after"] is not None
    assert saved["place"]["latitude"] == moved


async def test_a_zone_moved_while_not_loaded_forgets_what_was_learned(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Moved while Home Assistant was off, or the person was not loaded."""
    caplog.set_level(logging.INFO)
    entry = person_entry()
    seeded(entry, hass_storage, 4)
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)

    # Nudged within its radius meanwhile: still the same place.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await move_home(hass, HOME_LAT + 0.0005, HOME_LON)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert hass.states.get(SPEEDS).state == "4"
    assert "forgotten" not in caplog.text

    assert await hass.config_entries.async_unload(entry.entry_id)
    await move_home(hass, HOME_LAT + 0.05, HOME_LON)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert hass.states.get(SPEEDS).state == "0"
    assert "zone.home moved, so the typical speeds learned around it" in caplog.text
    assert runtime(entry).tracker.homeward.speed_curves == BUILT_IN_CURVES
    saved = hass_storage[store_key(entry)]["data"]
    assert saved["trips"] == []
    assert saved["learn_after"] is not None
    assert saved["place"]["latitude"] == HOME_LAT + 0.05


async def test_speeds_saved_without_a_place_take_the_zone_as_it_is(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = person_entry()
    seeded(entry, hass_storage, 4, place=None)
    set_person(hass, ALEX, 30_000)
    await set_up(hass, entry)
    assert hass.states.get(SPEEDS).state == "4"
    assert runtime(entry).speeds.place == HOME


async def test_learning_that_fails_while_unloading_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    now = dt_util.utcnow().timestamp()
    with patch.object(SpeedCalibrator, "adding", side_effect=RuntimeError("boom")):
        runtime(entry)._learn([_trip(now - 60, 20_000.0, 14.0)])
        assert await hass.config_entries.async_unload(entry.entry_id)
        await settle(hass)
    assert "learning or saving typical speeds failed while stopping" in caplog.text
    assert "boom" in caplog.text
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_relearning_while_unloading_is_refused(hass: HomeAssistant) -> None:
    """Stopping waits for the learning there is: none may start meanwhile."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    person = runtime(entry)
    release = asyncio.Event()
    person._start_task(release.wait())  # learning a trip, say
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    for _ in range(100):
        if person._stopping:
            break
        await asyncio.sleep(0)
    assert person._stopping
    assert not unload.done()
    with pytest.raises(HomeAssistantError) as err:
        await person.async_relearn_speeds()
    assert err.value.translation_key == "relearn_interrupted"
    release.set()
    assert await unload
