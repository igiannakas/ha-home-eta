"""Tests for setup, unload, removal, history storage, services and diagnostics."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.home_eta.const import (
    CONF_ADVANCED,
    CONF_ENABLE_HISTORICAL,
    CONF_IGNORED_ZONES,
    CONF_MEMBERS,
    DOMAIN,
    SERVICE_EXPORT_HISTORY,
    SERVICE_RESET_HISTORY,
)
from custom_components.home_eta.core.params import MIN_HIST_TRIPS
from custom_components.home_eta.runtime import DATA_KEY, PersonRuntime, store_key
from tests.helpers import (
    ALEX,
    SAM,
    drive_home,
    group_entry,
    person_entry,
    set_person,
    step,
)

pytestmark = pytest.mark.usefixtures("zones")

HISTORY = {CONF_ENABLE_HISTORICAL: True}


async def setup(hass: HomeAssistant, **kwargs: Any) -> Any:
    entry = person_entry(**kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def trip(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Leave home, wait, then walk 3 km home with a fix every 5 minutes."""
    await step(hass, freezer, ALEX, 3_000, seconds=600)
    await step(hass, freezer, ALEX, 3_000, seconds=3_600, zones=("zone.work",))
    await drive_home(
        hass, freezer, ALEX, start_m=3_000, speed_mps=1.4, interval_s=300, stop_m=150
    )
    await step(hass, freezer, ALEX, 10, zones=("zone.home",))


async def test_unload_and_reload(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert (ALEX, "zone.home") in hass.data[DATA_KEY].persons
    runtime = entry.runtime_data

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.data[DATA_KEY].persons == {}
    # No listener or timer survives the unload: the runtime is never called.
    view = runtime.view
    await step(hass, freezer, ALEX, 4_000)
    assert runtime.view is view

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    set_person(hass, ALEX, 4_000)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.alex_distance_to_home").state == "4000"


async def test_failed_start_leaves_nothing_behind(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Regression: cleanup was registered only after a successful start."""
    set_person(hass, ALEX, 5_000)
    calls: list[PersonRuntime] = []

    def broken_refresh(self: PersonRuntime, now: object = None) -> None:
        calls.append(self)
        raise RuntimeError("boom")

    with patch.object(PersonRuntime, "_refresh", broken_refresh):
        entry = await setup(hass)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert hass.data[DATA_KEY].persons == {}
        assert len(calls) == 1
        # Neither the state listener nor the tick outlived the failure.
        await step(hass, freezer, ALEX, 4_000)
        assert len(calls) == 1


async def test_zone_not_ready(hass: HomeAssistant) -> None:
    """A deleted zone is reported (zones load before this integration)."""
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass, zone="zone.missing")
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"zone_missing_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {
        "zone": "zone.missing",
        "entry": "Alex",
    }


async def test_invalid_advanced_options(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass, options={CONF_ADVANCED: {"max_gps_accuracy_m": -5}})
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_history_learned_saved_and_removed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await setup(hass, options={**HISTORY, CONF_IGNORED_ZONES: ["zone.work"]})
    for _ in range(MIN_HIST_TRIPS):
        await trip(hass, freezer)
    key = store_key(entry.entry_id)
    assert hass_storage[key]["data"]["trips_learned"] == MIN_HIST_TRIPS
    historical = hass.states.get("sensor.alex_eta_historical")
    assert historical.attributes["trips_learned"] == MIN_HIST_TRIPS

    # The next trip has a historical ETA once it is on the way.
    await step(hass, freezer, ALEX, 3_000, seconds=600)
    await step(hass, freezer, ALEX, 2_600, seconds=300)
    await step(hass, freezer, ALEX, 2_200, seconds=300)
    assert hass.states.get("sensor.alex_approach").state == "approaching"
    historical = hass.states.get("sensor.alex_eta_historical")
    assert historical.state != "unknown"
    assert historical.attributes["trips"] == MIN_HIST_TRIPS

    # Restart: the history is loaded back from storage.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.tracker.historical.trips_learned == MIN_HIST_TRIPS

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_services(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    set_person(hass, SAM, 5_000)
    alex = await setup(hass, options=HISTORY)
    await setup(hass, person=SAM)
    await trip(hass, freezer)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_EXPORT_HISTORY, {}, blocking=True, return_response=True
    )
    people = {p["person"]: p for p in response["people"]}
    assert people[ALEX]["enabled"] is True
    assert people[ALEX]["trips_learned"] == 1
    assert people[SAM]["enabled"] is False

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_HISTORY,
        {"person": [SAM]},
        blocking=True,
        return_response=True,
    )
    assert [p["person"] for p in response["people"]] == [SAM]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_HISTORY, {"person": [SAM]}, blocking=True
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RESET_HISTORY, {"person": ["person.nobody"]}, blocking=True
        )

    await hass.services.async_call(DOMAIN, SERVICE_RESET_HISTORY, {}, blocking=True)
    assert alex.runtime_data.tracker.historical.trips_learned == 0
    assert hass_storage[store_key(alex.entry_id)]["data"]["trips_learned"] == 0


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass)
    await step(hass, freezer, ALEX, 4_400)
    await step(hass, freezer, ALEX, 3_800)
    group = group_entry([ALEX])
    group.add_to_hass(hass)
    await hass.config_entries.async_setup(group.entry_id)
    await hass.async_block_till_done()

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    runtime = data["runtime"]
    assert runtime["person"] == ALEX
    # The state present at setup is not logged; the two changes are.
    assert len(runtime["recent_fixes"]) == 2
    fix = runtime["recent_fixes"][-1]
    assert fix["verdict"] == "moved"
    assert fix["latitude"] == round(fix["latitude"], 2)
    assert fix["longitude"] == round(fix["longitude"], 2)
    assert runtime["tracker"]["approach"]["state"] in ("approaching", "arriving_soon")

    data = await get_diagnostics_for_config_entry(hass, hass_client, group)
    assert data["runtime"]["members"] == [ALEX]


async def test_turning_learning_off_deletes_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    """Regression: learned trips outlived the option and could not be reset."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await setup(hass, options=HISTORY)
    await trip(hass, freezer)
    key = store_key(entry.entry_id)
    assert hass_storage[key]["data"]["trips_learned"] == 1
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ENABLE_HISTORICAL: False}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_services_accept_entity_registry_ids(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Regression: the schema allowed registry ids but nothing resolved them."""
    registry = er.async_get(hass)
    person = registry.async_get_or_create(
        "person", "person", "alex-id", suggested_object_id="alex"
    )
    assert person.entity_id == ALEX
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup(hass, options=HISTORY)
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_HISTORY,
        {"person": [person.id]},
        blocking=True,
        return_response=True,
    )
    assert [p["person"] for p in response["people"]] == [ALEX]


async def test_renamed_person_and_zone_are_followed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Regression: renaming the person broke tracking and orphaned the entry."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "person", "person", "alex-id", suggested_object_id="alex"
    )
    office = registry.async_get_or_create(
        "zone", "zone", "office-id", suggested_object_id="office"
    )
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass, options={CONF_IGNORED_ZONES: [office.entity_id]})
    # A group created as "Home ETA" and retitled since.
    template = group_entry([ALEX])
    group = MockConfigEntry(
        domain=DOMAIN,
        title="Renamed later",
        data=dict(template.data),
        options=dict(template.options),
        unique_id="group|zone.home|home_eta",
    )
    group.add_to_hass(hass)
    await hass.config_entries.async_setup(group.entry_id)
    await hass.async_block_till_done()

    registry.async_update_entity(ALEX, new_entity_id="person.alexander")
    hass.states.async_remove(ALEX)
    set_person(hass, "person.alexander", 4_000)
    await hass.async_block_till_done()
    assert entry.data["person"] == "person.alexander"
    assert entry.unique_id == "person.alexander|zone.home"
    assert group.options[CONF_MEMBERS] == ["person.alexander"]
    # The group's unique id keeps the name it was created with.
    assert group.unique_id == "group|zone.home|home_eta"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.person_id == "person.alexander"
    assert hass.states.get("sensor.alex_distance_to_home").state == "4000"
    assert not ir.async_get(hass).async_get_issue(
        DOMAIN, f"person_missing_{entry.entry_id}"
    )

    registry.async_update_entity(office.entity_id, new_entity_id="zone.workplace")
    await hass.async_block_till_done()
    assert entry.options[CONF_IGNORED_ZONES] == ["zone.workplace"]
    assert entry.state is ConfigEntryState.LOADED


async def test_removing_a_person_takes_them_out_of_groups(
    hass: HomeAssistant,
) -> None:
    """Regression: the group kept a removed member it could not even re-save."""
    set_person(hass, ALEX, 5_000)
    set_person(hass, SAM, 6_000)
    alex = await setup(hass)
    sam = await setup(hass, person=SAM)
    both = group_entry([ALEX, SAM])
    alone = group_entry([SAM], title="Sam only")
    for group in (both, alone):
        group.add_to_hass(hass)
        await hass.config_entries.async_setup(group.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_remove(sam.entry_id)
    await hass.async_block_till_done()
    assert both.options[CONF_MEMBERS] == [ALEX]
    assert both.runtime_data.members == (ALEX,)
    assert (
        hass.states.get("sensor.home_eta_first_arrival").attributes["missing_members"]
        == []
    )
    issues = ir.async_get(hass)
    assert alone.options[CONF_MEMBERS] == []
    assert issues.async_get_issue(DOMAIN, f"group_no_members_{alone.entry_id}")
    assert alex.state is ConfigEntryState.LOADED

    await hass.config_entries.async_remove(alone.entry_id)
    await hass.async_block_till_done()
    assert not issues.async_get_issue(DOMAIN, f"group_no_members_{alone.entry_id}")


async def test_events_after_unload_change_nothing(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    changes: list[str] = []
    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda event: changes.append(event.data["entity_id"]),
    )
    set_person(hass, ALEX, 4_000)
    await hass.async_block_till_done()
    assert changes == [ALEX]


async def test_registry_changes_other_than_renames_are_ignored(
    hass: HomeAssistant,
) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "person", "person", "alex-id", suggested_object_id="alex"
    )
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass)
    registry.async_update_entity(ALEX, name="Alexander")
    await hass.async_block_till_done()
    registry.async_remove(ALEX)
    await hass.async_block_till_done()
    assert entry.data["person"] == ALEX
    assert entry.state is ConfigEntryState.LOADED


async def test_rename_onto_a_tracked_id_keeps_unique_ids_distinct(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An entry for a since-deleted person.alexander still owns that unique id."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "person", "person", "alex-id", suggested_object_id="alex"
    )
    set_person(hass, ALEX, 5_000)
    alex = await setup(hass)
    old = person_entry("person.alexander")
    old.add_to_hass(hass)

    registry.async_update_entity(ALEX, new_entity_id="person.alexander")
    await hass.async_block_till_done()
    assert alex.data["person"] == "person.alexander"
    assert alex.unique_id == f"{ALEX}|zone.home"
    assert old.unique_id == "person.alexander|zone.home"
    assert "which another Home ETA entry already tracks" in caplog.text


async def test_moving_the_zone_far_forgets_learned_trips(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    entry = await setup(hass, options=HISTORY)
    await trip(hass, freezer)
    key = store_key(entry.entry_id)
    assert hass_storage[key]["data"]["trips_learned"] == 1
    zone = hass.states.get("zone.home")
    hass.states.async_set(
        "zone.home",
        zone.state,
        {**zone.attributes, "latitude": zone.attributes["latitude"] + 0.01},
    )
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["trips_learned"] == 0
