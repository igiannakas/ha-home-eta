# Home ETA

A Home Assistant integration that estimates when each person will arrive home, and who will be first, from the Companion app's phone location. It works whether you walk, take the train or drive, needs no map or routing service, and never reads step counters.

Each person is added on their own, with their own sensors and their own "arriving soon" threshold. A separate **first arrival** entry groups people heading to the same zone and tells you who is due first.

## What you get

For each person (entity IDs shown for a person called "person"):

| Entity | What it shows |
| --- | --- |
| `sensor.person_eta_kinematic` | Arrival time from distance and closing speed. The primary ETA. |
| `sensor.person_eta_historical` | Arrival time from how long past trips took from the same distance. Only when learning is turned on, and only once five past trips started at least that far out. |
| `sensor.person_minutes_to_home` | Minutes until the kinematic ETA. |
| `sensor.person_distance_to_home` | Straight-line distance to the zone centre. |
| `sensor.person_approach` | `home`, `away`, `approaching`, `arriving_soon` or `stationary`; unknown until the first location arrives. |
| `binary_sensor.person_arriving_soon` | On while arriving soon. |
| `binary_sensor.person_heading_home` | On from early in the trip home until arrival, for warming the house (see below). Attributes: why and since when, the expected arrival, and where the speed behind it came from. |
| `number.person_arriving_soon_threshold` | Minutes out that count as arriving soon (default 15). |
| `sensor.person_closing_speed` | Diagnostic: filtered speed towards the zone, negative when moving away. |
| `sensor.person_eta_confidence` | Diagnostic: `high`, `medium` or `low`. |

For a first-arrival group (named "Home ETA" by default):

| Entity | What it shows |
| --- | --- |
| `sensor.home_eta_first_arrival` | When the first person on their way will arrive. Attributes list everyone on the way with an ETA, who is home, and any member without data. |
| `sensor.home_eta_first_arrival_person` | Who that is. |
| `sensor.home_eta_first_arrival_minutes` | Minutes until then. |
| `binary_sensor.home_eta_someone_arriving_soon` | On while any member is arriving soon by their own threshold. Its `persons` and `names` attributes say who; unknown while no member has data. |
| `sensor.home_eta_expected_home` | When the first person heading home is expected; unknown while nobody is. Its `heading_home` attribute lists everyone on their way. |
| `sensor.home_eta_near_home` | How near home the nearest person is, 0-100 %, whichever way they are going: 100 % when someone is home or within 800 m, 0 % at the near-home radius (3 km by default). It fades to 0 % after a long stay at one place. |

The two ETAs are never blended. The kinematic one reacts to how you are travelling right now; the historical one only knows how long it took before. Put both on a dashboard and use whichever earns your trust.

## How the kinematic ETA works

Every position report from the phone gives a straight-line distance to the zone. The integration measures how fast that distance shrinks, filters it, and divides what is left by that speed. Because both are measured along the same straight line, a road or rail route's detours largely cancel out.

- **Noisy fixes**: phones report accuracy as a 68 % radius. Movement only counts when two fixes differ by more than three combined standard deviations, and every speed sample is weighted by how noisy it is expected to be. Reports worse than 150 m are ignored, a report with no accuracy (or 0) is treated as the worst usable, and physically impossible jumps are dropped.
- **Bouncing positions**: indoors, Wi-Fi positions sometimes jump away and straight back. A report that jumps and is followed by one back at the earlier spot is undone, and the size of the jump raises the movement threshold (up to twice the normal noise) until real movement is seen.
- **Mode changes**: when the speed changes clearly and stays changed (getting off a train, hitting a traffic jam), the filter restarts from the new speed instead of drifting towards it. A single odd sample, such as one taken at a red light, is held back and dropped if the next one disagrees.
- **Stops**: a person becomes `stationary` on evidence: a report showing no movement ten minutes after the last movement, when at their speed the movement would have shown; no movement at all for 15 minutes; or no report at all for 15 minutes. Long gaps between moving reports (an iPhone walking slowly may report only every 10 minutes) do not count. Stationary people have no ETA and are not first-arrival candidates. When they set off again, the next leg's speed is measured fresh.
- **Stale data**: with no report for 15 minutes the ETA is cleared until a new report arrives. Between reports the ETA never shows a time in the past; if the person is overdue it shows now, with low confidence.
- **Poor fixes**: a report too inaccurate to use never moves the distance sensor, so an indoor fix that lands near home cannot trigger "arriving soon".
- **Other zones**: inside any zone other than home (a station, say), the person can be approaching but not arriving soon until they leave it. Zones that contain home, such as a neighbourhood zone, do not count.

What it cannot do: the Companion app does not send the time a position was measured, so reports are timed when Home Assistant receives them; a burst of queued reports after losing signal is mostly discarded as implausible jumps. On a train it does not know about the walk from the station, so the ETA is optimistic until the walk starts; it catches up within one or two reports. A route that first heads away from home reads as moving away until it turns. Someone walking slower than about 2 km/h with an iPhone can go more than 15 minutes between reports and is then shown as stationary; raise **Stale after** in Advanced for them. The historical ETA exists to cover the routine cases.

On simulated journeys with the default settings, the ETA is within 4 % on a straight drive and typically within 6 % (at worst 25 %) on a winding road. After a change from train to foot it is typically back within 5 % from the second report on foot. The test suite holds the estimator to these figures.

## Warming the house on the way home

The kinematic ETA and "arriving soon" are about the last stretch. Warming a house takes longer than most trips home, so for that the integration answers two other questions, early and in any mode of transport.

**Is someone heading home?** `binary_sensor.person_heading_home` comes on when the person has come at least 20 % (and 500 m) closer than the farthest point since they last stayed somewhere, and is still closing; or the moment they enter a zone around home (see rings below). That is scale-invariant: 500 m on a walk back from town, a few kilometres on a drive back from a friend's, 7 km on the way back from central London. Driving round to park, or walking about somewhere far away, is not mistaken for it. It stays on until they get home, or until the trip clearly is not one: they stay at one place for 20 minutes (a quiet phone counts: a still report, then silence; so do indoor positions bouncing a few hundred metres), they move back out beyond the closest point by 500 m or 15 % of the distance, whichever is more, they leave the zone around home that started it, or their phone goes silent well past the expected arrival. Each zone around home starts a trip home once per outing, so a road along a zone's edge does not keep restarting it. Trips home and stays are kept across restarts.

**When will they get home?** The distance over a speed. With few reports, the speed is the typical door-to-door speed for a trip home of that length (walking under 1 km, a local drive at a few kilometres, 9-11 m/s for 15-35 km, motorway speeds at 100 km), sharpened by the speed measured between rings or by the closing speed. Close and slow is walking, and the measured speed is used as it is. The estimate only changes with a new report. `sensor.home_eta_expected_home` shows the earliest of everyone heading home.

**How near is anyone?** `sensor.home_eta_near_home` is for keeping the house a little warmer while someone is close by, in any direction, since a short trip home is over before any room could warm up. After someone has stayed at one place for 90 minutes it fades to 0 % over an hour, so an afternoon at a café round the corner does not keep the house warm throughout. The radius and the fade are in the group's options.

### Rings around home

iPhones report crossing a zone's edge within seconds, but otherwise send a position only every few minutes while driving, rarely on foot, and sometimes not for half an hour. Passive zones centred on home, at doubling distances, give a report at each crossing and so spot the trip home much sooner on long trips. Create them in **Settings → Areas, labels & zones → Zones**, all centred on home and marked **Passive**:

| Zone | Radius |
| --- | --- |
| Home ring 1 km | 1,000 m |
| Home ring 2 km | 2,000 m |
| Home ring 4 km | 4,000 m |
| Home ring 8 km | 8,000 m |
| Home ring 16 km | 16,000 m |
| Home ring 32 km | 32,000 m |

Passive zones never become a person's state, so automations that count people in Home, or check for `not_home`, are unaffected; their own state counts the people inside them, those at home included. The Companion app watches passive zones too (open it once after adding zones so it picks them up). An iPhone watches at most 20 zones; zones smaller than 100 m use three each. Any zone that wholly contains home counts as a ring; zones that do not are left alone.

For the house it was built for, simulated on 15 everyday absences and three phone profiles (strolls, a café 400 m away for 6 h, car errands, a friend's house, train and car days in London, a motorway day, a weekend away) plus the household's own trips: heating each room once the expected arrival was within its heat-up time, and keeping a lift while someone was within 3 km, cut the time rooms were still cold after arrival from 78 to 41 minutes on average, for about 1.6p more gas per absence; without rings, 50 minutes. Most trips home are shorter than the time a room takes to heat up, so no signal can make every arrival warm. Heading home exists to start as early as the phone allows.

## How the historical ETA works

Each trip that ends at home is kept as a record of how long it took from every distance on the way, from the last time the person was at their farthest. For the current distance, every past trip that started at least that far out says how long it took from there; the ETA is the middle of those times, and its earliest and latest attributes span the middle 60 %. Each trip counts once, however often the phone reported. Time spent stopped, or in a zone that ends a trip, is never learned as travel time. The last 50 trips are kept; turning learning off deletes them.

## Install

1. In HACS, add `https://github.com/igiannakas/ha-home-eta` as a custom repository of type **Integration**, then install **Home ETA**.
2. Restart Home Assistant.

Requires Home Assistant 2026.10 or later.

## Set up

In **Settings → Devices & services → Add integration → Home ETA**:

1. Choose **Person** once for each person. Pick the person, the destination zone (usually Home), and optionally:
   - **Activity sensor**: the Companion app's activity sensor, shown as an attribute.
   - **Reference travel-time sensor**: a Waze, Google or HERE travel-time sensor, shown as an attribute for comparison.
   - **Learn arrival times from past trips**: adds the historical ETA sensor.
   - **Zones that end a trip**: places like work, so a day there is never learned as travel time.
2. Choose **First-arrival group** and pick the people to compare. The group also sets the **near-home radius** (3 km by default) and after how long at one place **near home fades** (90 minutes; 0 never fades).

Each person's **Configure** button also has an **Advanced** section: worst usable GPS accuracy, slowest closing speed, speed smoothing, arriving-soon distance, and how long until data is stale. Only values you change are stored; the rest follow the defaults of the installed version.

Renaming a person, a zone or one of the optional sensors is followed automatically. Removing a person takes them out of every first-arrival group; a group left with nobody in it raises a repair.

On iPhones, make sure the Companion app has location set to **Always** with background refresh on. If the activity sensor shows as unavailable, grant the app Motion & Fitness access.

## Automations

Warm a room once the first person heading home is closer than the 45 minutes it takes to heat up:

```yaml
triggers:
  - trigger: template
    value_template: >
      {{ states('sensor.home_eta_expected_home') not in ['unknown', 'unavailable']
         and as_datetime(states('sensor.home_eta_expected_home'))
             - now() < timedelta(minutes=45) }}
conditions:
  - condition: state
    entity_id: input_boolean.someone_home
    state: "off"
actions:
  - action: climate.set_preset_mode
    target:
      entity_id: climate.living_room
    data:
      preset_mode: comfort
```

Go back to away when the trip home turns out not to be one (`sensor.home_eta_expected_home` becomes unknown while nobody is home).

Announce who is nearly home (each person by their own threshold; someone close by but walking slowly may have no ETA, so this names them rather than quoting minutes):

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.home_eta_someone_arriving_soon
    to: "on"
actions:
  - action: notify.mobile_app_iphone
    data:
      message: >
        {{ state_attr('binary_sensor.home_eta_someone_arriving_soon', 'names')
           | join(' and ') }} will be home soon.
```

## Services

- `home_eta.reset_history` forgets learned trips, for everyone or the people given.
- `home_eta.export_history` returns the learned trips, for inspection.

## Check it against your own history

`scripts/backtest.py` replays recorder history through the integration's own estimator code and reports how far off the ETAs were, by distance, how often "arriving soon" fired too early or not at all, and for heading home how long before each arrival it came on and how often it came on for a trip that was not one. It needs Python 3.11 or later and nothing else:

```bash
HA_TOKEN=<long-lived access token> python3 scripts/backtest.py \
  --url http://homeassistant.local:8123 \
  --person person.person1 --person person.person2 \
  --days 10 --ignore-zone zone.work --save history.json
```

It prints the arguments to re-run on the saved file (`--input history.json --zone <latitude>,<longitude>,<radius>`, plus `--enclosing-zone <zone>=<latitude>,<longitude>,<radius>` for each zone around home, so rings are replayed as rings). The saved file keeps only positions and zones, and only you can read it; keep it out of version control. Use `--person-threshold person.sam=10` for a person's own threshold and `--param stale_after_s=1200` for any Advanced value you changed.

The target before relying on it: a 90th-percentile error of 10 minutes or less within 5 km, and no more than one early "arriving soon" per ten trips, over at least five completed trips. The exit status is 0 when both pass, 1 when one fails and 2 when there is not enough data. One difference from live use: after a Home Assistant restart the recorder stores the last position again at the restart time, which the integration ignores for speed but the replay cannot tell apart.

## Development

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
ruff format --check . && ruff check .
mypy custom_components/home_eta scripts/backtest.py
pytest --cov=custom_components/home_eta
```

The estimators live in `custom_components/home_eta/core` and never import Home Assistant, so they can be tested and backtested on their own; CI also runs them on Python 3.11. `tests/sim.py` generates synthetic journeys that report the way iOS does.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
