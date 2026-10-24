"""Tests for the group: the first expected, anyone arriving soon, the nearest."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person, step

EXPECTED = "sensor.home_eta_expected_home"
MINUTES = "sensor.home_eta_minutes_to_home"
NEAR = "sensor.home_eta_near_home"
SOMEONE_SOON = "binary_sensor.home_eta_someone_arriving_soon"
ALL = (EXPECTED, MINUTES, NEAR, SOMEONE_SOON)

pytestmark = pytest.mark.usefixtures("zones")


async def setup_entries(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    """Add the entries; setting up the first loads the domain and all of them."""
    for entry in entries:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    for entry in entries:
        assert entry.state is ConfigEntryState.LOADED


async def test_group_entities(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    group = group_entry([ALEX])
    await setup_entries(hass, person_entry(ALEX), group)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, group.entry_id)
    assert sorted(e.entity_id for e in entries) == sorted(ALL)
    device = dr.async_get(hass).async_get(registry.async_get(EXPECTED).device_id)
    assert device is not None
    assert device.name == "Home ETA"
    assert device.model == "Group"


async def test_group_picks_the_first_expected_and_the_nearest(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 30_000)
    set_person(hass, SAM, None, zones=("zone.home",))
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    expected = hass.states.get(EXPECTED)
    assert expected.state == STATE_UNKNOWN
    assert expected.attributes["persons_home"] == [SAM]
    assert expected.attributes["heading_home"] == []
    assert hass.states.get(MINUTES).state == STATE_UNKNOWN
    assert hass.states.get(SOMEONE_SOON).state == STATE_OFF
    near = hass.states.get(NEAR)
    assert near.state == "100"
    assert near.attributes["person"] == SAM
    assert near.attributes["persons_home"] == [SAM]

    # Alex heads home from 30 km.
    await step(hass, freezer, ALEX, 26_000, seconds=300)
    await step(hass, freezer, ALEX, 22_000, seconds=300)
    expected = hass.states.get(EXPECTED)
    assert dt_util.parse_datetime(expected.state) is not None
    assert expected.attributes["person"] == ALEX
    assert expected.attributes["person_name"] == "Alex"
    assert [m["person"] for m in expected.attributes["heading_home"]] == [ALEX]
    minutes = int(hass.states.get(MINUTES).state)
    assert minutes == expected.attributes["heading_home"][0]["minutes"]
    assert minutes > 15
    assert hass.states.get("sensor.alex_minutes_to_home").state == str(minutes)

    # Sam goes out; nobody is home and both are far.
    await step(hass, freezer, SAM, 5_000)
    near = hass.states.get(NEAR)
    assert near.state == "0"
    assert near.attributes["persons_home"] == []

    # Sam turns round and is due before Alex, and arriving soon.
    for east in (4_000, 3_000, 2_000):
        await step(hass, freezer, SAM, east)
    expected = hass.states.get(EXPECTED)
    assert expected.attributes["person"] == SAM
    assert [m["person"] for m in expected.attributes["heading_home"]] == [SAM, ALEX]
    assert hass.states.get(SOMEONE_SOON).state == STATE_ON
    assert hass.states.get(SOMEONE_SOON).attributes["persons"] == [SAM]
    assert hass.states.get(SOMEONE_SOON).attributes["names"] == ["Sam"]
    # 2 km: (3000 - 2000) / (3000 - 800) = 45 %.
    near = hass.states.get(NEAR)
    assert near.state == "45"
    assert near.attributes["person"] == SAM
    assert near.attributes["person_name"] == "Sam"


async def test_missing_members_reported(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, group_entry([ALEX, SAM]))
    assert hass.states.get(EXPECTED).attributes["missing_members"] == [SAM]
    assert hass.states.get(SOMEONE_SOON).state == STATE_OFF

    await hass.config_entries.async_unload(alex.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(EXPECTED).attributes["missing_members"] == [ALEX, SAM]
    # With no member reporting, the group cannot say nobody is arriving.
    assert hass.states.get(SOMEONE_SOON).state == STATE_UNKNOWN
    assert hass.states.get(NEAR).state == STATE_UNKNOWN


async def test_member_without_a_location_yet_is_missing(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    hass.states.async_set(SAM, STATE_UNKNOWN, {})
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    assert hass.states.get(EXPECTED).attributes["missing_members"] == [SAM]
    assert hass.states.get(NEAR).attributes["person"] == ALEX


async def test_group_ignores_other_zones(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    await setup_entries(
        hass,
        person_entry(ALEX, zone="zone.work", title="Alex to Work"),
        group_entry([ALEX]),
    )
    assert hass.states.get(EXPECTED).attributes["missing_members"] == [ALEX]
