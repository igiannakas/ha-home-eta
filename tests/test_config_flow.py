"""Tests for the config and options flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest

from custom_components.home_eta.const import (
    CONF_ACTIVITY_ENTITY,
    CONF_ADVANCED,
    CONF_ENABLE_HISTORICAL,
    CONF_ENTRY_TYPE,
    CONF_IGNORED_ZONES,
    CONF_MEMBERS,
    CONF_NEAR_HOME_FADE_AFTER_MIN,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_ZONE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
)
from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person

pytestmark = pytest.mark.usefixtures("zones")


async def start(hass: HomeAssistant, step: str) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["person", "group"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    if result["type"] is FlowResultType.ABORT:
        return result["reason"]
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    return result["flow_id"]


async def test_add_person(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    hass.states.async_set("sensor.iphone_activity", "Walking")
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_PERSON: ALEX,
            CONF_ZONE: "zone.home",
            CONF_ACTIVITY_ENTITY: "sensor.iphone_activity",
            CONF_ENABLE_HISTORICAL: True,
            CONF_IGNORED_ZONES: ["zone.work"],
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alex"
    assert result["data"] == {
        CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON,
        CONF_PERSON: ALEX,
        CONF_ZONE: "zone.home",
    }
    assert result["options"] == {
        CONF_ACTIVITY_ENTITY: "sensor.iphone_activity",
        CONF_ENABLE_HISTORICAL: True,
        CONF_IGNORED_ZONES: ["zone.work"],
    }
    entry = result["result"]
    assert entry.unique_id == f"{ALEX}|zone.home"
    assert hass.states.get("sensor.alex_eta_historical") is not None


async def test_person_to_another_zone_has_zone_in_title(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PERSON: ALEX, CONF_ZONE: "zone.work"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alex to Work"


async def test_person_already_configured(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    person_entry(ALEX).add_to_hass(hass)
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PERSON: ALEX, CONF_ZONE: "zone.home"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_person_validation(hass: HomeAssistant) -> None:
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_PERSON: "person.nobody",
            CONF_ZONE: "zone.home",
            CONF_IGNORED_ZONES: ["zone.home"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_PERSON: "person_invalid",
        CONF_IGNORED_ZONES: "target_ignored",
    }

    set_person(hass, ALEX, 5_000)
    hass.states.async_set("zone.broken", "0", {"latitude": 1.0})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PERSON: ALEX, CONF_ZONE: "zone.broken"}
    )
    assert result["errors"] == {CONF_ZONE: "zone_invalid"}


async def test_group_needs_people(hass: HomeAssistant) -> None:
    assert await start(hass, "group") == "no_people"


async def test_add_group(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    set_person(hass, SAM, 5_000)
    for person in (ALEX, SAM):
        entry = person_entry(person)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
    flow_id = await start(hass, "group")

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Home ETA", CONF_ZONE: "zone.home", CONF_MEMBERS: []}
    )
    assert result["errors"] == {CONF_MEMBERS: "no_members"}
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Home ETA", CONF_ZONE: "zone.work", CONF_MEMBERS: [ALEX]},
    )
    assert result["errors"] == {CONF_MEMBERS: "member_not_tracked"}

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Home ETA", CONF_ZONE: "zone.home", CONF_MEMBERS: [ALEX, SAM]},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home ETA"
    assert result["data"] == {CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_ZONE: "zone.home"}
    # The near-home settings take their defaults.
    assert result["options"] == {
        CONF_MEMBERS: [ALEX, SAM],
        CONF_NEAR_HOME_RADIUS_M: 3_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 90,
    }
    assert hass.states.get("sensor.home_eta_first_arrival") is not None
    assert hass.states.get("sensor.home_eta_expected_home") is not None
    assert hass.states.get("sensor.home_eta_near_home") is not None


async def test_person_options(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    entry = person_entry(ALEX)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENABLE_HISTORICAL: False,
            CONF_IGNORED_ZONES: ["zone.home"],
            CONF_ADVANCED: {"max_gps_accuracy_m": 100},
        },
    )
    assert result["errors"] == {CONF_IGNORED_ZONES: "target_ignored"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENABLE_HISTORICAL: True,
            CONF_IGNORED_ZONES: [],
            CONF_ADVANCED: {
                "max_gps_accuracy_m": 100,
                "min_closing_speed_mps": 0.5,
                "speed_filter_tau_s": 300,
                "arriving_soon_distance_m": 800,
                "stale_after_s": 900,
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENABLE_HISTORICAL] is True
    # Only the changed value is stored; the rest follow the defaults.
    assert entry.options[CONF_ADVANCED] == {"max_gps_accuracy_m": 100}
    # Reloaded with the new options.
    assert hass.states.get("sensor.alex_eta_historical") is not None
    assert entry.runtime_data.config.params.max_gps_accuracy_m == 100


async def test_group_options(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    set_person(hass, SAM, 5_000)
    entries = [person_entry(ALEX), person_entry(SAM), group_entry([ALEX])]
    for entry in entries:
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    group = entries[2]

    result = await hass.config_entries.options.async_init(group.entry_id)
    # An entry made before the near-home settings existed shows the defaults.
    schema = result["data_schema"].schema
    suggested = {
        str(key): key.description["suggested_value"]
        for key in schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested == {
        CONF_MEMBERS: [ALEX],
        CONF_NEAR_HOME_RADIUS_M: 3_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 90,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MEMBERS: []}
    )
    assert result["errors"] == {CONF_MEMBERS: "no_members"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MEMBERS: [ALEX, SAM],
            CONF_NEAR_HOME_RADIUS_M: 5_000,
            CONF_NEAR_HOME_FADE_AFTER_MIN: 0,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert group.options[CONF_MEMBERS] == [ALEX, SAM]
    assert group.runtime_data.members == (ALEX, SAM)
    config = group.runtime_data.near_config
    assert config.radius_m == 5_000
    assert config.fade_after_s is None
    near = hass.states.get("sensor.home_eta_near_home")
    assert near.attributes["radius_m"] == 5_000
    assert near.attributes["fade_after_min"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_NEAR_HOME_RADIUS_M, 999),
        (CONF_NEAR_HOME_RADIUS_M, 30_001),
        (CONF_NEAR_HOME_FADE_AFTER_MIN, -5),
        (CONF_NEAR_HOME_FADE_AFTER_MIN, 721),
    ],
)
async def test_group_options_out_of_range(
    hass: HomeAssistant, field: str, value: int
) -> None:
    set_person(hass, ALEX, 5_000)
    entries = [person_entry(ALEX), group_entry([ALEX])]
    for entry in entries:
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entries[1].entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MEMBERS: [ALEX], field: value}
        )


async def test_duplicate_group_aborts(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    entry = person_entry(ALEX)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        flow_id = await start(hass, "group")
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_NAME: "Home ETA", CONF_ZONE: "zone.home", CONF_MEMBERS: [ALEX]},
        )
        await hass.async_block_till_done()
        assert result["type"] is expected
    assert result["reason"] == "group_exists"


ADVANCED_DEFAULTS = {
    "max_gps_accuracy_m": 150,
    "min_closing_speed_mps": 0.5,
    "speed_filter_tau_s": 300,
    "arriving_soon_distance_m": 800,
    "stale_after_s": 900,
}


async def open_person_options(hass: HomeAssistant) -> tuple[str, object]:
    set_person(hass, ALEX, 5_000)
    entry = person_entry(ALEX)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return result["flow_id"], entry


async def test_slowest_speed_cannot_reach_the_stopped_speed(
    hass: HomeAssistant,
) -> None:
    """Regression: 0.2 m/s was accepted by the form and broke the entry."""
    flow_id, _ = await open_person_options(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_ENABLE_HISTORICAL: False,
                CONF_IGNORED_ZONES: [],
                CONF_ADVANCED: {**ADVANCED_DEFAULTS, "min_closing_speed_mps": 0.2},
            },
        )


async def test_unusable_advanced_combination_is_a_form_error(
    hass: HomeAssistant,
) -> None:
    flow_id, entry = await open_person_options(hass)
    with patch(
        "custom_components.home_eta.config_flow.params_from_options",
        side_effect=ValueError("x must be below y"),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_ENABLE_HISTORICAL: False,
                CONF_IGNORED_ZONES: [],
                CONF_ADVANCED: ADVANCED_DEFAULTS,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_advanced"}
    assert result["description_placeholders"] == {"error": "x must be below y"}
    assert CONF_ADVANCED not in entry.options


async def test_untouched_advanced_values_follow_the_defaults(
    hass: HomeAssistant,
) -> None:
    """Regression: saving the form once pinned every advanced default."""
    flow_id, entry = await open_person_options(hass)
    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_ENABLE_HISTORICAL: False,
            CONF_IGNORED_ZONES: [],
            CONF_ADVANCED: ADVANCED_DEFAULTS,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_ADVANCED not in entry.options


async def test_group_options_drop_members_no_longer_tracked(
    hass: HomeAssistant,
) -> None:
    set_person(hass, ALEX, 5_000)
    alex = person_entry(ALEX)
    group = group_entry([ALEX, SAM])
    for entry in (alex, group):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(group.entry_id)
    schema = result["data_schema"].schema
    member_key = next(key for key in schema if key == CONF_MEMBERS)
    assert member_key.description == {"suggested_value": [ALEX]}
