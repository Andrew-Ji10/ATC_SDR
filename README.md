# ATC_SDR

Tools for listening to, recording and decoding Bay Area air traffic with an ADALM-PLUTO
(or an AD9361 clone):

- **Airband voice (118–137 MHz AM):** many tower, ground and approach channels at once
  from a single capture, with a squelch on each, a clip saved for every transmission and
  a cleaned copy of each clip. There's also a single-channel receiver with a spectrum GUI.
- **ADS-B (1090 MHz):** aircraft positions, decoded, logged, mapped and animated.

| Script | What it does |
|---|---|
| [radio/south_bay.py](radio/south_bay.py), [sfo.py](radio/sfo.py), [oak.py](radio/oak.py) | Records every tower / ground / approach transmission in one area and summarizes the session |
| [radio/bay_all.py](radio/bay_all.py) | Surveys all 43 Bay Area channels to see what you can pick up |
| [radio/record.py](radio/record.py), [pi/record.sh](pi/record.sh) | Records any channel set for a set time, unattended (e.g. on a Raspberry Pi) |
| [radio/linkwatch.py](radio/linkwatch.py), [pi/linktest.sh](pi/linktest.sh) | Watches the Pi-Pluto link for hours and logs every drop, stall and reconnect |
| [radio/link_diag.py](radio/link_diag.py), [pi/linkdiag.sh](pi/linkdiag.sh) | Finds why the Pluto can't stream 21.6 MSPS: radio capture, network stack or iiod |
| [radio/dsp_bench.py](radio/dsp_bench.py), [pi/dspbench.sh](pi/dspbench.sh) | Benchmarks the DSP for the whole band in one window (21.6 MSPS), single- and multi-core |
| [radio/characterize.py](radio/characterize.py), [pi/characterize.sh](pi/characterize.sh) | Measures CPU, link and RF (gain sweep, interference), and recommends `--max-rate` / `--gain` |
| [radio/multi_atc.py](radio/multi_atc.py) | Plays any set of channels live, in stereo |
| [radio/listen.py](radio/listen.py), [gui.py](radio/gui.py), [atc_receiver.grc](radio/atc_receiver.grc) | One channel at a time (FM or AM), on the command line, in a GUI, or in GNU Radio Companion |
| [radio/capture_clips.py](radio/capture_clips.py) | Saves transmissions on one channel as clips |
| [radio/clean_audio.py](radio/clean_audio.py) | Reduces the static in recorded clips |
| [adsb/adsb_decode.py](adsb/adsb_decode.py) | Decodes ADS-B into a live aircraft table and log files |
| [adsb/adsb_plot.py](adsb/adsb_plot.py) | Shows ADS-B tracks on a live map, or re-plots a saved session |
| [adsb/animate_tracks.py](adsb/animate_tracks.py) | Replays a saved ADS-B session as an MP4 or GIF |

## Layout

```
atcsdr/            shared library used by every script
  pluto.py           Pluto access through libiio: configure, stream, retune, reconnect
  frequencies.py     Bay Area airport and NorCal frequencies, the area channel sets
  airband.py         multichannel AM receiver: filter bank, squelch, capture planning, recorder
  audio.py           clip files and noise reduction
  adsb.py            Mode S demodulator, input sources, pyModeS tracker
  adsb_map.py        track maps and label layout
  gr_receiver.py     GNU Radio single-channel receiver (used by listen.py, gui.py, capture_clips.py)
radio/             airband scripts (listed above)
adsb/              ADS-B scripts (listed above)
pi/                Raspberry Pi: setup.sh (install, checks), characterize.sh, linktest.sh, linkdiag.sh, dspbench.sh, record.sh
samples/adsb/      a recorded ADS-B session: tracks, maps, animations
tests/             pytest suite on synthetic signals (no hardware needed)
recordings/        where recordings go (not tracked by git)
```

Run scripts from the repository root, for example `python3 radio/sfo.py`. Each script
finds `atcsdr/` on its own, so nothing needs installing besides the environment below.

## Setup

### Install (macOS)

Everything comes from conda-forge. If you don't have conda yet, install
[Miniforge](https://github.com/conda-forge/miniforge):

```sh
curl -fsSLo miniforge.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-arm64.sh
bash miniforge.sh -b -p ~/miniforge3
~/miniforge3/bin/conda init zsh     # then open a new terminal

conda env create -f environment.yml
conda activate sdr
iio_info -s                         # should list the Pluto, e.g. [usb:1.1.5]
python -m pytest                    # optional: checks the DSP on synthetic signals
```

`iio_info -s` always prints `Unable to create Local IIO context`. That line is harmless:
it refers to a Linux-only backend. GNU Radio may print a few `vmcircbuf` warnings on its
first run. For RTL-SDR, HackRF or Airspy with the GNU Radio tools, also run
`conda install -c conda-forge gnuradio-osmosdr rtl-sdr`.

### Install (Raspberry Pi)

On Raspberry Pi OS (64-bit; a Pi 4 or 5), copy or clone this repository. The Pi talks
to the Pluto over **Ethernet**. First give the Pluto a fixed address: plug it into any
computer over USB, set `ipaddr_eth` and `netmask_eth` under `[NETWORK]` in `config.txt`
on its drive (e.g. `192.168.3.2` / `255.255.255.0`), eject it, and wait for it to reboot.
Then cable it to the Pi, or to the same switch, and run the setup once:

```sh
bash pi/setup.sh --pluto 192.168.3.2 --eth-ip 192.168.3.1/24  # cabled straight to the Pi
bash pi/setup.sh --pluto 192.168.3.2                          # on the same LAN / switch
bash pi/setup.sh --check                                      # just the checks, any time
```

It installs everything the airband recorder needs (not GNU Radio, so the single-channel
GUI tools stay on the Mac) and saves the Pluto's address in `pi/pluto.conf` for the
other scripts. `--eth-ip` gives the Pi's Ethernet port a static address on the Pluto's
subnet, with no gateway, so the internet stays on Wi-Fi. Don't use it while you're
SSHed in over that port; the script refuses. Setup ends by pinging the Pluto, opening it
through libiio and checking that it tunes 120 MHz. If that last check fails, unlock the
tuning range as below. With no `--pluto`, it uses a Pluto on USB.

#### Characterize

```sh
pi/characterize.sh              # about two minutes; --area sfo, --cpu-only, --gains 30 40 50 ...
```

This measures this setup and saves the best `--max-rate` and `--gain` in `pi/pluto.conf`
as `record.sh`'s defaults:

- **System:** the board, CPU temperature, under-voltage or throttling (a throttled Pi
  falls behind), free disk space, and the speed of the network link to the Pluto
  (it needs gigabit).
- **CPU:** how fast the Pi runs the receiver's DSP on synthetic IQ at each useful sample
  rate. That's the lowest rate for each number of capture windows: for the whole Bay,
  10.8 MSPS gives 2 windows, 5.76 gives 3, 4.32 gives 4. It picks the fewest windows
  that run at 1.5× real time or better.
- **Link:** the fraction of the samples the Pluto actually delivers at those rates.
- **RF:** a gain sweep across every capture window, with the ADC level, clipping and
  each channel's noise floor. The floor stays flat while the receiver's own noise
  dominates, then rises dB for dB once the antenna's noise does. Past that knee, more
  gain adds no sensitivity, so it recommends 6 dB past the knee, if the ADC has headroom
  there. Channels whose floor sits far above the rest are flagged as steady signals or
  local interference (the Pi, USB 3, power supplies).

The report and the per-channel floors are saved in `recordings/characterize_<time>/`
(`report.txt`, `rf.csv`). Run it with the antenna where it will stay. `radio/characterize.py`
is the same script without the wrapper (`--device ip:...`).

#### Link test

```sh
pi/linktest.sh 8h               # overnight; 0 = until Ctrl-C; --rate 10.8, --rate 0 (no stream)
```

This shows whether, and how often, the Pi loses the Pluto. Every second it pings the
Pluto, checks the Ethernet port's link, and reads samples at the rate the recorder will
use. It prints and logs each event with its time: an outage (3 or more lost pings in a
row), the cable link dropping, a stream error and how long the reconnect took, a stall
(a read taking more than about half a second), or a second short of samples. The summary
gives the counts, the total and longest outage, and the round-trip times.
`recordings/linktest_<time>/` holds `events.csv`, `summary.txt` and `link.csv` (one
row per second: ping, link and the % of samples received), for plotting. The recorder
reconnects by itself after a drop; this test tells you how often that would happen.

#### Link diagnosis: why doesn't the whole band fit through?

```sh
pi/linkdiag.sh                  # about 2 minutes; --rates 10.8 16.8 21.6, --seconds 8, --password
```

The whole band in one window is 21.6 MSPS, or 86 MB/s non-stop. The radio digitizes that
easily, but streaming it over Ethernet has fallen short (about 16 MSPS measured). This
runs three tests to find the bottleneck, reading the radio's CPU load over SSH as root
(default password `analog`) during each:

- **A.** Capture on the radio itself, with no network (`iio_readdev` to `/dev/null`): the
  chip, FPGA, DMA and RAM path.
- **B.** Plain TCP from the radio to the Pi (`dd /dev/zero | nc`), without `iiod`: the
  radio's CPU, TCP/IP stack and Ethernet.
- **C.** The normal libiio stream at several rates and buffer sizes: the whole path,
  including `iiod`, with `iiod`'s share of a radio core.
- **D.** The same stream with the work spread over the radio's two cores: the Ethernet
  interrupts on one core and `iiod` on the other, both ways round. The original
  placement is put back afterwards, and it resets on reboot anyway. Skip it with
  `--no-steering`.

The verdict says which part is the limit and what would fix it: buffer settings, a
newer `iiod`, a custom streaming program on the radio (12-bit samples need 65 MB/s), or
processing on the radio's FPGA.

#### DSP benchmark: the whole band in one window?

```sh
pi/dspbench.sh                  # about 3 minutes, no radio needed; --sustain 300, --quick
```

The 43 channels span 118.000-135.650 MHz, which fits one capture window at 21.6 MSPS: every
channel is heard all the time. This tests whether the Pi can process that rate:

1. **Today's receiver** at 10.8 MSPS (2 windows) and 21.6 MSPS (1 window).
2. **A polyphase channelizer.** 21.6 MSPS / 25 kHz = 864 channels, exactly on the airband
   grid, with a 12-tap filter per branch and small 864-point FFTs. It runs in numpy, and
   compiled with numba on 1, 2 and 4 cores. It also checks that a tone comes out of its own
   channel only (neighbours more than 50 dB down).
3. **Demodulation** of the 43 channels: today's Python loop against compiled code.
4. **The whole proposed pipeline** (raw IQ in, 43 demodulated channels out), with the share
   of time each stage takes.
5. **A sustained run** on the best core count, watching temperature and throttling: four
   busy cores heat a Pi 5 quickly, so fit the active cooler.

On a desktop, the compiled pipeline runs about 7x real time on one core and 10x on four;
today's receiver manages about 2.2x at 21.6 MSPS. The radio link must also carry
21.6 MSPS (86 MB/s): check with `pi/linktest.sh 2m --rate 21.6`.

#### Record

`pi/record.sh DURATION [options]`:

```sh
pi/record.sh 2m                            # a quick test
pi/record.sh 2h --area sfo                 # 2 hours of the SFO channels
pi/record.sh 1h30m --area south_bay --gain 50
pi/record.sh 0                             # until Ctrl-C or kill
```

- Durations look like `90` (minutes), `90s`, `45m`, `2h`, `1h30m` or `1d`.
- `--area` is `bay` (the default, all 43 channels), `sfo`, `oak`, `south_bay` or a
  `multi_atc.py` preset; `--channels "120.5 Tower, 121.5 Guard"` takes a custom list.
  Every other option of the area scripts works too (`--gain`, `--threshold`, `--raw`, ...),
  and options given here override the ones saved by `characterize.sh`.
- Output goes to `recordings/<area>_<date>-<time>/`, the same as the area scripts. Run
  from cron or systemd, the console output goes to `recordings/logs/`.
- At the end the recorder warns `received only NN% of the samples` if the Pi couldn't keep
  up. If so, use a lower `--max-rate` or record fewer channels.

To record on a schedule, add a line with `crontab -e`, for example every day 07:00-09:00:

```
0 7 * * * /home/pi/ATC_SDR/pi/record.sh 2h --area sfo
```

`radio/record.py` is the same recorder without the wrapper, and runs anywhere:
`python3 radio/record.py --device ip:192.168.3.2 --area oak --duration 45m`.

### Pluto: unlock the tuning range

A stock Pluto's AD9363 tunes only **325 MHz–3.8 GHz**, which leaves out airband (ADS-B at
1090 MHz is fine). The same chip runs as an AD9364 (**70 MHz–6 GHz**) after a firmware
setting change. SSH into the Pluto (default password `analog`) and run:

```sh
ssh root@192.168.2.1
fw_printenv attr_name attr_val    # already set if this prints "compatible" / "ad9364"
fw_setenv attr_name compatible
fw_setenv attr_val ad9364
reboot
```

If `fw_setenv` isn't available or the setting doesn't stick, update the firmware from the
[plutosdr-fw releases](https://github.com/analogdevicesinc/plutosdr-fw/releases). If
`iio_attr -u usb: -C` reports `ad9361-phy,model: ad9361`, the board already tunes
70 MHz–6 GHz.

### Pluto clones (e.g. HamGeek Zynq7020 + AD9361)

These boards have two Type-C ports. **UART + JTAG** is a serial console only. **USB 2.0**
is the data port: connect that one. The board shows up as a `PlutoSDR (ADALM-PLUTO)` USB
device with a `PlutoSDR` drive, and the scripts find it automatically. If it stops
responding (`Failed to reset pipes`), unplug it for a few seconds and plug it back in.

The board also has a **gigabit Ethernet** port. USB 2.0 carries about 8 MSPS, which is
roughly 6.5 MHz of spectrum. Ethernet carries about 30 MSPS, enough for all of the Bay's
airband channels at once. To use Ethernet:

1. Set `ipaddr_eth` in the drive's `config.txt`.
2. Connect the board with a USB-C Ethernet adapter.
3. Pass the address with `--device ip:<address>`.

### Antennas

Antenna length matters more than anything else:

- **Airband (118–137 MHz):** use a vertical quarter-wave whip, about 60 cm (24 in).
- **ADS-B (1090 MHz):** use a quarter-wave whip of about 6.9 cm, ideally on a ground
  plane. A 1090 MHz filter with an LNA at the antenna (for example a Nooelec SAWbird+ ADS-B)
  helps a lot.

For both, put the antenna outdoors or at a window, away from the computer, because laptops
and USB hubs radiate broadband noise.

## Airband

### Area test scripts

Each script monitors one area's tower, ground and clearance frequencies, plus the NorCal
approach and departure sectors around it. It saves every transmission and ends with a
summary of what each channel heard.

```sh
python3 radio/south_bay.py          # SJC, PAO, RHV, NUQ + NorCal South (17 channels)
python3 radio/sfo.py                # SFO, SQL + NorCal West (15 channels)
python3 radio/oak.py                # OAK, HWD + NorCal East (20 channels)
python3 radio/bay_all.py            # all 43 channels above, plus guard 121.5

python3 radio/sfo.py --list         # show the channels and how they'll be captured
python3 radio/sfo.py --minutes 30 --listen        # longer, and play the audio
python3 radio/bay_all.py --device ip:192.168.3.2 # over Ethernet: everything at once
```

Each run writes to `recordings/<area>_<date>-<time>/`:

- `clips/` holds every transmission, as `<MHz>MHz_<name>_<date>-<time>.wav` and `.mp3`.
- `clips_clean/` holds the same clips with the static reduced. Turn it off with `--no-clean`.
- `transmissions.csv` lists each transmission: time, frequency, channel, length and strength.
- `summary.txt` gives, per channel, the time monitored, the number of transmissions, the
  total talk time and the strongest signal.
- `raw/` (only with `--raw`) holds one continuous WAV per channel. Each file is padded with
  silence while its channel isn't monitored, so all the files line up with the clock.

**Capture windows.** The channels have to fit in what the link can stream.

- **Over USB:** each area spans about 17 MHz, so the script splits it into the fewest
  6.5 MHz windows (three for every area) and rotates through them, `--dwell` seconds
  each (default 30). Each channel is then monitored about a third of the time, and the
  summary shows exactly how long. A transmission in progress when the window changes is
  cut off at that point.
- **Over Ethernet:** everything fits in one capture at 21.6 MSPS.

Other options:

- `--threshold` (default 8 dB above each channel's noise): raise it if noise opens the
  squelch, lower it to catch weaker aircraft.
- `--gain` sets the RF gain (default 40).
- `--atis` adds the ATIS loops, mixed in quietly and without squelch.

The frequencies come from AirNav (October 2026) and are listed in
[atcsdr/frequencies.py](atcsdr/frequencies.py). The towers at smaller fields close at night,
so record during the day to hear them.

### Many channels, live: `multi_atc.py`

This uses the same receiver as the area scripts, but plays the audio by default and takes
any channel set:

```sh
python3 radio/multi_atc.py                       # SFO clearance, tower, ground, departure, guard
python3 radio/multi_atc.py --preset norcal-usb   # the 7 NorCal sectors in 120.1-126.475 MHz
python3 radio/multi_atc.py --channels "120.5 Tower, 121.8 Ground"
python3 radio/multi_atc.py --preset sfo --minutes 5 --no-audio --clips rec/clips --log rec/log.csv
python3 radio/multi_atc.py --list                # all presets
```

Channels are spread across the stereo field, lowest frequency on the left. A status line
shows who's talking.

How it works:

- **Reading:** the receiver reads the Pluto directly with libiio, because GNU Radio's Pluto
  source tops out near 6.5 MSPS on this Mac.
- **Channel separation:** one FFT per block, then a small inverse FFT per channel, at
  48 kHz. On this Mac, that runs at 2.2× real time for all 43 channels at 21.6 MSPS
  (Ethernet), and 6.6× for the USB windows.
- **Demodulation and squelch:** AM envelope detection, with a squelch on each channel that
  learns that channel's noise floor.
- **Reliability:** reading runs on its own thread, so slow processing never makes the Pluto
  overflow. If the USB link drops, the receiver reconnects automatically.

### One channel at a time

```sh
python3 radio/listen.py 118.6 --squelch -45      # AM tower, with squelch
python3 radio/listen.py 88.5                     # FM broadcast (mode from the frequency)
python3 radio/gui.py 118.6                       # spectrum + waterfall, click to tune
gnuradio-companion radio/atc_receiver.grc        # the same receiver as a GRC flowgraph
python3 radio/capture_clips.py 120.1 --clips 5   # save 5 transmissions from one channel
```

These use GNU Radio and gr-iio and work with an RTL-SDR too (`--sdr osmosdr`). In
`listen.py`, type a frequency to retune. `v`, `g` and `sq` set the volume, gain and
squelch, `s` prints the channel power, and `q` quits. The mode is picked from the
frequency (87.5–108 MHz is WFM, 108–137 MHz is AM, anything else is NBFM), and `--mode`
overrides it. Use manual gain for ATC: the AGC raises the noise between transmissions.

In the GUI, the spectrum and waterfall show the selected band; click or drag the marker
to tune. The step follows the mode. Changing the span restarts the radio for about a
second. Power readings are relative (dB full scale), not dBm.

In GNU Radio Companion, double-click the spectrum to tune. Running the flowgraph writes
a generated `atc_receiver.py` next to it, which git ignores.

### Cleaning clips

The area scripts already save cleaned copies. For other recordings, or to try a
different strength:

```sh
python3 radio/clean_audio.py recordings/sfo_20261016-1400 --strength 0.7
```

The cleaning has three steps:

1. **Voice filter:** keeps 300–3000 Hz.
2. **Noise reduction:** spectral noise reduction, using the gaps between words in each
   clip as the noise sample. With 0.9 strength, this gained 8–17 dB of speech over
   static on 14 real clips.
3. **Loudness:** normalizes each clip to the same level.

Weak transmissions get quieter between words, but buried words can't be recovered. A
better antenna is the real fix.

## ADS-B

ADS-B works on a stock Pluto, because 1090 MHz is inside its range. It samples at 4 MSPS
and finds Mode S preambles. Only 112-bit DF17/18 frames with a valid CRC are kept, and
[pyModeS](https://github.com/junzis/pyModeS) 3.x turns them into callsign, altitude,
speed and position.

### Decode

```sh
python3 adsb/adsb_decode.py --ref 37.43 -122.17                 # live aircraft table
python3 adsb/adsb_decode.py --minutes 30 --out recordings/adsb_evening
python3 adsb/adsb_decode.py --messages                          # every decoded message
python3 adsb/adsb_decode.py --hex recordings/adsb_evening/messages.txt   # decode a log again
```

The table shows each aircraft's callsign, altitude, speed, track, climb rate, position,
range from `--ref`, message count and age. With `--out DIR`, the script also writes:

- `positions.csv`: every position fix.
- `messages.txt`: every valid message, as `<unix time> <hex>`.
- `decoded.jsonl`: every decoded message.
- `aircraft.csv`: one row per aircraft.

`--ref` is your latitude and longitude. With it, positions decode from the first message.
Without it, pyModeS waits for a few even/odd position pairs per aircraft.

### Map

```sh
python3 adsb/adsb_plot.py --ref 37.43 -122.17 --csv tracks.csv  # live map, logging positions
python3 adsb/adsb_plot.py --track-csv samples/adsb/tracks.csv --ref 37.43 -122.17 \
    --no-plot --save map.png --label-every 0                     # re-plot a session
```

Tracks are colored by altitude. Every fix is a dot, and about ten fixes per track are
labeled with their time. `--label-every 30` labels one fix every 30 s. `--label-every 0`
labels every fix, stacked beside the track with leader lines, and `--save` enlarges the
image until none of the labels overlap. Gaps longer than 30 s are drawn dotted.

### Animate

```sh
python3 adsb/animate_tracks.py samples/adsb/tracks.csv --ref 37.43 -122.17   # -> tracks.mp4
python3 adsb/animate_tracks.py tracks.csv --speed 20 --out tracks.gif
```

Each aircraft is drawn as an arrow along its track, colored by altitude. It has a fading
trail, a dashed velocity vector (where it will be in 60 s), and a label with altitude,
speed and climb/descent. Stretches with no aircraft in view are skipped.

### Other inputs

The decode and map scripts all take the same inputs:

- `--iq file.cs16` replays a capture made with `--record`. RTL-SDR `.cu8` captures work
  too, with `--iq-rate 2e6`.
- `--network host:30002` reads a dump1090 or readsb raw feed.
- `--hex file` reads a message log.

For better sensitivity, run a dedicated decoder ([PlutoSDR/dump1090](https://github.com/PlutoSDR/dump1090),
[readsb](https://github.com/wiedehopf/readsb)) and point `--network` at it.

[samples/adsb/](samples/adsb/) holds a session recorded on 2026-10-12: the position
logs, maps and animations made from them.

## Tests

```sh
python -m pytest            # about 50 s, no hardware needed
```

The tests run on synthetic signals:

- **Filter bank:** each channel's own tone comes back cleanly, at both USB and Ethernet
  rates.
- **Capture planning:** every channel is covered exactly once, clear of the Pluto's DC
  offset.
- **Squelch:** it opens on a transmission and closes after it.
- **Raw files:** they stay aligned in time.
- **ADS-B:** frames are found at 2 and 4 MSPS, positions decode for known messages and a
  simulated flight, and the input readers parse correctly.
- **Noise reduction:** it improves speech over static.
- **Frequency tables:** the area channel sets are well formed.
- **Full receiver:** an end-to-end run of the 43-channel survey against a simulated
  Pluto, rotating through its 3 windows.
