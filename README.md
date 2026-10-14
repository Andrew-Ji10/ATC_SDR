# ATC_SDR

A GNU Radio receiver for local FM broadcast stations and airband ATC (AM, 108–137 MHz).
It uses an ADALM-PLUTO through gr-iio by default. RTL-SDR, HackRF and Airspy work
through gr-osmosdr (`--sdr osmosdr`).

## Pluto: unlock the tuning range first

A stock Pluto's AD9363 tunes only **325 MHz–3.8 GHz**, which leaves out FM broadcast and
airband. The same chip runs as an AD9364 (**70 MHz–6 GHz**) after a firmware setting
change. SSH into the Pluto (default password `analog`) and run:

```sh
ssh root@192.168.2.1
fw_printenv attr_name attr_val    # already set if this prints "compatible" / "ad9364"
fw_setenv attr_name compatible
fw_setenv attr_val ad9364
reboot
```

If `fw_setenv` isn't available or the setting doesn't stick, update the firmware from
the [plutosdr-fw releases](https://github.com/analogdevicesinc/plutosdr-fw/releases).
Clones such as Pluto+ or LibreSDR often ship with the wide range already enabled.

## Pluto clones (e.g. HamGeek Zynq7020 + AD9363/AD9361)

These boards have two Type-C ports. **UART + JTAG** is a serial console only
(`/dev/tty.usbserial-…`). **USB 2.0** is the data port. Connect the USB 2.0 port to the
Mac. The board then shows up as a `PlutoSDR (ADALM-PLUTO)` USB device with a `PlutoSDR`
drive, and libiio talks to it directly over USB:

```sh
iio_info -s        # lists e.g. [usb:0.5.5]; the scripts find it automatically
```

The default `usb_ethernet_mode = rndis` doesn't give macOS a network link, so
`192.168.2.1` won't respond. That's fine, because USB is all the scripts need. To use the
network link anyway, set `usb_ethernet_mode = ncm` in the drive's `config.txt`, eject the
drive, and reboot the board.

If `iio_attr -u usb: -C` reports `ad9361-phy,model: ad9361`, the board already tunes
70 MHz–6 GHz and the firmware change above isn't needed.

## Install (macOS)

GNU Radio, the Pluto drivers (gr-iio, libiio) and the GUI libraries all come from conda-forge.
If you don't have conda yet, install [Miniforge](https://github.com/conda-forge/miniforge):

```sh
curl -fsSLo miniforge.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-arm64.sh
bash miniforge.sh -b -p ~/miniforge3
~/miniforge3/bin/conda init zsh     # then open a new terminal
```

Create the environment and check that the Pluto is found:

```sh
conda create -n sdr -c conda-forge gnuradio gnuradio-iio libiio libad9361-iio pyqt pyqtgraph
conda activate sdr
iio_info -s                       # should list the Pluto (USB and/or ip:192.168.2.1)
iio_info -u ip:192.168.2.1        # or query it directly over its USB network link
```

`iio_info -s` always prints `Unable to create Local IIO context`. That line is harmless:
it refers to a Linux-only backend. The first GNU Radio run may also print a few
`vmcircbuf` warnings while it picks a buffer method. They stop after that.

For RTL-SDR, HackRF or Airspy, also run `conda install -c conda-forge gnuradio-osmosdr rtl-sdr`.

## GNU Radio Companion flowgraph

[atc_receiver.grc](atc_receiver.grc) is the same receiver as a GNU Radio Companion
flowgraph, with the Pluto source and all the controls already set up.

```sh
gnuradio-companion atc_receiver.grc   # open it in the editor, then press Run (F6)
grcc -r atc_receiver.grc              # or compile and run it without the editor
```

The window gives you frequency and band-center boxes, a mode chooser (Auto / WFM / AM /
NBFM), an audio on/off checkbox, sliders for volume, squelch and RF gain, a signal meter,
and a spectrum and waterfall of the band. **Double-click** the spectrum or waterfall to
tune. The band stays put while the tuned frequency is inside it. If you tune outside the
band, the band follows. To change the span, edit the `samp_rate` variable in the editor.
It must be a multiple of 240000. Running the flowgraph from the editor writes
`atc_receiver.py` next to it; that file is generated and can be ignored.

## Python GUI

```sh
python3 sdr_gui.py 118.6                 # airband, 2.4 MHz of spectrum around 118.6
python3 sdr_gui.py 98.5 --span 3.84      # FM broadcast, wider view
```

The window has these parts:

- **Spectrum and waterfall** of the band you select. Click anywhere on them, or drag the
  yellow marker, to tune. The shaded strip shows the channel you're listening to.
- **Tuning**: type a frequency or scroll it. The step follows the mode: 100 kHz for WFM,
  25 kHz for AM and 12.5 kHz for NBFM. Mode `auto` switches the demodulator as you move
  between the FM band and airband.
- **Band**: the center and span (1.2, 2.4 or 3.84 MHz) of the displayed spectrum.
  "Strongest" shows the loudest signal in the band. Tuning within the band is instant.
  Tuning outside it moves the band to follow. Changing the span restarts the radio
  for about a second.
- **Signal**: the power in the tuned channel and whether the squelch is open. Set the
  squelch slider between the idle and transmitting readings. Moving it all the way left
  turns the squelch off.
- **Audio**: on/off and volume. Muting doesn't affect a `--record` file.
- **RF**: manual gain or AGC.

All the command-line options below also work with `sdr_gui.py`. Power is shown in dB
relative to full scale. It isn't calibrated to dBm, but it works for comparing signals.

## Capturing transmissions as clips

`capture_clips.py` listens on an AM or NBFM channel and saves each transmission as its own
WAV and MP3 file in `clips/`. It skips the silence between transmissions.

```sh
python3 capture_clips.py 120.1                          # wait for one transmission
python3 capture_clips.py 120.1 --clips 10 --max-time 1800
python3 capture_clips.py 118.6 --listen                 # also play it while waiting
```

The tool measures the channel's noise floor for 2 seconds, then starts a clip when the
signal rises `--threshold` dB (default 8) above it. A clip includes 0.3 s from before
the trigger, so the first word isn't lost. It ends after `--hang` seconds of quiet
(default 1.5), so short pauses don't split a transmission. Gain defaults to 40 dB.
If clips are being triggered by noise, raise `--threshold`. If weak aircraft are being
missed, lower it.

## Command line

```sh
python3 sdr_receiver.py 88.5                    # FM broadcast (mode auto-detected)
python3 sdr_receiver.py 118.6 --squelch -45     # ATC tower, AM with squelch
python3 sdr_receiver.py 120.5 --record tower.wav
python3 sdr_receiver.py 162.55 --mode nbfm      # NOAA weather radio
python3 sdr_receiver.py 118.6 --device ip:192.168.2.1   # Pluto at a specific URI
python3 sdr_receiver.py 88.5 --sdr osmosdr      # RTL-SDR instead of Pluto
```

Pluto gain is set in dB from 0 to 73 (`--gain 40`). You can also use the AGC with
`--gain auto` (slow attack) or `--gain fast` (fast attack). For ATC, use manual gain:
the AGC raises the noise between transmissions.

The mode is picked from the frequency: 87.5–108 MHz is `wfm`, 108–137 MHz is `am`,
and anything else is `nbfm`. Use `--mode` to override it.

While it's running, you can type commands:

| Command        | Effect                                        |
|----------------|-----------------------------------------------|
| `121.8`        | retune to 121.8 MHz (same mode)               |
| `v 2`          | set the volume                                |
| `g 40` / `g auto` / `g fast` | set the RF gain              |
| `sq -45`       | set the squelch threshold (AM/NBFM only)      |
| `s`            | print channel power in dB                     |
| `q`            | quit                                          |

### Tips for ATC

- ATC is quiet between transmissions. Type `s` when the channel is idle and again
  during a transmission, then set the squelch between the two values.
- Look up your airport's tower, ground and ATIS frequencies on
  [AirNav](https://www.airnav.com) or [LiveATC](https://www.liveatc.net).
  ATIS transmits all the time, so it's a good first test.
- Airband antennas are vertical and about 60 cm long (a ¼ wave). Line of sight matters:
  you'll hear aircraft in the air better than you hear the ground.
- RTL-SDR: if stations show up slightly off frequency, measure your dongle's error and
  pass `--ppm`.

## ADS-B: decode and plot aircraft positions

[adsb_plot.py](adsb_plot.py) receives ADS-B on 1090 MHz, decodes it and draws a live
map. The map shows each aircraft's track colored by altitude and a table of flights.
1090 MHz is inside a stock Pluto's range, so this part doesn't need the firmware change.
Decoding uses [pyModeS](https://github.com/junzis/pyModeS) 3.x, which needs Python 3.11
or newer:

```sh
pip install "pyModeS>=3"           # inside the sdr env; matplotlib is already there
python3 adsb_plot.py --ref 37.43 -122.17               # Pluto; --ref = your lat/lon
python3 adsb_plot.py --csv tracks.csv --save map.png   # also log positions, save the map
python3 adsb_plot.py --record adsb.cs16                # keep the raw IQ ...
python3 adsb_plot.py --iq adsb.cs16                    # ... and replay it later
python3 adsb_plot.py --network localhost:30002         # use dump1090/readsb as receiver
python3 adsb_plot.py --hex messages.txt --no-plot --csv out.csv
python3 adsb_plot.py --track-csv tracks.csv --save map.png   # re-plot a saved --csv log
```

Every position fix is drawn as a dot. Fixes are labeled with their local time, about
ten per track, and labels that would overlap are skipped. `--label-every 30` labels one
fix every 30 seconds. `--label-every 0` labels every fix, to a tenth of a second. Those
labels are stacked in columns beside each track, with a thin line to their fix, and
`--save` makes the image as large as it needs to be for none of them to overlap.

With a Pluto, the script samples at 4 MSPS. It finds Mode S preambles and keeps only the
112-bit DF17/18 frames whose CRC checks out. pyModeS then turns them into callsign,
altitude, speed and position. `--ref` is optional. With it, positions show up from the
first message. Without it, pyModeS waits for a few even/odd position pairs per aircraft.
`--iq` also reads RTL-SDR `.cu8` captures; for those, add `--iq-rate 2e6`.

For better range, use a dedicated decoder as the receiver and point `--network` at its
raw output (port 30002):

- [PlutoSDR/dump1090](https://github.com/PlutoSDR/dump1090): dump1090 with Pluto support.
- [readsb](https://github.com/wiedehopf/readsb): build it with Pluto support.
- [dump1090-fa](https://github.com/flightaware/dump1090): RTL-SDR, or SoapySDR devices.

The antenna matters more than anything else here. A ¼ wave at 1090 MHz is about 6.9 cm,
so the 60 cm airband antenna is a poor match. Put the antenna near a window or outdoors.

## Many channels at once

[multi_atc.py](multi_atc.py) demodulates several airband channels from a single Pluto
capture. Each channel has its own squelch, which learns that channel's noise level. The
script plays the active channels spread across the stereo field and logs each transmission:

```sh
python3 multi_atc.py                                  # SFO clearance/tower/ground/departure/guard
python3 multi_atc.py --preset norcal-usb --duration 300 --no-audio \
    --raw recordings/raw --clips recordings/clips      # 5-minute NorCal recording
python3 multi_atc.py --channels "120.5 Tower, 121.8 Ground"
```

`--raw` writes one continuous WAV per channel. `--clips` saves each transmission as
`<MHz>_<name>_<date>-<time>.wav` (plus `.mp3`). The script needs `pip install sounddevice`
for audio output. It reads the Pluto directly with libiio, because GNU Radio's Pluto
source can't sustain the sample rate it needs on this machine.

All the channels must fit in one capture. Over USB 2.0, the Pluto streams about 8 MSPS,
which covers roughly 6.5 MHz. That fits SFO's airport channels (118.2–124.25 MHz) or seven
NorCal approach/departure channels (`norcal-usb`, 120.1–126.475 MHz). The full 14-channel
`norcal` preset (120.1–135.2 MHz) needs about 19 MSPS, so it needs the board's gigabit
Ethernet port (`--device ip:…`). If the USB link drops, the script reconnects and fills the
gap in the raw files with silence. If samples are lost, it says so at the end.

To reduce the static in a recording's clips, run [clean_audio.py](clean_audio.py):

```sh
python3 clean_audio.py recordings/norcal_20261014-0022    # writes clips_clean/
```

It applies a 300–3000 Hz voice band-pass, then spectral noise reduction (`pip install
noisereduce`) using the gaps between words in each clip as the noise sample, and then
evens out the loudness. Use `--strength` (0–1, default 0.9) to set how much static it
removes.

To replay a logged ADS-B session as a video, run [animate_tracks.py](animate_tracks.py):

```sh
python3 animate_tracks.py tracks.csv --ref 37.43 -122.17            # -> tracks.mp4 (10x)
python3 animate_tracks.py tracks.csv --speed 20 --out tracks.gif
```

Each aircraft is drawn as an arrow along its track, colored by altitude. It has a fading
trail, a dashed velocity vector (where it will be in 60 s), and a label with altitude,
speed and climb/descent. Stretches with no aircraft in view are skipped. MP4 output needs
`pip install imageio-ffmpeg`.
