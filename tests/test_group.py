"""Tests for the first-arrival group."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person, step

FIRST = "sensor.home_eta_first_arrival"
FIRST_PERSON = "sensor.home_eta_first_arrival_person"
FIRST_MINUTES = "sensor.home_eta_first_arrival_minutes"
SOMEONE_SOON = "binary_sensor.home_eta_someone_arriving_soon"

pytestmark = pytest.mark.usefixtures("zones")


async def setup_entries(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    """Add the entries; setting up the first loads the domain and all of them."""
    for entry in entries:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    for entry in entries:
        assert entry.state is ConfigEntryState.LOADED


async def test_first_arrival_picks_earliest(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 30_000)
    set_person(hass, SAM, None, zones=("zone.home",))
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    assert hass.states.get(FIRST).state == STATE_UNKNOWN
    assert hass.states.get(FIRST_PERSON).state == STATE_UNKNOWN
    assert hass.states.get(FIRST).attributes["persons_home"] == [SAM]
    assert hass.states.get(SOMEONE_SOON).state == STATE_OFF

    for east in (29_400, 28_800, 28_200):
        await step(hass, freezer, ALEX, east)
    first = hass.states.get(FIRST)
    assert first.attributes["person"] == ALEX
    assert first.attributes["person_name"] == "Alex"
    assert hass.states.get(FIRST_PERSON).state == "Alex"
    assert hass.states.get(FIRST_PERSON).attributes["person_entity_id"] == ALEX
    assert dt_util.parse_datetime(first.state) is not None
    assert int(hass.states.get(FIRST_MINUTES).state) > 15

    # Sam leaves, turns round and comes back faster from closer by.
    await step(hass, freezer, SAM, 6_000)
    for east in (5_000, 4_000, 3_000):
        await step(hass, freezer, SAM, east)
    first = hass.states.get(FIRST)
    assert first.attributes["person"] == SAM
    assert [c["person"] for c in first.attributes["candidates"]] == [SAM, ALEX]
    assert hass.states.get(SOMEONE_SOON).state == STATE_ON
    assert hass.states.get(SOMEONE_SOON).attributes["persons"] == [SAM]
    assert hass.states.get(SOMEONE_SOON).attributes["names"] == ["Sam"]


async def test_missing_members_reported(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, group_entry([ALEX, SAM]))
    assert hass.states.get(FIRST).attributes["missing_members"] == [SAM]

    await hass.config_entries.async_unload(alex.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(FIRST).attributes["missing_members"] == [ALEX, SAM]
    # With no member reporting, the group cannot say nobody is arriving.
    assert hass.states.get(SOMEONE_SOON).state == STATE_UNKNOWN


async def test_group_ignores_other_zones(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    await setup_entries(
        hass,
        person_entry(ALEX, zone="zone.work", title="Alex to Work"),
        group_entry([ALEX]),
    )
    assert hass.states.get(FIRST).attributes["missing_members"] == [ALEX]
