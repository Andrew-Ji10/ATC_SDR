# Reception tests (RTL-SDR, Mac)

Two tests, run one after the other on the same RTL-SDR dongle: what can this antenna, at this
spot, hear? Run them from the repository root, in the `sdr` environment.

| Script | Antenna | Default length |
|---|---|---|
| [atc_reception.py](atc_reception.py) | airband whip, about 60 cm (24 in) | 20 min |
| [adsb_reception.py](adsb_reception.py) | ADS-B whip, about 6.9 cm | 10 min |

[adsb_viewer.py](adsb_viewer.py) draws an ADS-B run on a map, and [setup.sh](setup.sh) gets a Mac ready.

## Before you start

Plug in the dongle, then run the setup once:

```sh
bash reception_test/setup.sh               # installs what's missing, then checks it all
bash reception_test/setup.sh --check       # just the checks, any time
conda activate sdr
```

The setup installs conda (Miniforge) if you don't have it, and creates the `sdr` environment
with the tools the tests need. On a Mac the RTL-SDR needs no other driver. It then checks that the
dongle shows up on USB and opens, and runs the scanner's self-test. `--full` installs the whole
repository's environment instead (GNU Radio and the Pluto libraries, about 2 GB).

Close any other program that uses the dongle (SDR++, GQRX, an earlier test). Only one program
can open it at a time.

## 1. ATC: every channel

```sh
python3 reception_test/atc_reception.py                    # 20 minutes
python3 reception_test/atc_reception.py --duration 1h      # longer is better at quiet times
```

It covers all 51 Bay Area channels (towers, ground, clearance, NorCal approach, guard and the
8 ATIS), in 6 capture windows of about 2 MHz each:

1. **Calibration** (about 25 s): it listens to each window and learns every channel's noise floor.
   It also prints the ADC level. If it says `too much gain`, run again with `--gain 30`.
   A channel marked `ATIS on the air` is already a received signal.
2. **Scan**: it hops through the windows, about 4 s per round when everything is quiet. When a
   channel talks, it stays on that window until 4 s after the last transmission, so the reply
   gets caught too. Each transmission is printed and saved as a clip. Each ATIS is recorded once
   (20 s), and then it no longer holds up the scan.

Ctrl-C stops it early. The summary is still written.

## 2. ADS-B: aircraft and range

Swap to the short antenna, then:

```sh
python3 reception_test/adsb_reception.py --ref 37.43 -122.17     # your latitude and longitude
```

Get `--ref` from a map: in Google Maps, right-click your spot and click the numbers to copy them.
At Cory Hall (UC Berkeley), `--ref cory` fills them in for you.
A line is printed for each new aircraft. At the end it reports the message rate, how many aircraft
were heard, and the farthest aircraft overall and in each of 8 directions. If one direction is
much shorter than the others, something blocks the antenna that way.

It also writes `map.html` in the run folder. Open it in a browser:

```sh
open recordings/adsb_reception_*/map.html          # the latest run's, if there's only one
```

The map shows each aircraft's track, colored by altitude (light = low, dark = high), along with your
receiver, range rings, and a shaded outline of how far out aircraft were heard in each direction.
Hover over a track for its callsign, altitude, speed and distance. Use the menu at the top right to
switch layers on and off: "Every fix" shows each single position. The street map needs internet to
load; the data is inside the file. To redraw a map, or draw one from any positions CSV:

```sh
python3 reception_test/adsb_viewer.py recordings/adsb_reception_<time>/ --open
python3 reception_test/adsb_viewer.py samples/adsb/tracks.csv --ref 37.43 -122.17 --open
```

Given a run folder, it reads `--ref` from that folder's `reception.txt`. Next to `map.html` it also
saves `map.png`, a picture of the same map for a quick look. The ADS-B test writes both.

The HTML map is drawn on Esri's light grey map. The picture uses OpenStreetMap's standard map, the
same as the demo video; its tiles are downloaded once into `recordings/tile_cache/`. `--tile-url`
sets both: `esri`, `osm`, or any tile URL with `{z}/{x}/{y}`. OpenStreetMap blocks maps opened from
a file, so `osm` shows "Access blocked" tiles in the HTML map. `--no-png` skips the picture.

## What you get

Everything goes in `recordings/` (not tracked by git):

- `atc_reception_<time>/`
  - `summary.txt`: per channel, the time monitored, transmissions, talk time and peak level
    (dB above the noise); which ATIS were received; the channels never heard.
  - `clips/`, `clips_clean/`: one MP3 (and WAV) per transmission, plain and noise-reduced.
  - `transmissions.csv`, `calibration.csv` (noise floors, ADC level), `visits.csv` (every stop).
- `adsb_reception_<time>/`
  - `reception.txt`: the range report. `map.html`: the map.
  - `summary.txt`, `positions.csv`, `aircraft.csv`, `sightings.csv`, `messages.txt.gz`,
    `health.csv`: the same files as [radio/adsb_listen.py](../radio/adsb_listen.py).

Send back both folders, or at least the two summaries plus a few clips.

## Options

`--gain` (default 40; RTL gains run 0–49.6), `--duration` (`90s`, `20m`, `2h`), `--rtl rtl:1`
for a second dongle. For the ATC test: `--threshold` (squelch, dB above the noise, default 8),
`--linger`, `--max-visit`, `--atis-seconds` and `--no-clean`. With `--source synthetic`, either
test runs on simulated signals with no dongle, to check the setup.

## If nothing is heard

- **ATC:** with no ATIS received and no traffic, check that the long antenna is connected, and try
  near a window or outside. The small towers (SJC, PAO, SQL, RHV, HWD) close at night, and NorCal
  Approach is quiet late at night. Run during the day.
- **ADS-B:** zero messages points to the antenna, cable or gain (try `--gain 49.6`). A few
  aircraft, all close, points to a blocked view: try a window or higher up.
- **`Could not open the RTL-SDR`:** unplug it and plug it back in, and close other SDR programs.
