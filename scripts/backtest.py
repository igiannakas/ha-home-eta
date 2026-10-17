#!/usr/bin/env python3
r"""Replay recorder history through the Home ETA estimators and score them.

Every trip that ends inside the target zone is replayed fix by fix through the
integration's own estimator code, and the ETA at each fix is compared with the
time actually taken. Learned history is built up online, trip by trip, so the
historical ETA is scored only on trips it had not seen.

Pull straight from Home Assistant (the token is read from HA_TOKEN):

    HA_TOKEN=... python3 scripts/backtest.py --url http://homeassistant.local:8123 \
        --person person.alex --person person.sam --days 10 --save history.json

Re-run on a saved export (REST ``/api/history/period`` output or the
websocket ``history/history_during_period`` result, made without
``minimal_response`` or ``no_attributes``):

    python3 scripts/backtest.py --input history.json --zone LATITUDE,LONGITUDE,RADIUS_M

Exit status: 0 when every acceptance check passes, 1 when one fails, 2 when
there is not enough data for a verdict or the input cannot be used.

Only the Python standard library (3.11 or later) is needed; no Home Assistant.

Known difference from the live integration: after a Home Assistant restart the
recorder stores the person's last position again, stamped with the restart
time. The integration ignores those for speed until Home Assistant has started;
a recorder export does not mark them, so they are replayed as ordinary fixes.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
import email.message
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import sys
from types import ModuleType
from typing import IO, Any, NoReturn
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "custom_components" / "home_eta" / "core"
CORE_PACKAGE = "home_eta_core"
TICK_S = 60.0
HTTP_TIMEOUT_S = 120.0
DISTANCE_BANDS_M: tuple[tuple[float, float], ...] = (
    (0.0, 1_000.0),
    (1_000.0, 5_000.0),
    (5_000.0, 20_000.0),
    (20_000.0, math.inf),
)
ACCEPT_P90_MIN = 10.0
ACCEPT_P90_BELOW_M = 5_000.0
ACCEPT_FALSE_SOON_PER_10 = 1.0
ACCEPT_MIN_TRIPS = 5
"""Completed trips needed (over everyone) before a verdict is given."""
ACCEPT_MIN_FIXES = 20
"""Scored fixes within the acceptance distance needed before a verdict."""

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NO_VERDICT = 2

KEEP_ATTRIBUTES = ("in_zones", "latitude", "longitude", "gps_accuracy")
"""All the estimators read; a saved export keeps nothing else."""


class BacktestError(Exception):
    """A problem with the input or the connection, reported without a traceback."""


def load_core() -> dict[str, ModuleType]:
    """Load the HA-free core package without importing the integration.

    Importing ``custom_components.home_eta.core`` would run the integration's
    ``__init__`` (which needs Home Assistant), so the core directory is loaded
    as a stand-alone package under another name.
    """
    spec = importlib.util.spec_from_file_location(
        CORE_PACKAGE,
        CORE_DIR / "__init__.py",
        submodule_search_locations=[str(CORE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load the estimator core from {CORE_DIR}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[CORE_PACKAGE] = package
    spec.loader.exec_module(package)
    return {
        name: importlib.import_module(f"{CORE_PACKAGE}.{name}")
        for name in ("approach", "geo", "kinematic", "params", "tracker")
    }


@dataclass(frozen=True, slots=True)
class Row:
    """One recorded state of a person."""

    state: str
    attributes: Mapping[str, Any]
    timestamp: float


@dataclass(frozen=True, slots=True)
class Zone:
    """A zone: the target, or one that might enclose it."""

    entity_id: str
    latitude: float
    longitude: float
    radius_m: float
    name: str


# -- loading -----------------------------------------------------------------


def _parse_time(value: Any) -> float:
    if isinstance(value, bool):
        raise BacktestError(f"Unrecognised timestamp {value!r}")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as err:
            raise BacktestError(f"Unrecognised timestamp {value!r}") from err
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    raise BacktestError(f"Unrecognised timestamp {value!r}")


_STRIPPED = (
    "the export has no attributes on some rows; export it without "
    "minimal_response or no_attributes"
)


def parse_export(data: Any) -> dict[str, list[Row]]:
    """Return rows per entity from a REST or websocket history export."""
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    rows: dict[str, list[Row]] = {}
    if isinstance(data, list):
        # REST: a list per entity of full state objects.
        for series in data:
            if not isinstance(series, list):
                raise BacktestError("Unrecognised export format")
            for item in series:
                if not isinstance(item, dict) or "entity_id" not in item:
                    raise BacktestError(f"Rows without an entity id: {_STRIPPED}")
                if not isinstance(item.get("attributes"), dict):
                    raise BacktestError(_STRIPPED.capitalize())
                stamp = item.get("last_updated") or item.get("last_changed")
                rows.setdefault(item["entity_id"], []).append(
                    Row(str(item["state"]), item["attributes"], _parse_time(stamp))
                )
    elif isinstance(data, dict):
        # Websocket compressed: {entity_id: [{"s", "a", "lu", "lc"}]}.
        for entity_id, series in data.items():
            if not isinstance(series, list):
                raise BacktestError("Unrecognised export format")
            for item in series:
                if not isinstance(item, dict) or not isinstance(item.get("a"), dict):
                    raise BacktestError(_STRIPPED.capitalize())
                stamp = item.get("lu", item.get("lc"))
                rows.setdefault(entity_id, []).append(
                    Row(str(item["s"]), item["a"], _parse_time(stamp))
                )
    else:
        raise BacktestError("Unrecognised export format")
    if rows and not any(
        key in row.attributes
        for series in rows.values()
        for row in series
        for key in KEEP_ATTRIBUTES
    ):
        raise BacktestError(_STRIPPED.capitalize())
    for series in rows.values():
        series.sort(key=lambda row: row.timestamp)
    return rows


def minimal_export(rows: Mapping[str, list[Row]]) -> list[list[dict[str, Any]]]:
    """Return a REST-style export holding only what the backtest reads."""
    return [
        [
            {
                "entity_id": entity_id,
                "state": row.state,
                "attributes": {
                    key: row.attributes[key]
                    for key in KEEP_ATTRIBUTES
                    if key in row.attributes
                },
                "last_updated": datetime.fromtimestamp(row.timestamp, UTC).isoformat(),
            }
            for row in series
        ]
        for entity_id, series in rows.items()
    ]


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read: it holds location history."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    # An existing file keeps its old mode through O_CREAT; tighten it too.
    os.chmod(path, 0o600)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token must not travel to another address."""

    # The signature is urllib's; typing.override needs Python 3.12.
    def redirect_request(  # noqa: PLR0917
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: email.message.Message,
        newurl: str,
    ) -> NoReturn:
        raise BacktestError(
            f"{req.full_url} redirects to {newurl}; pass that address with --url"
        )


_OPENER = urllib.request.build_opener(_RefuseRedirects())


def _open(request: urllib.request.Request) -> Any:
    return _OPENER.open(request, timeout=HTTP_TIMEOUT_S)


def _get_json(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with _open(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        if err.code == 401:
            raise BacktestError(
                "Home Assistant rejected the token in HA_TOKEN"
            ) from err
        raise BacktestError(f"HTTP {err.code} from {url}") from err
    except urllib.error.URLError as err:
        raise BacktestError(f"Cannot reach {url}: {err.reason}") from err
    except OSError as err:
        raise BacktestError(f"Connection to {url} failed: {err}") from err
    except json.JSONDecodeError as err:
        raise BacktestError(f"{url} did not return JSON") from err


def fetch_history(url: str, token: str, persons: list[str], days: float) -> Any:
    """Pull person history from Home Assistant's REST API."""
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    query = urllib.parse.urlencode(
        {
            "filter_entity_id": ",".join(persons),
            "end_time": end.isoformat(),
            # Person position changes are attribute-only; keep them all.
            "significant_changes_only": "0",
        }
    )
    path = urllib.parse.quote(start.isoformat())
    return _get_json(f"{url}/api/history/period/{path}?{query}", token)


def fetch_zones(url: str, token: str, zone_id: str) -> tuple[Zone, list[Zone]]:
    """Return the target zone and every zone, from the current states."""
    states = _get_json(f"{url}/api/states", token)
    if not isinstance(states, list):
        raise BacktestError(f"{url}/api/states did not return a list of states")
    zones = [
        zone
        for state in states
        if isinstance(state, dict)
        and str(state.get("entity_id", "")).startswith("zone.")
        and (zone := _zone_from_state(state)) is not None
    ]
    target = next((z for z in zones if z.entity_id == zone_id), None)
    if target is None:
        raise BacktestError(f"{zone_id} was not found or has no location")
    return target, zones


def _zone_from_state(state: Mapping[str, Any]) -> Zone | None:
    attributes = state.get("attributes") or {}
    try:
        zone = Zone(
            str(state["entity_id"]),
            float(attributes["latitude"]),
            float(attributes["longitude"]),
            float(attributes["radius"]),
            str(attributes.get("friendly_name", state["entity_id"])),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not _valid_zone(zone.latitude, zone.longitude, zone.radius_m):
        return None
    return zone


def _valid_zone(latitude: float, longitude: float, radius_m: float) -> bool:
    return (
        all(math.isfinite(v) for v in (latitude, longitude, radius_m))
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
        and radius_m > 0
    )


def enclosing_zones(
    core: dict[str, ModuleType], target: Zone, zones: Iterable[Zone]
) -> frozenset[str]:
    """Return the zones that wholly contain the target, as Home Assistant does."""
    haversine = core["geo"].haversine_m
    return frozenset(
        zone.entity_id
        for zone in zones
        if zone.entity_id != target.entity_id
        and haversine(target.latitude, target.longitude, zone.latitude, zone.longitude)
        + target.radius_m
        <= zone.radius_m
    )


# -- arguments ---------------------------------------------------------------


def _positive(value: str) -> float:
    try:
        number = float(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from err
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {value!r}")
    return number


def _zone_arg(value: str) -> tuple[float, float, float]:
    try:
        latitude, longitude, radius = (float(part) for part in value.split(","))
    except ValueError as err:
        raise argparse.ArgumentTypeError(
            "expected LATITUDE,LONGITUDE,RADIUS_M"
        ) from err
    if not _valid_zone(latitude, longitude, radius):
        raise argparse.ArgumentTypeError(
            "latitude -90..90, longitude -180..180 and a positive radius"
        )
    return latitude, longitude, radius


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"no such file: {value}")
    return path


def _key_value(value: str) -> tuple[str, str]:
    key, sep, raw = value.partition("=")
    if not sep or not key or not raw:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {value!r}")
    return key.strip(), raw.strip()


def _params(
    core: dict[str, ModuleType], pairs: list[tuple[str, str]], parser: Any
) -> Any:
    """Build the estimator parameters from ``--param`` overrides."""
    params_cls = core["params"].EstimatorParams
    types = {f.name: f.type for f in fields(params_cls)}
    overrides: dict[str, Any] = {}
    for key, raw in pairs:
        kind = types.get(key)
        if kind is None:
            parser.error(f"--param: unknown parameter {key!r}")
        try:
            overrides[key] = int(raw) if kind in ("int", int) else float(raw)
        except ValueError:
            parser.error(f"--param {key}: not a number: {raw!r}")
    try:
        return params_cls(**overrides)
    except (TypeError, ValueError) as err:
        parser.error(f"--param: {err}")


# -- replay ------------------------------------------------------------------


@dataclass(slots=True)
class Sample:
    """The estimates at one accepted fix of a completed trip."""

    distance_m: float
    true_s: float
    kinematic_s: float | None
    historical_s: float | None


@dataclass(slots=True)
class PersonResult:
    """Scores for one person."""

    person: str
    threshold_s: float
    rows: int = 0
    positions: int = 0
    trips: int = 0
    samples: list[Sample] = field(default_factory=list)
    false_soon: int = 0
    missed_soon: int = 0
    soon_lead_s: list[float] = field(default_factory=list)


def report_from_row(
    core: dict[str, ModuleType],
    row: Row,
    zone: Zone,
    enclosing: frozenset[str] = frozenset(),
) -> Any:
    """Build a PersonReport exactly as the integration does."""
    return core["tracker"].build_report(
        row.state,
        row.attributes,
        row.timestamp,
        zone.entity_id,
        zone.name,
        enclosing_zones=enclosing,
    )


def replay(
    core: dict[str, ModuleType],
    person: str,
    rows: list[Row],
    zone: Zone,
    *,
    params: Any,
    threshold_s: float,
    ignored: frozenset[str],
    enclosing: frozenset[str] = frozenset(),
) -> PersonResult:
    """Replay one person's history and score every completed trip."""
    approach = core["approach"]
    tracker_mod = core["tracker"]
    target = core["kinematic"].Target(zone.latitude, zone.longitude, zone.radius_m)
    rows = [r for r in rows if r.state not in ("unknown", "unavailable")]
    result = PersonResult(person, threshold_s, rows=len(rows))
    if not rows:
        return result

    first = report_from_row(core, rows[0], zone, enclosing)
    config = tracker_mod.TrackerConfig(params, ignored, historical=True)
    trk = tracker_mod.PersonTracker(config, target, at_target=first.in_target)
    trk.set_threshold(threshold_s)

    trip: list[tuple[float, float, float | None, float | None]] = []
    soon_entries: list[float] = []
    trip_live = False  # only score trips that began inside this export
    last_time = first.timestamp

    def on_transition(transition: Any, at: float) -> None:
        nonlocal trip_live
        if transition is None:
            return
        if transition.current is approach.ApproachState.ARRIVING_SOON:
            soon_entries.append(at)
        if transition.previous is approach.ApproachState.HOME:
            trip.clear()
            soon_entries.clear()
            trip_live = True

    for row in rows:
        report = report_from_row(core, row, zone, enclosing)
        if report.latitude is not None and report.longitude is not None:
            result.positions += 1
        # Housekeeping ticks between reports, as the integration runs them.
        tick_at = last_time + TICK_S
        while (
            tick_at < report.timestamp and trk.state is not approach.ApproachState.HOME
        ):
            on_transition(trk.tick(tick_at), tick_at)
            tick_at += TICK_S
        last_time = report.timestamp

        before = trk.state
        update = trk.update(report)
        on_transition(update.transition, report.timestamp)

        if update.outcome is not None and update.outcome.accepted and trip_live:
            snap = trk.snapshot(report.timestamp)
            kinematic = (
                None
                if snap.kinematic is None
                else snap.kinematic.arrival - report.timestamp
            )
            historical = (
                None
                if snap.historical is None
                else snap.historical.arrival - report.timestamp
            )
            if update.outcome.distance_m is not None:
                trip.append(
                    (report.timestamp, update.outcome.distance_m, kinematic, historical)
                )

        arrived = (
            update.transition is not None
            and update.transition.current is approach.ApproachState.HOME
        )
        if arrived and trip_live:
            arrival = report.timestamp
            duration = arrival - trip[0][0] if trip else 0.0
            if (
                len(trip) >= params.min_trip_fixes
                and duration >= params.min_trip_duration_s
            ):
                result.trips += 1
                result.samples.extend(
                    Sample(d, arrival - t, k, h) for t, d, k, h in trip
                )
                for entry in soon_entries:
                    lead = arrival - entry
                    result.soon_lead_s.append(lead)
                    if lead > 2 * threshold_s:
                        result.false_soon += 1
                if before is not approach.ApproachState.ARRIVING_SOON:
                    result.missed_soon += 1
            trip.clear()
            soon_entries.clear()
            trip_live = False
    return result


# -- reporting ---------------------------------------------------------------


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _errors_min(samples: Iterable[Sample], attr: str) -> tuple[list[float], int]:
    errors: list[float] = []
    total = 0
    for sample in samples:
        total += 1
        predicted = getattr(sample, attr)
        if predicted is not None:
            errors.append(abs(predicted - sample.true_s) / 60.0)
    return errors, total


def _band_label(low: float, high: float) -> str:
    if math.isinf(high):
        return f"over {low / 1000:g} km"
    return f"{low / 1000:g}-{high / 1000:g} km"


def summarise(result: PersonResult) -> Iterator[str]:
    """Yield report lines for one person."""
    yield ""
    yield f"{result.person}: {result.trips} trips, {len(result.samples)} fixes scored"
    if not result.samples:
        yield "  No completed trips with enough fixes in this window."
        return
    yield (
        f"  {'distance':<12}{'fixes':>6}  {'kinematic: cover  med  p90 (min)':>34}"
        f"  {'historical: cover  med  p90 (min)':>35}"
    )
    for low, high in DISTANCE_BANDS_M:
        band = [s for s in result.samples if low <= s.distance_m < high]
        if not band:
            continue
        cells = []
        for attr in ("kinematic_s", "historical_s"):
            errors, total = _errors_min(band, attr)
            if errors:
                cells.append(
                    f"{len(errors) / total:>11.0%} {statistics.median(errors):>5.1f}"
                    f" {_quantile(errors, 0.9):>5.1f}"
                )
            else:
                cells.append(f"{'0%':>11} {'-':>5} {'-':>5}")
        label = _band_label(low, high)
        yield f"  {label:<12}{len(band):>6}  {cells[0]:>34}  {cells[1]:>35}"
    per_10 = 10 * result.false_soon / result.trips if result.trips else 0.0
    yield (
        f"  arriving soon ({result.threshold_s / 60:g} min): {result.false_soon} "
        f"early (> 2x threshold, {per_10:.1f} per 10 trips), {result.missed_soon} "
        "trips arrived without it"
    )
    if result.soon_lead_s:
        yield (
            "  lead time when it turned on: median "
            f"{statistics.median(result.soon_lead_s) / 60:.1f} min"
        )


def acceptance(results: list[PersonResult]) -> tuple[list[str], int]:
    """Return the go-live acceptance report and the exit status it implies."""
    near = [s for r in results for s in r.samples if s.distance_m < ACCEPT_P90_BELOW_M]
    errors, _ = _errors_min(near, "kinematic_s")
    trips = sum(r.trips for r in results)
    false_soon = sum(r.false_soon for r in results)
    lines = ["", "Acceptance"]
    if trips < ACCEPT_MIN_TRIPS or len(errors) < ACCEPT_MIN_FIXES:
        lines.append(
            f"  Not enough data for a verdict: {trips} completed trips (need "
            f"{ACCEPT_MIN_TRIPS}) and {len(errors)} scored fixes under "
            f"{ACCEPT_P90_BELOW_M / 1000:g} km (need {ACCEPT_MIN_FIXES})."
        )
        return lines, EXIT_NO_VERDICT
    p90 = _quantile(errors, 0.9)
    p90_ok = p90 <= ACCEPT_P90_MIN
    lines.append(
        f"  kinematic p90 error under {ACCEPT_P90_BELOW_M / 1000:g} km: "
        f"{p90:.1f} min (target <= {ACCEPT_P90_MIN:g}) "
        f"{'pass' if p90_ok else 'FAIL'}"
    )
    per_10 = 10 * false_soon / trips
    soon_ok = per_10 <= ACCEPT_FALSE_SOON_PER_10
    lines.append(
        f"  early arriving-soon: {per_10:.1f} per 10 trips "
        f"(target <= {ACCEPT_FALSE_SOON_PER_10:g}) {'pass' if soon_ok else 'FAIL'}"
    )
    return lines, EXIT_PASS if p90_ok and soon_ok else EXIT_FAIL


# -- entry point -------------------------------------------------------------


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Home Assistant base URL")
    source.add_argument(
        "--input", type=_existing_file, help="saved history export (JSON)"
    )
    parser.add_argument(
        "--person",
        action="append",
        default=[],
        help="person entity id (repeat); default: every person.* in the export",
    )
    parser.add_argument("--zone-entity", default="zone.home", help="target zone")
    parser.add_argument(
        "--zone", type=_zone_arg, help="LATITUDE,LONGITUDE,RADIUS_M (with --input)"
    )
    parser.add_argument(
        "--enclosing-zone",
        action="append",
        default=[],
        help="with --input: a zone that contains the target, e.g. a neighbourhood "
        "(repeat); with --url these are found automatically",
    )
    parser.add_argument(
        "--days", type=_positive, default=10.0, help="days of history to pull"
    )
    parser.add_argument(
        "--save",
        type=Path,
        help="write the pulled history here (owner-only, positions and zones only)",
    )
    parser.add_argument(
        "--threshold-min",
        type=_positive,
        default=15.0,
        help="arriving-soon threshold in minutes",
    )
    parser.add_argument(
        "--person-threshold",
        type=_key_value,
        action="append",
        default=[],
        metavar="PERSON=MINUTES",
        help="a person's own arriving-soon threshold (repeat)",
    )
    parser.add_argument(
        "--param",
        type=_key_value,
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="estimator parameter as set in Advanced, e.g. stale_after_s=1200",
    )
    parser.add_argument(
        "--ignore-zone",
        action="append",
        default=[],
        help="zone entity id that ends a leg, e.g. zone.work (repeat)",
    )
    parser.add_argument("--json", type=Path, help="write per-fix results here")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the backtest and return the exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.url and not args.url.startswith(("http://", "https://")):
        parser.error("--url must start with http:// or https://")
    if args.url and not args.person:
        parser.error("--person is required with --url")
    if args.input and args.zone is None:
        parser.error("--zone is required with --input")

    thresholds: dict[str, float] = {}
    for person, raw in args.person_threshold:
        try:
            thresholds[person] = _positive(raw) * 60.0
        except argparse.ArgumentTypeError as err:
            parser.error(f"--person-threshold {person}: {err}")

    core = load_core()
    params = _params(core, args.param, parser)
    try:
        if args.url:
            token = os.environ.get("HA_TOKEN")
            if not token:
                parser.error("set HA_TOKEN to a long-lived access token")
            base = args.url.rstrip("/")
            raw = fetch_history(base, token, args.person, args.days)
            rows = parse_export(raw)
            if args.save:
                # Saved before anything else can fail, so a slow pull is kept.
                write_private(args.save, json.dumps(minimal_export(rows)))
            zone, all_zones = fetch_zones(base, token, args.zone_entity)
            enclosing = enclosing_zones(core, zone, all_zones)
            rerun = [f"--zone {zone.latitude},{zone.longitude},{zone.radius_m:g}"]
            rerun += [f"--enclosing-zone {z}" for z in sorted(enclosing)]
            print(f"To re-run on a saved export: {' '.join(rerun)}")
        else:
            rows = parse_export(json.loads(args.input.read_text(encoding="utf-8")))
            latitude, longitude, radius = args.zone
            zone = Zone(
                args.zone_entity,
                latitude,
                longitude,
                radius,
                args.zone_entity.removeprefix("zone.").title(),
            )
            enclosing = frozenset(args.enclosing_zone)
    except BacktestError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_NO_VERDICT
    except (OSError, json.JSONDecodeError) as err:
        print(f"error: cannot read the export: {err}", file=sys.stderr)
        return EXIT_NO_VERDICT

    persons = args.person or sorted(e for e in rows if e.startswith("person."))
    for person in persons:
        if person not in rows:
            _warn(f"{person} is not in the export")
    persons = [p for p in persons if p in rows]
    if not persons:
        print("error: none of the people asked for are in the export", file=sys.stderr)
        return EXIT_NO_VERDICT
    for person in thresholds:
        if person not in persons:
            _warn(f"--person-threshold {person}: not among the people replayed")

    results = [
        replay(
            core,
            person,
            rows[person],
            zone,
            params=params,
            threshold_s=thresholds.get(person, args.threshold_min * 60.0),
            ignored=frozenset(args.ignore_zone),
            enclosing=enclosing,
        )
        for person in persons
    ]
    for result in results:
        if result.rows and not result.positions:
            _warn(
                f"{result.person} has no position in the export; export with "
                "significant_changes_only=0 so position updates are included"
            )

    print(f"Target {zone.entity_id}, radius {zone.radius_m:g} m")
    for result in results:
        for line in summarise(result):
            print(line)
    lines, status = acceptance(results)
    for line in lines:
        print(line)

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    r.person: {
                        "trips": r.trips,
                        "threshold_min": r.threshold_s / 60.0,
                        "false_arriving_soon": r.false_soon,
                        "missed_arriving_soon": r.missed_soon,
                        "samples": [
                            {
                                "distance_m": round(s.distance_m, 1),
                                "true_s": round(s.true_s, 1),
                                "kinematic_s": s.kinematic_s,
                                "historical_s": s.historical_s,
                            }
                            for s in r.samples
                        ],
                    }
                    for r in results
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
