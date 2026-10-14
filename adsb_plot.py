#!/usr/bin/env python3
"""
Receive ADS-B (1090 MHz), decode it and plot aircraft tracks on a live map.

ADS-B is the position / identity / velocity data aircraft broadcast for ATC
surveillance. 1090 MHz is inside a stock Pluto's 325 MHz - 3.8 GHz range, so no
firmware change is needed for this script.

Sources (pick one):
    python3 adsb_plot.py                          # Pluto, auto-detected
    python3 adsb_plot.py --device ip:192.168.2.1  # Pluto at a specific URI
    python3 adsb_plot.py --network localhost:30002   # dump1090 / readsb raw feed
    python3 adsb_plot.py --iq capture.cs16 --iq-rate 4e6   # recorded IQ file
    python3 adsb_plot.py --hex messages.txt       # one hex message per line
    python3 adsb_plot.py --track-csv tracks.csv   # re-plot a --csv log

Useful options:
    --ref 37.42 -122.17    receiver location: centers the map and lets single
                           position messages decode right away
    --csv tracks.csv       log every decoded position
    --save map.png         write the final map when the window closes
    --record out.cs16      save the raw Pluto IQ for replay with --iq

Signal chain (Pluto / --iq):
    IQ at 4 MSPS -> magnitude -> preamble search (pulses at 0, 1, 3.5, 4.5 us)
    -> 112 pulse-position bits -> CRC check (only DF17/18 with CRC 0 are kept)
    -> pyModeS PipeDecoder (CPR position pairing, callsign, altitude, velocity)
    -> per-aircraft state -> matplotlib map, refreshed once a second

Decoding uses pyModeS (pip install "pyModeS>=3"). For better sensitivity, run
readsb or PlutoSDR/dump1090 as the receiver and point --network at its raw port.
"""

import argparse
import csv
import math
import queue
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

try:
    import pyModeS as pms
    from pyModeS.util import crc
except ImportError:
    sys.exit('pyModeS not found. Install it with: pip install "pyModeS>=3"')

ADSB_FREQ = 1090e6
SAMP_RATE = 4e6
CHUNK = 1 << 20              # samples per Pluto buffer (~0.26 s at 4 MSPS)

# Mode S timing in half-microsecond "slots": an 8 us preamble (16 slots) with
# pulses in slots 0, 2, 7, 9, then 112 bits of 2 slots each. A bit is 1 when
# its first half carries the pulse.
PREAMBLE_HIGH = (0, 2, 7, 9)
PREAMBLE_LOW = (1, 3, 4, 5, 6, 8, 10, 11, 12, 13, 14, 15)
LONG_BITS = 112
MSG_SLOTS = 16 + 2 * LONG_BITS

# Position messages: airborne (9-18 baro, 20-22 GNSS) and surface (5-8).
POSITION_TC = set(range(5, 19)) | {20, 21, 22}

TRACK_SECONDS = 600          # how much history to draw per aircraft
STALE_SECONDS = 60           # aircraft not heard for this long are greyed out
GAP_SECONDS = 30             # fixes further apart than this are joined by a dotted line

# Sequential blue ramp (light = low altitude, dark = high), surface #fcfcfb.
ALT_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]
ALT_MAX_FT = 40000


# ---------------------------------------------------------------- demodulator

class Demodulator:
    """Finds and CRC-checks 112-bit Mode S frames in a stream of IQ samples."""

    def __init__(self, samp_rate, min_snr=2.5):
        sps = samp_rate / 1e6
        if abs(sps - round(sps)) > 1e-6 or round(sps) % 2:
            sys.exit("IQ sample rate must be an even number of MHz (2, 4, 6 ...).")
        self.half = int(round(sps)) // 2        # samples per half-microsecond slot
        self.min_snr = min_snr
        self.tail = np.zeros(0, dtype=np.float32)
        self.frames = 0

    def process(self, iq):
        """Return (offset, hex) for each valid frame; offset is in samples from
        the start of `iq` (negative for frames that began in the last chunk)."""
        mag = np.abs(iq).astype(np.float32)
        carried = len(self.tail)
        mag = np.concatenate([self.tail, mag])
        span = MSG_SLOTS * self.half
        # Keep one frame's worth of samples so frames across chunk edges decode.
        self.tail = mag[-span:].copy()
        if len(mag) <= span:
            return []

        h = self.half
        cs = np.concatenate([[0.0], np.cumsum(mag, dtype=np.float64)])
        n = len(mag) - span

        def slot(j, idx):
            a = idx + j * h
            return cs[a + h] - cs[a]

        # Cheap first pass: the first preamble pulse must stand above the noise.
        # Only those offsets get the full preamble test.
        s0 = cs[h:n + h] - cs[:n]
        noise = np.median(s0[::64]) + 1e-9
        idx = np.flatnonzero(s0 > 3 * noise)
        if not len(idx):
            return []
        s = [s0[idx]] + [slot(j, idx) for j in range(1, 16)]
        high = (s[0] + s[2] + s[7] + s[9]) / 4
        low = sum(s[j] for j in PREAMBLE_LOW) / len(PREAMBLE_LOW)
        ok = ((high > self.min_snr * low) &
              (s[0] > s[1]) & (s[2] > s[1]) & (s[2] > s[3]) &
              (s[7] > s[8]) & (s[9] > s[8]))
        cand = idx[ok]
        if not len(cand):
            return []
        high, low = high[ok], low[ok]
        score = high / (low + 1e-9)

        # Neighbouring offsets of one preamble all pass; try the best few of
        # each cluster until one gives a valid CRC.
        out = []
        breaks = np.flatnonzero(np.diff(cand) > h) + 1
        next_free = 0
        for group, gscore in zip(np.split(cand, breaks), np.split(score, breaks)):
            if group[0] < next_free:
                continue
            for i in group[np.argsort(-gscore)[:3]]:
                msg = self._bits_to_hex(cs, int(i))
                if msg is not None:
                    out.append((int(i) - carried, msg))
                    next_free = i + span
                    break
        self.frames += len(out)
        return out

    def _bits_to_hex(self, cs, i):
        h = self.half
        first = i + (16 + 2 * np.arange(LONG_BITS)) * h
        a = cs[first + h] - cs[first]
        b = cs[first + 2 * h] - cs[first + h]
        bits = (a > b).astype(np.uint8)
        df = int(np.packbits(bits[:5])[0]) >> 3
        if df not in (17, 18):
            return None
        msg = np.packbits(bits).tobytes().hex().upper()
        return msg if crc(msg) == 0 else None


# -------------------------------------------------------------------- sources

def find_pluto_uri():
    import iio
    for uri, desc in iio.scan_contexts().items():
        if "PlutoSDR" in desc or "ADALM-PLUTO" in desc or "ad936" in desc.lower():
            return uri
    return "ip:192.168.2.1"


def open_pluto(args, stop):
    """Configure the Pluto (on the calling thread, so errors reach the user)
    and return a generator of complex64 chunks."""
    try:
        import iio
    except ImportError:
        sys.exit("libiio Python bindings not found (conda install -c conda-forge libiio).")
    uri = args.device or find_pluto_uri()
    try:
        ctx = iio.Context(uri)
    except OSError as e:
        sys.exit(f"Could not open Pluto at {uri}: {e}\nTry `iio_info -s` to list devices.")
    phy = ctx.find_device("ad9361-phy")
    rx = ctx.find_device("cf-ad9361-lpc")
    phy.find_channel("altvoltage0", True).attrs["frequency"].value = str(int(ADSB_FREQ))
    ch = phy.find_channel("voltage0", False)
    ch.attrs["sampling_frequency"].value = str(int(SAMP_RATE))
    ch.attrs["rf_bandwidth"].value = str(int(3e6))
    if args.gain in ("auto", "fast"):
        ch.attrs["gain_control_mode"].value = "slow_attack" if args.gain == "auto" else "fast_attack"
    else:
        ch.attrs["gain_control_mode"].value = "manual"
        ch.attrs["hardwaregain"].value = str(float(args.gain))
    for name in ("voltage0", "voltage1"):
        rx.find_channel(name, False).enabled = True
    rx.set_kernel_buffers_count(8)
    buf = iio.Buffer(rx, CHUNK, False)
    print(f"Pluto at {uri}: 1090 MHz, {SAMP_RATE / 1e6:g} MSPS, gain {args.gain}")
    rec = open(args.record, "wb") if args.record else None

    def chunks():
        try:
            while not stop.is_set():
                buf.refill()
                raw = np.frombuffer(buf.read(), dtype=np.int16)
                if rec:
                    rec.write(raw.tobytes())
                yield (raw[0::2] + 1j * raw[1::2]).astype(np.complex64) / 2048.0
        finally:
            if rec:
                rec.close()

    return chunks()


def iq_file_source(args, stop):
    """Yield complex64 chunks from a recorded file (cs16, cu8 or cf32)."""
    fmt = args.iq_format or {"cu8": "cu8", "bin": "cu8", "cf32": "cf32",
                             "fc32": "cf32", "cfile": "cf32"}.get(args.iq.rsplit(".", 1)[-1], "cs16")
    dtype = {"cs16": np.int16, "cu8": np.uint8, "cf32": np.float32}[fmt]
    with open(args.iq, "rb") as f:
        while not stop.is_set():
            raw = np.frombuffer(f.read(CHUNK * 2 * np.dtype(dtype).itemsize), dtype=dtype)
            if len(raw) < 2:
                return
            raw = raw[: len(raw) // 2 * 2].astype(np.float32)
            if fmt == "cu8":
                raw = raw - 127.5
            yield (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
            if args.realtime:
                time.sleep(len(raw) / 2 / args.iq_rate)


def iq_reader(args, chunks, out, stop):
    rate = args.iq_rate if args.iq else SAMP_RATE
    demod = Demodulator(rate, args.min_snr)
    t0, n = time.time(), 0
    try:
        for chunk in chunks:
            # Live: wall clock. File: time from the sample count, so replay is exact.
            base = time.time() if not args.iq else t0 + n / rate
            for offset, msg in demod.process(chunk):
                out.put((base + offset / rate, msg))
            n += len(chunk)
    finally:
        out.put(None)


def hex_lines(lines, out, stop, live):
    """Parse dump1090 raw ('*8D...;'), plain hex, or '<unix time> <hex>' lines."""
    t = time.time()
    for line in lines:
        if stop.is_set():
            break
        parts = line.split()
        ts = None
        if len(parts) == 2:
            try:
                ts = float(parts[0])
            except ValueError:
                pass
            line = parts[1]
        msg = line.strip().strip("*;@").split(";")[0].strip()
        if len(msg) == 40 and line.lstrip().startswith("@"):   # '@' + 12 hex MLAT time
            msg = msg[12:]
        if len(msg) not in (14, 28):
            continue
        if ts is None:
            ts = time.time() if live else t
            t += 0.01
        out.put((ts, msg.upper()))


def network_reader(args, out, stop):
    host, _, port = args.network.rpartition(":")
    while not stop.is_set():
        try:
            with socket.create_connection((host or "localhost", int(port)), timeout=10) as s:
                print(f"Connected to {args.network}")
                hex_lines(s.makefile("r", errors="ignore"), out, stop, live=True)
        except OSError as e:
            print(f"{args.network}: {e}; retrying in 5 s")
            time.sleep(5)
    out.put(None)


def hex_file_reader(args, out, stop):
    try:
        with open(args.hex) as f:
            hex_lines(f, out, stop, live=False)
    finally:
        out.put(None)


# ------------------------------------------------------------------- tracking

@dataclass
class Aircraft:
    icao: str
    callsign: str = ""
    altitude: float | None = None
    speed: float | None = None
    track: float | None = None
    vrate: float | None = None
    last_seen: float = 0.0
    messages: int = 0
    path: deque = field(default_factory=lambda: deque(maxlen=2000))  # (t, lat, lon, alt)


class Tracker:
    def __init__(self, ref, csv_path):
        self.decoder = pms.PipeDecoder(surface_ref=tuple(ref) if ref else None)
        self.ref = ref
        self.aircraft: dict[str, Aircraft] = {}
        self.lock = threading.Lock()
        self.now = 0.0
        self.total = 0
        self.csv = None
        if csv_path:
            f = open(csv_path, "w", newline="", buffering=1)
            self.csv = (f, csv.writer(f))
            self.csv[1].writerow(["time", "icao", "callsign", "lat", "lon", "altitude_ft",
                                  "groundspeed_kt", "track_deg", "vertical_rate_fpm"])

    def add(self, t, msg):
        try:
            d = self.decoder.decode(msg, timestamp=t)
        except Exception:
            return
        if not d.get("crc_valid") or not d.get("icao"):
            return
        lat, lon = d.get("latitude"), d.get("longitude")
        # The pipe decoder waits for a few consistent even/odd pairs before it
        # trusts a new aircraft. With a receiver reference, decode right away.
        if lat is None and self.ref and d.get("typecode") in POSITION_TC:
            try:
                r = pms.decode(msg, reference=tuple(self.ref), surface_ref=tuple(self.ref))
                lat, lon = r.get("latitude"), r.get("longitude")
            except Exception:
                pass
        with self.lock:
            self.now = max(self.now, t)
            self.total += 1
            ac = self.aircraft.setdefault(d["icao"], Aircraft(d["icao"]))
            ac.last_seen, ac.messages = t, ac.messages + 1
            if d.get("callsign"):
                ac.callsign = d["callsign"].strip("_ ")
            for key, attr in (("altitude", "altitude"), ("groundspeed", "speed"),
                              ("track", "track"), ("vertical_rate", "vrate")):
                if d.get(key) is not None:
                    setattr(ac, attr, d[key])
            if lat is not None and lon is not None:
                ac.path.append((t, lat, lon, ac.altitude))
                if self.csv:
                    self.csv[1].writerow([f"{t:.3f}", ac.icao, ac.callsign, f"{lat:.5f}",
                                          f"{lon:.5f}", ac.altitude, ac.speed,
                                          None if ac.track is None else round(ac.track, 1),
                                          ac.vrate])

    def load_csv(self, path):
        """Load positions from a --csv log, so a past session can be re-plotted."""
        def num(v):
            return float(v) if v not in ("", "None") else None
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                t = float(row["time"])
                with self.lock:
                    self.now = max(self.now, t)
                    self.total += 1
                    ac = self.aircraft.setdefault(row["icao"], Aircraft(row["icao"]))
                    ac.last_seen, ac.messages = t, ac.messages + 1
                    ac.callsign = row["callsign"] or ac.callsign
                    ac.altitude = num(row["altitude_ft"])
                    ac.speed = num(row["groundspeed_kt"]) or ac.speed
                    ac.track = num(row["track_deg"]) or ac.track
                    ac.vrate = num(row["vertical_rate_fpm"]) or ac.vrate
                    ac.path.append((t, float(row["lat"]), float(row["lon"]), ac.altitude))

    def snapshot(self, keep_all=False):
        """keep_all: return whole tracks rather than the last TRACK_SECONDS."""
        with self.lock:
            now = self.now
            return now, self.total, [
                (ac.icao, ac.callsign, ac.altitude, ac.speed, ac.vrate, ac.last_seen,
                 [p for p in ac.path if keep_all or now - p[0] < TRACK_SECONDS])
                for ac in self.aircraft.values()]

    def close(self):
        if self.csv:
            self.csv[0].close()


# ----------------------------------------------------------------------- plot

def build_map(tracker, args):
    """Create the figure; return (fig, draw) where draw() redraws from tracker."""
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    cmap = LinearSegmentedColormap.from_list("alt", ALT_RAMP)
    norm = Normalize(0, ALT_MAX_FT)
    ink, muted, grid, surface = "#1f1f1e", "#6b6a66", "#e6e5e1", "#fcfcfb"

    fig = plt.figure(figsize=(12, 7.5), facecolor=surface)
    ax = fig.add_axes([0.06, 0.08, 0.62, 0.84], facecolor=surface)
    side = fig.add_axes([0.71, 0.08, 0.27, 0.84], facecolor=surface)
    side.axis("off")
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    cb = fig.colorbar(sm, ax=ax, orientation="horizontal", fraction=0.035, pad=0.08)
    cb.set_label("Altitude (ft)", color=muted)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=muted, labelsize=8)
    fig.canvas.manager.set_window_title("ADS-B tracks")

    def draw(_=None):
        # A re-plotted CSV is a finished session: show all of it, nothing greyed out.
        replay = args.track_csv is not None
        label_all = args.label_every == 0
        now, total, planes = tracker.snapshot(keep_all=replay)
        ax.clear()
        ax.set_facecolor(surface)
        ax.grid(True, color=grid, linewidth=0.8)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(colors=muted, labelsize=8)
        ax.ticklabel_format(useOffset=False)
        ax.set_xlabel("Longitude", color=muted)
        ax.set_ylabel("Latitude", color=muted)

        lats, lons = [], []
        if args.ref:
            ax.plot(args.ref[1], args.ref[0], marker="^", color=ink, markersize=9, zorder=5)
            lats.append(args.ref[0]); lons.append(args.ref[1])
        rows = []
        # Text is placed after the limits are known: (lon, lat, text, style) for
        # callsign tags (always shown) and fix timestamps (shown where they fit).
        tags, stamps = [], []
        for icao, cs, alt, spd, vr, seen, path in sorted(planes, key=lambda p: -p[5]):
            age = now - seen
            if age > TRACK_SECONDS and not replay:
                continue
            rows.append((cs or icao, alt, spd, age, seen))
            if not path:
                continue
            t, la, lo, al = zip(*path)
            lats += la; lons += lo
            stale = age > STALE_SECONDS and not replay
            color = "#b4b2ab" if stale else cmap(norm(alt or 0))
            if len(path) > 1:
                # One segment per fix, colored by the altitude at that fix. Where
                # no fixes were heard for a while, the join is thin and dotted.
                pts = np.column_stack([lo, la])
                segs = np.stack([pts[:-1], pts[1:]], axis=1)
                gap = np.diff(t) > GAP_SECONDS
                seg_alt = np.array([a or 0 for a in al[1:]], dtype=float)
                for mask, style in ((~gap, dict(linewidth=2, capstyle="round")),
                                    (gap, dict(linewidth=1, linestyle=(0, (1, 3)), alpha=0.8))):
                    if not mask.any():
                        continue
                    seg = LineCollection(segs[mask], **style)
                    if stale:
                        seg.set_color(color)
                    else:
                        seg.set_array(seg_alt[mask])
                        seg.set_cmap(cmap); seg.set_norm(norm)
                    ax.add_collection(seg)
            # A small dot at every fix, and its time on a thinned subset.
            dots = color if stale else cmap(norm(np.array([a or 0 for a in al], dtype=float)))
            # No outline: on dense tracks each outline would paint over the dot before it.
            ax.scatter(lo, la, s=14, c=dots if stale else list(dots), linewidths=0, zorder=3)
            # Newest first, so when stamps collide the recent ones win. When every
            # fix is labeled, alternate sides so neighbouring labels don't stack.
            for k in reversed(label_fixes(t, args.label_every)):
                side_style = "stamp_r" if label_all and k % 2 else "stamp"
                stamps.append((lo[k], la[k], clock(t[k], tenths=label_all), side_style, icao))
            ax.plot(lo[-1], la[-1], "o", color=color, markersize=8,
                    markeredgecolor=surface, markeredgewidth=2, zorder=4)
            label = cs or icao
            if alt is not None:
                label += f"\nFL{int(alt) // 100:03d}"
            tags.append((lo[-1], la[-1], label, "stale" if stale else "tag"))

        if lats:
            # Fit the tracks, with a margin of 15% (at least ~1 km).
            pad = max(0.15 * max(max(lats) - min(lats), max(lons) - min(lons)), 0.01)
            lat0 = np.mean([min(lats), max(lats)])
            ax.set_xlim(min(lons) - pad, max(lons) + pad)
            ax.set_ylim(min(lats) - pad, max(lats) + pad)
            ax.set_aspect(1 / max(math.cos(math.radians(lat0)), 0.1), adjustable="box")
        if args.ref:   # the receiver label yields to aircraft tags, like a timestamp
            stamps.insert(0, (args.ref[1], args.ref[0], "Receiver", "receiver", None))
        overlaps = place_labels(ax, tags, stamps, ink, muted, force=label_all)
        n_pos = sum(1 for p in planes if p[6])
        ax.set_title(f"{len(rows)} aircraft heard, {n_pos} with position  ·  "
                     f"{total} messages", color=ink, loc="left", fontsize=11)

        side.clear(); side.axis("off")
        last = "Last seen" if replay else "Age"
        side.annotate(f"{'Flight':<9}{'Alt ft':>7}{'Kt':>5}{last:>10}", (0, 1),
                      xycoords="axes fraction", family="monospace", fontsize=9, color=muted,
                      va="top")
        for k, (name, alt, spd, age, seen) in enumerate(rows[:40]):
            when = time.strftime("%H:%M:%S", time.localtime(seen)) if replay else f"{int(age)}s"
            line = (f"{name:<9.9}{'' if alt is None else int(alt):>7}"
                    f"{'' if spd is None else int(spd):>5}{when:>10}")
            side.annotate(line, (0, 1), xycoords="axes fraction", xytext=(0, -14 * (k + 1)),
                          textcoords="offset points", family="monospace", fontsize=9,
                          color=muted if age > STALE_SECONDS and not replay else ink, va="top")

        return overlaps

    return fig, draw


# Text style and its approximate box in points (dx, dy, width, height) relative
# to the point it labels, for overlap checks.
LABEL_STYLE = {
    "tag":      dict(xytext=(7, 4), fontsize=8, ha="left", va="bottom"),
    "stale":    dict(xytext=(7, 4), fontsize=8, ha="left", va="bottom"),
    "receiver": dict(xytext=(6, -12), fontsize=8, ha="left", va="bottom"),
    "stamp":    dict(xytext=(-6, 0), fontsize=7, ha="right", va="center"),
    "stamp_r":  dict(xytext=(6, 0), fontsize=7, ha="left", va="center"),
}


def clock(t, tenths=False):
    """Local time of day; with tenths, since fixes often come twice a second."""
    text = time.strftime("%H:%M:%S", time.localtime(t))
    return f"{text}.{int(t * 10) % 10}" if tenths else text


def dodge(ys, h):
    """Move positions `ys` apart until neighbours are at least `h` apart, each
    run of crowded labels staying centered on where its labels want to be."""
    clusters = []                          # [sum of wanted ys, count, member indices]
    for i in np.argsort(ys):
        clusters.append([ys[i], 1, [i]])
        while len(clusters) > 1:
            (sa, na, _), (sb, nb, _) = clusters[-2], clusters[-1]
            if (sb / nb - (nb - 1) * h / 2) - (sa / na + (na - 1) * h / 2) >= h:
                break
            b = clusters.pop()
            clusters[-1] = [sa + sb, na + nb, clusters[-1][2] + b[2]]
    out = np.empty(len(ys))
    for total, n, members in clusters:
        start = total / n - (n - 1) * h / 2
        for k, i in enumerate(members):
            out[i] = start + k * h
    return out


# Approximate glyph widths in em for the default font, for label boxes.
NARROW_GLYPHS = set(":.,;!|'iIl1 ")


def text_width_em(line):
    return sum(0.33 if c in NARROW_GLYPHS else 0.64 for c in line)


def place_labels(ax, tags, stamps, ink, muted, force=False):
    """Draw the callsign tags and the timestamps.

    Normally tags go first and a timestamp is drawn only where it doesn't overlap
    text already drawn. With force, every timestamp is drawn: they go first, in
    stacks beside their track with a leader line to their fix, and the tags then
    take a free corner. Returns how many labels overlap or leave the map."""
    ax.apply_aspect()                      # so data -> pixel mapping is final
    fig = ax.figure
    px = fig.dpi / 72
    W = fig.bbox.width
    bottom, top = ax.bbox.y0, ax.bbox.y1    # keep labels off the title and the colorbar
    placed = []

    def box(x, y, text, style, st=None):
        st = st or LABEL_STYLE[style]
        lines = text.split("\n")
        w = max(map(text_width_em, lines)) * st["fontsize"] * px
        h = len(lines) * st["fontsize"] * 1.2 * px
        cx, cy = ax.transData.transform((x, y)) + np.array(st["xytext"]) * px
        x0 = cx - w if st["ha"] == "right" else cx
        y0 = {"center": cy - h / 2, "top": cy - h}.get(st["va"], cy)
        return x0, y0, x0 + w, y0 + h

    def bad(a):
        outside = a[0] < 0 or a[2] > W or a[1] < bottom or a[3] > top
        return outside or any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                                 for b in placed)

    def draw(x, y, text, style, st=None, leader=False):
        st = st or LABEL_STYLE[style]
        color = ink if style in ("tag", "receiver") else muted
        arrow = dict(arrowprops=dict(arrowstyle="-", color=muted, lw=0.4, alpha=0.5,
                                     shrinkA=1, shrinkB=2)) if leader else {}
        ax.annotate(text, (x, y), textcoords="offset points", color=color,
                    annotation_clip=False, **arrow, **st)
        placed.append(box(x, y, text, style, st))

    clashes = 0
    if force:
        clashes += stack_stamps([s for s in stamps if s[3] != "receiver"], box, bad, draw, px)
    # Tags are always drawn; each takes the first corner around its dot that is free.
    for x, y, text, style in tags:
        base = LABEL_STYLE[style]
        corners = [base, dict(base, xytext=(7, -4), va="top"),
                   dict(base, xytext=(-7, 4), ha="right"),
                   dict(base, xytext=(-7, -4), ha="right", va="top")]
        st = next((c for c in corners if not bad(box(x, y, text, style, c))), None)
        clashes += st is None
        draw(x, y, text, style, st or base)
    for x, y, text, style, _ in stamps:
        if force and style != "receiver":
            continue
        if not bad(box(x, y, text, style)):
            draw(x, y, text, style)
        elif style != "receiver":
            clashes += 1
    return clashes


def stack_stamps(stamps, box, bad, draw, px):
    """Draw every timestamp in a column beside its track (one column per side),
    with a leader line to its fix. The column sits just outside the track, so
    leader lines never cross a label. Labels slide up or down just enough that
    none overlap; columns whose x ranges overlap are spaced as one stack."""
    reach = 16                             # points from the outermost fix to the column
    columns = {}
    for x, y, text, style, group in stamps:
        columns.setdefault((group, style), []).append((x, y, text, style))
    stacks = []                            # [x0, x1, items]
    for (_, style), fixes in columns.items():
        right = style == "stamp_r"
        boxes = [box(x, y, text, style) for x, y, text, style in fixes]
        dots_x = [b[0] - LABEL_STYLE[style]["xytext"][0] * px if right else
                  b[2] - LABEL_STYLE[style]["xytext"][0] * px for b in boxes]
        col_x = max(dots_x) + reach * px if right else min(dots_x) - reach * px
        items = []
        for (x, y, text, style), b, dx in zip(fixes, boxes, dots_x):
            st = dict(LABEL_STYLE[style], xytext=((col_x - dx) / px, 0))
            items.append(((b[1] + b[3]) / 2, b[3] - b[1], x, y, text, style, st))
        stacks.append([col_x if right else col_x - max(b[2] - b[0] for b in boxes),
                       col_x + max(b[2] - b[0] for b in boxes) if right else col_x, items])
    # Merge stacks that share horizontal space.
    stacks.sort(key=lambda st: st[0])
    merged = []
    for st in stacks:
        if merged and st[0] < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], st[1])
            merged[-1][2] += st[2]
        else:
            merged.append(st)
    clashes = 0
    for _, _, items in merged:
        h = max(it[1] for it in items) * 1.1    # a little air between rows
        ys = dodge(np.array([it[0] for it in items]), h)
        for (cy, _, x, y, text, style, st), ly in zip(items, ys):
            st = dict(st, xytext=(st["xytext"][0], (ly - cy) / px))
            clashes += bad(box(x, y, text, style, st))
            draw(x, y, text, style, st, leader=True)
    return clashes


def label_fixes(times, every):
    """Indices of fixes to timestamp: one per `every` seconds (0 = all of them),
    or about ten per track when `every` is None. The newest fix is always included."""
    if every is None:
        every = (times[-1] - times[0]) / 9
    picked, last = [], -math.inf
    for k, t in enumerate(times):
        if t - last >= every:
            picked.append(k)
            last = t
    if picked[-1] != len(times) - 1:
        picked.append(len(times) - 1)
    return picked


SAVE_DPI = 150
MAX_SAVE_PX = 12000


def save_map(fig, draw, path, fit_labels=False):
    """Save the map. fit_labels: enlarge the image (text keeps its size) until
    no timestamp overlaps another label, up to MAX_SAVE_PX on the long side."""
    clashes = draw()
    if fit_labels:
        w, h = fig.get_size_inches()
        while clashes and max(w, h) * 1.25 * SAVE_DPI <= MAX_SAVE_PX:
            w, h = w * 1.25, h * 1.25
            fig.set_size_inches(w, h)
            clashes = draw()
        if clashes:
            print(f"{clashes} timestamps still overlap at {w * SAVE_DPI:.0f} x "
                  f"{h * SAVE_DPI:.0f} px; zoom in on the image to read them.")
    fig.savefig(path, dpi=SAVE_DPI, facecolor=fig.get_facecolor())
    print(f"Saved {path} ({fig.get_size_inches()[0] * SAVE_DPI:.0f} x "
          f"{fig.get_size_inches()[1] * SAVE_DPI:.0f} px)")


def run_plot(tracker, args):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    fig, draw = build_map(tracker, args)
    anim = FuncAnimation(fig, draw, interval=1000, cache_frame_data=False)
    draw()
    plt.show()
    if args.save:
        save_map(fig, draw, args.save, args.label_every == 0)
    del anim


# ----------------------------------------------------------------------- main

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--device", default="", help="Pluto libiio URI (default: auto-detect)")
    src.add_argument("--network", metavar="HOST:PORT", help="dump1090/readsb raw output, e.g. localhost:30002")
    src.add_argument("--iq", metavar="FILE", help="recorded IQ file centered on 1090 MHz")
    src.add_argument("--hex", metavar="FILE", help="text file of hex messages")
    src.add_argument("--track-csv", metavar="FILE", help="re-plot positions from a --csv log")
    p.add_argument("--iq-rate", type=float, default=SAMP_RATE, help="sample rate of --iq (default 4e6)")
    p.add_argument("--iq-format", choices=("cs16", "cu8", "cf32"),
                   help="--iq sample format (default from extension; .cu8/.bin = RTL-SDR, else cs16)")
    p.add_argument("--realtime", action="store_true", help="replay --iq at its real speed")
    p.add_argument("--gain", default="50", help="Pluto RX gain in dB (0-73), or auto / fast")
    p.add_argument("--min-snr", type=float, default=2.5, help="preamble pulse/quiet ratio (default 2.5)")
    p.add_argument("--ref", nargs=2, type=float, metavar=("LAT", "LON"), help="receiver position")
    p.add_argument("--record", metavar="FILE", help="save the raw Pluto IQ (cs16) for --iq replay")
    p.add_argument("--csv", metavar="FILE", help="log decoded positions to CSV")
    p.add_argument("--save", metavar="PNG", help="save the map when the window closes")
    p.add_argument("--label-every", type=float, metavar="SECONDS",
                   help="timestamp a fix every SECONDS along each track; 0 = every fix, with "
                        "--save enlarging the image until they fit (default: about ten per track)")
    p.add_argument("--no-plot", action="store_true",
                   help="decode and log only, no window (--save still writes the map at the end)")
    return p.parse_args()


def _terminate(*_):
    raise KeyboardInterrupt


def main():
    args = parse_args()
    # `kill` stops the script like Ctrl-C does, so the CSV and --save map are written.
    signal.signal(signal.SIGTERM, _terminate)
    tracker = Tracker(args.ref, args.csv)
    msgs: queue.Queue = queue.Queue(maxsize=100000)
    stop, done = threading.Event(), threading.Event()

    if args.network:
        reader = threading.Thread(target=network_reader, args=(args, msgs, stop), daemon=True)
    elif args.track_csv:
        tracker.load_csv(args.track_csv)
        reader = threading.Thread(target=msgs.put, args=(None,), daemon=True)
    elif args.hex:
        reader = threading.Thread(target=hex_file_reader, args=(args, msgs, stop), daemon=True)
    else:
        chunks = iq_file_source(args, stop) if args.iq else open_pluto(args, stop)
        reader = threading.Thread(target=iq_reader, args=(args, chunks, msgs, stop), daemon=True)

    def consume():
        while True:
            item = msgs.get()
            if item is None:
                break
            tracker.add(*item)
        done.set()

    reader.start()
    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    try:
        if args.no_plot:
            while not done.wait(5):
                _, total, planes = tracker.snapshot()
                print(f"{total} messages, {len(planes)} aircraft, "
                      f"{sum(1 for p in planes if p[6])} with position", flush=True)
        else:
            run_plot(tracker, args)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if done.is_set() or args.hex or args.iq:
            consumer.join(timeout=2)
        _, total, planes = tracker.snapshot()
        print(f"Done: {total} messages from {len(planes)} aircraft.")
        if args.save and args.no_plot:
            import matplotlib
            matplotlib.use("Agg")
            save_map(*build_map(tracker, args), args.save, args.label_every == 0)
        tracker.close()


if __name__ == "__main__":
    main()
