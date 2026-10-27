# Home ETA

A Home Assistant integration that says who is heading home, when they will get there and how near home they are, from the Companion app's phone location. It works whether you walk, take the train or drive, needs no map or routing service, and never reads step counters. It was built to warm the house on the way home.

Each person is added on their own, with their own entities and their own "arriving soon" threshold. A separate **group** entry combines people heading to the same zone: who is expected first, whether anyone is arriving soon, whether anyone is close to home or nearby, and how near the nearest is.

## What you get

For each person (entity IDs shown for a person called "person"):

| Entity | What it shows |
| --- | --- |
| `sensor.person_status` | `home`, `arriving_soon`, `heading_home` or `away` (see below). Attributes: why the trip home last started or ended and when, the speed behind the expected arrival and where it came from, and the farthest distance since the person last stayed somewhere. |
| `sensor.person_expected_home` | When they are expected home, while heading home; unknown otherwise. |
| `sensor.person_minutes_to_home` | Minutes until then. |
| `number.person_arriving_soon_threshold` | Minutes out that count as arriving soon (default 15). |
| `sensor.person_distance_to_home` | Straight-line distance to the zone centre; 0 at home. |
| `sensor.person_near_home` | How near home they are, 0-100 %, whichever way they are going: 100 % at home or within 800 m, 0 % at their near-home radius (3 km by default), however long they stay there. |
| `binary_sensor.person_close_to_home` | On while they are home or within 800 m of the zone centre; off again only beyond 950 m (see Near home). Attributes: their distance and the 800 m. |
| `binary_sensor.person_nearby` | On while they are home or within their near-home radius; off again only 150 m beyond it. Attributes: their distance and the radius. |
| `sensor.person_typical_speeds` | How many trips home the person's typical speeds were learned from (see below). Attributes: the two learned tables, and what each of the latest trips changed in them. Diagnostic. |
| `button.person_relearn_typical_speeds` | Learn the typical speeds afresh from the recorder's history. |

For a group (named "Home ETA" by default):

| Entity | What it shows |
| --- | --- |
| `sensor.home_eta_status` | The most-home status of any member: `home` while anyone is home, else `arriving_soon` if anyone is, and so on. Unknown while nobody is home and a member has no data (being reloaded, say), since they could change the answer. Attributes list the members in each status (`persons_home`, `persons_arriving_soon`, `persons_heading_home`, `persons_away`) and any member without data. |
| `sensor.home_eta_expected_home` | When the first member heading home is expected; unknown while nobody is. Attributes: who that is, everyone heading home with their expected time, who is home, and any member without data. |
| `sensor.home_eta_minutes_to_home` | Minutes until then. |
| `binary_sensor.home_eta_someone_arriving_soon` | On while any member is arriving soon by their own threshold. Its `persons` and `names` attributes say who; unknown while nobody is and a member has no data. |
| `binary_sensor.home_eta_someone_close_to_home` | On while any member is home or within 800 m. Its `persons` and `names` attributes say who; unknown while nobody is and a member has no data or no known distance yet. |
| `binary_sensor.home_eta_someone_nearby` | On while any member is home or within their own near-home radius. Attributes and unknown as for someone close to home. |
| `sensor.home_eta_near_home` | The highest near home of any member, each by their own radius (so not always the nearest member); 100 % while anyone is home. Attributes: who that is and their distance, and who is home. |

All but the threshold and the typical speeds are unknown until the person's first location arrives. The group's sensors are unknown rather than off while a member could change the answer, so an automation that must not act on a guess can wait for on or off.

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

It lasts until they get home, or until the trip clearly is not one: they stay at one place for 20 minutes (a quiet phone counts: a still report, then silence; so do indoor positions bouncing a few hundred metres), they move back out beyond the closest point by 500 m or 15 % of the distance, whichever is more, they leave the zone around home that started it, or their phone goes silent well past the expected arrival. Each zone around home starts a trip home once per outing, so a road along a zone's edge does not keep restarting it. Trips home and stays are kept across restarts by the status sensor, so not while it is disabled.

### Expected home

The distance over a speed. With no measured speed, the speed is the typical door-to-door speed for a trip home of that length, learned from the person's own trips (below). With one (between rings, or the closing speed), the two are blended, `measured ^ w × typical ^ (1 - w)`, so one fast stretch of motorway or one queue does not swing it; `w` is learned too, and starts at 0.5. Close and slow is walking, and the measured speed is used as it is. At driving speed the typical speed and weight for driving are used. Once someone is clearly closing (above 0.3 m/s), closing faster never means a later estimate at a report. Between reports it can: the expected arrival shown moves on to a later estimate once its time passes (below), and closing faster makes it pass sooner, so for a while the faster trip shows the later arrival.

Each report gives an estimate, and in stop-start traffic one of them is often far off and the next report puts it back: a stretch crawled or sped through (the measured speed follows each stretch between reports closely), a gap in reports that leaves no measured speed, or a switch to or from the speed between rings. So the expected arrival is the middle of the last three: one estimate that disagrees with the two before it waits for the next report, and a change the next report confirms follows one report late. Between reports the expected arrival shown stays until its time passes with the person still out, when it is known to be wrong: the one estimate due later then takes over, if there is one, rather than it stay at "now" (once all have passed, it is "now"). An earlier estimate passing changes nothing. Crossing a ring when the speed from the ring before is used is believed at once, whatever that speed (on foot too), since it is measured over kilometres. The status shows the speed behind the estimate shown. The estimate changes only with a new report or when the one shown has passed; between reports the minutes count down, and the expected time never falls in the past. A restart or a reload is not a report: the estimate is kept as it was, and the person's last report, which a reload reads again, is not counted twice, whatever became of it (merged into the one before it, say).

### Typical speeds, learned

The built-in values are a starting point: walking under 1 km, a local drive at a few kilometres, 9-11 m/s for 15-35 km, motorway speeds at 100 km; when driving, at least 4.5 m/s door to door (the end of a drive, junctions and parking included); and a measured speed worth half. Each person's own trips home then correct them, at each distance in the tables, for any trip and when driving:

- **What is learned from**: every trip home that ends at home. At each position report while heading home, the distance, the speed actually achieved from there to the edge of the zone, and the speed measured then. Time spent going out, wandering about or visiting is not travel home: a trip home that ends without arriving (a stay, turning back, going quiet) is dropped. Reports close to the edge, or in the last minute, are left out. Reports measured at driving speed (2.5 m/s or more) teach the driving table.
- **The typical speed**: a correction factor moves the built-in speed towards the speeds achieved there. Each trip counts once, a report at most three times faster or slower than built in. The built-in speed counts as three trips, so one trip moves it a little and a handful most of the way. Learned speeds never fall with distance, and driving is never slower than any trip; to keep to that, a distance never travelled can be pulled along by its neighbours (fast short drives raise the longer distances to match), so its `trips` can be 0 while its speed has moved.
- **The weight of a measured speed**: each trip counts for the weight that would have predicted it best, from 0 (the typical speed knew better) to 1 (the measurement did), the more the further its measured speeds were from the typical one (however many reports it had). A report counts as at most three times faster or slower than typical, measured or achieved, which limits what one glitch can do. The built-in 0.5 counts as three trips. A fast stretch before the slower roads near home, say, makes the measured speed count for less there.
- **Ageing**: a trip's weight halves every 60 days, so a new routine or road works are picked up; trips are forgotten after about 11 months. The tables are refitted daily as trips age.
- **When**: as each trip home ends. When a person is first set up (and at the first start after upgrading), the trips home in the recorder's history are learned from too, up to 60 days back (the recorder keeps 10 by default). **Relearn typical speeds** replaces what was learned with what the history holds now; it refuses without the recorder, and says so when the history cannot be read. Moving the zone further than its radius forgets what was learned around the old place, also when it moved while Home Assistant was off.
- **What is kept**: in Home Assistant's storage, up to 200 trips, 30 reports each: distances to 10 m, speeds to 0.01 m/s, and arrival times to the minute; no coordinates but the zone's own centre and radius, to notice it moving. Learning works from exactly that, so a restart does not change what was learned. Removing the person removes them.

`sensor.person_typical_speeds` shows the tables. Each row of its `all_trips` and `driving` attributes is one distance: `built_in_mps`, `learned_mps`, the `factor` between them, the weight of the `trips` behind it (after ageing), `measured_weight` and the `weight_trips` behind that. `recent_trips` lists the latest five trips, newest first, each with where it was first sampled (`from_m`), how many reports were kept (`samples`) and what learning it changed, as it was then: per distance, the `factor` and `measured_weight` before and after. `fitted_at` is when the tables were last fitted. The sensor's state is the number of trips learned from, so its history shows when it learned; its attributes are recorded with it.

### Arriving soon

On a trip home, the status becomes `arriving_soon` once the person is expected home within their threshold or they are within 800 m of the zone centre (**Arriving-soon distance** in Advanced). It stays so until they get home or the trip home ends, so an estimate that wobbles around the threshold does not switch it back and forth. Inside any zone other than home it is held off (a shop near home is not arriving home); zones that contain home, such as the rings, do not count. Changing the threshold judges it afresh. Arriving soon goes by the newest estimate, from the latest report alone, not by the expected arrival shown, and so does ending a trip home that went quiet well past it, exactly as before 2.4.0: heating that should come on as the person gets out of a queue must not wait for the next report to confirm it. So it can come on while the expected arrival shown is still a little beyond the threshold, or from a single estimate far earlier than the rest that the expected arrival leaves out. After a restart or a reload, until the next report, both go by the typical speed, as before.

### Near home

Near home is for keeping the house a little warmer while someone is close by, in any direction, since a short trip home is over before any room could warm up. It is 100 % within 800 m and falls to 0 % at the person's near-home radius. It does not fade however long someone stays close by: the longer they have been out, the sooner they are likely to come back, so a stay round the corner is when the house most needs to be ready. The radius is in each person's options.

Close to home and nearby are the on/off versions: close to home comes on within 800 m (where near home reaches 100 %), nearby within the radius (where near home rises above 0 %). Each goes off again only 150 m further out, about the worst GPS accuracy used, so a phone lying still near the edge does not switch it on and off with every report; near home itself has no such margin, so near them the two can briefly disagree. Both are on at home, and unknown while the distance is not known: no usable position since leaving home, none yet, or the zone just moved. Close to home always implies nearby.

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

On those trips (16-24 October; four and three trips home learned from, mostly in the same car), each scored with what was learned from all the others, the learned values made the worst estimates better and the typical ones about as good: the 90th-percentile error fell at every distance for both people (5-20 km from 39.6 to 31.1 and from 38.5 to 36.3 minutes, beyond 20 km from 64.9 to 49.9 and from 36.3 to 33.6), while the mean error over every scored report went from 18.1 to 17.2 minutes for one and from 21.3 to 22.5 for the other, whose trips included the one with a long jam (the measured speed was right there, and the other trips had taught it to count for less). What was learned fits the household: driving about 4.5 m/s near home, as built in; any trip faster under 2 km (most trips home there were drives); a measured speed worth less than half (0.3-0.45) between 3 and 35 km, where the road just driven is faster than the road ahead. With so few trips they are still pulled towards the built-in 0.5: a trip counts towards them by how far its measured speeds were from the typical one, not by how many reports it had.

On the household's trips home of 16-27 October 2026 (ten, two people), taking the middle of the last three estimates halved the changes of more than ten minutes from one report to the next (30 to 16; on the drive home through queues of 27 October, 12 to 6 across the two phones), and the mean error over every report went from 18.1 to 17.3 minutes. Heading home and arriving soon came on at exactly the same moments as before on every trip, and on 420 simulated drives heading home, arriving soon and why a trip home ended were the same at every minute: they still go by the newest estimate. The cost is the lag: a change the next report confirms is shown one report late. On the drive home of 27 October that made the mean error a little worse (17.2 to 17.7 and 20.2 to 21.0 minutes for the two phones), and the report 4 minutes from home, which had been spot on, was shown 2.6 minutes off (the worst in the last ten minutes went from 4.3 to 5.7).

## Install

1. In HACS, add `https://github.com/igiannakas/ha-home-eta` as a custom repository of type **Integration**, then install **Home ETA**.
2. Restart Home Assistant.

Requires Home Assistant 2026.10 or later.

## Set up

In **Settings → Devices & services → Add integration → Home ETA**:

1. Choose **Person** once for each person. Pick the person, the destination zone (usually Home) and the **near-home radius** (3 km by default).
2. Choose **Group** and pick the people to combine.

Each person's **Configure** button changes the near-home radius and has an **Advanced** section: worst usable GPS accuracy, speed smoothing, arriving-soon distance, and how long until the measured speed is stale. Only advanced values you change are stored; the rest follow the defaults of the installed version.

Renaming a person or a zone is followed automatically. Removing a person takes them out of every group; a group left with nobody in it raises a repair.

On iPhones, make sure the Companion app has location set to **Always** with background refresh on.

## Upgrading

### From 2.3

- **Steadier expected arrival**: it is now the middle of the last three estimates, so one report that is far off no longer moves it (see Expected home). Arriving soon and the end of a trip home gone quiet are unchanged: they go by the newest estimate, as before. Nothing to set up.
- **Kept across a restart**: the expected arrival of a trip home in progress is now kept through a restart or a reload until the next report, instead of falling back to the typical speed (arriving soon and the end of a trip home gone quiet use the typical speed until then, as before). A trip home in progress during the upgrade restart was saved without estimates, so its expected arrival falls back, once.

### From 2.2

- **Near home no longer fades** after a long stay at one place: someone at a café round the corner stays at 100 % however long they are there. The **near home fades** setting is gone from each person's options and is removed from their entry on upgrade. The near home sensor's `fade_after_min` attribute is gone.
- **New binary sensors**: each person gains `binary_sensor.person_close_to_home` (home or within 800 m) and `binary_sensor.person_nearby` (home or within their near-home radius); a group gains `binary_sensor.home_eta_someone_close_to_home` and `binary_sensor.home_eta_someone_nearby`. Nothing to set up.
- **Fixed**: after leaving home, distance to home and near home stayed at 0 and 100 % until a usable position arrived (none at all from a tracker without GPS). They are now unknown until then.

### From 2.1

Each person gains `sensor.person_typical_speeds` and `button.person_relearn_typical_speeds`. At the first start after upgrading, the trips home in the recorder's history are learned from in the background, and the expected arrival uses what was learned from then on. Nothing to set up.

### From 2.0

Each person's heading-home and arriving-soon binary sensors are now the states `heading_home` and `arriving_soon` of `sensor.person_status`, which also says `home` and `away`. The binary sensors are removed on upgrade; in an automation, `binary_sensor.person_heading_home` being on becomes the status being `heading_home` or `arriving_soon`, and arriving soon being on becomes the status being `arriving_soon`. The threshold now lives only on its number entity (the `threshold_min` attribute is gone). A group gains `sensor.home_eta_status`. A trip home in progress, or a stay somewhere, during the upgrade restart is not carried over.

### From 1.x

Version 2.0 keeps only what turned out to be useful, and per person:

- **Removed**: the kinematic and past-trips ETAs, closing speed, ETA confidence, the approach state, and the group's first-arrival sensors. Their entities are deleted on upgrade. Use expected home, minutes to home and arriving soon instead; they now exist for each person as well as for the group.
- **Arriving soon** now follows heading home and expected home: it comes on when minutes to home reaches the threshold (or within 800 m) and stays on until the trip home ends.
- **Learning from past trips** is gone: learned trips are deleted on upgrade, and so are the `reset_history` and `export_history` actions.
- **Options**: the activity, reference travel-time and "zones that end a trip" options are gone, and so is **Slowest closing speed** in Advanced. A group's near-home radius moves to each of its members (its fade setting is gone since 2.3).
- **Attributes** gone with them: heading home's `expected_at` and `minutes` (now sensors of their own) and `staying_since` (now on near home); the group expected home's `minutes`, `reason`, `speed_source` and `distance_m`, and each member's `reason` and `speed_source` (on the person's own entities now); the group near home's `staying_since` and `radius_m` (on each person's near home now) and `fade_after_min`.
- A trip home in progress, or a stay somewhere, during the upgrade restart is not carried over.

## Automations

While Home Assistant starts or a person's entry reloads, their entities are briefly unavailable and the group's may be unknown. For a trigger that should fire only on a real change, add `not_from: [unknown, unavailable]`.

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

Or in two steps, with the binary sensors: one temperature while anyone is nearby, the full one while anyone is close to home. Changes to unknown are left out, so a member reloading changes nothing; the change back is not, so nothing that happened meanwhile is missed. One report can change both sensors at once, hence `queued`:

```yaml
mode: queued
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.home_eta_someone_nearby
      - binary_sensor.home_eta_someone_close_to_home
    not_to: [unknown, unavailable]
actions:
  - action: climate.set_temperature
    target:
      entity_id: climate.hallway
    data:
      temperature: >-
        {{ 20 if is_state('binary_sensor.home_eta_someone_close_to_home', 'on')
           else 19 if is_state('binary_sensor.home_eta_someone_nearby', 'on')
           else 16 }}
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

`scripts/backtest.py` replays recorder history through the integration's own code, minute by minute, and reports: for heading home, how long before each arrival it came on and how often it came on for a trip that was not one; for expected home, how far off it was, by distance; and for arriving soon, its lead time and how often it came on far too early (over twice the threshold) or not at all. The typical speeds are learned from the history's trips home as the integration learns them, and each trip is replayed with what was learned from all the others, so no trip learns from itself; the learned tables are printed. `--speeds built-in` replays with the built-in speeds instead. It needs Python 3.11 or later and nothing else:

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
