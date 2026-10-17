"""Tests for a person entry's entities and behaviour."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
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
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.home_eta.const import CONF_ENABLE_HISTORICAL, DOMAIN
from tests.helpers import ALEX, drive_home, person_entry, set_person, step

ETA = "sensor.alex_eta_kinematic"
MINUTES = "sensor.alex_minutes_to_home"
DISTANCE = "sensor.alex_distance_to_home"
SPEED = "sensor.alex_closing_speed"
APPROACH = "sensor.alex_approach"
CONFIDENCE = "sensor.alex_eta_confidence"
SOON = "binary_sensor.alex_arriving_soon"
THRESHOLD = "number.alex_arriving_soon_threshold"
HISTORICAL = "sensor.alex_eta_historical"

pytestmark = pytest.mark.usefixtures("zones")


async def setup_person(hass: HomeAssistant, **kwargs: object) -> None:
    entry = person_entry(**kwargs)  # type: ignore[arg-type]
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_created(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)

    for entity_id in (
        ETA,
        MINUTES,
        DISTANCE,
        SPEED,
        APPROACH,
        CONFIDENCE,
        SOON,
        THRESHOLD,
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(HISTORICAL) is None
    assert hass.states.get(APPROACH).state == "home"
    assert hass.states.get(DISTANCE).state == "0"
    assert hass.states.get(ETA).state == STATE_UNKNOWN
    assert hass.states.get(SOON).state == STATE_OFF
    assert hass.states.get(THRESHOLD).state == "15.0"

    registry = er.async_get(hass)
    entry = registry.async_get(ETA)
    assert entry is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    assert device.name == "Alex"
    assert registry.async_get(SPEED).entity_category == "diagnostic"


async def test_drive_home(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass)

    await drive_home(hass, freezer, ALEX, start_m=20_000, speed_mps=13, stop_m=13_000)
    assert hass.states.get(APPROACH).state == "approaching"
    eta = hass.states.get(ETA)
    arrival = dt_util.parse_datetime(eta.state)
    assert arrival is not None
    remaining = (arrival - dt_util.utcnow()).total_seconds()
    distance = float(hass.states.get(DISTANCE).state)
    assert remaining == pytest.approx((distance - 100) / 13, rel=0.05)
    assert eta.attributes["method"] == "kinematic"
    assert eta.attributes["confidence"] in ("medium", "high")
    assert eta.attributes["stale"] is False
    assert float(hass.states.get(SPEED).state) == pytest.approx(13, rel=0.05)
    # The timestamp state is whole seconds, so allow for rounding at the half.
    assert abs(int(hass.states.get(MINUTES).state) - remaining / 60) <= 0.51
    approach = hass.states.get(APPROACH)
    assert approach.attributes["direction"] == "towards"
    assert approach.attributes["reason"] == "closing"

    # Minutes count down on the tick between fixes.
    before = int(hass.states.get(MINUTES).state)
    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert int(hass.states.get(MINUTES).state) == before - 2

    await drive_home(hass, freezer, ALEX, start_m=11_000, speed_mps=13, stop_m=500)
    assert hass.states.get(APPROACH).state == "arriving_soon"
    assert hass.states.get(SOON).state == STATE_ON

    await step(hass, freezer, ALEX, 20, zones=("zone.home",))
    assert hass.states.get(APPROACH).state == "home"
    assert hass.states.get(SOON).state == STATE_OFF
    assert hass.states.get(ETA).state == STATE_UNKNOWN
    assert hass.states.get(DISTANCE).state == "0"


async def test_other_zone_holds_off_arriving_soon(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 5_000)
    await setup_person(hass)
    for east in (4_000, 3_000, 2_000):
        await step(hass, freezer, ALEX, east, zones=("zone.station",))
    assert hass.states.get(APPROACH).state == "approaching"
    assert hass.states.get(APPROACH).attributes["zones"] == ["Station"]
    assert hass.states.get(SOON).state == STATE_OFF
    await step(hass, freezer, ALEX, 1_000)
    assert hass.states.get(SOON).state == STATE_ON


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
    assert hass.states.get(SOON).attributes["threshold_min"] == 5.0

    for east in (29_400, 28_800, 28_200):
        await step(hass, freezer, ALEX, east)
    # About 46 minutes away: approaching, not arriving soon.
    assert hass.states.get(APPROACH).state == "approaching"

    await hass.services.async_call(
        "number", "set_value", {"entity_id": THRESHOLD, "value": 60}, blocking=True
    )
    assert hass.states.get(THRESHOLD).state == "60.0"
    assert hass.states.get(APPROACH).state == "arriving_soon"


async def test_restored_threshold_out_of_range_ignored(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass, [(State(THRESHOLD, "500"), {"native_value": 500.0})]
    )
    set_person(hass, ALEX, 30_000)
    await setup_person(hass)
    assert hass.states.get(THRESHOLD).state == "15.0"


async def test_approach_state_restored_when_fresh(hass: HomeAssistant) -> None:
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "approaching"),
                {"state": "approaching", "since": now - 600, "last_heard": now - 60},
            )
        ],
    )
    set_person(hass, ALEX, 8_000)
    await setup_person(hass)
    state = hass.states.get(APPROACH)
    assert state.state == "approaching"
    assert state.attributes["reason"] == "restored"
    assert dt_util.parse_datetime(state.attributes["since"]) == (
        dt_util.utc_from_timestamp(now - 600)
    )


async def test_approach_state_not_restored_when_old(hass: HomeAssistant) -> None:
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "approaching"),
                {
                    "state": "approaching",
                    "since": now - 4_000,
                    "last_heard": now - 3_600,
                },
            )
        ],
    )
    set_person(hass, ALEX, 8_000)
    await setup_person(hass)
    assert hass.states.get(APPROACH).state == "away"


async def test_approach_state_restore_ignores_bad_data(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(APPROACH, "approaching")])
    set_person(hass, ALEX, 8_000)
    await setup_person(hass)
    assert hass.states.get(APPROACH).state == "away"


async def test_startup_states_do_not_measure_speed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Restored states during startup carry restore times, not fix times."""
    hass.set_state(CoreState.starting)
    set_person(hass, ALEX, 20_000)
    await setup_person(hass)
    await step(hass, freezer, ALEX, 19_000)
    await step(hass, freezer, ALEX, 18_000)
    assert hass.states.get(SPEED).state == STATE_UNKNOWN
    assert hass.states.get(DISTANCE).state == "18000"

    await hass.async_start()
    await hass.async_block_till_done()
    await step(hass, freezer, ALEX, 17_400)
    await step(hass, freezer, ALEX, 16_800)
    assert float(hass.states.get(SPEED).state) == pytest.approx(10, rel=0.05)


async def test_stationary_then_stale(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 30_000)
    await setup_person(hass)
    for east in (29_400, 28_800, 28_200):
        await step(hass, freezer, ALEX, east)
    assert hass.states.get(APPROACH).state == "approaching"
    for _ in range(14):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    # Over ten minutes without a report is not yet a stop...
    assert hass.states.get(APPROACH).state == "approaching"
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # ...fifteen is.
    assert hass.states.get(APPROACH).state == "stationary"
    assert hass.states.get(APPROACH).attributes["reason"] == "no_reports"
    eta = hass.states.get(ETA)
    assert eta.state == STATE_UNKNOWN
    assert eta.attributes["stale"] is True
    assert eta.attributes["last_eta"] is not None


async def test_missing_person_raises_issue(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await setup_person(hass)
    issues = ir.async_get(hass)
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry_id}") is not None
    assert hass.states.get(ETA).state == STATE_UNAVAILABLE
    assert hass.states.get(THRESHOLD).state != STATE_UNAVAILABLE

    set_person(hass, ALEX, 5_000)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry_id}") is None
    assert hass.states.get(APPROACH).state == "away"

    hass.states.async_remove(ALEX)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"person_missing_{entry_id}") is not None
    assert hass.states.get(APPROACH).state == STATE_UNAVAILABLE


async def test_unknown_person_state_is_ignored(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    await setup_person(hass)
    hass.states.async_set(ALEX, STATE_UNKNOWN, {})
    await hass.async_block_till_done()
    assert hass.states.get(DISTANCE).state == "5000"


async def test_zone_moved_and_removed(hass: HomeAssistant) -> None:
    set_person(hass, ALEX, 5_000)
    await setup_person(hass)
    zone = hass.states.get("zone.home")
    attributes = dict(zone.attributes)
    attributes["longitude"] = zone.attributes["longitude"] + 0.01
    hass.states.async_set("zone.home", zone.state, attributes)
    await hass.async_block_till_done()
    assert hass.states.get(DISTANCE).state == STATE_UNKNOWN

    hass.states.async_remove("zone.home")
    await hass.async_block_till_done()
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"zone_missing_{entry_id}")

    hass.states.async_set("zone.home", zone.state, attributes)
    await hass.async_block_till_done()
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"zone_missing_{entry_id}")


async def test_extra_attributes_follow_their_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.iphone_activity", "Automotive")
    hass.states.async_set("sensor.waze", "23", {"unit_of_measurement": "min"})
    set_person(hass, ALEX, 5_000)
    await setup_person(
        hass,
        options={
            "activity_entity": "sensor.iphone_activity",
            "reference_eta_entity": "sensor.waze",
        },
    )
    eta = hass.states.get(ETA)
    assert eta.attributes["activity"] == "Automotive"
    assert eta.attributes["reference_eta"] == "23"
    assert eta.attributes["reference_eta_unit"] == "min"
    assert hass.states.get(APPROACH).attributes["activity"] == "Automotive"

    hass.states.async_set("sensor.iphone_activity", "Walking")
    await hass.async_block_till_done()
    assert hass.states.get(ETA).attributes["activity"] == "Walking"


async def test_historical_sensor_follows_option(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, None, zones=("zone.home",))
    await setup_person(hass, options={CONF_ENABLE_HISTORICAL: True})
    state = hass.states.get(HISTORICAL)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["trips_learned"] == 0

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ENABLE_HISTORICAL: False}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(HISTORICAL) is None
    assert er.async_get(hass).async_get(HISTORICAL) is None


async def test_restored_arriving_soon_ends_without_reports(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A phone that goes quiet after a restart must not hold arriving-soon on."""
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "arriving_soon"),
                {"state": "arriving_soon", "since": now - 300, "last_heard": now - 30},
            )
        ],
    )
    set_person(hass, ALEX, 3_000)
    await setup_person(hass)
    assert hass.states.get(SOON).state == STATE_ON
    for _ in range(16):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass.states.get(APPROACH).state == "stationary"
    assert hass.states.get(SOON).state == STATE_OFF


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


async def test_poor_fix_near_home_is_not_arriving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_person(hass, ALEX, 20_000)
    await setup_person(hass)
    for east in (19_400, 18_800, 18_200):
        await step(hass, freezer, ALEX, east)
    assert hass.states.get(APPROACH).state == "approaching"
    freezer.tick(timedelta(seconds=30))
    set_person(hass, ALEX, 400, accuracy=2_000)
    await hass.async_block_till_done()
    assert hass.states.get(DISTANCE).state == "18200"
    assert hass.states.get(SOON).state == STATE_OFF


async def test_restored_state_cannot_outlive_silence_across_reloads(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Regression: each reload restarted the 15-minute expiry of a restored state."""
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(APPROACH, "arriving_soon"),
                {"state": "arriving_soon", "since": now - 300, "last_heard": now - 30},
            )
        ],
    )
    set_person(hass, ALEX, 3_000)
    await setup_person(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert hass.states.get(SOON).state == STATE_ON
    for _ in range(12):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SOON).state == STATE_ON
    for _ in range(4):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass.states.get(APPROACH).state == "stationary"
    assert hass.states.get(SOON).state == STATE_OFF


async def test_person_without_a_location_yet_is_unknown(hass: HomeAssistant) -> None:
    """Regression: a person with no data was published as away."""
    hass.states.async_set(ALEX, STATE_UNKNOWN, {})
    await setup_person(hass)
    assert hass.states.get(APPROACH).state == STATE_UNKNOWN
    assert hass.states.get(SOON).state == STATE_UNKNOWN
    set_person(hass, ALEX, 5_000)
    await hass.async_block_till_done()
    assert hass.states.get(APPROACH).state == "away"


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
    for east in (5_400, 4_800, 4_200, 3_600, 3_000, 2_400):
        await step(hass, freezer, ALEX, east)
    for east in (1_800, 1_200):
        await step(hass, freezer, ALEX, east, zones=("zone.neighbourhood",))
        assert hass.states.get(SOON).state == STATE_ON
    assert hass.states.get(APPROACH).attributes["zones"] == []
    # A zone that does not contain home still holds it off.
    await step(hass, freezer, ALEX, 1_000, zones=("zone.station",))
    assert hass.states.get(SOON).state == STATE_OFF
