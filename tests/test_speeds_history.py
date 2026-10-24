"""Typical speeds learned from the recorder's history, in Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy.exc import OperationalError

from custom_components.home_eta.core.calibration import SpeedCalibrator
from custom_components.home_eta.runtime import PersonRuntime
from tests.helpers import ALEX, HOME_LAT, HOME_LON, person_entry, set_person
from tests.test_speeds import (
    HOME,
    RELEARN,
    SPEEDS,
    move_home,
    round_trip,
    runtime,
    set_up,
    settle,
    store_key,
)


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder: Any) -> None:
    """Prepare the recorder before Home Assistant starts."""


async def record_trips(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, count: int
) -> None:
    """Record ``count`` trips home, then move on a moment.

    The history runs up to (not including) now.
    """
    set_person(hass, ALEX, None, zones=("zone.home",))
    await hass.async_block_till_done()
    for _ in range(count):
        await round_trip(hass, freezer)
    await async_wait_recording_done(hass)
    freezer.tick(timedelta(minutes=15))


async def press_relearn(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": RELEARN}, blocking=True
    )


async def test_the_recorders_history_is_learned_when_first_set_up(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await record_trips(hass, freezer, 2)
    entry = await set_up(hass)
    state = hass.states.get(SPEEDS)
    assert state.state == "2"
    assert state.attributes["learned_from_history"] is True
    assert hass_storage[store_key(entry)]["data"]["history_learned"] is True
    assert runtime(entry).tracker.homeward.speed_curves == runtime(entry).speeds.curves

    # Once only: neither asking again nor a reload learns it again.
    with patch.object(
        PersonRuntime, "_async_history_trips", side_effect=AssertionError
    ):
        assert await runtime(entry)._async_learn_history(relearn=False)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await settle(hass)
    assert hass.states.get(SPEEDS).state == "2"

    # Relearning reads it afresh: the same two trips.
    await press_relearn(hass)
    assert hass.states.get(SPEEDS).state == "2"


async def test_an_unreadable_history_is_tried_again_next_time(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    unreadable = patch(
        "homeassistant.components.recorder.history.get_significant_states",
        side_effect=OperationalError("SELECT", {}, Exception("locked")),
    )
    with unreadable:
        entry = await set_up(hass)
    assert "could not read the recorder's history" in caplog.text
    assert hass.states.get(SPEEDS).attributes["learned_from_history"] is False
    assert store_key(entry) not in hass_storage
    # Relearning says so rather than pretending.
    with unreadable, pytest.raises(HomeAssistantError) as err:
        await press_relearn(hass)
    assert err.value.translation_key == "relearn_unreadable"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert hass.states.get(SPEEDS).attributes["learned_from_history"] is True


async def test_a_database_that_is_not_ready_is_tried_again_next_time(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    failed = hass.loop.create_future()
    failed.set_result(False)
    with patch.object(recorder_mock, "async_db_ready", failed):
        entry = await set_up(hass)
    assert "database is not available" in caplog.text
    assert hass.states.get(SPEEDS).attributes["learned_from_history"] is False
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert hass.states.get(SPEEDS).attributes["learned_from_history"] is True


class _Slow:
    """Holds the history read until released, to stop the entry meanwhile."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.started = hass.loop.create_future()
        self.release = hass.loop.create_future()
        self.finished = False
        self._original = PersonRuntime._async_history_trips

    def let_go(self) -> None:
        """Release the read, unless stopping the entry already cancelled it."""
        if not self.release.done():
            self.release.set_result(None)

    def patch(self) -> Any:
        slow = self

        async def history(runtime: PersonRuntime, end: Any) -> Any:
            if not slow.started.done():
                slow.started.set_result(None)
            await slow.release
            result = await slow._original(runtime, end)
            slow.finished = True
            return result

        return patch.object(PersonRuntime, "_async_history_trips", history)


async def test_unloading_while_learning_from_history_stops_it(
    recorder_mock: Any, zones: None, hass: HomeAssistant
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    slow = _Slow(hass)
    with slow.patch():
        entry = person_entry()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await slow.started
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert slow.release.cancelled()
    assert not slow.finished
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_a_reload_while_relearning_interrupts_it_and_keeps_the_saved(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Regression: the old run's relearn wrote after the new run had loaded."""
    await record_trips(hass, freezer, 1)
    # Saved: nothing learned, the history taken as learned.
    entry = person_entry()
    hass_storage[store_key(entry)] = {
        "version": 1,
        "key": store_key(entry),
        "data": SpeedCalibrator(history_learned=True, place=HOME).as_dict(),
    }
    await set_up(hass, entry)
    assert hass.states.get(SPEEDS).state == "0"
    # Relearn, slowly, and reload in the middle.

    slow = _Slow(hass)
    with slow.patch():
        press = hass.async_create_task(press_relearn(hass))
        await slow.started
        assert await hass.config_entries.async_reload(entry.entry_id)
        slow.let_go()
        with pytest.raises(HomeAssistantError) as err:
            await press
    assert err.value.translation_key == "relearn_interrupted"
    await settle(hass)
    # What the new run loaded is what is on disk, and nothing changed it since.
    assert hass.states.get(SPEEDS).state == "0"
    assert hass_storage[store_key(entry)]["data"]["trips"] == []


async def test_removing_the_person_while_relearning_leaves_nothing_behind(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await record_trips(hass, freezer, 1)
    entry = await set_up(hass)
    assert store_key(entry) in hass_storage
    slow = _Slow(hass)
    with slow.patch():
        press = hass.async_create_task(press_relearn(hass))
        await slow.started
        await hass.config_entries.async_remove(entry.entry_id)
        slow.let_go()
        with pytest.raises(HomeAssistantError):
            await press
        await settle(hass)
    assert store_key(entry) not in hass_storage


async def test_removing_the_person_while_writing_waits_for_the_write(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Regression: a relearn cancelled while its result was being written
    let the write land after the removal, bringing the person's data back.

    A write to disk runs in the executor: cancelling whoever waits for it
    does not stop it. The mock storage writes at once, so it is held here
    the same way.
    """
    await record_trips(hass, freezer, 1)
    entry = await set_up(hass)
    key = store_key(entry)
    writing = hass.loop.create_future()
    release = asyncio.Event()
    mock_write = Store._async_write_data

    async def write_in_executor(store: Store[Any], data: dict[str, Any]) -> None:
        if store.key != key:
            await mock_write(store, data)
            return

        async def job() -> None:
            await release.wait()
            await mock_write(store, data)

        if not writing.done():
            writing.set_result(None)
        await asyncio.shield(hass.async_create_task(job()))

    with patch.object(Store, "_async_write_data", write_in_executor):
        press = hass.async_create_task(press_relearn(hass))
        await writing
        remove = hass.async_create_task(
            hass.config_entries.async_remove(entry.entry_id)
        )
        for _ in range(100):
            await asyncio.sleep(0)
        # Removing waits for the write rather than letting it land after.
        assert not remove.done()
        release.set()
        await remove
        with pytest.raises(HomeAssistantError):
            await press
        await settle(hass)
    assert key not in hass_storage


async def test_after_the_zone_moved_only_later_history_is_learned(
    recorder_mock: Any,
    zones: None,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    await record_trips(hass, freezer, 1)
    await set_up(hass)
    assert hass.states.get(SPEEDS).state == "1"
    await move_home(hass, HOME_LAT + 0.05, HOME_LON)
    assert hass.states.get(SPEEDS).state == "0"
    # At once: no history after it yet.
    await press_relearn(hass)
    assert hass.states.get(SPEEDS).state == "0"
    # Later: the trip before the move is still not learned.
    freezer.tick(timedelta(hours=1))
    await press_relearn(hass)
    assert hass.states.get(SPEEDS).state == "0"


async def test_a_relearn_cancelled_by_its_caller_is_not_an_error(
    recorder_mock: Any, zones: None, hass: HomeAssistant
) -> None:
    """Stopping waits for no one: the caller's own cancellation goes through."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await set_up(hass)
    slow = _Slow(hass)
    with slow.patch():
        press = hass.async_create_task(runtime(entry).async_relearn_speeds())
        await slow.started
        press.cancel()
        with pytest.raises(asyncio.CancelledError):
            await press
        slow.let_go()
        await settle(hass)
