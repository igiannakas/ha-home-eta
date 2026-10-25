"""Config and options flows for Home ETA."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.util import slugify
import probatio

from .const import (
    CONF_ADVANCED,
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    CONF_NEAR_HOME_RADIUS_M,
    CONF_PERSON,
    CONF_ZONE,
    DEFAULT_GROUP_NAME,
    DEFAULT_NEAR_HOME_RADIUS_M,
    DEFAULT_ZONE,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_PERSON,
    MAX_NEAR_HOME_RADIUS_M,
    MIN_NEAR_HOME_RADIUS_M,
)
from .core import params as defaults
from .migration import CONFIG_MINOR_VERSION, CONFIG_VERSION
from .runtime import friendly_name, params_from_options, target_from_state

# (key, default, min, max, step, unit) for the advanced section.
ADVANCED_FIELDS: tuple[tuple[str, float, float, float, float, str], ...] = (
    ("max_gps_accuracy_m", defaults.MAX_GPS_ACCURACY_M, 10, 1_000, 5, "m"),
    ("speed_filter_tau_s", defaults.SPEED_FILTER_TAU_S, 30, 1_800, 10, "s"),
    (
        "arriving_soon_distance_m",
        defaults.ARRIVING_SOON_DISTANCE_M,
        50,
        10_000,
        50,
        "m",
    ),
    # Not below the stationary time: iPhones often report only every 5-15 min.
    (
        "stale_after_s",
        defaults.STALE_AFTER_S,
        defaults.STATIONARY_AFTER_S,
        7_200,
        30,
        "s",
    ),
)


def _near_home_fields() -> dict[probatio.Marker, Any]:
    return {
        probatio.Required(
            CONF_NEAR_HOME_RADIUS_M, default=DEFAULT_NEAR_HOME_RADIUS_M
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_NEAR_HOME_RADIUS_M,
                max=MAX_NEAR_HOME_RADIUS_M,
                step=100,
                unit_of_measurement="m",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }


def _advanced_section() -> dict[probatio.Marker, Any]:
    fields: dict[probatio.Marker, Any] = {}
    for key, default, low, high, step, unit in ADVANCED_FIELDS:
        fields[probatio.Required(key, default=default)] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=low,
                max=high,
                step=step,
                unit_of_measurement=unit,
                mode=selector.NumberSelectorMode.BOX,
            )
        )
    return {
        probatio.Required(CONF_ADVANCED): section(
            probatio.Schema(fields), {"collapsed": True}
        )
    }


def _person_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return a person's options from a submitted form."""
    options: dict[str, Any] = {
        CONF_NEAR_HOME_RADIUS_M: int(
            user_input.get(CONF_NEAR_HOME_RADIUS_M, DEFAULT_NEAR_HOME_RADIUS_M)
        ),
    }
    # Keep only values changed from the defaults, so improved defaults in a
    # later release still reach everyone who never changed them.
    advanced = user_input.get(CONF_ADVANCED) or {}
    changed = {
        key: advanced[key]
        for key, default, *_ in ADVANCED_FIELDS
        if key in advanced and float(advanced[key]) != default
    }
    if changed:
        options[CONF_ADVANCED] = changed
    return options


def _person_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_PERSON
    ]


def _tracked_persons(hass: HomeAssistant, zone: str | None) -> set[str]:
    """Return the persons with an entry for ``zone`` (any zone when None)."""
    return {
        entry.data[CONF_PERSON]
        for entry in _person_entries(hass)
        if zone is None or entry.data[CONF_ZONE] == zone
    }


def _members_selector(hass: HomeAssistant, zone: str | None) -> selector.EntitySelector:
    persons = sorted(_tracked_persons(hass, zone))
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="person", multiple=True, include_entities=persons
        )
    )


def _validate_members(
    hass: HomeAssistant, zone: str, members: list[str], errors: dict[str, str]
) -> None:
    tracked = _tracked_persons(hass, zone)
    if not members:
        errors[CONF_MEMBERS] = "no_members"
    elif not set(members) <= tracked:
        errors[CONF_MEMBERS] = "member_not_tracked"


class HomeEtaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a person or a group."""

    VERSION = CONFIG_VERSION
    MINOR_VERSION = CONFIG_MINOR_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what to add."""
        return self.async_show_menu(
            step_id="user", menu_options=[ENTRY_TYPE_PERSON, ENTRY_TYPE_GROUP]
        )

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Track one person."""
        errors: dict[str, str] = {}
        if user_input is not None:
            person = user_input[CONF_PERSON]
            zone = user_input[CONF_ZONE]
            if self.hass.states.get(person) is None:
                errors[CONF_PERSON] = "person_invalid"
            if target_from_state(self.hass.states.get(zone)) is None:
                errors[CONF_ZONE] = "zone_invalid"
            if not errors:
                await self.async_set_unique_id(f"{person}|{zone}")
                self._abort_if_unique_id_configured()
                title = friendly_name(self.hass, person)
                if zone != DEFAULT_ZONE:
                    title = f"{title} to {friendly_name(self.hass, zone)}"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_ENTRY_TYPE: ENTRY_TYPE_PERSON,
                        CONF_PERSON: person,
                        CONF_ZONE: zone,
                    },
                    options=_person_options(user_input),
                )

        schema = probatio.Schema(
            {
                probatio.Required(CONF_PERSON): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="person")
                ),
                probatio.Required(
                    CONF_ZONE, default=DEFAULT_ZONE
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="zone")
                ),
                **_near_home_fields(),
            }
        )
        return self.async_show_form(
            step_id="person",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Combine several people heading to the same zone."""
        if not _person_entries(self.hass):
            return self.async_abort(reason="no_people")
        errors: dict[str, str] = {}
        if user_input is not None:
            zone = user_input[CONF_ZONE]
            members = list(user_input.get(CONF_MEMBERS) or [])
            if target_from_state(self.hass.states.get(zone)) is None:
                errors[CONF_ZONE] = "zone_invalid"
            _validate_members(self.hass, zone, members, errors)
            if not errors:
                await self.async_set_unique_id(
                    f"group|{zone}|{slugify(user_input[CONF_NAME])}"
                )
                self._abort_if_unique_id_configured(error="group_exists")
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_ZONE: zone},
                    options={CONF_MEMBERS: members},
                )

        schema = probatio.Schema(
            {
                probatio.Required(CONF_NAME, default=DEFAULT_GROUP_NAME): str,
                probatio.Required(
                    CONF_ZONE, default=DEFAULT_ZONE
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="zone")
                ),
                probatio.Required(CONF_MEMBERS): _members_selector(self.hass, None),
            }
        )
        return self.async_show_form(
            step_id="group",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this kind of entry."""
        if config_entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_GROUP:
            return GroupOptionsFlow()
        return PersonOptionsFlow()


class PersonOptionsFlow(OptionsFlowWithReload):
    """Change how a person is tracked."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}
        if user_input is not None:
            options = _person_options(user_input)
            try:
                params_from_options(options)
            except (TypeError, ValueError) as err:
                # The fields interact (and with fixed parameters); a value
                # inside each field's own range can still be unusable.
                errors["base"] = "invalid_advanced"
                placeholders["error"] = str(err)
            if not errors:
                return self.async_create_entry(data=options)

        schema = probatio.Schema({**_near_home_fields(), **_advanced_section()})
        current = dict(self.config_entry.options)
        current.setdefault(CONF_ADVANCED, {})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or current
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class GroupOptionsFlow(OptionsFlowWithReload):
    """Change who is in a group."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the members."""
        errors: dict[str, str] = {}
        zone = self.config_entry.data[CONF_ZONE]
        if user_input is not None:
            members = list(user_input.get(CONF_MEMBERS) or [])
            _validate_members(self.hass, zone, members, errors)
            if not errors:
                return self.async_create_entry(data={CONF_MEMBERS: members})

        schema = probatio.Schema(
            {probatio.Required(CONF_MEMBERS): _members_selector(self.hass, zone)}
        )
        if user_input is None:
            # Offer only members that are still tracked, so the form can be
            # saved as shown even if a person was removed meanwhile.
            tracked = _tracked_persons(self.hass, zone)
            saved = self.config_entry.options.get(CONF_MEMBERS) or []
            suggested: Mapping[str, Any] = {
                CONF_MEMBERS: [member for member in saved if member in tracked]
            }
        else:
            suggested = user_input
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )
