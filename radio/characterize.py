#!/usr/bin/env python3
"""
Characterize this computer + Pluto + antenna for the airband recorder, and
recommend --max-rate and --gain for radio/record.py (pi/record.sh).

    python3 radio/characterize.py --device ip:192.168.3.2
    python3 radio/characterize.py --cpu-only               # no Pluto needed
    python3 radio/characterize.py --device ip:192.168.3.2 --area sfo --gains 30 40 50 60

Four parts, about two minutes in all:
  1. System: board, CPU temperature, under-voltage / throttling (Raspberry Pi),
     free disk, and the speed of the network link to the Pluto.
  2. CPU: how fast this computer runs the receiver's DSP at each sample rate
     worth using (the lowest rate for each number of capture windows), on
     synthetic IQ. Real time needs 1x; the recommendation wants 1.5x, leaving room
     for reading the Pluto and saving clips.
  3. Link: the sample rate the Pluto actually delivers at each of those rates.
  4. RF: for each capture window and gain, the ADC level (clipping) and each
     channel's noise floor. The noise floor stays flat while the receiver's own
     ADC noise dominates, then rises dB for dB with gain once the antenna's noise
     does; past that point more gain buys no sensitivity, only overload risk.
     The recommended gain is 6 dB past that knee, if the ADC has headroom there.
     Channels far above the others' floor have a steady signal or local
     interference (USB 3, power supplies, the Pi itself).

Writes report.txt and rf.csv to recordings/characterize_<date>-<time>/ (or --out).
"""

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import frequencies as fq
from atcsdr import pluto
from atcsdr.airband import KEEP, Channel, FilterBank, find_uri, max_rate, plan, rates
from atcsdr.pluto import is_ethernet, to_complex

FULL_SCALE = 2048            # the AD936x's 12-bit samples, in int16
CLIP_LEVEL = 2000            # |I| or |Q| at least this counts as clipped
NEED_RT = 1.5                # DSP speed (x real time) the recommendation asks for
NEED_LINK = 0.99             # fraction of the samples the link must deliver
KNEE_SLOPE = 0.7             # floor rising this many dB per dB of gain: antenna noise dominates
KNEE_MARGIN = 6              # dB of gain past the knee
INTERFERENCE_DB = 6          # a channel floor this far above the median gets flagged


class Report:
    """Prints as it goes and keeps the text for report.txt."""

    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def section(self, title):
        self("")
        self(f"== {title}")


def candidates(freqs, limit):
    """The sample rates worth trying: for each number of capture windows, the
    lowest rate that gives it (the same coverage for the least CPU and link)."""
    best = {}
    for r in rates(limit):
        rate, windows = plan(freqs, r)
        best.setdefault(len(windows), (rate, windows))
    return sorted(best.values(), key=lambda rw: rw[0])


# ------------------------------------------------------------------ 1. system

def _read(path):
    try:
        return Path(path).read_text().strip("\x00\n ")
    except OSError:
        return None


def system(rep, uri):
    rep.section("System")
    rep(f"board:      {_read('/proc/device-tree/model') or sys.platform}, {os.cpu_count()} cores, "
        f"Python {sys.version.split()[0]}, numpy {np.__version__}")
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    if temp:
        rep(f"CPU temp:   {int(temp) / 1000:.0f} C")
    if shutil.which("vcgencmd"):
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout
        flags = int(out.split("=")[-1], 16) if "=" in out else 0
        notes = [text for bit, text in [(0, "UNDER-VOLTAGE NOW"), (2, "THROTTLED NOW"),
                                        (16, "under-voltage has occurred"),
                                        (18, "throttling has occurred")] if flags >> bit & 1]
        rep(f"power:      {', '.join(notes) if notes else 'ok (no under-voltage or throttling)'}")
        if flags & 0x10001:
            rep("            -> use the official power supply; a throttled Pi falls behind.")
    Path("recordings").mkdir(exist_ok=True)
    free = shutil.disk_usage("recordings").free / 1e9
    rep(f"disk free:  {free:.1f} GB in recordings/ (clips use roughly 0.1-1 GB a day)")
    if is_ethernet(uri):
        host = uri.split(":", 1)[1]
        route = subprocess.run(["ip", "route", "get", host], capture_output=True, text=True).stdout \
            if shutil.which("ip") else ""
        dev = re.search(r"\bdev (\S+)", route)
        if dev:
            speed = _read(f"/sys/class/net/{dev.group(1)}/speed")
            rep(f"network:    {host} via {dev.group(1)}" + (f", {speed} Mb/s" if speed else ""))
            if speed and speed.lstrip("-").isdigit() and 0 < int(speed) < 1000:
                rep(f"            -> {speed} Mb/s carries at most ~{int(speed) / 32 * 0.8:.0f} "
                    "MSPS; use a gigabit port and cable.")


# ------------------------------------------------------------------ 2. CPU

def bench(freqs, rate, windows, seconds):
    """DSP speed, in x real time, for the busiest window at `rate`: the same work
    the receiver does per buffer (convert, filter bank, demodulate, squelch, mix)."""
    w = max(windows, key=lambda w: len(w.channels))
    bank = FilterBank(rate, [f.mhz * 1e6 - w.center for f in w.channels])
    chans = [Channel(f) for f in w.channels]
    raw = (np.random.default_rng(0).standard_normal(2 * (1 << 19)) * 30).astype(np.int16)
    done, start = 0, time.perf_counter()
    while done < seconds * rate:
        now = time.time()
        for block in bank.process(to_complex(raw)):
            mix = np.zeros((KEEP, 2), np.float32)
            for ch, x in zip(chans, block):
                audio, _ = ch.process(x, now, now)
                mix += audio[:, None] * ch.lr
        done += len(raw) // 2
    return done / rate / (time.perf_counter() - start), len(w.channels)


# ------------------------------------------------------------------ 3. link

def open_radio(uri, center, rate, gain):
    try:
        return pluto.Pluto(uri, center, rate, gain)
    except OSError as e:
        return str(e)


def link(uri, rate, center, gain, seconds):
    """Fraction of `rate` the Pluto delivers, reading flat out (no DSP)."""
    radio = open_radio(uri, center, rate, gain)
    if isinstance(radio, str):
        return None, radio
    try:
        radio.flush()
        radio.read()
        n, start = 0, time.perf_counter()
        while time.perf_counter() - start < seconds:
            n += len(radio.read()) // 2
        return n / ((time.perf_counter() - start) * rate), None
    except OSError as e:
        return None, str(e)
    finally:
        radio.close()


# ------------------------------------------------------------------ 4. RF

def capture(radio, seconds):
    radio.flush()
    chunks, n = [], 0
    while n < seconds * radio.rate:
        chunks.append(radio.read())
        n += len(chunks[-1]) // 2
    return np.concatenate(chunks)


def sweep(rep, uri, rate, windows, gains, seconds):
    """rows of (window, gain, Freq, floor_db) and per-(window, gain) ADC stats."""
    radio = open_radio(uri, windows[0].center, rate, gains[0])
    if isinstance(radio, str):
        rep(f"could not open the Pluto at {uri}: {radio}")
        return [], {}
    rows, adc = [], {}
    try:
        for wi, w in enumerate(windows):
            radio.tune(w.center)
            bank = FilterBank(rate, [f.mhz * 1e6 - w.center for f in w.channels])
            for g in gains:
                radio.set_gain(g)
                raw = capture(radio, seconds)
                iq = to_complex(raw)
                adc[wi, g] = (10 * math.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-20),
                              float(np.mean(np.abs(raw) >= CLIP_LEVEL)))
                bank.reset()
                power = np.array([np.mean(np.abs(b) ** 2, axis=1) for b in bank.process(iq)])
                # 20th percentile over ~16 ms frames: the floor between transmissions,
                # as the squelch measures it.
                floors = 10 * np.log10(np.percentile(power, 20, axis=0) + 1e-20)
                rows += [(wi, g, f, float(db)) for f, db in zip(w.channels, floors)]
                if sys.stdout.isatty():
                    print(f"\r\033[K  window {wi + 1}/{len(windows)}, gain {g:g} dB",
                          end="", flush=True)
    except OSError as e:
        rep(f"connection error during the sweep: {e}")
    finally:
        radio.close()
        if sys.stdout.isatty():
            print("\r\033[K", end="")
    return rows, adc


def pick_gain(rep, rows, adc, gains, n_windows):
    """Report the sweep and return (recommended gain, interference rows)."""
    med = {g: float(np.median([db for _, gg, _, db in rows if gg == g])) for g in gains}
    worst_rms = {g: max(adc[w, g][0] for w in range(n_windows) if (w, g) in adc) for g in gains}
    worst_clip = {g: max(adc[w, g][1] for w in range(n_windows) if (w, g) in adc) for g in gains}
    rep(f"{'gain':>5} {'ADC rms':>8} {'clipped':>9} {'median floor':>13} {'slope':>6}")
    knee = None
    for i, g in enumerate(gains):
        slope = ((med[gains[i + 1]] - med[g]) / (gains[i + 1] - g)) if i + 1 < len(gains) else None
        if knee is None and slope is not None and slope >= KNEE_SLOPE:
            knee = g
        rep(f"{g:5g} {worst_rms[g]:7.1f}  {100 * worst_clip[g]:8.4f}% {med[g]:12.1f}  "
            + (f"{slope:5.2f}" if slope is not None else "     "))
    rep("ADC rms in dBFS and clipped samples: the worst window. Floor: relative dB, "
        "median over channels. Slope: dB of floor per dB of gain, to the next step.")
    headroom = [g for g in gains if worst_clip[g] < 1e-5 and worst_rms[g] < -15]
    if not headroom:
        rep("-> the ADC clips even at the lowest gain: a very strong signal is in the band "
            "(a nearby transmitter?). Try an airband filter.")
        return gains[0], []
    if knee is None:
        target = max(headroom)
        rep(f"-> no knee: the receiver's own noise dominates at every gain tried, so use the "
            f"highest gain with headroom, {target:g} dB. A better antenna or an LNA would help.")
    else:
        want = knee + KNEE_MARGIN
        target = min([g for g in headroom if g >= want] or [max(headroom)])
        rep(f"-> antenna noise dominates from {knee:g} dB: recommend {target:g} dB"
            + ("" if target >= want else f" (headroom stops it short of {want:g})") + ".")
    at = [(w, f, db) for w, g, f, db in rows if g == target]
    hot = [(w, f, db - med[target]) for w, f, db in at if db - med[target] > INTERFERENCE_DB]
    return target, hot


# ------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    names = sorted(set(fq.PRESETS) | set(fq.AREAS))
    p.add_argument("--device", default="", help="Pluto URI, e.g. ip:192.168.3.2 (default: auto-detect)")
    p.add_argument("--area", choices=names, default="bay", help="channel set (default bay)")
    p.add_argument("--gains", type=float, nargs="+", default=[10, 20, 30, 40, 50, 60, 70],
                   help="gains to sweep, dB (default 10 20 ... 70)")
    p.add_argument("--seconds", type=float, default=4.0,
                   help="seconds per CPU and link test (default 4)")
    p.add_argument("--cpu-only", action="store_true", help="only the system and CPU tests")
    p.add_argument("--out", help="output folder (default recordings/characterize_<time>)")
    p.add_argument("--flags-file", help="also write the recommended flags to this file "
                                        "(pi/characterize.sh uses it)")
    args = p.parse_args()

    rep = Report()
    uri = find_uri(args.device)
    freqs = fq.area_channels(args.area) if args.area in fq.AREAS else fq.preset_channels(args.area)
    limit = max_rate(uri)
    rep(f"Characterization, {datetime.now():%Y-%m-%d %H:%M}: {args.area} ({len(freqs)} channels), "
        f"Pluto {uri} ({'Ethernet' if is_ethernet(uri) else 'USB'}, up to {limit / 1e6:g} MSPS)")
    system(rep, uri)

    options = candidates(freqs, limit)
    rep.section("CPU (DSP speed on synthetic IQ, busiest window)")
    rep(f"{'MSPS':>6} {'windows':>8} {'heard':>6} {'ch/window':>10} {'speed':>7}")
    speed = {}
    for rate, windows in options:
        speed[rate], n = bench(freqs, rate, windows, args.seconds)
        rep(f"{rate / 1e6:6.2f} {len(windows):8d} {100 / len(windows):5.0f}% {n:10d} "
            f"{speed[rate]:6.1f}x" + ("" if speed[rate] >= NEED_RT else "  too slow"))
    rep(f"heard = share of the time each channel is monitored. Needs >= {NEED_RT:g}x.")

    got = {}
    if not args.cpu_only:
        rep.section("Link (samples delivered, reading flat out)")
        for rate, windows in options:
            got[rate], err = link(uri, rate, windows[0].center, "40", args.seconds)
            if err:
                rep(f"could not read the Pluto at {uri}: {err}")
                if not args.device and not is_ethernet(uri):
                    rep("Over Ethernet, pass --device ip:<address>.")
                got = {}
                break
            rep(f"{rate / 1e6:6.2f} MSPS: {100 * got[rate]:5.1f}%"
                + ("" if got[rate] >= NEED_LINK else "  drops samples"))

    # The fewest windows this computer (and link) can keep up with.
    usable = [(rate, w) for rate, w in options
              if speed[rate] >= NEED_RT and (not got or got.get(rate, 0) >= NEED_LINK)]
    rate, windows = min(usable, key=lambda rw: (len(rw[1]), rw[0])) if usable else options[0]

    gain, hot, rows = None, [], []
    if got:
        rep.section(f"RF (gain sweep at {rate / 1e6:g} MSPS, {len(windows)} windows, "
                    f"{args.seconds / 2:g} s each)")
        gains = sorted(args.gains)
        rows, adc = sweep(rep, uri, rate, windows, gains, args.seconds / 2)
        if rows:
            gain, hot = pick_gain(rep, rows, adc, gains, len(windows))
            if hot:
                rep(f"Channels {INTERFERENCE_DB} dB+ above the median floor at {gain:g} dB "
                    "(a steady signal, or interference: move the antenna away from the Pi "
                    "and USB 3 / power cables, add ferrites; or just traffic, so re-run):")
                for w, f, excess in sorted(hot, key=lambda h: -h[2]):
                    rep(f"  {f.mhz:8.3f} {f.name:<11} +{excess:4.1f} dB  (window {w + 1})")

    rep.section("Recommendation")
    if not usable:
        rep(f"This computer is too slow even at {rate / 1e6:g} MSPS: record fewer channels "
            "(--area sfo, or --channels), and use --no-clean.")
    rep(f"--max-rate {rate / 1e6:g}: {len(windows)} capture window(s), each channel heard "
        f"{100 / len(windows):.0f}% of the time, DSP at {speed[rate]:.1f}x real time.")
    if gain is not None:
        rep(f"--gain {gain:g}")
    flags = f"--max-rate {rate / 1e6:g}" + (f" --gain {gain:g}" if gain is not None else "")
    rep(f"\n  pi/record.sh 2h --area {args.area} {flags}")

    out = Path(args.out or f"recordings/characterize_{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text("\n".join(rep.lines) + "\n")
    if rows:
        with open(out / "rf.csv", "w") as f:
            f.write("window,gain_db,mhz,name,floor_db\n")
            f.writelines(f"{w + 1},{g:g},{fr.mhz:.3f},{fr.name},{db:.2f}\n" for w, g, fr, db in rows)
    print(f"\nSaved {out}/report.txt" + (" and rf.csv" if rows else ""))
    if args.flags_file:
        Path(args.flags_file).write_text(flags + "\n")


if __name__ == "__main__":
    main()
