"""ADS-B (1090 MHz Mode S extended squitter): demodulate, decode, track.

ADS-B is the position / identity / velocity data aircraft broadcast for ATC
surveillance. 1090 MHz is inside a stock Pluto's 325 MHz - 3.8 GHz range.

Signal chain (Pluto / IQ file):
    IQ at 4 MSPS -> magnitude -> preamble search (pulses at 0, 1, 3.5, 4.5 us)
    -> 112 pulse-position bits -> CRC check (only DF17/18 with CRC 0 are kept)
    -> pyModeS PipeDecoder (CPR position pairing, callsign, altitude, velocity)
    -> per-aircraft state (Tracker)

Other inputs: a dump1090 / readsb raw feed (--network, port 30002), or a text
file of hex messages (--hex: plain, '*8D...;', or '<unix time> <hex>' lines).
"""

import csv
import queue
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

from .pluto import find_uri, open_pluto, to_complex

ADSB_FREQ = 1090e6
SAMP_RATE = 4e6
RTL_RATE = 2e6               # what an RTL-SDR streams reliably; the demodulator takes 2 MSPS
CHUNK = 1 << 20              # samples per buffer (~0.26 s at 4 MSPS)

# Mode S timing in half-microsecond "slots": an 8 us preamble (16 slots) with
# pulses in slots 0, 2, 7, 9, then 112 bits of 2 slots each. A bit is 1 when
# its first half carries the pulse.
PREAMBLE_LOW = (1, 3, 4, 5, 6, 8, 10, 11, 12, 13, 14, 15)
LONG_BITS = 112
MSG_SLOTS = 16 + 2 * LONG_BITS

# Position messages: airborne (9-18 baro, 20-22 GNSS) and surface (5-8).
POSITION_TC = set(range(5, 19)) | {20, 21, 22}
TRACK_SECONDS = 600          # history kept per aircraft for live display
CSV_FIELDS = ["time", "icao", "callsign", "lat", "lon", "altitude_ft",
              "groundspeed_kt", "track_deg", "vertical_rate_fpm"]


# ------------------------------------------------------------------ demodulator

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
        score = high[ok] / (low[ok] + 1e-9)

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


# ------------------------------------------------------------------ sources

def add_source_args(p):
    """Input options shared by adsb/adsb_plot.py and adsb/adsb_decode.py."""
    src = p.add_mutually_exclusive_group()
    src.add_argument("--device", default="", help="Pluto libiio URI (default: auto-detect)")
    src.add_argument("--rtl", nargs="?", const="rtl", metavar="rtl:N",
                     help="an RTL-SDR at 1090 MHz, 2 MSPS (rtl, or rtl:N for the Nth dongle)")
    src.add_argument("--network", metavar="HOST:PORT",
                     help="dump1090/readsb raw output, e.g. localhost:30002")
    src.add_argument("--iq", metavar="FILE", help="recorded IQ file centered on 1090 MHz")
    src.add_argument("--hex", metavar="FILE",
                     help="text file of hex messages (plain, *8D...; or '<unix time> <hex>')")
    p.add_argument("--iq-rate", type=float, default=SAMP_RATE,
                   help="sample rate of --iq (default 4e6)")
    p.add_argument("--iq-format", choices=("cs16", "cu8", "cf32"),
                   help="--iq sample format (default from extension; .cu8/.bin = RTL-SDR, else cs16)")
    p.add_argument("--realtime", action="store_true", help="replay --iq at its real speed")
    p.add_argument("--gain", default="50", help="Pluto RX gain in dB (0-73), or auto / fast")
    p.add_argument("--min-snr", type=float, default=2.5,
                   help="preamble pulse/quiet ratio (default 2.5)")
    p.add_argument("--record", metavar="FILE", help="save the raw Pluto IQ (cs16) for --iq replay")


def start_source(args, out, stop):
    """Start the input chosen in `args` on a thread that puts (unix time, hex)
    on `out`, then None when it ends. A Pluto is opened here, on the calling
    thread, so errors reach the user."""
    if getattr(args, "rtl", None):
        from .rtl import open_rtl
        gain = args.gain if args.gain != "50" else "40"     # 50 is the Pluto's default; R820T2 tops out ~50
        rtl = open_rtl(args.rtl, ADSB_FREQ, RTL_RATE, gain)
        print(f"RTL-SDR {args.rtl}: 1090 MHz, {RTL_RATE / 1e6:g} MSPS, gain {gain}")
        target = _iq_reader, (_pluto_chunks(rtl, args.record, stop), RTL_RATE, args.min_snr, False,
                              out, stop)
    elif args.network:
        target = _network_reader, (args, out, stop)
    elif args.hex:
        target = _hex_file_reader, (args, out, stop)
    elif args.iq:
        target = _iq_reader, (_iq_file_chunks(args, stop), args.iq_rate, args.min_snr, True,
                              out, stop)
    else:
        uri = find_uri(args.device)
        pluto = open_pluto(uri, ADSB_FREQ, SAMP_RATE, args.gain, bandwidth=3e6,
                           buffer_size=CHUNK)
        print(f"Pluto at {uri}: 1090 MHz, {SAMP_RATE / 1e6:g} MSPS, gain {args.gain}")
        target = _iq_reader, (_pluto_chunks(pluto, args.record, stop), SAMP_RATE,
                              args.min_snr, False, out, stop)
    thread = threading.Thread(target=target[0], args=target[1], daemon=True)
    thread.start()
    return thread


def _pluto_chunks(pluto, record, stop):
    rec = open(record, "wb") if record else None
    try:
        while not stop.is_set():
            error = None
            try:
                raw = pluto.read()
            except OSError as e:
                error = str(e)
            if error is None:
                if rec:
                    rec.write(raw.tobytes())
                yield to_complex(raw)
                continue
            # Reconnect outside the except block (see atcsdr.pluto.Pluto).
            print(f"USB error ({error}); reconnecting...")
            gone = pluto.reconnect(stop)
            if gone is None:
                return
            print(f"Reconnected after {gone:.1f} s.")
    finally:
        if rec:
            rec.close()


def _iq_file_chunks(args, stop):
    """Complex64 chunks from a recorded file (cs16, cu8 or cf32)."""
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
            yield raw.view(np.complex64)
            if args.realtime:
                time.sleep(len(raw) / 2 / args.iq_rate)


def _iq_reader(chunks, rate, min_snr, from_file, out, stop):
    demod = Demodulator(rate, min_snr)
    t0, n = time.time(), 0
    try:
        for chunk in chunks:
            # Live: wall clock. File: time from the sample count, so replay is exact.
            base = t0 + n / rate if from_file else time.time()
            for offset, msg in demod.process(chunk):
                out.put((base + offset / rate, msg))
            n += len(chunk)
    finally:
        out.put(None)


def parse_hex_line(line):
    """(unix time or None, hex) from one input line, or None if it isn't a message."""
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
        return None
    return ts, msg.upper()


def _hex_lines(lines, out, stop, live):
    t = time.time()
    for line in lines:
        if stop.is_set():
            break
        parsed = parse_hex_line(line)
        if parsed is None:
            continue
        ts, msg = parsed
        if ts is None:
            ts = time.time() if live else t
            t += 0.01
        out.put((ts, msg))


def _network_reader(args, out, stop):
    host, _, port = args.network.rpartition(":")
    while not stop.is_set():
        try:
            with socket.create_connection((host or "localhost", int(port)), timeout=10) as s:
                print(f"Connected to {args.network}")
                _hex_lines(s.makefile("r", errors="ignore"), out, stop, live=True)
        except OSError as e:
            print(f"{args.network}: {e}; retrying in 5 s")
            time.sleep(5)
    out.put(None)


def _hex_file_reader(args, out, stop):
    try:
        with open(args.hex) as f:
            _hex_lines(f, out, stop, live=False)
    finally:
        out.put(None)


# ------------------------------------------------------------------ tracking

@dataclass
class Aircraft:
    icao: str
    callsign: str = ""
    altitude: float | None = None
    speed: float | None = None
    track: float | None = None
    vrate: float | None = None
    lat: float | None = None
    lon: float | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    messages: int = 0
    path: deque = field(default_factory=lambda: deque(maxlen=2000))  # (t, lat, lon, alt)


class Tracker:
    """Decodes messages with pyModeS and keeps per-aircraft state. Optionally
    logs every position fix to CSV (the format adsb_plot / animate_tracks read)."""

    def __init__(self, ref=None, csv_path=None):
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
            self.csv[1].writerow(CSV_FIELDS)

    def add(self, t, msg):
        """Decode one message received at unix time t. Returns pyModeS's dict
        (with latitude / longitude filled in when known), or None if invalid."""
        try:
            d = dict(self.decoder.decode(msg, timestamp=t))
        except Exception:
            return None
        if not d.get("crc_valid") or not d.get("icao"):
            return None
        lat, lon = d.get("latitude"), d.get("longitude")
        # The pipe decoder waits for a few consistent even/odd pairs before it
        # trusts a new aircraft. With a receiver reference, decode right away.
        if lat is None and self.ref and d.get("typecode") in POSITION_TC:
            try:
                r = pms.decode(msg, reference=tuple(self.ref), surface_ref=tuple(self.ref))
                lat, lon = r.get("latitude"), r.get("longitude")
                d["latitude"], d["longitude"] = lat, lon
            except Exception:
                pass
        with self.lock:
            self.now = max(self.now, t)
            self.total += 1
            ac = self.aircraft.get(d["icao"])
            if ac is None:
                ac = self.aircraft[d["icao"]] = Aircraft(d["icao"], first_seen=t)
            ac.last_seen, ac.messages = t, ac.messages + 1
            if d.get("callsign"):
                ac.callsign = d["callsign"].strip("_ ")
            for key, attr in (("altitude", "altitude"), ("groundspeed", "speed"),
                              ("track", "track"), ("vertical_rate", "vrate")):
                if d.get(key) is not None:
                    setattr(ac, attr, d[key])
            if lat is not None and lon is not None:
                ac.lat, ac.lon = lat, lon
                ac.path.append((t, lat, lon, ac.altitude))
                if self.csv:
                    self.csv[1].writerow([f"{t:.3f}", ac.icao, ac.callsign, f"{lat:.5f}",
                                          f"{lon:.5f}", ac.altitude, ac.speed,
                                          None if ac.track is None else round(ac.track, 1),
                                          ac.vrate])
        return d

    def load_csv(self, path):
        """Load positions from a CSV log, so a past session can be re-plotted."""
        def num(v):
            return float(v) if v not in ("", "None") else None
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                t = float(row["time"])
                with self.lock:
                    self.now = max(self.now, t)
                    self.total += 1
                    ac = self.aircraft.get(row["icao"])
                    if ac is None:
                        ac = self.aircraft[row["icao"]] = Aircraft(row["icao"], first_seen=t)
                    ac.last_seen, ac.messages = t, ac.messages + 1
                    ac.callsign = row["callsign"] or ac.callsign
                    ac.altitude = num(row["altitude_ft"])
                    ac.speed = num(row["groundspeed_kt"]) or ac.speed
                    ac.track = num(row["track_deg"]) or ac.track
                    ac.vrate = num(row["vertical_rate_fpm"]) or ac.vrate
                    ac.lat, ac.lon = float(row["lat"]), float(row["lon"])
                    ac.path.append((t, ac.lat, ac.lon, ac.altitude))

    def snapshot(self, keep_all=False):
        """(now, total messages, [(icao, callsign, alt, speed, vrate, last_seen,
        path)]). keep_all: whole tracks rather than the last TRACK_SECONDS."""
        with self.lock:
            now = self.now
            return now, self.total, [
                (ac.icao, ac.callsign, ac.altitude, ac.speed, ac.vrate, ac.last_seen,
                 [p for p in ac.path if keep_all or now - p[0] < TRACK_SECONDS])
                for ac in self.aircraft.values()]

    def close(self):
        if self.csv:
            self.csv[0].close()


def consume(source_q, tracker, on_message=None):
    """Feed messages from `source_q` into `tracker` until the source ends.
    on_message(t, hex, decoded) is called for each valid message."""
    while (item := source_q.get()) is not None:
        d = tracker.add(*item)
        if d is not None and on_message:
            on_message(item[0], item[1], d)


def new_queue():
    return queue.Queue(maxsize=100000)
