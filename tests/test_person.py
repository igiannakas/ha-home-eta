"""Tests for a person entry's entities and behaviour."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.home_eta.const import CONF_NEAR_HOME_RADIUS_M, DOMAIN
from tests.helpers import ALEX, person_entry, set_person, step

STATUS = "sensor.alex_status"
EXPECTED = "sensor.alex_expected_home"
MINUTES = "sensor.alex_minutes_to_home"
DISTANCE = "sensor.alex_distance_to_home"
NEAR = "sensor.alex_near_home"
THRESHOLD = "number.alex_arriving_soon_threshold"
SPEEDS = "sensor.alex_typical_speeds"
RELEARN = "button.alex_relearn_typical_speeds"
CLOSE = "binary_sensor.alex_close_to_home"
NEARBY = "binary_sensor.alex_nearby"
ALL = (
    STATUS,
    EXPECTED,
    MINUTES,
    DISTANCE,
    NEAR,
    THRESHOLD,
    SPEEDS,
    RELEARN,
    CLOSE,
    NEARBY,
)

pytestmark = pytest.mark.usefixtures("zones")


ON_THE_WAY = ("heading_home", "arriving_soon")


def heading(hass: HomeAssistant) -> bool:
    """Return True while the status says heading home (arriving soon included)."""
    return hass.states.get(STATUS).state in ON_THE_WAY


def soon(hass: HomeAssistant) -> bool:
    """Return True while the status says arriving soon."""
    return hass.states.get(STATUS).state == "arriving_soon"


async def setup_person(hass: HomeAssistant, **kwargs: object) -> None:
    entry = person_entry(**kwargs)  # type: ignore[arg-type]
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def drive(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    points: list[float],
    *,
    zones_for: dict[float, tuple[str, ...]] | None = None,
) -> None:
    """Report Alex at each point east of home, a minute apart."""
    for east in points:
        await step(hass, freezer, ALEX, east, zones=(zones_for or {}).get(east, ()))


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int
) -> None:
    for _ in range(minutes):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_entities_created(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)

    for entity_id in ALL:
        assert hass.states.get(entity_id) is not None, entity_id
    assert not heading(hass)
    assert not soon(hass)
    assert hass.states.get(EXPECTED).state == STATE_UNKNOWN
    assert hass.states.get(MINUTES).state == STATE_UNKNOWN
    assert hass.states.get(DISTANCE).state == "0"
    assert hass.states.get(NEAR).state == "100"
    assert hass.states.get(THRESHOLD).state == "15.0"

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(
        registry, hass.config_entries.async_entries(DOMAIN)[0].entry_id
    )
    assert sorted(e.entity_id for e in entries) == sorted(ALL)
    device = dr.async_get(hass).async_get(registry.async_get(EXPECTED).device_id)
    assert device is not None
    assert device.name == "Alex"
    assert registry.async_get(THRESHOLD).entity_category == "config"
    assert registry.async_get(RELEARN).entity_category == "config"
    assert registry.async_get(SPEEDS).entity_category == "diagnostic"


async def test_status_follows_the_trip(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)
    state = hass.states.get(STATUS)
    assert state.state == "home"
    assert state.attributes["options"] == [
        "home",
        "arriving_soon",
        "heading_home",
        "away",
    ]
    assert state.attributes["reason"] == "initial"
    # With 15 minutes this drive would go from away straight to arriving soon.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": THRESHOLD, "value": 5}, blocking=True
    )

    seen: list[str] = []

    def note() -> None:
        current = hass.states.get(STATUS).state
        if not seen or seen[-1] != current:
            seen.append(current)

    for east in [780.0 * i for i in range(1, 14)]:
        await step(hass, freezer, ALEX, east)
        note()
    for east in [10_140.0 - 780.0 * i for i in range(1, 13)]:
        await step(hass, freezer, ALEX, east)
        note()
    await step(hass, freezer, ALEX, None, zones=("zone.home",))
    note()
    assert seen == ["away", "heading_home", "arriving_soon", "home"]
    state = hass.states.get(STATUS)
    assert state.attributes["reason"] == "arrived"
    assert dt_util.parse_datetime(state.attributes["since"]) == dt_util.utcnow()


async def test_drive_home(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": THRESHOLD, "value": 5}, blocking=True
    )

    # Out to 10 km at 13 m/s: not heading home.
    await drive(hass, freezer, [780.0 * i for i in range(1, 14)])
    assert not heading(hass)
    assert hass.states.get(EXPECTED).state == STATE_UNKNOWN
    assert hass.states.get(MINUTES).state == STATE_UNKNOWN
    assert int(hass.states.get(DISTANCE).state) == pytest.approx(10_140, abs=50)
    assert hass.states.get(NEAR).state == "0"

    # Back 21 %: heading home, expected in a while.
    await drive(hass, freezer, [9_400.0, 8_620.0, 7_840.0])
    assert heading(hass)
    expected = dt_util.parse_datetime(hass.states.get(EXPECTED).state)
    assert expected is not None
    remaining = (expected - dt_util.utcnow()).total_seconds()
    # The timestamp state is whole seconds, so allow for rounding at the half.
    assert abs(int(hass.states.get(MINUTES).state) - remaining / 60) <= 0.51
    assert 8 <= int(hass.states.get(MINUTES).state) <= 25
    assert not soon(hass)

    # Minutes count down on the tick between fixes.
    before = int(hass.states.get(MINUTES).state)
    await tick(hass, freezer, 2)
    assert int(hass.states.get(MINUTES).state) == before - 2

    await drive(hass, freezer, [7_840.0 - 780.0 * i for i in range(1, 10)] + [500.0])
    assert soon(hass)
    assert hass.states.get(NEAR).state == "100"

    await step(hass, freezer, ALEX, 20, zones=("zone.home",))
    assert not heading(hass)
    assert hass.states.get(STATUS).attributes["reason"] == "arrived"
    assert not soon(hass)
    assert hass.states.get(EXPECTED).state == STATE_UNKNOWN
    assert hass.states.get(DISTANCE).state == "0"


async def test_other_zone_holds_off_arriving_soon(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 5_000)
    await setup_person(hass)
    await drive(hass, freezer, [4_200, 3_400, 2_600])
    assert heading(hass)
    await drive(hass, freezer, [1_800, 1_000], zones_for={1_000: ("zone.station",)})
    assert heading(hass)
    assert not soon(hass)
    await step(hass, freezer, ALEX, 400)
    assert soon(hass)


async def test_zone_around_home_does_not_hold_off_arriving_soon(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Regression: a neighbourhood zone containing home turned arriving soon off."""
    home = hass.states.get("zone.home")
    hass.states.async_set(
        "zone.neighbourhood",
        "0",
        {
            "latitude": home.attributes["latitude"],
            "longitude": home.attributes["longitude"],
            "radius": 2_000,
            "friendly_name": "Neighbourhood",
        },
    )
    set_person(hass, ALEX, 6_000)
    await setup_person(hass)
    await drive(hass, freezer, [5_200, 4_400, 3_600, 2_800])
    for east in (1_800.0, 700.0):
        await step(hass, freezer, ALEX, east, zones=("zone.neighbourhood",))
    assert soon(hass)


async def test_threshold_is_per_person_and_restored(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(THRESHOLD, "5.0"),
                {
                    "native_max_value": 120,
                    "native_min_value": 1,
                    "native_step": 1,
                    "native_unit_of_measurement": "min",
                    "native_value": 5.0,
                },
            )
        ],
    )
    set_person(hass, ALEX, 30_000)
    await setup_person(hass)
    assert hass.states.get(THRESHOLD).state == "5.0"

    await step(hass, freezer, ALEX, 26_000, seconds=300)
    await step(hass, freezer, ALEX, 22_000, seconds=300)
    assert heading(hass)
    assert int(hass.states.get(MINUTES).state) > 5
    assert not soon(hass)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": THRESHOLD, "value": 120}, blocking=True
    )
    assert hass.states.get(THRESHOLD).state == "120.0"
    assert soon(hass)


async def test_restored_threshold_out_of_range_ignored(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass, [(State(THRESHOLD, "500"), {"native_value": 500.0})]
    )
    set_person(hass, ALEX, 30_000)
    await setup_person(hass)
    assert hass.states.get(THRESHOLD).state == "15.0"


async def test_startup_states_do_not_start_a_trip_home(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Restored states during startup carry restore times, not fix times."""
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 20_000)
    await setup_person(hass)
    await step(hass, freezer, ALEX, 15_000)
    await step(hass, freezer, ALEX, 10_000)
    assert hass.states.get(DISTANCE).state == "10000"
    assert not heading(hass)

    await hass.async_start()
    await hass.async_block_till_done()
    await step(hass, freezer, ALEX, 9_000, seconds=300)
    await step(hass, freezer, ALEX, 6_000, seconds=300)
    assert heading(hass)


async def test_near_home_follows_the_person_options(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 4_000)
    await setup_person(
        hass,
        options={CONF_NEAR_HOME_RADIUS_M: 8_000},
    )
    near = hass.states.get(NEAR)
    # (8000 - 4000) / (8000 - 800) = 56 %
    assert near.state == "56"
    assert near.attributes["radius_m"] == 8_000
    assert "fade_after_min" not in near.attributes
    assert near.attributes["staying_since"] is None
    nearby = hass.states.get(NEARBY)
    assert nearby.state == "on"
    assert nearby.attributes["radius_m"] == 8_000
    assert nearby.attributes["distance_m"] == pytest.approx(4_000, abs=20)
    assert hass.states.get(CLOSE).state == "off"


async def test_near_home_does_not_fade_however_long_the_stay(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Six hours at a café 400 m away: near home, close and nearby throughout."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)
    near = hass.states.get(NEAR)
    assert near.attributes["radius_m"] == 3_000
    assert "fade_after_min" not in near.attributes
    assert hass.states.get(CLOSE).state == "on"
    assert hass.states.get(NEARBY).state == "on"
    await step(hass, freezer, ALEX, 400)
    assert hass.states.get(NEAR).state == "100"

    # At the café, the phone reporting every 10 minutes.
    for minute in range(10, 361, 10):
        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        set_person(hass, ALEX, 400, accuracy=10.0 + minute % 20 / 10)
        await hass.async_block_till_done()
        assert hass.states.get(NEAR).state == "100", minute
        assert hass.states.get(CLOSE).state == "on", minute
        assert hass.states.get(NEARBY).state == "on", minute
    assert hass.states.get(NEAR).attributes["staying_since"] is not None
    assert not heading(hass)


async def test_close_to_home_and_nearby_follow_the_distance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)
    for east, close, nearby in (
        (500, "on", "on"),
        (1_500, "off", "on"),
        (2_900, "off", "on"),
        (3_200, "off", "off"),
        (1_000, "off", "on"),
        (700, "on", "on"),
    ):
        await step(hass, freezer, ALEX, east)
        close_state = hass.states.get(CLOSE)
        assert close_state.state == close, east
        assert close_state.attributes["within_m"] == 800
        assert close_state.attributes["distance_m"] == pytest.approx(east, abs=20)
        assert hass.states.get(NEARBY).state == nearby, east


async def test_close_to_home_and_nearby_are_unknown_until_a_distance_is_known(
    hass: HomeAssistant,
) -> None:
    set_person(hass, ALEX, None)  # out, with no position
    await setup_person(hass)
    assert hass.states.get(CLOSE).state == STATE_UNKNOWN
    assert hass.states.get(NEARBY).state == STATE_UNKNOWN
    assert hass.states.get(CLOSE).attributes["distance_m"] is None


async def test_leaving_home_with_a_poor_fix_is_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Regression: the distance stayed 0 after leaving, so close to home stayed on."""
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)
    assert hass.states.get(CLOSE).state == "on"
    freezer.tick(timedelta(minutes=5))
    set_person(hass, ALEX, 20_000, accuracy=500.0)
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "away"
    for entity_id in (DISTANCE, NEAR, CLOSE, NEARBY):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    await step(hass, freezer, ALEX, 20_000, seconds=300)
    assert hass.states.get(DISTANCE).state == "20000"
    assert hass.states.get(CLOSE).state == "off"
    assert hass.states.get(NEARBY).state == "off"


async def test_missing_person_raises_issue(hass: HomeAssistant) -> None:
    await setup_person(hass)
    issues = ir.async_get(hass)
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry_id}") is not None
    assert hass.states.get(EXPECTED).state == STATE_UNAVAILABLE
    assert hass.states.get(THRESHOLD).state != STATE_UNAVAILABLE

    set_person(hass, ALEX, 5_000)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry_id}") is None
    assert hass.states.get(DISTANCE).state == "5000"
    assert not heading(hass)

    hass.states.async_remove(ALEX)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry_id}") is not None
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE


async def test_person_without_a_location_yet_is_unknown(hass: HomeAssistant) -> None:
    """Regression: a person with no data was published as away."""
    hass.states.async_set(ALEX, STATE_UNKNOWN, {})
    await setup_person(hass)
    for entity_id in (STATUS, DISTANCE, NEAR, CLOSE, NEARBY):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    set_person(hass, ALEX, 5_000)
    await hass.async_block_till_done()
    assert not heading(hass)
    assert hass.states.get(NEAR).state == "0"
    assert hass.states.get(CLOSE).state == "off"
    assert hass.states.get(NEARBY).state == "off"


async def test_unknown_person_state_is_ignored(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    await setup_person(hass)
    hass.states.async_set(ALEX, STATE_UNKNOWN, {})
    await hass.async_block_till_done()
    assert hass.states.get(DISTANCE).state == "5000"


async def test_poor_fix_near_home_is_not_arriving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 20_000)
    await setup_person(hass)
    await step(hass, freezer, ALEX, 20_000, seconds=300)
    await step(hass, freezer, ALEX, 16_000, seconds=300)
    freezer.tick(timedelta(seconds=30))
    set_person(hass, ALEX, 400, accuracy=2_000)
    await hass.async_block_till_done()
    assert hass.states.get(DISTANCE).state == "16000"
    assert not soon(hass)
    assert hass.states.get(NEAR).state == "0"


async def test_zone_moved_and_removed(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    await setup_person(hass)
    zone = hass.states.get("zone.home")
    attributes = dict(zone.attributes)
    attributes["longitude"] = zone.attributes["longitude"] + 0.01
    hass.states.async_set("zone.home", zone.state, attributes)
    await hass.async_block_till_done()
    assert hass.states.get(DISTANCE).state == STATE_UNKNOWN
    assert hass.states.get(NEAR).state == STATE_UNKNOWN
    assert hass.states.get(CLOSE).state == STATE_UNKNOWN
    assert hass.states.get(NEARBY).state == STATE_UNKNOWN

    hass.states.async_remove("zone.home")
    await hass.async_block_till_done()
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"zone_missing_{entry_id}")

    hass.states.async_set("zone.home", zone.state, attributes)
    await hass.async_block_till_done()
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"zone_missing_{entry_id}")


async def test_stale_issues_cleared_on_setup(hass: HomeAssistant) -> None:
    entry = person_entry()
    entry.add_to_hass(hass)
    issues = ir.async_get(hass)
    for issue in ("person_missing", "zone_missing"):
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{issue}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={
                "person": ALEX,
                "zone": "zone.home",
                "entry": "x",
            },
        )
    set_person(hass, ALEX, 3_000)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry.entry_id}") is None
    assert issues.async_get_issue(DOMAIN, f"zone_missing_{entry.entry_id}") is None
