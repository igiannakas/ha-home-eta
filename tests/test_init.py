"""Tests for setup, unload, removal, migration, renames and diagnostics."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.home_eta.const import (
    CONF_ADVANCED,
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    CONF_NEAR_HOME_FADE_AFTER_MIN,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_ZONE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
)
from custom_components.home_eta.migration import (
    CONFIG_MINOR_VERSION,
    LEGACY_GROUP_ENTITIES,
    LEGACY_PERSON_ENTITIES,
)
from custom_components.home_eta.runtime import DATA_KEY, PersonRuntime
from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person, step

pytestmark = pytest.mark.usefixtures("zones")


async def setup(hass: HomeAssistant, **kwargs: Any) -> Any:
    entry = person_entry(**kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


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
    await step(hass, freezer, ALEX, 3_500)
    assert hass.states.get("sensor.alex_distance_to_home").state == "3500"


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


@pytest.mark.parametrize(
    "options",
    [
        {CONF_ADVANCED: {"max_gps_accuracy_m": -5}},
        {CONF_NEAR_HOME_RADIUS_M: 500},
        {CONF_NEAR_HOME_FADE_AFTER_MIN: -10},
    ],
)
async def test_invalid_options(hass: HomeAssistant, options: dict[str, Any]) -> None:
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass, options=options)
    assert entry.state is ConfigEntryState.SETUP_ERROR


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
    assert runtime["near_home"]["radius_m"] == 3_000
    # The state present at setup is not logged; the two changes are.
    assert len(runtime["recent_fixes"]) == 2
    fix = runtime["recent_fixes"][-1]
    assert fix["verdict"] == "moved"
    assert fix["latitude"] == round(fix["latitude"], 2)
    assert fix["longitude"] == round(fix["longitude"], 2)
    assert runtime["tracker"]["homeward"]["heading"] is True

    data = await get_diagnostics_for_config_entry(hass, hass_client, group)
    assert data["runtime"]["members"] == [ALEX]
    assert [m["person"] for m in data["runtime"]["heading_home"]] == [ALEX]
    assert data["runtime"]["near_home"]["person"] == ALEX


async def test_renamed_person_and_zone_are_followed(hass: HomeAssistant) -> None:
    """Regression: renaming the person broke tracking and orphaned the entry."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "person", "person", "alex-id", suggested_object_id="alex"
    )
    set_person(hass, ALEX, 5_000)
    entry = await setup(hass)
    # A group created as "Home ETA" and retitled since.
    template = group_entry([ALEX])
    group = MockConfigEntry(
        domain=DOMAIN,
        title="Renamed later",
        data=dict(template.data),
        options=dict(template.options),
        unique_id="group|zone.home|home_eta",
        minor_version=CONFIG_MINOR_VERSION,
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


async def test_renamed_zone_is_followed(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    office = registry.async_get_or_create(
        "zone", "zone", "office-id", suggested_object_id="office"
    )
    shape = dict(hass.states.get("zone.work").attributes)
    hass.states.async_set(office.entity_id, "0", shape)
    set_person(hass, SAM, 5_000)
    entry = await setup(hass, person=SAM, zone=office.entity_id, title="Sam to Office")
    assert entry.state is ConfigEntryState.LOADED
    registry.async_update_entity(office.entity_id, new_entity_id="zone.workplace")
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONE] == "zone.workplace"
    assert entry.unique_id == f"{SAM}|zone.workplace"
    # The zone's state moves to its new id a moment after the rename; until
    # then the entry waits for it, as for any zone not there yet.
    hass.states.async_remove(office.entity_id)
    hass.states.async_set("zone.workplace", "0", shape)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.zone_id == "zone.workplace"


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
        hass.states.get("sensor.home_eta_expected_home").attributes["missing_members"]
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


# -- migration from 1.x ----------------------------------------------------------


def legacy_person(person: str, options: dict[str, Any]) -> MockConfigEntry:
    """Return a person entry as Home ETA 1.x saved it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=person.split(".")[1].title(),
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON,
            CONF_PERSON: person,
            CONF_ZONE: "zone.home",
        },
        options=options,
        unique_id=f"{person}|zone.home",
        version=1,
        minor_version=1,
    )


def legacy_group(members: list[str], **near: int) -> MockConfigEntry:
    """Return a group entry as Home ETA 1.x saved it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home ETA",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_ZONE: "zone.home"},
        options={CONF_MEMBERS: members, **near},
        unique_id="group|zone.home|home_eta",
        version=1,
        minor_version=1,
    )


LEGACY_OPTIONS: dict[str, Any] = {
    "enable_historical": True,
    "ignored_zones": ["zone.work"],
    "activity_entity": "sensor.iphone_activity",
    "reference_eta_entity": "sensor.waze",
    CONF_ADVANCED: {"min_closing_speed_mps": 0.8, "stale_after_s": 1_200},
}


def register_legacy_entities(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, keys: tuple[str, ...]
) -> list[str]:
    registry = er.async_get(hass)
    return [
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=f"{entry.title}_{key}",
        ).entity_id
        for key in keys
    ]


@pytest.mark.parametrize(
    "group_first", [True, False], ids=["group_first", "person_first"]
)
async def test_migration_from_1_x(
    hass: HomeAssistant, hass_storage: dict[str, Any], group_first: bool
) -> None:
    set_person(hass, ALEX, 5_000)
    set_person(hass, SAM, 6_000)
    alex = legacy_person(ALEX, LEGACY_OPTIONS)
    sam = legacy_person(SAM, {"enable_historical": False, "ignored_zones": []})
    group = legacy_group([ALEX], near_home_radius_m=5_000, near_home_fade_after_min=0)
    entries = [group, alex, sam] if group_first else [alex, sam, group]
    for entry in entries:
        entry.add_to_hass(hass)
    retired = register_legacy_entities(hass, alex, "sensor", LEGACY_PERSON_ENTITIES)
    retired += register_legacy_entities(hass, group, "sensor", LEGACY_GROUP_ENTITIES)
    kept = register_legacy_entities(hass, alex, "sensor", ("minutes_to_home",))
    history = f"{DOMAIN}.history.{alex.entry_id}"
    hass_storage[history] = {"version": 1, "key": history, "data": {"trips": []}}

    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()

    for entry in entries:
        assert entry.state is ConfigEntryState.LOADED
        assert (entry.version, entry.minor_version) == (1, CONFIG_MINOR_VERSION)
    # Alex keeps their changed stale time, and takes the group's near home.
    assert dict(alex.options) == {
        CONF_ADVANCED: {"stale_after_s": 1_200},
        CONF_NEAR_HOME_RADIUS_M: 5_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 0,
    }
    # Sam was in no group: the defaults.
    assert dict(sam.options) == {
        CONF_NEAR_HOME_RADIUS_M: 3_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 90,
    }
    assert dict(group.options) == {CONF_MEMBERS: [ALEX]}
    assert alex.runtime_data.config.near_home.radius_m == 5_000
    assert alex.runtime_data.config.params.stale_after_s == 1_200
    registry = er.async_get(hass)
    for entity_id in retired:
        assert registry.async_get(entity_id) is None, entity_id
    for entity_id in kept:
        assert registry.async_get(entity_id) is not None, entity_id
    assert history not in hass_storage


async def test_migration_falls_back_to_defaults_for_unusable_group_settings(
    hass: HomeAssistant,
) -> None:
    set_person(hass, ALEX, 5_000)
    alex = legacy_person(ALEX, {})
    group = legacy_group([ALEX], near_home_radius_m=50, near_home_fade_after_min=1)
    for entry in (alex, group):
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(alex.entry_id)
    await hass.async_block_till_done()
    assert dict(alex.options) == {
        CONF_NEAR_HOME_RADIUS_M: 3_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 1,
    }


async def test_entry_from_a_newer_release_loads(hass: HomeAssistant) -> None:
    """Minor versions are backwards compatible (a downgrade, then an upgrade)."""
    set_person(hass, ALEX, 5_000)
    newer = MockConfigEntry(
        domain=DOMAIN,
        title="Alex",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON,
            CONF_PERSON: ALEX,
            CONF_ZONE: "zone.home",
        },
        options={"a_future_option": True},
        unique_id=f"{ALEX}|zone.home",
        version=1,
        minor_version=CONFIG_MINOR_VERSION + 1,
    )
    newer.add_to_hass(hass)
    assert await hass.config_entries.async_setup(newer.entry_id)
    await hass.async_block_till_done()
    assert newer.state is ConfigEntryState.LOADED
    assert newer.minor_version == CONFIG_MINOR_VERSION + 1
    assert newer.options == {"a_future_option": True}


async def test_entry_from_an_unknown_older_version_is_refused(
    hass: HomeAssistant,
) -> None:
    set_person(hass, ALEX, 5_000)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alex",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON,
            CONF_PERSON: ALEX,
            CONF_ZONE: "zone.home",
        },
        version=0,
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_entry_from_a_newer_major_version_is_refused(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alex",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON,
            CONF_PERSON: ALEX,
            CONF_ZONE: "zone.home",
        },
        version=2,
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_removing_a_never_migrated_entry_deletes_its_learned_trips(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = legacy_person(ALEX, {"enable_historical": True})
    entry.add_to_hass(hass)
    history = f"{DOMAIN}.history.{entry.entry_id}"
    hass_storage[history] = {"version": 1, "key": history, "data": {"trips": []}}
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert history not in hass_storage
