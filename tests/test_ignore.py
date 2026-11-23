"""Tests for the members a group ignores: a phone that went silent at home."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    STATE_OFF,
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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.home_eta.const import (
    CONF_MEMBERS,
    DOMAIN,
    EVENT_IGNORED_CHANGED,
    SERVICE_IGNORE_MEMBERS_AT_HOME,
    SERVICE_STOP_IGNORING,
    IgnoreCause,
    IgnoreReason,
)
from custom_components.home_eta.runtime import GroupRuntime, is_new_report
from tests.helpers import ALEX, SAM, group_entry, person_entry, set_person, step

KIM = "person.kim"

EXPECTED = "sensor.home_eta_expected_home"
MINUTES = "sensor.home_eta_minutes_to_home"
NEAR = "sensor.home_eta_near_home"
STATUS = "sensor.home_eta_status"
SOMEONE_HOME = "binary_sensor.home_eta_someone_home"
SOMEONE_SOON = "binary_sensor.home_eta_someone_arriving_soon"
SOMEONE_CLOSE = "binary_sensor.home_eta_someone_close_to_home"
SOMEONE_NEARBY = "binary_sensor.home_eta_someone_nearby"
GROUP_ENTITIES = (
    STATUS,
    EXPECTED,
    MINUTES,
    NEAR,
    SOMEONE_HOME,
    SOMEONE_SOON,
    SOMEONE_CLOSE,
    SOMEONE_NEARBY,
)
IGNORE_ALEX = "switch.home_eta_ignore_alex"
IGNORE_SAM = "switch.home_eta_ignore_sam"
IGNORE_KIM = "switch.home_eta_ignore_kim"

pytestmark = pytest.mark.usefixtures("zones")


async def setup_entries(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    """Add the entries; setting up the first loads the domain and all of them."""
    for entry in entries:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    for entry in entries:
        assert entry.state is ConfigEntryState.LOADED


def at_home(hass: HomeAssistant, person: str, east_m: float | None = None) -> None:
    """Set a person home, with a position ``east_m`` east of the centre if given."""
    set_person(hass, person, east_m, zones=("zone.home",))


def store_key(group: MockConfigEntry) -> str:
    return f"{DOMAIN}.ignored.{group.entry_id}"


def stored(person: str, reason: str = "away_pressed") -> dict[str, Any]:
    return {person: {"reason": reason, "since": "2026-11-23T16:09:10+00:00"}}


async def ignore(hass: HomeAssistant, **data: Any) -> dict[str, Any] | None:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IGNORE_MEMBERS_AT_HOME,
        data,
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    return response


async def stop_ignoring(hass: HomeAssistant, **data: Any) -> dict[str, Any] | None:
    response = await hass.services.async_call(
        DOMAIN, SERVICE_STOP_IGNORING, data, blocking=True, return_response=True
    )
    await hass.async_block_till_done()
    return response


async def switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def alex_out_sam_home(hass: HomeAssistant) -> MockConfigEntry:
    """Alex 30 km out, Sam at home: the 23 Nov 2026 case with two people."""
    set_person(hass, ALEX, 30_000)
    at_home(hass, SAM)
    group = group_entry([ALEX, SAM])
    await setup_entries(hass, person_entry(ALEX), person_entry(SAM), group)
    return group


def state(hass: HomeAssistant, entity_id: str) -> str:
    found = hass.states.get(entity_id)
    assert found is not None, entity_id
    return found.state


# -- what is a report -------------------------------------------------------


def person_state(state_: str, **attributes: Any) -> State:
    return State(ALEX, state_, attributes)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (None, person_state("home"), False),
        (person_state("home"), None, False),
        (person_state("unknown"), person_state("home"), False),
        (person_state("unavailable"), person_state("home"), False),
        (person_state("home"), person_state("unavailable"), False),
        (person_state("home"), person_state("not_home"), True),
        (
            person_state("home", latitude=51.0, longitude=0.1, gps_accuracy=10),
            person_state("home", latitude=51.0001, longitude=0.1, gps_accuracy=10),
            True,
        ),
        (
            person_state("home", latitude=51.0, longitude=0.1, gps_accuracy=10),
            person_state("home", latitude=51.0, longitude=0.1, gps_accuracy=12),
            True,
        ),
        (
            person_state("home", latitude=51.0, friendly_name="Alex"),
            person_state("home", latitude=51.0, friendly_name="Alexander"),
            False,
        ),
        (
            person_state("home", device_trackers=["device_tracker.a"]),
            person_state("home", device_trackers=["device_tracker.b"]),
            False,
        ),
    ],
)
def test_what_is_a_new_report(
    old: State | None, new: State | None, expected: bool
) -> None:
    assert is_new_report(old, new) is expected


# -- the group without the ignored ---------------------------------------------


async def test_ignoring_the_only_member_at_home(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    group = await alex_out_sam_home(hass)
    assert state(hass, STATUS) == "home"
    assert state(hass, SOMEONE_HOME) == STATE_ON
    assert hass.states.get(SOMEONE_HOME).attributes["persons"] == [SAM]
    assert hass.states.get(SOMEONE_HOME).attributes["names"] == ["Sam"]
    assert state(hass, SOMEONE_CLOSE) == STATE_ON
    assert state(hass, SOMEONE_NEARBY) == STATE_ON
    assert state(hass, IGNORE_SAM) == STATE_OFF

    response = await ignore(hass, config_entry_id=group.entry_id)
    assert response == {
        "persons": [SAM],
        "names": ["Sam"],
        "ignored": [SAM],
        "ignored_names": ["Sam"],
    }
    assert state(hass, STATUS) == "away"
    status = hass.states.get(STATUS).attributes
    assert status["persons_home"] == []
    assert status["persons_away"] == [ALEX]
    assert status["missing_members"] == []
    assert state(hass, SOMEONE_HOME) == STATE_OFF
    assert state(hass, SOMEONE_CLOSE) == STATE_OFF
    assert state(hass, SOMEONE_NEARBY) == STATE_OFF
    assert state(hass, SOMEONE_SOON) == STATE_OFF
    assert state(hass, NEAR) == "0"
    assert hass.states.get(NEAR).attributes["person"] == ALEX
    assert state(hass, IGNORE_SAM) == STATE_ON
    # Ignoring is the group's: Sam's own entities still say home.
    assert state(hass, "sensor.sam_status") == "home"
    assert state(hass, "binary_sensor.sam_close_to_home") == STATE_ON

    # The expected arrival is Alex's.
    await step(hass, freezer, ALEX, 26_000, seconds=300)
    await step(hass, freezer, ALEX, 22_000, seconds=300)
    expected = hass.states.get(EXPECTED)
    assert dt_util.parse_datetime(expected.state) is not None
    assert expected.attributes["person"] == ALEX
    assert expected.attributes["persons_home"] == []
    assert state(hass, STATUS) == "heading_home"
    # Alex's reports on the way are not arrivals: Sam is still ignored.
    assert state(hass, IGNORE_SAM) == STATE_ON


async def test_every_member_ignored_or_away_is_away_never_unknown(
    hass: HomeAssistant,
) -> None:
    at_home(hass, ALEX)
    at_home(hass, SAM)
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    response = await ignore(hass)
    assert response is not None
    assert response["persons"] == [ALEX, SAM]
    assert state(hass, STATUS) == "away"
    assert state(hass, SOMEONE_HOME) == STATE_OFF
    assert state(hass, SOMEONE_CLOSE) == STATE_OFF
    assert state(hass, SOMEONE_NEARBY) == STATE_OFF
    assert state(hass, SOMEONE_SOON) == STATE_OFF
    assert state(hass, NEAR) == "0"
    assert hass.states.get(NEAR).attributes["person"] is None
    # Nobody heading home: unknown, as without ignoring.
    assert state(hass, EXPECTED) == STATE_UNKNOWN


async def test_an_ignored_member_without_data_is_not_missing(
    hass: HomeAssistant,
) -> None:
    """Sam has no entry loaded: unknown, until Sam is ignored."""
    set_person(hass, ALEX, 30_000)
    hass.states.async_set(SAM, STATE_UNKNOWN, {"friendly_name": "Sam"})
    await setup_entries(hass, person_entry(ALEX), group_entry([ALEX, SAM]))
    assert state(hass, STATUS) == STATE_UNKNOWN
    assert state(hass, SOMEONE_HOME) == STATE_UNKNOWN
    assert state(hass, SOMEONE_CLOSE) == STATE_UNKNOWN
    # The switch is there and named, though Sam has no data.
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert hass.states.get(IGNORE_SAM).attributes["status"] is None

    await switch(hass, IGNORE_SAM, True)
    assert state(hass, STATUS) == "away"
    assert hass.states.get(STATUS).attributes["missing_members"] == []
    assert state(hass, SOMEONE_HOME) == STATE_OFF
    assert state(hass, SOMEONE_CLOSE) == STATE_OFF
    assert state(hass, SOMEONE_NEARBY) == STATE_OFF
    assert state(hass, SOMEONE_SOON) == STATE_OFF
    assert hass.states.get(EXPECTED).attributes["missing_members"] == []


async def test_a_member_counted_without_data_still_makes_it_unknown(
    hass: HomeAssistant,
) -> None:
    """Ignoring Sam at home leaves Alex, who has no data: that is unknown."""
    at_home(hass, SAM)
    await setup_entries(hass, person_entry(SAM), group_entry([ALEX, SAM]))
    assert state(hass, SOMEONE_HOME) == STATE_ON
    await ignore(hass, members=[SAM])
    assert state(hass, STATUS) == STATE_UNKNOWN
    assert state(hass, SOMEONE_HOME) == STATE_UNKNOWN
    assert hass.states.get(STATUS).attributes["missing_members"] == [ALEX]


async def test_every_group_entity_says_who_is_ignored(hass: HomeAssistant) -> None:
    await alex_out_sam_home(hass)
    for entity_id in GROUP_ENTITIES:
        attributes = hass.states.get(entity_id).attributes
        assert attributes["ignored"] == [], entity_id
        assert attributes["ignored_names"] == [], entity_id
    await ignore(hass)
    for entity_id in GROUP_ENTITIES:
        attributes = hass.states.get(entity_id).attributes
        assert attributes["ignored"] == [SAM], entity_id
        assert attributes["ignored_names"] == ["Sam"], entity_id


# -- the switches ----------------------------------------------------------------


async def test_switch_round_trip(hass: HomeAssistant) -> None:
    await alex_out_sam_home(hass)
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)
    registry = er.async_get(hass)
    assert registry.async_get(IGNORE_SAM) is not None
    assert registry.async_get(IGNORE_ALEX) is not None
    off = hass.states.get(IGNORE_SAM)
    assert off.attributes["person"] == SAM
    assert off.attributes["since"] is None
    assert off.attributes["reason"] is None
    assert off.attributes["status"] == "home"
    assert off.attributes["friendly_name"] == "Home ETA Ignore Sam"

    await switch(hass, IGNORE_SAM, True)
    on = hass.states.get(IGNORE_SAM)
    assert on.state == STATE_ON
    assert on.attributes["reason"] == "by_hand"
    assert dt_util.parse_datetime(on.attributes["since"]) is not None
    # The member's own status, ignored or not.
    assert on.attributes["status"] == "home"
    assert state(hass, SOMEONE_HOME) == STATE_OFF
    assert [event.data["cause"] for event in events] == ["switch"]
    assert events[0].data["added"] == [SAM]
    assert events[0].data["reason"] == "by_hand"

    # Turning it on again changes nothing, not even since when.
    await switch(hass, IGNORE_SAM, True)
    assert hass.states.get(IGNORE_SAM).attributes["since"] == on.attributes["since"]
    assert len(events) == 1

    await switch(hass, IGNORE_SAM, False)
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert state(hass, SOMEONE_HOME) == STATE_ON
    assert len(events) == 2
    assert events[1].data["removed"] == [SAM]
    assert events[1].data["cause"] == "switch"
    assert events[1].data["ignored"] == []


async def test_ignoring_a_member_who_is_out(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Allowed by hand; their next report, wherever they are, ends it."""
    await alex_out_sam_home(hass)
    await switch(hass, IGNORE_ALEX, True)
    assert hass.states.get(STATUS).attributes["persons_away"] == []
    assert state(hass, STATUS) == "home"
    await step(hass, freezer, ALEX, 29_000)
    assert state(hass, IGNORE_ALEX) == STATE_OFF
    assert hass.states.get(STATUS).attributes["persons_away"] == [ALEX]


# -- the actions ---------------------------------------------------------------------


async def test_nobody_at_home_is_nothing_to_do(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 30_000)
    set_person(hass, SAM, 5_000)
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)
    response = await ignore(hass)
    assert response == {
        "persons": [],
        "names": [],
        "ignored": [],
        "ignored_names": [],
    }
    assert events == []
    assert state(hass, IGNORE_ALEX) == STATE_OFF
    assert state(hass, IGNORE_SAM) == STATE_OFF


async def test_members_filter_and_reason(hass: HomeAssistant) -> None:
    at_home(hass, ALEX)
    at_home(hass, SAM)
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    response = await ignore(hass, members=[SAM], reason="stale")
    assert response is not None
    assert response["persons"] == [SAM]
    assert hass.states.get(IGNORE_SAM).attributes["reason"] == "stale"
    assert state(hass, IGNORE_ALEX) == STATE_OFF
    assert state(hass, SOMEONE_HOME) == STATE_ON
    assert hass.states.get(SOMEONE_HOME).attributes["persons"] == [ALEX]

    # Already ignored: nothing new, and the reason stays.
    response = await ignore(hass, members=SAM, reason="away_pressed")
    assert response is not None
    assert response["persons"] == []
    assert response["ignored"] == [SAM]
    assert hass.states.get(IGNORE_SAM).attributes["reason"] == "stale"

    # The rest of those at home.
    response = await ignore(hass, reason="away_pressed")
    assert response is not None
    assert response["persons"] == [ALEX]
    assert response["ignored"] == [ALEX, SAM]
    assert hass.states.get(IGNORE_ALEX).attributes["reason"] == "away_pressed"

    response = await stop_ignoring(hass, members=[ALEX])
    assert response == {
        "persons": [ALEX],
        "names": ["Alex"],
        "ignored": [SAM],
        "ignored_names": ["Sam"],
    }
    response = await stop_ignoring(hass)
    assert response is not None
    assert response["persons"] == [SAM]
    assert response["ignored"] == []
    response = await stop_ignoring(hass)
    assert response is not None
    assert response["persons"] == []


async def test_the_actions_refuse_what_they_cannot_do(hass: HomeAssistant) -> None:
    alex = person_entry(ALEX)
    group = group_entry([ALEX])
    at_home(hass, ALEX)
    await setup_entries(hass, alex, person_entry(SAM), group)

    with pytest.raises(ServiceValidationError) as err:
        await ignore(hass, members=[SAM, "person.nobody"])
    assert err.value.translation_key == "not_members"
    assert err.value.translation_placeholders == {
        "persons": f"{SAM}, person.nobody",
        "group": "Home ETA",
    }
    with pytest.raises(ServiceValidationError) as err:
        await stop_ignoring(hass, members=[SAM])
    assert err.value.translation_key == "not_members"
    with pytest.raises(ServiceValidationError) as err:
        await ignore(hass, config_entry_id=alex.entry_id)
    assert err.value.translation_key == "not_a_group"
    with pytest.raises(ServiceValidationError) as err:
        await ignore(hass, config_entry_id="no_such_entry")
    assert err.value.translation_key == "service_config_entry_not_found"
    assert state(hass, IGNORE_ALEX) == STATE_OFF

    other = group_entry([ALEX], title="Other")
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await ignore(hass)
    assert err.value.translation_key == "group_ambiguous"
    # Named, either group works on its own.
    response = await ignore(hass, config_entry_id=other.entry_id)
    assert response is not None
    assert response["persons"] == [ALEX]
    assert state(hass, "switch.other_ignore_alex") == STATE_ON
    assert state(hass, IGNORE_ALEX) == STATE_OFF

    await hass.config_entries.async_unload(other.entry_id)
    await hass.config_entries.async_unload(group.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await ignore(hass)
    assert err.value.translation_key == "no_group"
    with pytest.raises(ServiceValidationError) as err:
        await ignore(hass, config_entry_id=group.entry_id)
    assert err.value.translation_key == "service_config_entry_not_loaded"


# -- clearing --------------------------------------------------------------------------


async def test_a_member_counted_arriving_ends_all_ignoring(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 3_000)
    at_home(hass, SAM)
    at_home(hass, KIM)
    group = group_entry([ALEX, SAM, KIM])
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), person_entry(KIM), group
    )
    await ignore(hass, reason="away_pressed")
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert state(hass, IGNORE_KIM) == STATE_ON
    assert state(hass, SOMEONE_HOME) == STATE_OFF
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)

    await step(hass, freezer, ALEX, 2_000)
    assert state(hass, IGNORE_SAM) == STATE_ON
    await step(hass, freezer, ALEX, None, zones=("zone.home",))
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert state(hass, IGNORE_KIM) == STATE_OFF
    home = hass.states.get(SOMEONE_HOME)
    assert home.state == STATE_ON
    assert home.attributes["persons"] == [ALEX, SAM, KIM]
    assert home.attributes["ignored"] == []
    assert len(events) == 1
    assert events[0].data == {
        "entry_id": group.entry_id,
        "group": "Home ETA",
        "ignored": [],
        "ignored_names": [],
        "added": [],
        "added_names": [],
        "removed": [SAM, KIM],
        "removed_names": ["Sam", "Kim"],
        "reason": None,
        "cause": "arrival",
        "cause_person": ALEX,
        "cause_name": "Alex",
    }
    assert hass.states.get(STATUS).attributes["persons_home"] == [ALEX, SAM, KIM]


async def test_a_report_at_home_of_a_member_counted_is_no_arrival(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    at_home(hass, ALEX)
    at_home(hass, SAM)
    await setup_entries(
        hass, person_entry(ALEX), person_entry(SAM), group_entry([ALEX, SAM])
    )
    await ignore(hass, members=[SAM])
    await step(hass, freezer, ALEX, 30, zones=("zone.home",))
    await step(hass, freezer, ALEX, 40, zones=("zone.home",))
    assert state(hass, IGNORE_SAM) == STATE_ON


async def test_the_ignored_members_own_report_ends_only_their_ignoring(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 30_000)
    at_home(hass, SAM)
    at_home(hass, KIM)
    await setup_entries(
        hass,
        person_entry(ALEX),
        person_entry(SAM),
        person_entry(KIM),
        group_entry([ALEX, SAM, KIM]),
    )
    await ignore(hass)
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)

    # Ticks, and a change of an attribute that is no report, change nothing.
    await step(hass, freezer, ALEX, 30_000, seconds=600)
    hass.states.async_set(
        SAM,
        "home",
        {**hass.states.get(SAM).attributes, "friendly_name": "Samantha"},
    )
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert events == []

    # Sam's phone comes back to life out of the house: Sam is counted, away.
    await step(hass, freezer, SAM, 5_000)
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert state(hass, IGNORE_KIM) == STATE_ON
    assert hass.states.get(STATUS).attributes["persons_away"] == [ALEX, SAM]
    assert state(hass, SOMEONE_HOME) == STATE_OFF
    assert len(events) == 1
    assert events[0].data["cause"] == "own_report"
    assert events[0].data["cause_person"] == SAM
    assert events[0].data["removed"] == [SAM]
    assert events[0].data["ignored"] == [KIM]

    # Kim's phone reports again at home (home, a new position): Kim is home.
    at_home(hass, KIM, 25)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_KIM) == STATE_OFF
    assert state(hass, SOMEONE_HOME) == STATE_ON
    assert hass.states.get(SOMEONE_HOME).attributes["persons"] == [KIM]
    assert [event.data["cause"] for event in events] == ["own_report"] * 2


async def test_ignoring_does_not_end_itself(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Ignored straight after a report of their own, and many ticks after."""
    await alex_out_sam_home(hass)
    at_home(hass, SAM, 10)
    await hass.async_block_till_done()
    await switch(hass, IGNORE_SAM, True)
    for _ in range(5):
        await step(hass, freezer, ALEX, 30_000, seconds=3_600)
    assert state(hass, IGNORE_SAM) == STATE_ON


async def test_an_arrival_right_after_the_ignore_ends_it(
    hass: HomeAssistant,
) -> None:
    """The ignore and an arrival in the same update: the arrival wins."""
    group = await alex_out_sam_home(hass)
    runtime = group.runtime_data
    assert isinstance(runtime, GroupRuntime)
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)
    # Nothing awaited in between: one pass of the event loop.
    assert runtime.ignore(None, IgnoreReason.AWAY_PRESSED, IgnoreCause.ACTION) == (SAM,)
    at_home(hass, ALEX)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert state(hass, SOMEONE_HOME) == STATE_ON
    assert [event.data["cause"] for event in events] == ["action", "arrival"]


async def test_an_arrival_right_before_the_ignore_is_ignored_too(
    hass: HomeAssistant,
) -> None:
    """The arrival first: whoever is shown home then is ignored, as asked.

    Their own next report counts them again; naming ``members`` avoids it.
    """
    group = await alex_out_sam_home(hass)
    runtime = group.runtime_data
    assert isinstance(runtime, GroupRuntime)
    at_home(hass, ALEX)
    assert runtime.ignore(None, IgnoreReason.AWAY_PRESSED, IgnoreCause.ACTION) == (
        ALEX,
        SAM,
    )
    await hass.async_block_till_done()
    at_home(hass, ALEX, 20)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_ALEX) == STATE_OFF
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert state(hass, SOMEONE_HOME) == STATE_ON


# -- restarts and reloads --------------------------------------------------------------


async def test_kept_across_a_restart_and_startup_states_clear_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    group = group_entry([ALEX, SAM])
    hass_storage[store_key(group)] = {
        "version": 1,
        "key": store_key(group),
        "data": {"members": stored(SAM)},
    }
    hass.set_state(CoreState.starting)
    seen: list[str] = []

    @callback
    def listener(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if event.data["entity_id"] == SOMEONE_HOME and new is not None:
            seen.append(new.state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, listener)
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)
    # Restored states, as Home Assistant starts: Alex out, Sam home.
    set_person(hass, ALEX, 30_000)
    at_home(hass, SAM)
    await setup_entries(hass, person_entry(ALEX), person_entry(SAM), group)
    switch_state = hass.states.get(IGNORE_SAM)
    assert switch_state.state == STATE_ON
    assert switch_state.attributes["reason"] == "away_pressed"
    assert switch_state.attributes["since"] == "2026-11-23T16:09:10+00:00"
    assert state(hass, SOMEONE_HOME) == STATE_OFF

    # While starting, trackers restore: Sam with a new position, Alex home.
    at_home(hass, SAM, 30)
    at_home(hass, ALEX)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_ON

    await hass.async_start()
    await hass.async_block_till_done()
    await step(hass, freezer, ALEX, None, seconds=600, zones=("zone.home",))
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert events == []
    # Someone home was never on because of Sam: only once Alex was home.
    assert seen[0] == STATE_OFF

    # Once started, Sam's own report counts.
    at_home(hass, SAM, 40)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert hass_storage[store_key(group)]["data"] == {"members": {}}


async def test_saved_when_changed(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    group = await alex_out_sam_home(hass)
    await ignore(hass, reason="away_pressed")
    saved = hass_storage[store_key(group)]["data"]["members"]
    assert list(saved) == [SAM]
    assert saved[SAM]["reason"] == "away_pressed"
    assert dt_util.parse_datetime(saved[SAM]["since"]) is not None


async def test_reloads_keep_the_list(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 30_000)
    at_home(hass, SAM)
    sam = person_entry(SAM)
    alex = person_entry(ALEX)
    group = group_entry([ALEX, SAM])
    await setup_entries(hass, alex, sam, group)
    await ignore(hass)
    events = async_capture_events(hass, EVENT_IGNORED_CHANGED)

    await hass.config_entries.async_reload(group.entry_id)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert state(hass, SOMEONE_HOME) == STATE_OFF

    # Sam's own entry reloading neither counts as a report nor makes Sam missing.
    await hass.config_entries.async_reload(sam.entry_id)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert state(hass, STATUS) == "away"

    # Alex's entry reloading while out, then back in, is no arrival either.
    await hass.config_entries.async_reload(alex.entry_id)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert events == []

    # After Sam's reload, Sam's next report still counts.
    await step(hass, freezer, SAM, None, zones=("zone.home",))
    assert state(hass, IGNORE_SAM) == STATE_ON  # the same state: no report
    at_home(hass, SAM, 15)
    await hass.async_block_till_done()
    assert state(hass, IGNORE_SAM) == STATE_OFF


async def test_a_member_taken_out_of_the_group_is_no_longer_ignored(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    group = await alex_out_sam_home(hass)
    await ignore(hass)
    hass.config_entries.async_update_entry(group, options={CONF_MEMBERS: [ALEX]})
    await hass.config_entries.async_reload(group.entry_id)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(IGNORE_SAM) is None
    assert hass.states.get(IGNORE_SAM) is None
    assert hass_storage[store_key(group)]["data"] == {"members": {}}
    assert state(hass, SOMEONE_HOME) == STATE_OFF  # Alex is out
    assert hass.states.get(SOMEONE_HOME).attributes["ignored"] == []


async def test_unreadable_entries_are_dropped(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    group = group_entry([ALEX, SAM])
    hass_storage[store_key(group)] = {
        "version": 1,
        "key": store_key(group),
        "data": {
            "members": {
                **stored(SAM, "by_hand"),
                ALEX: {"reason": "because", "since": "2026-11-23T16:09:10+00:00"},
            }
        },
    }
    set_person(hass, ALEX, 30_000)
    at_home(hass, SAM)
    await setup_entries(hass, person_entry(ALEX), person_entry(SAM), group)
    assert state(hass, IGNORE_SAM) == STATE_ON
    assert state(hass, IGNORE_ALEX) == STATE_OFF
    assert list(hass_storage[store_key(group)]["data"]["members"]) == [SAM]


async def test_saved_by_a_newer_version(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    group = group_entry([ALEX, SAM])
    hass_storage[store_key(group)] = {
        "version": 2,
        "key": store_key(group),
        "data": {"members": stored(SAM)},
    }
    set_person(hass, ALEX, 30_000)
    at_home(hass, SAM)
    await setup_entries(hass, person_entry(ALEX), person_entry(SAM), group)
    assert state(hass, IGNORE_SAM) == STATE_OFF
    assert state(hass, SOMEONE_HOME) == STATE_ON


async def test_removing_the_group_removes_the_list(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    group = await alex_out_sam_home(hass)
    await ignore(hass)
    assert store_key(group) in hass_storage
    await hass.config_entries.async_remove(group.entry_id)
    await hass.async_block_till_done()
    assert store_key(group) not in hass_storage


async def test_diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    group = await alex_out_sam_home(hass)
    await ignore(hass, reason="away_pressed")
    data = await get_diagnostics_for_config_entry(hass, hass_client, group)
    ignored = data["runtime"]["ignored"]
    assert list(ignored) == [SAM]
    assert ignored[SAM]["reason"] == "away_pressed"


async def test_each_change_is_logged(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_person(hass, ALEX, 3_000)
    at_home(hass, SAM)
    at_home(hass, KIM)
    await setup_entries(
        hass,
        person_entry(ALEX),
        person_entry(SAM),
        person_entry(KIM),
        group_entry([ALEX, SAM, KIM]),
    )
    caplog.set_level("INFO", logger="custom_components.home_eta")
    await ignore(hass, reason="away_pressed")
    await switch(hass, IGNORE_KIM, False)
    await switch(hass, IGNORE_KIM, True)
    await stop_ignoring(hass, members=[KIM])
    await switch(hass, IGNORE_KIM, True)
    at_home(hass, KIM, 10)
    await hass.async_block_till_done()
    await switch(hass, IGNORE_KIM, True)
    await step(hass, freezer, ALEX, None, zones=("zone.home",))
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.home_eta.runtime"
        and record.levelname == "INFO"
    ]
    assert messages == [
        "Home ETA: ignoring Sam and Kim (away_pressed, by the ignore action)",
        "Home ETA: no longer ignoring Kim: by hand, with the switch",
        "Home ETA: ignoring Kim (by_hand, by the switch)",
        "Home ETA: no longer ignoring Kim: by hand, with the stop ignoring action",
        "Home ETA: ignoring Kim (by_hand, by the switch)",
        "Home ETA: no longer ignoring Kim: their own phone reported again",
        "Home ETA: ignoring Kim (by_hand, by the switch)",
        "Home ETA: no longer ignoring Sam and Kim: Alex arrived home",
    ]


async def test_a_failed_save_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    group = await alex_out_sam_home(hass)
    with patch(
        "homeassistant.helpers.storage.Store.async_save", side_effect=OSError("full")
    ):
        await ignore(hass)
        await hass.config_entries.async_unload(group.entry_id)
        await hass.async_block_till_done()
    assert "could not save the members ignored" in caplog.text
    assert "full" in caplog.text


async def test_the_runtime_refuses_people_not_in_the_group(
    hass: HomeAssistant,
) -> None:
    group = await alex_out_sam_home(hass)
    runtime = group.runtime_data
    assert isinstance(runtime, GroupRuntime)
    with pytest.raises(ValueError, match=KIM):
        runtime.ignore([KIM], IgnoreReason.BY_HAND, IgnoreCause.ACTION)
    with pytest.raises(ValueError, match=KIM):
        runtime.stop_ignoring([KIM], IgnoreCause.ACTION)
    assert runtime.ignored == {}
