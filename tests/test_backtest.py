"""Tests for scripts/backtest.py on synthetic recorder exports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import email.message
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any
import urllib.error
import urllib.request

import pytest

from tests.sim import HOME_LAT, HOME_LON, IOS, Segment, offset_m, simulate

SCRIPT = Path(__file__).parents[1] / "scripts" / "backtest.py"
ZONE_ARG = f"{HOME_LAT},{HOME_LON},100"
PERSON = "person.alex"
T0 = 1_760_000_000.0


@pytest.fixture(name="backtest")
def backtest_fixture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backtest", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["backtest"] = module
    spec.loader.exec_module(module)
    return module


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def row(state: str, at: float, **attributes: Any) -> dict[str, Any]:
    return {
        "entity_id": PERSON,
        "state": state,
        "attributes": {
            "in_zones": ["zone.home"] if state == "home" else [],
            **attributes,
        },
        "last_updated": iso(at),
    }


def trip_rows(
    index: int, segments: list[Segment] | None = None, *, start_m: float = 12_000.0
) -> list[dict[str, Any]]:
    """Return one day's rows: at home, a drive home, then home again."""
    start = T0 + index * 86_400
    rows = [row("home", start - 3_600, gps_accuracy=5)]
    fixes = simulate(
        (start_m, 0.0),
        segments or [Segment((0.0, 0.0), 12.0)],
        policy=IOS,
        seed=index,
        t0=start,
    )
    rows.extend(
        row(
            "not_home",
            f.fix.timestamp,
            latitude=f.fix.latitude,
            longitude=f.fix.longitude,
            gps_accuracy=f.fix.accuracy_m,
            user_id="private",
            entity_picture="/private.jpg",
        )
        for f in fixes
    )
    rows.append(row("home", fixes[-1].fix.timestamp + fixes[-1].true_remaining_s))
    return rows


def journeys(trips: int, **kwargs: Any) -> list[dict[str, Any]]:
    return [r for i in range(trips) for r in trip_rows(i, **kwargs)]


def run(
    backtest: ModuleType, tmp_path: Path, export: Any, *args: str
) -> tuple[int, dict[str, Any]]:
    path = tmp_path / "history.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    out = tmp_path / "result.json"
    code = backtest.main(
        ["--input", str(path), "--zone", ZONE_ARG, "--json", str(out), *args]
    )
    return code, json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}


def test_good_trips_pass(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result = run(backtest, tmp_path, [journeys(8)])
    assert code == backtest.EXIT_PASS
    text = capsys.readouterr().out
    assert f"{PERSON}: 8 trips" in text
    assert "FAIL" not in text
    assert text.count("pass") == 2
    person = result[PERSON]
    assert person["trips"] == 8
    assert person["false_arriving_soon"] == 0
    assert person["missed_arriving_soon"] == 0
    # Expected home only once heading home: not at the first fix of a trip.
    samples = person["samples"]
    assert samples[0]["expected_s"] is None
    assert samples[-1]["expected_s"] is not None
    assert abs(samples[-1]["expected_s"] - samples[-1]["true_s"]) < 120


def test_heading_home_is_scored(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Out to 12 km, an hour there, then the drive home: spotted on the way."""
    segments = [
        Segment((12_000.0, 0.0), 12.0, dwell_s=3_600.0),
        Segment((0.0, 0.0), 12.0),
    ]
    _, result = run(backtest, tmp_path, [journeys(6, segments=segments, start_m=300.0)])
    heading = result[PERSON]["heading_home"]
    assert len(heading["warning_s"]) == 6
    # 20 % of 12 km is 2.4 km, about 3-8 minutes into the 16-minute drive.
    assert all(8 * 60 <= w <= 17 * 60 for w in heading["warning_s"])
    assert heading["false_starts"] == 0
    out = capsys.readouterr().out
    assert "heading home: on at arrival in 6 of 6 trips" in out
    assert "heading home that was not: 0 times, 0.0 h in all" in out


def test_rings_given_with_their_shape_are_replayed(
    backtest: ModuleType, tmp_path: Path
) -> None:
    """A ring at 8 km starts the trip home on the way in, before 20 % progress."""
    segments = [
        Segment((9_000.0, 0.0), 12.0, dwell_s=3_600.0),
        Segment((0.0, 0.0), 12.0),
    ]
    export = [journeys(6, segments=segments, start_m=300.0)]
    _, plain = run(backtest, tmp_path, export)
    _, ringed = run(
        backtest,
        tmp_path,
        export,
        "--enclosing-zone",
        f"zone.ring_8_km={HOME_LAT},{HOME_LON},8000",
    )
    plain_warnings = plain[PERSON]["heading_home"]["warning_s"]
    ringed_warnings = ringed[PERSON]["heading_home"]["warning_s"]
    assert all(r >= w for r, w in zip(ringed_warnings, plain_warnings, strict=True))
    assert sum(ringed_warnings) > sum(plain_warnings)
    # Without its shape a zone around home still counts as enclosing (for
    # arriving soon) but cannot be replayed as a ring.
    _, named = run(backtest, tmp_path, export, "--enclosing-zone", "zone.ring_8_km")
    assert named[PERSON]["heading_home"]["warning_s"] == plain_warnings


@pytest.mark.parametrize(
    "value", ["ring", "zone.ring=51,0", "zone.ring=51,0,-1", "zone.ring="]
)
def test_bad_enclosing_zone_refused(
    backtest: ModuleType, tmp_path: Path, value: str
) -> None:
    export = tmp_path / "history.json"
    export.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        backtest.main(
            ["--input", str(export), "--zone", ZONE_ARG, "--enclosing-zone", value]
        )


def test_early_arriving_soon_fails(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each trip waits 40 minutes 600 m from home: arriving soon is far too early."""
    segments = [
        Segment((600.0, 0.0), 12.0, dwell_s=2_400.0),
        Segment((0.0, 0.0), 1.4),
    ]
    code, result = run(backtest, tmp_path, [journeys(6, segments=segments)])
    assert code == backtest.EXIT_FAIL
    assert result[PERSON]["trips"] == 6
    assert result[PERSON]["false_arriving_soon"] == 6
    assert "10.0 per 10 trips (target <= 1) FAIL" in capsys.readouterr().out


def test_arrivals_without_arriving_soon_are_counted(
    backtest: ModuleType, tmp_path: Path
) -> None:
    """With a 1-minute threshold and the last fix 5 km out, nothing warns."""
    rows = []
    for day in range(5):
        start = T0 + day * 86_400
        rows.append(row("home", start - 3_600, gps_accuracy=5))
        for i, east in enumerate((9_000.0, 8_000.0, 7_000.0, 6_000.0, 5_000.0)):
            lat, lon = offset_m(HOME_LAT, HOME_LON, 0.0, east)
            rows.append(
                row(
                    "not_home",
                    start + 60 * i,
                    latitude=lat,
                    longitude=lon,
                    gps_accuracy=10,
                )
            )
        # Home a minute after the last fix: before the expected arrival
        # (about 8 min) had counted down to the threshold.
        rows.append(row("home", start + 60 * 5))
    _, result = run(backtest, tmp_path, [rows], "--person-threshold", f"{PERSON}=1")
    assert result[PERSON]["trips"] == 5
    assert result[PERSON]["missed_arriving_soon"] == 5


def test_unknown_and_unavailable_rows_are_dropped(backtest: ModuleType) -> None:
    """The integration ignores them, so the replay must too."""
    core = backtest.load_core()
    zone = backtest.Zone("zone.home", HOME_LAT, HOME_LON, 100.0, "Home")
    rows = [
        backtest.Row(state, {}, T0 + i)
        for i, state in enumerate(("unknown", "unavailable", "unknown"))
    ]
    result = backtest.replay(
        core,
        PERSON,
        rows,
        zone,
        params=core["params"].EstimatorParams(),
        threshold_s=900.0,
    )
    assert result.rows == 0
    assert result.trips == 0


def test_too_little_data_gives_no_verdict(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run(backtest, tmp_path, [journeys(2)])
    assert code == backtest.EXIT_NO_VERDICT
    assert "Not enough data for a verdict: 2 completed trips" in (
        capsys.readouterr().out
    )


def test_only_trips_inside_the_export_are_scored(
    backtest: ModuleType, tmp_path: Path
) -> None:
    rows = journeys(6)
    # Starts mid-trip (the departure is before the export) and ends away.
    first_trip_start = next(i for i, r in enumerate(rows) if r["state"] != "home")
    last_home = max(i for i, r in enumerate(rows) if r["state"] == "home")
    clipped = rows[first_trip_start + 3 : last_home]
    _, result = run(backtest, tmp_path, [clipped])
    assert result[PERSON]["trips"] == 4


def test_unknown_rows_are_skipped(backtest: ModuleType, tmp_path: Path) -> None:
    rows = journeys(6)
    noisy = []
    for item in rows:
        noisy.append(item)
        if item["state"] == "not_home":
            noisy.append({**item, "state": "unavailable", "attributes": {}})
    _, plain = run(backtest, tmp_path, [rows])
    _, result = run(backtest, tmp_path, [noisy])
    assert result[PERSON]["trips"] == plain[PERSON]["trips"] == 6
    assert result[PERSON]["samples"] == plain[PERSON]["samples"]


def test_websocket_compressed_export(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compressed = {
        PERSON: [
            {
                "s": s["state"],
                "a": s["attributes"],
                "lu": datetime.fromisoformat(s["last_updated"]).timestamp(),
            }
            for s in journeys(6)
        ]
    }
    code, _ = run(backtest, tmp_path, {"id": 1, "result": compressed})
    assert code == backtest.EXIT_PASS
    assert f"{PERSON}: 6 trips" in capsys.readouterr().out


@pytest.mark.parametrize(
    "export",
    [
        # REST minimal_response: later rows have no entity id or attributes.
        [[row("home", T0), {"state": "not_home", "last_updated": iso(T0 + 60)}]],
        # Websocket minimal_response / no_attributes: rows without "a".
        {PERSON: [{"s": "home", "a": {"in_zones": []}, "lu": T0}, {"s": "x", "lu": 1}]},
        {"result": "nonsense"},
        [[{**row("home", T0), "last_updated": "yesterday"}]],
        # REST no_attributes: every row has an empty attributes object.
        [[{**r, "attributes": {}} for r in journeys(1)]],
    ],
    ids=["rest_minimal", "websocket_minimal", "not_an_export", "bad_time", "no_attrs"],
)
def test_unusable_exports_are_refused(
    backtest: ModuleType,
    tmp_path: Path,
    export: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = run(backtest, tmp_path, export)
    assert code == backtest.EXIT_NO_VERDICT
    assert "error:" in capsys.readouterr().err


def test_missing_person_and_missing_positions_are_reported(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run(backtest, tmp_path, [journeys(1)], "--person", "person.ioanis")
    assert code == backtest.EXIT_NO_VERDICT
    err = capsys.readouterr().err
    assert "person.ioanis is not in the export" in err

    # State changes only, as the default history export gives.
    states_only = [r for r in journeys(2) if "latitude" not in r["attributes"]]
    run(backtest, tmp_path, [states_only])
    assert "no position in the export" in capsys.readouterr().err


def test_parameters_and_thresholds_apply(
    backtest: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, result = run(
        backtest,
        tmp_path,
        [journeys(5)],
        "--person-threshold",
        f"{PERSON}=5",
        "--param",
        "stale_after_s=1200",
        "--param",
        "homeward_min_speed_mps=0.4",
    )
    assert result[PERSON]["threshold_min"] == 5
    assert "arriving soon (5 min)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["--param", "no_such_thing=1"],
        ["--param", "stale_after_s=soon"],
        ["--param", "stale_after_s=60"],  # shorter than stationary_after_s
        ["--param", "mode_change_confirm_n=2.5"],
        ["--param", "nonsense"],
        ["--person-threshold", f"{PERSON}=0"],
        ["--threshold-min", "0"],
        ["--days", "nan"],
    ],
)
def test_bad_options_are_refused(
    backtest: ModuleType, tmp_path: Path, args: list[str]
) -> None:
    export = tmp_path / "history.json"
    export.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        backtest.main(["--input", str(export), "--zone", ZONE_ARG, *args])


@pytest.mark.parametrize(
    "zone", ["nonsense", "51,0", "51,0,-100", "91,0,100", "51,181,100", "nan,0,100"]
)
def test_bad_zone_refused(backtest: ModuleType, tmp_path: Path, zone: str) -> None:
    export = tmp_path / "history.json"
    export.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        backtest.main(["--input", str(export), "--zone", zone])


def test_missing_input_and_zone_refused(backtest: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        backtest.main(["--input", str(tmp_path / "absent.json"), "--zone", ZONE_ARG])
    export = tmp_path / "history.json"
    export.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        backtest.main(["--input", str(export)])


def test_legacy_state_without_in_zones(backtest: ModuleType) -> None:
    core = backtest.load_core()
    zone = backtest.Zone("zone.home", HOME_LAT, HOME_LON, 100.0, "Home")
    at_home = backtest.report_from_row(core, backtest.Row("home", {}, 0.0), zone)
    assert at_home.in_target
    elsewhere = backtest.report_from_row(core, backtest.Row("Work", {}, 0.0), zone)
    assert not elsewhere.in_target
    assert elsewhere.other_zones == ("zone:Work",)


# -- pulling from Home Assistant ---------------------------------------------------


def zone_state(entity_id: str, lat: float, lon: float, radius: float) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": "0",
        "attributes": {
            "latitude": lat,
            "longitude": lon,
            "radius": radius,
            "friendly_name": entity_id.split(".")[1].title(),
        },
    }


def fake_home_assistant(
    history: Any, states: Any = None, *, fail: Exception | None = None
) -> tuple[list[urllib.request.Request], Callable[[urllib.request.Request], Any]]:
    """Return the requests seen and an ``_open`` replacement."""
    requests: list[urllib.request.Request] = []
    near_lat, near_lon = offset_m(HOME_LAT, HOME_LON, 300.0, 0.0)
    states = states or [
        zone_state("zone.home", HOME_LAT, HOME_LON, 100),
        zone_state("zone.neighbourhood", near_lat, near_lon, 2_000),
        zone_state("zone.station", HOME_LAT, HOME_LON + 0.02, 150),
        {"entity_id": "zone.broken", "attributes": {}},
        {"entity_id": "sensor.other", "state": "1", "attributes": {}},
    ]

    def open_(request: urllib.request.Request) -> Any:
        requests.append(request)
        if "/api/states" in request.full_url:
            if fail is not None:
                raise fail
            return io.BytesIO(json.dumps(states).encode())
        return io.BytesIO(json.dumps(history).encode())

    return requests, open_


def pull(backtest: ModuleType, *extra: str) -> int:
    return int(
        backtest.main(
            ["--url", "http://ha.test:8123/", "--person", PERSON, "--days", "3", *extra]
        )
    )


def test_pull_from_home_assistant(
    backtest: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests, open_ = fake_home_assistant([journeys(6)])
    monkeypatch.setattr(backtest, "_open", open_)
    monkeypatch.setenv("HA_TOKEN", "test-token")
    saved = tmp_path / "pulled.json"
    assert pull(backtest, "--save", str(saved)) == backtest.EXIT_PASS
    out = capsys.readouterr().out
    assert f"{PERSON}: 6 trips" in out
    # The neighbourhood wholly contains home; the station does not.
    near_lat, near_lon = offset_m(HOME_LAT, HOME_LON, 300.0, 0.0)
    assert f"--enclosing-zone zone.neighbourhood={near_lat},{near_lon},2000" in out
    assert "zone.station" not in out

    history_url = requests[0].full_url
    assert "significant_changes_only=0" in history_url
    assert f"filter_entity_id={PERSON}" in history_url
    # The token is never forwarded on a redirect.
    assert requests[0].unredirected_hdrs["Authorization"] == "Bearer test-token"
    assert "Authorization" not in requests[0].headers

    # The saved export is private and holds only what the estimators read.
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    export = json.loads(saved.read_text(encoding="utf-8"))
    keys = {key for series in export for item in series for key in item["attributes"]}
    assert keys == {"in_zones", "latitude", "longitude", "gps_accuracy"}
    # ...and replays to the same result.
    capsys.readouterr()
    assert (
        backtest.main(["--input", str(saved), "--zone", ZONE_ARG]) == backtest.EXIT_PASS
    )
    assert f"{PERSON}: 6 trips" in capsys.readouterr().out


def test_history_is_saved_before_the_zone_request_fails(
    backtest: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, open_ = fake_home_assistant(
        [journeys(1)], fail=urllib.error.URLError(ConnectionResetError("reset"))
    )
    monkeypatch.setattr(backtest, "_open", open_)
    monkeypatch.setenv("HA_TOKEN", "test-token")
    saved = tmp_path / "pulled.json"
    assert pull(backtest, "--save", str(saved)) == backtest.EXIT_NO_VERDICT
    assert saved.exists()
    assert "Cannot reach" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (urllib.error.HTTPError("u", 401, "Unauthorized", None, None), "rejected"),
        (urllib.error.HTTPError("u", 500, "Oops", None, None), "HTTP 500"),
        (urllib.error.URLError("no route"), "Cannot reach"),
        (TimeoutError("slow"), "failed"),
        (json.JSONDecodeError("bad", "x", 0), "did not return JSON"),
    ],
)
def test_connection_problems_are_reported_plainly(
    backtest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    message: str,
) -> None:
    def open_(request: urllib.request.Request) -> Any:
        raise error

    monkeypatch.setattr(backtest, "_open", open_)
    monkeypatch.setenv("HA_TOKEN", "test-token")
    assert pull(backtest) == backtest.EXIT_NO_VERDICT
    assert message in capsys.readouterr().err


def test_zone_must_exist_with_a_location(
    backtest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, open_ = fake_home_assistant(
        [journeys(1)], states=[{"entity_id": "zone.home", "attributes": {}}]
    )
    monkeypatch.setattr(backtest, "_open", open_)
    monkeypatch.setenv("HA_TOKEN", "test-token")
    assert pull(backtest) == backtest.EXIT_NO_VERDICT
    assert "zone.home was not found" in capsys.readouterr().err


def test_redirects_are_refused(backtest: ModuleType) -> None:
    handler = backtest._RefuseRedirects()
    request = urllib.request.Request("http://ha.test:8123/api/states")
    with pytest.raises(backtest.BacktestError, match="redirects to"):
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", email.message.Message(), "http://x"
        )


def test_pull_requires_token_and_person(
    backtest: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HA_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        backtest.main(["--url", "http://ha.test:8123", "--person", PERSON])
    monkeypatch.setenv("HA_TOKEN", "test-token")
    with pytest.raises(SystemExit):
        backtest.main(["--url", "http://ha.test:8123"])
    with pytest.raises(SystemExit):
        backtest.main(["--url", "ftp://ha.test", "--person", PERSON])
