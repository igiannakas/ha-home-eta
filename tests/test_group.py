"""Tests for the group: the first expected, anyone arriving soon, the nearest."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_eta.const import CONF_NEAR_HOME_RADIUS_M
from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person, step

EXPECTED = "sensor.home_eta_expected_home"
MINUTES = "sensor.home_eta_minutes_to_home"
NEAR = "sensor.home_eta_near_home"
SOMEONE_SOON = "binary_sensor.home_eta_someone_arriving_soon"
STATUS = "sensor.home_eta_status"
ALL = (STATUS, EXPECTED, MINUTES, NEAR, SOMEONE_SOON)

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
    status = hass.states.get(STATUS)
    assert status.state == "home"
    assert status.attributes["persons_home"] == [SAM]
    assert status.attributes["persons_away"] == [ALEX]

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
    # Sam is still home: the group says so.
    assert hass.states.get(STATUS).state == "home"
    assert hass.states.get(STATUS).attributes["persons_heading_home"] == [ALEX]

    # Sam goes out; nobody is home and both are far.
    await step(hass, freezer, SAM, 5_000)
    near = hass.states.get(NEAR)
    assert near.state == "0"
    assert near.attributes["persons_home"] == []
    status = hass.states.get(STATUS)
    assert status.state == "heading_home"
    assert status.attributes["persons_away"] == [SAM]

    # Sam turns round and is due before Alex, and arriving soon.
    for east in (4_000, 3_000, 2_000):
        await step(hass, freezer, SAM, east)
    expected = hass.states.get(EXPECTED)
    assert expected.attributes["person"] == SAM
    assert [m["person"] for m in expected.attributes["heading_home"]] == [SAM, ALEX]
    assert hass.states.get(SOMEONE_SOON).state == STATE_ON
    assert hass.states.get(SOMEONE_SOON).attributes["persons"] == [SAM]
    assert hass.states.get(SOMEONE_SOON).attributes["names"] == ["Sam"]
    status = hass.states.get(STATUS)
    assert status.state == "arriving_soon"
    assert status.attributes["persons_arriving_soon"] == [SAM]
    # 2 km: (3000 - 2000) / (3000 - 800) = 45 %.
    near = hass.states.get(NEAR)
    assert near.state == "45"
    assert near.attributes["person"] == SAM
    assert near.attributes["person_name"] == "Sam"


async def test_near_home_uses_each_members_own_radius(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 4_000)
    set_person(hass, SAM, 2_000)
    await setup_entries(
        hass,
        person_entry(ALEX, options={CONF_NEAR_HOME_RADIUS_M: 8_000}),
        person_entry(SAM),
        group_entry([ALEX, SAM]),
    )
    # Alex: (8000 - 4000) / 7200 = 56 %; Sam, nearer, by 3 km: 45 %.
    near = hass.states.get(NEAR)
    assert near.state == "56"
    assert near.attributes["person"] == ALEX
    assert near.attributes["distance_m"] == 4_000


async def test_missing_members_reported(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, group_entry([ALEX, SAM]))
    assert hass.states.get(EXPECTED).attributes["missing_members"] == [SAM]
    # Sam could be the one home or arriving soon: the group cannot tell.
    assert hass.states.get(SOMEONE_SOON).state == STATE_UNKNOWN
    assert hass.states.get(STATUS).state == STATE_UNKNOWN
    assert hass.states.get(STATUS).attributes["persons_away"] == [ALEX]

    await hass.config_entries.async_unload(alex.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(EXPECTED).attributes["missing_members"] == [ALEX, SAM]
    # With no member reporting, the group cannot say nobody is arriving.
    assert hass.states.get(SOMEONE_SOON).state == STATE_UNKNOWN
    status = hass.states.get(STATUS)
    assert status.state == STATE_UNKNOWN
    assert status.attributes["missing_members"] == [ALEX, SAM]
    assert hass.states.get(NEAR).state == STATE_UNKNOWN


async def test_a_member_reloading_never_shows_a_wrong_status(
    hass: HomeAssistant,
) -> None:
    """Regression: home -> away -> home while the member at home reloaded."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    set_person(hass, SAM, 5_000)
    alex = person_entry(ALEX)
    await setup_entries(hass, alex, person_entry(SAM), group_entry([ALEX, SAM]))
    assert hass.states.get(STATUS).state == "home"
    assert hass.states.get(SOMEONE_SOON).state == STATE_OFF
    seen: list[tuple[str, str]] = []

    @callback
    def listener(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if event.data["entity_id"] in (STATUS, SOMEONE_SOON) and new is not None:
            seen.append((event.data["entity_id"], new.state))

    hass.bus.async_listen(EVENT_STATE_CHANGED, listener)
    await hass.config_entries.async_reload(alex.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "home"
    assert (STATUS, "away") not in seen
    assert (SOMEONE_SOON, STATE_ON) not in seen
    assert hass.states.get(SOMEONE_SOON).state == STATE_OFF

    # A member at home settles it even while another has no data.
    hass.states.async_remove(SAM)
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "home"
    assert hass.states.get(SOMEONE_SOON).state == STATE_UNKNOWN


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
