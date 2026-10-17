"""The shipped English translation must match strings.json."""

from __future__ import annotations

import json
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "home_eta"


def test_english_translation_matches_strings() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (INTEGRATION / "translations" / "en.json").read_text(encoding="utf-8")
    )
    assert english == strings
