#!/usr/bin/env python3
"""
ATC listen test: the full stack, the whole band at once, for as long as you like.

  radio (iqstream, 20.0 MSPS, all 43 Bay channels in one window)
  -> compiled polyphase channelizer + AM demodulation on cores 0-1
  -> a squelch per channel -> each transmission saved as a plain and a cleaned
     MP3 (speech quality, UTC-stamped) by a worker on core 3

    python3 radio/atc_listen.py --device packed:192.168.3.2 --duration 30m
    python3 radio/atc_listen.py --device packed:192.168.3.2 --duration 8h --threshold 10

Every second it logs to health.csv: the % of samples received, how busy the
receiver was (1.0 = all of real time), its backlog, chunks lost, reconnects,
clips waiting and the CPU temperature. The summary gives the uptime (seconds
fully received and kept up with), per-channel traffic, and the storage used,
projected to a week and a month. Output, in recordings/listen_<UTC time>/:
clips/, clips_clean/, transmissions.csv, health.csv, summary.txt.
"""

import argparse
import multiprocessing as mp
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atcsdr import airband as A
from atcsdr import frequencies as fq
from atcsdr import pluto
from atcsdr.airband import parse_duration

UP_STREAM = 0.98             # a second counts as up if this much of it arrived (and nothing dropped)
WARMUP_S = 2.0               # not counted: the stream starting up
TTY = sys.stdout.isatty()


def cores_arg(text):
    return sorted({int(c) for c in text.split(",") if c.strip()})


def pin(cores):
    if cores and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cores)


def utc(t, ms=True, files=False):
    """Unix time -> 2026-11-01T03:12:05.250Z (files: 2026-11-01T03-12-05.250Z)."""
    d = datetime.fromtimestamp(t, timezone.utc)
    s = d.strftime("%Y-%m-%dT%H-%M-%S" if files else "%Y-%m-%dT%H:%M:%S")
    return s + (f".{d.microsecond // 1000:03d}" if ms else "") + "Z"


def temp_c():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        return None


def throttled():
    if not shutil.which("vcgencmd"):
        return None
    out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout
    return int(out.split("=")[-1], 16) if "=" in out else None


def _terminate(*_):
    raise KeyboardInterrupt


# ------------------------------------------------------------------ clips (own process)

def clip_worker(out, clean, cores, q, results):
    """Resample to 16 kHz, write the plain MP3 and the cleaned MP3; on its own core."""
    pin(cores)
    signal.signal(signal.SIGINT, signal.SIG_IGN)              # the parent stops us with None
    from scipy.signal import resample_poly
    from atcsdr import audio as au

    lame = au.find_lame()
    out = Path(out)
    t0, cpu0, n, spent = time.time(), time.process_time(), 0, 0.0
    while (item := q.get()) is not None:
        start, mhz, name, audio = item
        t = time.process_time()
        x = resample_poly(audio, 16, 25).astype(np.float32)   # 25 kHz -> 16 kHz (AM voice is < 3 kHz)
        stem = f"{utc(start, files=True)}_{mhz:.3f}MHz_{name.replace(' ', '_')}"
        title, comment = f"{name} {mhz:.3f} MHz", utc(start)
        au.save_mp3(au.normalize(au.band_pass(x, 16_000)), str(out / "clips" / f"{stem}.mp3"),
                    16_000, lame, title, comment)
        if clean:
            au.save_mp3(au.clean(x, 16_000), str(out / "clips_clean" / f"{stem}.mp3"),
                        16_000, lame, title + " (cleaned)", comment)
        spent += time.process_time() - t
        n += 1
    wall = time.time() - t0
    results.put(dict(clips=n, per_clip=spent / max(n, 1), cpu=(time.process_time() - cpu0) / max(wall, 1e-9),
                     mp3=bool(lame)))


# ------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="", help="the radio, e.g. packed:192.168.3.2 (iqstream)")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("10m"),
                   help="how long: 90s, 30m, 8h, 1d (default 10m); 0 = until Ctrl-C")
    p.add_argument("--rate", type=float, default=20.0, help="MSPS (default 20.0: all 43 channels)")
    p.add_argument("--area", choices=sorted(fq.AREAS), default="bay", help="channel set (default bay)")
    p.add_argument("--gain", default="40", help="RF gain, dB (default 40)")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above a channel's noise floor that opens its squelch (default 8)")
    p.add_argument("--dsp-cores", type=cores_arg, default=[0, 1], help="receiver cores (default 0,1)")
    p.add_argument("--clip-cores", type=cores_arg, default=[3], help="MP3 / cleaning core (default 3)")
    p.add_argument("--no-clean", action="store_true", help="plain MP3s only")
    p.add_argument("--out", help="output folder (default recordings/listen_<UTC time>)")
    args = p.parse_args()
    pin(args.dsp_cores)                                   # numba's threads inherit this
    signal.signal(signal.SIGTERM, _terminate)

    from atcsdr.fastrx import Receiver                   # after pinning: numba starts its threads here

    if not args.no_clean:
        try:
            import noisereduce  # noqa: F401  (the clip worker needs it for the cleaned copies)
        except ImportError:
            sys.exit("noisereduce isn't installed (pip install noisereduce, or bash pi/setup.sh); "
                     "or run with --no-clean.")

    rate = int(round(args.rate * 1e6))
    freqs = fq.area_channels(args.area)
    # Compile the kernels now, not mid-stream, on a throwaway receiver (its squelches
    # would otherwise learn a noise floor from the zeros).
    Receiver(rate, freqs, args.threshold, threads=len(args.dsp_cores)).process(
        np.zeros(2 * (1 << 16), np.int16), time.time())
    rx = Receiver(rate, freqs, args.threshold, threads=len(args.dsp_cores))
    uri = pluto.find_uri(args.device)
    out = Path(args.out or ROOT / f"recordings/listen_{utc(time.time(), ms=False, files=True)}")
    for d in ("clips", "clips_clean"):
        (out / d).mkdir(parents=True, exist_ok=True)
    print(f"ATC listen test: {len(freqs)} channels, {freqs[0].mhz:.3f}-{freqs[-1].mhz:.3f} MHz, one window "
          f"at {rate / 1e6:g} MSPS centred on {rx.center / 1e6:.3f} MHz (outer channels at "
          f"{100 * rx.edge:.1f}% of the band); radio {uri}; receiver on cores {args.dsp_cores}, clips on "
          f"{args.clip_cores}; " + (f"for {args.duration / 60:g} min." if args.duration else "until Ctrl-C.")
          + f"\nSaving to {out}/", flush=True)

    ctx = mp.get_context("spawn")
    clip_q, results = ctx.Queue(), ctx.Queue()
    worker = ctx.Process(target=clip_worker, args=(str(out), not args.no_clean, args.clip_cores, clip_q, results))
    worker.start()

    try:
        radio = pluto.make(uri, rx.center, rate, args.gain)
    except OSError as e:
        clip_q.put(None)
        sys.exit(f"Could not open the radio at {uri}: {e}")

    stop = threading.Event()
    raw_q = queue.Queue(maxsize=64)
    stats = dict(received=0, dropped=0, reconnects=0, down_s=0.0, busy=0.0, gaps=0)
    events = open(out / "events.csv", "w", buffering=1)
    events.write("time,event\n")

    def event(text):
        events.write(f"{utc(time.time())},\"{text}\"\n")
        print(("\r\033[K" if TTY else "") + f"{utc(time.time(), ms=False)}  {text}", flush=True)

    def reader():
        """Only reads the radio, so a slow moment in the receiver never stalls it."""
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
                    raw_q.put(("gap", time.time()))  # blocks only until the receiver catches up
                continue
            event(f"radio: {error}; reconnecting")
            gone = radio.reconnect(stop)
            if gone is None:
                break
            stats["reconnects"] += 1
            stats["down_s"] += gone
            event(f"radio back after {gone:.1f} s")
            raw_q.put(("gap", time.time()))
        raw_q.put(None)

    log = open(out / "transmissions.csv", "w", buffering=1)
    log.write("time_utc,mhz,name,kind,seconds,peak_db,file\n")
    health = open(out / "health.csv", "w", buffering=1)
    health.write("time_utc,stream_pct,receiver_busy,backlog,dropped,lost_chunks,reconnects,clips_waiting,cpu_c\n")

    def handle(tx):
        ch = tx.channel
        if tx.seconds < A.MIN_CLIP_S:
            ch.blips += 1
            return
        ch.transmissions += 1
        ch.talk_s += tx.seconds
        ch.best_db = max(ch.best_db or tx.peak_db, tx.peak_db)
        stem = f"{utc(tx.start, files=True)}_{ch.mhz:.3f}MHz_{ch.name.replace(' ', '_')}.mp3"
        log.write(f"{utc(tx.start)},{ch.mhz:.3f},{ch.name},{ch.kind},{tx.seconds:.1f},{tx.peak_db:.1f},{stem}\n")
        clip_q.put((tx.start, ch.mhz, ch.name, tx.audio))
        print(("\r\033[K" if TTY else "") + f"{utc(tx.start, ms=False)}  {ch.name:<11} {ch.mhz:8.3f}  "
              f"{tx.seconds:5.1f} s  {tx.peak_db:+5.1f} dB", flush=True)

    # Health, once a second.
    seconds, up_seconds, temps, worst_busy = [0], [0], [], [0.0]
    last = dict(received=0, dropped=0, busy=0.0, lost=0, t=time.time())

    def monitor():
        while not stop.wait(1.0):
            now = time.time()
            dt = now - last["t"]
            got = (stats["received"] - last["received"]) / (rate * dt)
            busy = (stats["busy"] - last["busy"]) / dt
            dropped = stats["dropped"] - last["dropped"]
            lost = getattr(radio, "stats", {}).get("lost", 0)
            new_lost = lost - last["lost"]
            try:
                waiting = clip_q.qsize()
            except NotImplementedError:
                waiting = -1
            t = temp_c()
            if t:
                temps.append(t)
            if now - started > WARMUP_S:                  # the stream's first seconds are start-up
                seconds[0] += 1
                if got >= UP_STREAM and dropped == 0 and new_lost == 0:
                    up_seconds[0] += 1
            worst_busy[0] = max(worst_busy[0], busy)
            health.write(f"{utc(now)},{100 * got:.1f},{busy:.2f},{raw_q.qsize()},{dropped},{new_lost},"
                         f"{stats['reconnects']},{waiting},{'' if t is None else t}\n")
            last.update(received=stats["received"], dropped=stats["dropped"], busy=stats["busy"], lost=lost, t=now)
            if TTY:
                talking = [sq.name for sq in rx.squelches if sq.open]
                up = f"{100 * up_seconds[0] / seconds[0]:5.1f}%" if seconds[0] else "starting"
                print(f"\r\033[K  {seconds[0]:5d} s  stream {100 * got:5.1f}%  receiver {busy:4.2f}  "
                      f"up {up}  clips waiting {waiting}  "
                      + (f"{t:.0f} C  " if t else "") + (", ".join(talking[:4]) or "quiet"), end="", flush=True)

    started, thr0 = time.time(), throttled()
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()

    # The receiver: sample-count time, re-anchored to the clock if it drifts.
    t_end = None
    try:
        while not (args.duration and time.time() - started >= args.duration):
            try:
                item = raw_q.get(timeout=1)              # wakes up even if the radio goes quiet
            except queue.Empty:
                continue
            if item is None:
                break
            raw, arrived = item
            if isinstance(raw, str):                   # a gap: dropped chunks or a reconnect
                stats["gaps"] += 1
                for tx in rx.gap():
                    handle(tx)
                t_end = None
                continue
            span = len(raw) / 2 / rate
            t_end = arrived if t_end is None or abs(t_end + span - arrived) > 0.25 else t_end + span
            t = time.perf_counter()
            for tx in rx.process(raw, t_end):
                handle(tx)
            stats["busy"] += time.perf_counter() - t
    except KeyboardInterrupt:
        pass
    stop.set()
    for tx in rx.finish():
        handle(tx)
    elapsed = time.time() - started
    radio.close()
    clip_q.put(None)
    clips = None
    while clips is None and worker.is_alive():           # it finishes the clips still queued
        try:
            clips = results.get(timeout=2)
        except queue.Empty:
            pass
    if clips is None:
        try:
            clips = results.get(timeout=1)
        except queue.Empty:
            print("warning: the clip worker stopped without reporting")
            clip_q.cancel_join_thread()                  # don't wait at exit to hand it the rest
    worker.join(timeout=10)
    thr1 = throttled()

    # Summary.
    mb = sum(f.stat().st_size for d in ("clips", "clips_clean") for f in (out / d).glob("*")) / 1e6
    hours = elapsed / 3600
    lines = [f"ATC listen test {utc(started, ms=False)}, {elapsed / 60:.1f} min, {rate / 1e6:g} MSPS, "
             f"{len(freqs)} channels, radio {uri}",
             f"uptime:   {100 * up_seconds[0] / max(seconds[0], 1):.2f}% of {seconds[0]} s fully received "
             f"and kept up with",
             f"stream:   {100 * stats['received'] / (rate * elapsed):.2f}% of the samples received; "
             f"{getattr(radio, 'stats', {}).get('lost', 0)} chunks lost on the radio, {stats['dropped']} dropped "
             f"here, {stats['reconnects']} reconnect(s) ({stats['down_s']:.0f} s down)",
             f"receiver: {stats['busy'] / elapsed:.2f} of real time on average, {worst_busy[0]:.2f} in the "
             f"busiest second (1.00 = falling behind)"]
    if clips:
        lines.append(f"clips:    {clips['clips']} saved ({'MP3' if clips['mp3'] else 'WAV: lame not found'}), "
                     f"{clips['per_clip']:.2f} s of CPU each, the clip core {100 * clips['cpu']:.0f}% busy")
    lines.append(f"storage:  {mb:.1f} MB in {elapsed / 60:.0f} min = {mb / max(hours, 1e-9) / 1e3 * 24:.2f} GB/day, "
                 f"{mb / max(hours, 1e-9) / 1e3 * 24 * 7:.1f} GB/week, {mb / max(hours, 1e-9) / 1e3 * 24 * 30:.0f} "
                 "GB/month at this traffic")
    if temps:
        lines.append(f"Pi:       {temps[0]:.0f} C at start, peak {max(temps):.0f} C"
                     + ("" if thr1 is None else "; throttling " + (
                         "YES: fit the active cooler" if thr1 & 0x6 or (thr1 & ~(thr0 or 0)) & 0x60000 else "none")))
    lines += ["", A.summary(rx.squelches, elapsed, datetime.fromtimestamp(started))]
    report = "\n".join(lines)
    print("\n\n" + report)
    (out / "summary.txt").write_text(report + "\n")
    for f in (log, health, events):
        f.close()


if __name__ == "__main__":
    main()
