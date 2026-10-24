"""Tests for the config and options flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest

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
from custom_components.home_eta.migration import CONFIG_MINOR_VERSION
from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person

pytestmark = pytest.mark.usefixtures("zones")

NEAR_DEFAULTS = {CONF_NEAR_HOME_RADIUS_M: 3_000, CONF_NEAR_HOME_FADE_AFTER_MIN: 90}
ADVANCED_DEFAULTS = {
    "max_gps_accuracy_m": 150,
    "speed_filter_tau_s": 300,
    "arriving_soon_distance_m": 800,
    "stale_after_s": 900,
}


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


def suggested(result: dict[str, object]) -> dict[str, object]:
    """Return the suggested values of a form, by field."""
    schema = result["data_schema"].schema  # type: ignore[attr-defined]
    return {
        str(key): key.description["suggested_value"]
        for key in schema
        if key.description and "suggested_value" in key.description
    }


async def test_add_person(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_PERSON: ALEX,
            CONF_ZONE: "zone.home",
            CONF_NEAR_HOME_RADIUS_M: 4_000.0,
            CONF_NEAR_HOME_FADE_AFTER_MIN: 60.0,
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
        CONF_NEAR_HOME_RADIUS_M: 4_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 60,
    }
    entry = result["result"]
    assert entry.unique_id == f"{ALEX}|zone.home"
    assert entry.minor_version == CONFIG_MINOR_VERSION
    assert hass.states.get("sensor.alex_near_home").attributes["radius_m"] == 4_000


async def test_person_near_home_takes_the_defaults(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PERSON: ALEX, CONF_ZONE: "zone.home"}
    )
    assert result["options"] == NEAR_DEFAULTS


async def test_person_without_a_name_is_titled_by_entity_id(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(ALEX, "not_home", {})
    flow_id = await start(hass, "person")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PERSON: ALEX, CONF_ZONE: "zone.home"}
    )
    assert result["title"] == ALEX


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
    hass.states.async_set("zone.broken", "0", {"latitude": 1.0})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PERSON: "person.nobody", CONF_ZONE: "zone.broken"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_PERSON: "person_invalid",
        CONF_ZONE: "zone_invalid",
    }
    # What was entered is offered again.
    assert suggested(result)[CONF_PERSON] == "person.nobody"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_NEAR_HOME_RADIUS_M, 999),
        (CONF_NEAR_HOME_RADIUS_M, 30_001),
        (CONF_NEAR_HOME_FADE_AFTER_MIN, -5),
        (CONF_NEAR_HOME_FADE_AFTER_MIN, 721),
    ],
)
async def test_person_near_home_out_of_range(
    hass: HomeAssistant, field: str, value: int
) -> None:
    set_person(hass, ALEX, 5_000)
    flow_id = await start(hass, "person")
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            flow_id, {CONF_PERSON: ALEX, CONF_ZONE: "zone.home", field: value}
        )


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
        {CONF_NAME: "Home ETA", CONF_ZONE: "zone.nowhere", CONF_MEMBERS: [ALEX]},
    )
    assert result["errors"] == {
        CONF_ZONE: "zone_invalid",
        CONF_MEMBERS: "member_not_tracked",
    }

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Home ETA", CONF_ZONE: "zone.home", CONF_MEMBERS: [ALEX, SAM]},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home ETA"
    assert result["data"] == {CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_ZONE: "zone.home"}
    assert result["options"] == {CONF_MEMBERS: [ALEX, SAM]}
    for entity_id in (
        "sensor.home_eta_expected_home",
        "sensor.home_eta_minutes_to_home",
        "sensor.home_eta_near_home",
        "binary_sensor.home_eta_someone_arriving_soon",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


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


async def open_person_options(
    hass: HomeAssistant, options: dict[str, object] | None = None
) -> tuple[dict[str, object], object]:
    set_person(hass, ALEX, 5_000)
    entry = person_entry(ALEX, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    return result, entry


async def test_person_options(hass: HomeAssistant) -> None:
    result, entry = await open_person_options(
        hass, {**NEAR_DEFAULTS, CONF_NEAR_HOME_RADIUS_M: 4_000}
    )
    assert suggested(result) == {
        CONF_NEAR_HOME_RADIUS_M: 4_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 90,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NEAR_HOME_RADIUS_M: 5_000,
            CONF_NEAR_HOME_FADE_AFTER_MIN: 0,
            CONF_ADVANCED: {**ADVANCED_DEFAULTS, "max_gps_accuracy_m": 100},
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Only the changed advanced value is stored; the rest follow the defaults.
    assert dict(entry.options) == {  # type: ignore[attr-defined]
        CONF_NEAR_HOME_RADIUS_M: 5_000,
        CONF_NEAR_HOME_FADE_AFTER_MIN: 0,
        CONF_ADVANCED: {"max_gps_accuracy_m": 100},
    }
    # Reloaded with the new options.
    runtime = entry.runtime_data  # type: ignore[attr-defined]
    assert runtime.config.params.max_gps_accuracy_m == 100
    assert runtime.config.near_home.radius_m == 5_000
    assert runtime.config.near_home.fade_after_s is None
    near = hass.states.get("sensor.alex_near_home")
    assert near.attributes["radius_m"] == 5_000
    assert near.attributes["fade_after_min"] is None


async def test_unusable_advanced_combination_is_a_form_error(
    hass: HomeAssistant,
) -> None:
    result, entry = await open_person_options(hass)
    with patch(
        "custom_components.home_eta.config_flow.params_from_options",
        side_effect=ValueError("x must be below y"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {**NEAR_DEFAULTS, CONF_ADVANCED: ADVANCED_DEFAULTS},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_advanced"}
    assert result["description_placeholders"] == {"error": "x must be below y"}
    assert CONF_ADVANCED not in entry.options  # type: ignore[attr-defined]


async def test_untouched_advanced_values_follow_the_defaults(
    hass: HomeAssistant,
) -> None:
    """Regression: saving the form once pinned every advanced default."""
    result, entry = await open_person_options(hass)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**NEAR_DEFAULTS, CONF_ADVANCED: ADVANCED_DEFAULTS}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == NEAR_DEFAULTS  # type: ignore[attr-defined]


async def test_stale_time_cannot_go_below_the_stationary_time(
    hass: HomeAssistant,
) -> None:
    result, _ = await open_person_options(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **NEAR_DEFAULTS,
                CONF_ADVANCED: {**ADVANCED_DEFAULTS, "stale_after_s": 300},
            },
        )


async def test_group_options(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    set_person(hass, SAM, 5_000)
    entries = [person_entry(ALEX), person_entry(SAM), group_entry([ALEX])]
    for entry in entries:
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    group = entries[2]

    result = await hass.config_entries.options.async_init(group.entry_id)
    assert suggested(result) == {CONF_MEMBERS: [ALEX]}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MEMBERS: []}
    )
    assert result["errors"] == {CONF_MEMBERS: "no_members"}
    assert suggested(result) == {CONF_MEMBERS: []}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MEMBERS: [ALEX, SAM]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(group.options) == {CONF_MEMBERS: [ALEX, SAM]}
    assert group.runtime_data.members == (ALEX, SAM)


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
    assert suggested(result) == {CONF_MEMBERS: [ALEX]}
