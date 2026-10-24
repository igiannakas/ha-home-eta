# Home ETA

A Home Assistant integration that says who is heading home, when they will get there and how near home they are, from the Companion app's phone location. It works whether you walk, take the train or drive, needs no map or routing service, and never reads step counters. It was built to warm the house on the way home.

Each person is added on their own, with their own entities and their own "arriving soon" threshold. A separate **group** entry combines people heading to the same zone: who is expected first, whether anyone is arriving soon, and how near the nearest is.

## What you get

For each person (entity IDs shown for a person called "person"):

| Entity | What it shows |
| --- | --- |
| `sensor.person_status` | `home`, `arriving_soon`, `heading_home` or `away` (see below). Attributes: why the trip home last started or ended and when, the speed behind the expected arrival and where it came from, and the farthest distance since the person last stayed somewhere. |
| `sensor.person_expected_home` | When they are expected home, while heading home; unknown otherwise. |
| `sensor.person_minutes_to_home` | Minutes until then. |
| `number.person_arriving_soon_threshold` | Minutes out that count as arriving soon (default 15). |
| `sensor.person_distance_to_home` | Straight-line distance to the zone centre; 0 at home. |
| `sensor.person_near_home` | How near home they are, 0-100 %, whichever way they are going: 100 % at home or within 800 m, 0 % at their near-home radius (3 km by default). It fades to 0 % after a long stay at one place. |

For a group (named "Home ETA" by default):

| Entity | What it shows |
| --- | --- |
| `sensor.home_eta_status` | The most-home status of any member: `home` while anyone is home, else `arriving_soon` if anyone is, and so on. Attributes list the members in each status (`persons_home`, `persons_arriving_soon`, `persons_heading_home`, `persons_away`) and any member without data. |
| `sensor.home_eta_expected_home` | When the first member heading home is expected; unknown while nobody is. Attributes: who that is, everyone heading home with their expected time, who is home, and any member without data. |
| `sensor.home_eta_minutes_to_home` | Minutes until then. |
| `binary_sensor.home_eta_someone_arriving_soon` | On while any member is arriving soon by their own threshold. Its `persons` and `names` attributes say who; unknown while no member has data. |
| `sensor.home_eta_near_home` | The nearest member's near home (each by their own radius and fade); 100 % while anyone is home. Attributes: who that is and their distance, and who is home. |

All but the threshold are unknown until the person's first location arrives.

The status is one stage of the way home at a time, most home first:

| Status | When |
| --- | --- |
| `home` | In the destination zone. |
| `arriving_soon` | Heading home and expected within the threshold, or within 800 m. Held off inside another zone. |
| `heading_home` | On the way home, not yet arriving soon. |
| `away` | Anything else: out, staying somewhere, or going the other way. |

A short trip home, or a fast one, can go from `away` straight to `arriving_soon`. For "the trip home has started", trigger on leaving `away` for either of the two (see Automations).

## How it works

### Which reports to believe

Every position report from the phone gives a straight-line distance to the zone. Before anything uses a report, it is checked, and the speed towards home is measured and filtered:

- **Noisy fixes**: phones report accuracy as a 68 % radius. Movement only counts when two fixes differ by more than three combined standard deviations, and every speed sample is weighted by how noisy it is expected to be. Reports worse than 150 m are ignored, a report with no accuracy (or 0) is treated as the worst usable, and physically impossible jumps are dropped. A report too inaccurate to use never moves the distance, so an indoor fix that lands near home cannot count as close by.
- **Bouncing positions**: indoors, Wi-Fi positions sometimes jump away and straight back. A report that jumps and is followed by one back at the earlier spot is undone everywhere, and the size of the jump raises the movement threshold (up to twice the normal noise) until real movement is seen.
- **Mode changes**: when the speed changes clearly and stays changed (getting off a train, hitting a traffic jam), the filter restarts from the new speed instead of drifting towards it. A single odd sample, such as one taken at a red light, is held back and dropped if the next one disagrees.
- **Stops**: a report showing no movement ten minutes after the last one, when at that speed the movement would have shown, is a stop, as is no movement at all for 15 minutes. Long gaps between moving reports (an iPhone walking slowly may report only every 10 minutes) are not. After a stop the next leg's speed is measured fresh. With no report for 15 minutes the measured speed is no longer used.

### Heading home

The status becomes `heading_home` when the person has come at least 20 % (and 500 m) closer than the farthest point since they last stayed somewhere, and is still closing; or the moment they enter a zone around home (see rings below). That is scale-invariant: 500 m on a walk back from town, a few kilometres on a drive back from a friend's, 7 km on the way back from central London. Driving round to park, or walking about somewhere far away, is not mistaken for it.

It stays on until they get home, or until the trip clearly is not one: they stay at one place for 20 minutes (a quiet phone counts: a still report, then silence; so do indoor positions bouncing a few hundred metres), they move back out beyond the closest point by 500 m or 15 % of the distance, whichever is more, they leave the zone around home that started it, or their phone goes silent well past the expected arrival. Each zone around home starts a trip home once per outing, so a road along a zone's edge does not keep restarting it. Trips home and stays are kept across restarts.

### Expected home

The distance over a speed. With few reports, the speed is the typical door-to-door speed for a trip home of that length (walking under 1 km, a local drive at a few kilometres, 9-11 m/s for 15-35 km, motorway speeds at 100 km), sharpened by the speed measured between rings or by the closing speed: the two are blended, so one fast stretch of motorway or one queue does not swing it. Close and slow is walking, and the measured speed is used as it is. The estimate only changes with a new report; between reports the minutes count down, and the expected time never falls in the past.

### Arriving soon

On a trip home, the status becomes `arriving_soon` once the expected arrival is within the person's threshold or they are within 800 m of the zone centre (**Arriving-soon distance** in Advanced). It stays so until they get home or the trip home ends, so an estimate that wobbles around the threshold does not switch it back and forth. Inside any zone other than home it is held off (a shop near home is not arriving home); zones that contain home, such as the rings, do not count. Changing the threshold judges it afresh.

### Near home

Near home is for keeping the house a little warmer while someone is close by, in any direction, since a short trip home is over before any room could warm up. It is 100 % within 800 m and falls to 0 % at the person's near-home radius. After someone has stayed at one place for 90 minutes it fades to 0 % over an hour, so an afternoon at a café round the corner does not keep the house warm throughout. The radius and the fade are in each person's options.

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

Add one in between where you often stay: a place at 28 km sits inside the 32 km ring, so a 24 km ring is the first one crossed on the way back from there. Passive zones never become a person's state, so automations that count people in Home, or check for `not_home`, are unaffected; their own state counts the people inside them, those at home included. The Companion app watches passive zones too (open it once after adding zones so it picks them up). An iPhone watches at most 20 zones; zones smaller than 100 m use three each. Any zone that wholly contains home counts as a ring; zones that do not are left alone.

### What it cannot do

The Companion app does not send the time a position was measured, so reports are timed when Home Assistant receives them; a burst of queued reports after losing signal is mostly discarded as implausible jumps. A route that first heads away from home reads as moving away until it turns. A trip home that is shorter than the phone's reporting interval may get no warning at all: the first report is the arrival. Someone walking slower than about 2 km/h with an iPhone can go more than 15 minutes between reports, so their speed is not measured; raise **Speed stale after** in Advanced for them.

### How well it does

For the house it was built for, simulated on 15 everyday absences and three phone profiles (strolls, a café 400 m away for 6 h, car errands, a friend's house, train and car days in London, a motorway day, a weekend away) plus the household's own trips: heating each room once the expected arrival was within its heat-up time, and keeping a lift while someone was within 3 km, cut the time rooms were still cold after arrival from 78 to 41 minutes on average, for about 1.6p more gas per absence; without rings, 50 minutes. Most trips home are shorter than the time a room takes to heat up, so no signal can make every arrival warm.

On the household's own trips of 16-23 October 2026, heading home came on 41 and 109 minutes before the two arrivals from London, and arriving soon never came on early. On a drive back from London on 24 October it came on 34 minutes ahead, and the expected arrival was within 3 minutes over the last ten minutes.

## Install

1. In HACS, add `https://github.com/igiannakas/ha-home-eta` as a custom repository of type **Integration**, then install **Home ETA**.
2. Restart Home Assistant.

Requires Home Assistant 2026.10 or later.

## Set up

In **Settings → Devices & services → Add integration → Home ETA**:

1. Choose **Person** once for each person. Pick the person, the destination zone (usually Home), the **near-home radius** (3 km by default) and after how long at one place **near home fades** (90 minutes; 0 never fades).
2. Choose **Group** and pick the people to combine.

Each person's **Configure** button changes the near-home settings and has an **Advanced** section: worst usable GPS accuracy, speed smoothing, arriving-soon distance, and how long until the measured speed is stale. Only advanced values you change are stored; the rest follow the defaults of the installed version.

Renaming a person or a zone is followed automatically. Removing a person takes them out of every group; a group left with nobody in it raises a repair.

On iPhones, make sure the Companion app has location set to **Always** with background refresh on.

## Upgrading

### From 2.0

Each person's heading-home and arriving-soon binary sensors are now the states `heading_home` and `arriving_soon` of `sensor.person_status`, which also says `home` and `away`. The binary sensors are removed on upgrade; in an automation, `binary_sensor.person_heading_home` being on becomes the status being `heading_home` or `arriving_soon`, and arriving soon being on becomes the status being `arriving_soon`. The threshold now lives only on its number entity (the `threshold_min` attribute is gone). A group gains `sensor.home_eta_status`. A trip home in progress, or a stay somewhere, during the upgrade restart is not carried over, so near home may stay high for up to another 90 minutes for someone already staying near home.

### From 1.x

Version 2.0 keeps only what turned out to be useful, and per person:

- **Removed**: the kinematic and past-trips ETAs, closing speed, ETA confidence, the approach state, and the group's first-arrival sensors. Their entities are deleted on upgrade. Use expected home, minutes to home and arriving soon instead; they now exist for each person as well as for the group.
- **Arriving soon** now follows heading home and expected home: it comes on when minutes to home reaches the threshold (or within 800 m) and stays on until the trip home ends.
- **Learning from past trips** is gone: learned trips are deleted on upgrade, and so are the `reset_history` and `export_history` actions.
- **Options**: the activity, reference travel-time and "zones that end a trip" options are gone, and so is **Slowest closing speed** in Advanced. A group's near-home radius and fade move to each of its members.
- **Attributes** gone with them: heading home's `expected_at` and `minutes` (now sensors of their own) and `staying_since` (now on near home); the group expected home's `minutes`, `reason`, `speed_source` and `distance_m`, and each member's `reason` and `speed_source` (on the person's own entities now); the group near home's `staying_since`, `radius_m` and `fade_after_min` (on each person's near home now).
- A trip home in progress, or a stay somewhere, during the upgrade restart is not carried over.

## Automations

Warm a room once the first person heading home is closer than the 45 minutes it takes to heat up:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.home_eta_minutes_to_home
    below: 45
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

Go back to away when the trip home turns out not to be one: the group's status goes back to `away`. For one person, use their own `sensor.person_minutes_to_home` and `sensor.person_status` instead.

Do something as soon as a person sets off home, however short the trip:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.person_status
    from: away
    to:
      - heading_home
      - arriving_soon
```

Keep the house a little warmer while anyone is close by:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.home_eta_near_home
    above: 50
actions:
  - action: climate.set_temperature
    target:
      entity_id: climate.hallway
    data:
      temperature: 18
```

Announce who is nearly home, each person by their own threshold:

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

## Check it against your own history

`scripts/backtest.py` replays recorder history through the integration's own code, minute by minute, and reports: for heading home, how long before each arrival it came on and how often it came on for a trip that was not one; for expected home, how far off it was, by distance; and for arriving soon, its lead time and how often it came on far too early (over twice the threshold) or not at all. It needs Python 3.11 or later and nothing else:

```bash
HA_TOKEN=<long-lived access token> python3 scripts/backtest.py \
  --url http://homeassistant.local:8123 \
  --person person.person1 --person person.person2 \
  --days 10 --save history.json
```

It prints the arguments to re-run on the saved file (`--input history.json --zone <latitude>,<longitude>,<radius>`, plus `--enclosing-zone <zone>=<latitude>,<longitude>,<radius>` for each zone around home, so rings are replayed as rings). The saved file keeps only positions and zones, and only you can read it; keep it out of version control. Use `--person-threshold person.sam=10` for a person's own threshold and `--param stale_after_s=1200` for any Advanced value you changed.

The target before relying on it: a 90th-percentile expected-home error of 10 minutes or less within 5 km, and no more than one early "arriving soon" per ten trips, over at least five completed trips. The exit status is 0 when both pass, 1 when one fails and 2 when there is not enough data. One difference from live use: after a Home Assistant restart the recorder stores the last position again at the restart time, which the integration does not use as a fix but the replay cannot tell apart.

## Development

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
ruff format --check . && ruff check .
mypy custom_components/home_eta scripts/backtest.py
pytest --cov=custom_components/home_eta
```

The tracking code lives in `custom_components/home_eta/core` and never imports Home Assistant, so it can be tested and backtested on its own; CI also runs it on Python 3.11. `tests/sim.py` generates synthetic journeys that report the way iOS does.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
