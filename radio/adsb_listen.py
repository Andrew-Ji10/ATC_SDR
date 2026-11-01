#!/usr/bin/env python3
"""
ADS-B listen test: decode and track aircraft from the RTL-SDR for a set time.

  RTL-SDR at 1090 MHz, 2 MSPS -> Mode S demodulator -> pyModeS -> tracks,
  pinned to its own core (default core 2)

    python3 radio/adsb_listen.py --duration 30m --ref 37.43 -122.17
    python3 radio/adsb_listen.py --source synthetic --duration 2m     # no dongle needed

Saves, in recordings/adsb_<UTC time>/: messages.txt.gz (every message as
'<unix time> <hex>', for decoding again with adsb_decode.py --hex), positions.csv
(every position fix), aircraft.csv (one row per aircraft), health.csv (every
second: % of samples received, decoder load, backlog, drops, messages/s,
aircraft in view, CPU temperature) and summary.txt (uptime, traffic, storage
per day / week / month). --ref (your latitude and longitude) lets positions
decode from the first message.
"""

import argparse
import gzip
import os
import queue
import shutil
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atcsdr import adsb
from atcsdr.airband import parse_duration
from atcsdr.pluto import to_complex

RATE = adsb.RTL_RATE
UP_STREAM = 0.98             # a second counts as up if this much of it arrived (and nothing dropped)
WARMUP_S = 2.0
UP_WINDOW_S = 5.0          # judged over this long: a second holds a whole number of chunks
IN_VIEW_S = 60               # "in view" = heard in the last minute
TTY = sys.stdout.isatty()


def cores_arg(text):
    return sorted({int(c) for c in text.split(",") if c.strip()})


def pin(cores):
    if cores and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cores)


def utc(t, ms=True, files=False):
    d = datetime.fromtimestamp(t, timezone.utc)
    s = d.strftime("%Y-%m-%dT%H-%M-%S" if files else "%Y-%m-%dT%H:%M:%S")
    return s + (f".{d.microsecond // 1000:03d}" if ms else "") + "Z"


def temp_c():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        return None


def _terminate(*_):
    raise KeyboardInterrupt


class SyntheticSource:
    """Mode S frames from simulated flights, at real-time pace (no dongle)."""

    def __init__(self, msgs_per_s):
        sys.path.insert(0, str(ROOT / "tests"))
        import synth
        msgs = [m for k in range(20) for _, m in synth.flight(
            0xA00000 + k, f"TST{k:03d}", 37.4 + 0.02 * k, -122.3, 3000 + 500 * k, 18 * k, 250, 0, seconds=30)]
        seconds = 4.0
        iq = synth.mode_s_iq(RATE, msgs, int(msgs_per_s * seconds), seconds)
        self.pool = np.empty(2 * len(iq), np.int16)
        self.pool[0::2] = np.clip(iq.real * 2048, -2047, 2047)
        self.pool[1::2] = np.clip(iq.imag * 2048, -2047, 2047)
        self.n, self.k, self.due = 1 << 18, 0, time.time()

    def read(self):
        self.due += self.n / RATE
        time.sleep(max(0.0, self.due - time.time()))
        span = 2 * self.n
        start = (self.k * span) % (len(self.pool) - span)
        self.k += 1
        return self.pool[start: start + span].copy()

    def close(self):
        pass

    def reconnect(self, stop):
        return 0.0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["rtl", "synthetic"], default="rtl", help="default rtl")
    p.add_argument("--rtl", default="rtl", help="which dongle: rtl, or rtl:N (default rtl)")
    p.add_argument("--gain", default="40", help="RTL-SDR gain, dB (default 40)")
    p.add_argument("--ref", type=float, nargs=2, metavar=("LAT", "LON"),
                   help="your position, so positions decode from the first message")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("10m"),
                   help="how long: 90s, 30m, 8h, 1d (default 10m); 0 = until Ctrl-C")
    p.add_argument("--msgs", type=float, default=300, help="synthetic frames a second (default 300)")
    p.add_argument("--cores", type=cores_arg, default=[2], help="the decoder's core(s) (default 2)")
    p.add_argument("--out", help="output folder (default recordings/adsb_<UTC time>)")
    args = p.parse_args()
    pin(args.cores)
    signal.signal(signal.SIGTERM, _terminate)

    out = Path(args.out or ROOT / f"recordings/adsb_{utc(time.time(), ms=False, files=True)}")
    out.mkdir(parents=True, exist_ok=True)
    if args.source == "rtl":
        from atcsdr.rtl import RtlSdr
        try:
            radio = RtlSdr(args.rtl, adsb.ADSB_FREQ, RATE, args.gain)
        except OSError as e:
            sys.exit(f"Could not open the RTL-SDR ({args.rtl}): {e}")
    else:
        radio = SyntheticSource(args.msgs)
    print(f"ADS-B listen test: {args.source} at 1090 MHz, {RATE / 1e6:g} MSPS, on core(s) {args.cores}; "
          + (f"for {args.duration / 60:g} min." if args.duration else "until Ctrl-C.")
          + f"\nSaving to {out}/", flush=True)

    demod = adsb.Demodulator(RATE)
    tracker = adsb.Tracker(ref=args.ref, csv_path=str(out / "positions.csv"))
    messages = open(out / "messages.txt", "w", buffering=1 << 16)
    health = open(out / "health.csv", "w", buffering=1)
    health.write("time_utc,stream_pct,decoder_busy,backlog,dropped,msgs_per_s,aircraft_in_view,cpu_c\n")
    events = open(out / "events.csv", "w", buffering=1)
    events.write("time,event\n")

    def event(text):
        events.write(f"{utc(time.time())},\"{text}\"\n")
        print(("\r\033[K" if TTY else "") + f"{utc(time.time(), ms=False)}  {text}", flush=True)

    stop = threading.Event()
    raw_q = queue.Queue(maxsize=32)                    # ~4 s
    stats = dict(received=0, dropped=0, reconnects=0, down_s=0.0, busy=0.0, msgs=0)

    def reader():
        while not stop.is_set():
            error = None
            try:
                raw = radio.read()
            except OSError as e:
                error = str(e)
            if error is None:
                stats["received"] += len(raw) // 2
                try:
                    raw_q.put_nowait((raw, time.time()))
                except queue.Full:
                    stats["dropped"] += 1
                continue
            event(f"radio: {error}; reconnecting")
            gone = radio.reconnect(stop)
            if gone is None:
                break
            stats["reconnects"] += 1
            stats["down_s"] += gone
            event(f"radio back after {gone:.1f} s")
        raw_q.put(None)

    seconds, up_seconds, temps, worst = [0], [0], [], [0.0]
    last = dict(received=0, dropped=0, busy=0.0, msgs=0, t=time.time())
    window = deque(maxlen=int(UP_WINDOW_S) + 1)          # (time, samples received so far)

    def monitor():
        while not stop.wait(1.0):
            now = time.time()
            dt = now - last["t"]
            got = (stats["received"] - last["received"]) / (RATE * dt)
            busy = (stats["busy"] - last["busy"]) / dt
            dropped = stats["dropped"] - last["dropped"]
            rate = (stats["msgs"] - last["msgs"]) / dt
            with tracker.lock:
                in_view = sum(1 for ac in tracker.aircraft.values() if now - ac.last_seen < IN_VIEW_S)
            t = temp_c()
            if t:
                temps.append(t)
            window.append((now, stats["received"]))
            got_w = (window[-1][1] - window[0][1]) / (RATE * max(window[-1][0] - window[0][0], 1e-9))
            if now - started > WARMUP_S + UP_WINDOW_S:
                seconds[0] += 1
                if got_w >= UP_STREAM and dropped == 0:
                    up_seconds[0] += 1
            worst[0] = max(worst[0], busy)
            health.write(f"{utc(now)},{100 * got:.1f},{busy:.2f},{raw_q.qsize()},{dropped},{rate:.0f},"
                         f"{in_view},{'' if t is None else t}\n")
            last.update(received=stats["received"], dropped=stats["dropped"], busy=stats["busy"],
                        msgs=stats["msgs"], t=now)
            if TTY:
                up = f"{100 * up_seconds[0] / seconds[0]:5.1f}%" if seconds[0] else "starting"
                print(f"\r\033[K  {seconds[0]:5d} s  stream {100 * got:5.1f}%  decoder {busy:4.2f}  up {up}  "
                      f"{rate:4.0f} msg/s  {in_view} aircraft" + (f"  {t:.0f} C" if t else ""),
                      end="", flush=True)

    started = time.time()
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()

    t_end = None
    try:
        while not (args.duration and time.time() - started >= args.duration):
            try:
                item = raw_q.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                break
            raw, arrived = item
            n = len(raw) // 2
            t_end = arrived if t_end is None or abs(t_end + n / RATE - arrived) > 0.25 else t_end + n / RATE
            t_start = t_end - n / RATE
            t = time.perf_counter()
            for offset, msg in demod.process(to_complex(raw)):
                when = t_start + offset / RATE
                tracker.add(when, msg)
                messages.write(f"{when:.6f} {msg}\n")
                stats["msgs"] += 1
            stats["busy"] += time.perf_counter() - t
    except KeyboardInterrupt:
        pass
    stop.set()
    elapsed = time.time() - started
    radio.close()
    tracker.close()
    messages.close()
    with open(out / "messages.txt", "rb") as f, gzip.open(out / "messages.txt.gz", "wb") as g:
        shutil.copyfileobj(f, g)
    (out / "messages.txt").unlink()
    _, total, rows = tracker.snapshot(keep_all=True)
    with open(out / "aircraft.csv", "w") as f:
        f.write("icao,callsign,altitude_ft,speed_kt,last_seen_utc,positions\n")
        for icao, callsign, alt, speed, _vr, seen, path in rows:
            f.write(f"{icao},{callsign or ''},{alt or ''},{speed or ''},{utc(seen) if seen else ''},{len(path)}\n")

    mb = sum(f.stat().st_size for f in out.glob("*")) / 1e6
    per_day = mb / max(elapsed / 3600, 1e-9) / 1e3 * 24
    with_pos = sum(1 for r in rows if r[6])
    lines = [f"ADS-B listen test {utc(started, ms=False)}, {elapsed / 60:.1f} min, {args.source}",
             f"uptime:   {100 * up_seconds[0] / max(seconds[0], 1):.2f}% of {seconds[0]} s fully received "
             f"and kept up with",
             f"stream:   {100 * stats['received'] / (RATE * elapsed):.2f}% of the samples received; "
             f"{stats['dropped']} chunks dropped, {stats['reconnects']} reconnect(s) ({stats['down_s']:.0f} s down)",
             f"decoder:  {stats['busy'] / elapsed:.2f} of real time on average, {worst[0]:.2f} in the busiest "
             f"second (1.00 = falling behind)",
             f"traffic:  {stats['msgs']} messages ({stats['msgs'] / elapsed:.0f}/s), {len(rows)} aircraft, "
             f"{with_pos} with positions",
             f"storage:  {mb:.1f} MB = {per_day:.2f} GB/day, {per_day * 7:.1f} GB/week, {per_day * 30:.0f} GB/month "
             "at this traffic"]
    if temps:
        lines.append(f"Pi:       {temps[0]:.0f} C at start, peak {max(temps):.0f} C")
    report = "\n".join(lines)
    print("\n\n" + report)
    (out / "summary.txt").write_text(report + "\n")
    health.close()
    events.close()


if __name__ == "__main__":
    main()
