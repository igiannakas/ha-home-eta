"""Constants for the Home ETA integration.

Estimator tunables and their defaults live in ``core/params.py``; this module
holds the Home Assistant-facing names.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "home_eta"

CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_TYPE_PERSON: Final = "person"
ENTRY_TYPE_GROUP: Final = "group"

# Person entries: data
CONF_PERSON: Final = "person"
CONF_ZONE: Final = "zone"

# Person entries: options
CONF_NEAR_HOME_RADIUS_M: Final = "near_home_radius_m"
CONF_NEAR_HOME_FADE_AFTER_MIN: Final = "near_home_fade_after_min"
CONF_ADVANCED: Final = "advanced"

# Advanced options map one-to-one onto EstimatorParams fields.
ADVANCED_PARAMS: Final = (
    "max_gps_accuracy_m",
    "speed_filter_tau_s",
    "arriving_soon_distance_m",
    "stale_after_s",
)

DEFAULT_NEAR_HOME_RADIUS_M: Final = 3_000
MIN_NEAR_HOME_RADIUS_M: Final = 1_000
MAX_NEAR_HOME_RADIUS_M: Final = 30_000
NEAR_HOME_FULL_M: Final = 800.0
"""Near home is 100 % at this distance or closer."""
DEFAULT_NEAR_HOME_FADE_AFTER_MIN: Final = 90
MAX_NEAR_HOME_FADE_AFTER_MIN: Final = 720
NEAR_HOME_FADE_MIN: Final = 60
"""After the fade starts, near home falls to 0 over this many minutes."""

# Group entries: options
CONF_MEMBERS: Final = "members"

DEFAULT_ZONE: Final = "zone.home"
DEFAULT_GROUP_NAME: Final = "Home ETA"

TICK_INTERVAL: Final = timedelta(seconds=60)
FIX_LOG_SIZE: Final = 20
DIAGNOSTICS_COORDINATE_DECIMALS: Final = 2

# Typical speeds learned from each person's trips home
SPEEDS_STORE_VERSION: Final = 1
SPEEDS_SAVE_DELAY_S: Final = 30.0
SPEEDS_REFIT_INTERVAL_S: Final = 86_400.0
"""Learned typical speeds are refitted daily: older trips count for less."""
HISTORY_LOOKBACK: Final = timedelta(days=60)
"""How far back the recorder's history is learned from (it keeps 10 days by
default; all of it is used when it keeps more)."""
RELEARN_KEEP_S: Final = 600.0
"""Relearning keeps trips learned this recently: the recorder may not have
written their last states yet."""

SIGNAL_PERSON_UPDATED: Final = f"{DOMAIN}_person_updated_{{zone}}"
"""Dispatcher signal, formatted with the zone entity id."""

# Entity keys (also translation keys and unique-id suffixes)
KEY_STATUS: Final = "status"
KEY_THRESHOLD: Final = "arriving_soon_threshold"
KEY_EXPECTED_HOME: Final = "expected_home"
KEY_MINUTES: Final = "minutes_to_home"
KEY_DISTANCE: Final = "distance_to_home"
KEY_NEAR_HOME: Final = "near_home"
KEY_SOMEONE_SOON: Final = "someone_arriving_soon"
KEY_TYPICAL_SPEEDS: Final = "typical_speeds"
KEY_RELEARN_SPEEDS: Final = "relearn_typical_speeds"
