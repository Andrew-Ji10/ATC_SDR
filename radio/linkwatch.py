#!/usr/bin/env python3
"""
Link monitor: how often does the Pluto drop off, and for how long? Runs for a
set time (or until Ctrl-C) and watches three things at once, every second:

  ping     the Pluto's address (Ethernet): lost replies, latency, outages
  cable    the network port's link state: the cable or a port dropping
  stream   samples read flat out at the recorder's rate, as the recorder does:
           errors, reconnects, stalls (a read taking far too long) and seconds
           that came up short (samples lost on the way)

    python3 radio/linkwatch.py --device ip:192.168.3.2 --duration 8h
    python3 radio/linkwatch.py --device ip:192.168.3.2 --duration 1h --rate 10.8
    python3 radio/linkwatch.py --device ip:192.168.3.2 --rate 0     # ping and cable only

Every event is printed with its time as it happens. Writes events.csv, link.csv
(one row per second) and summary.txt to recordings/linktest_<date>-<time>/.
"""

import argparse
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import pluto
from atcsdr.airband import parse_duration
from atcsdr.pluto import is_ethernet

OUTAGE_PINGS = 3             # this many lost pings in a row count as an outage
SHORT_PCT = 90               # a second with fewer samples than this (% of the rate) is short
TTY = sys.stdout.isatty()


def _terminate(*_):
    raise KeyboardInterrupt


def stamp(t=None):
    return datetime.fromtimestamp(t or time.time()).strftime("%Y-%m-%d %H:%M:%S")


def span(seconds):
    """90 -> '1 min 30 s', 7200 -> '2 h 0 min'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s % 3600 // 60} min"


def route_dev(host):
    """The network interface that reaches `host` (Linux), or None."""
    try:
        out = subprocess.run(["ip", "route", "get", host], capture_output=True, text=True).stdout
    except OSError:
        return None
    m = re.search(r"\bdev (\S+)", out)
    return m.group(1) if m else None


def carrier(dev):
    """1 if the port has a link, 0 if not, None if unknown."""
    try:
        return int(Path(f"/sys/class/net/{dev}/carrier").read_text())
    except (OSError, ValueError):
        return 0 if dev and Path(f"/sys/class/net/{dev}").exists() else None


def ping(host):
    """Round-trip time in ms, or None if no reply within 1 s."""
    cmd = (["ping", "-n", "1", "-w", "1000", host] if sys.platform == "win32"
           else ["ping", "-c", "1", "-W", "1", host])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"time[=<]\s*([\d.]+)", r.stdout)
    return float(m.group(1)) if r.returncode == 0 and m else None


class Log:
    def __init__(self, out):
        self.events = open(out / "events.csv", "w", buffering=1)
        self.events.write("time,event\n")
        self.lock = threading.Lock()

    def __call__(self, text, t=None):
        with self.lock:
            print(("\r\033[K" if TTY else "") + f"{stamp(t)}  {text}", flush=True)
            self.events.write(f"{stamp(t)},\"{text}\"\n")


def stream(uri, rate, center, gain, stats, log, stop):
    """Read the Pluto flat out; reopen it whenever it fails, and log what happened."""
    buf = max(1 << 14, int(rate * 0.05))                 # ~50 ms per read
    stall_s = max(0.5, 4 * buf / rate)
    radio, down_since, last = None, None, None
    while not stop.is_set():
        if radio is None:
            try:
                radio = pluto.Pluto(uri, center, rate, gain, buffer_size=buf)
            except OSError as e:
                if down_since is None:
                    down_since = time.time()
                    log(f"stream: can't open the radio ({e}); retrying every second")
                stop.wait(1)
                continue
            if down_since is not None and stats["opens"]:
                gone = time.time() - down_since
                stats["down_s"] += gone
                log(f"stream: back after {gone:.1f} s")
            down_since, last = None, None
            stats["opens"] += 1
        error = None
        try:
            n = len(radio.read()) // 2
        except OSError as e:
            error = str(e)
        if error is None:
            now = time.perf_counter()
            if last is not None and now - last > stall_s:
                stats["stalls"].append(now - last)
                log(f"stream: stalled for {now - last:.2f} s")
            last = now
            stats["samples"] += n
            continue
        # Close outside the except block (see atcsdr.pluto.Pluto).
        stats["errors"] += 1
        log(f"stream: error ({error}); reconnecting")
        radio.close()
        radio, down_since = None, time.time()
    if radio is not None:
        radio.close()
    if down_since is not None:
        stats["down_s"] += time.time() - down_since


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="", help="Pluto URI, e.g. ip:192.168.3.2 (default: auto-detect)")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("1h"),
                   help="how long to watch: 90 (minutes), 45m, 2h, 1h30m, 1d (default 1h); 0 = until stopped")
    p.add_argument("--rate", type=float, default=5.76,
                   help="stream at this many MSPS, like the recorder (default 5.76); 0 = no stream")
    p.add_argument("--center", type=float, default=127.0, help="tuning while streaming, MHz (default 127)")
    p.add_argument("--gain", default="40", help="RF gain while streaming (default 40)")
    p.add_argument("--out", help="output folder (default recordings/linktest_<time>)")
    args = p.parse_args()
    signal.signal(signal.SIGTERM, _terminate)

    uri = pluto.find_uri(args.device)
    host = uri.split(":", 1)[1] if uri.startswith("ip:") else None
    dev = route_dev(host) if host else None
    rate = int(args.rate * 1e6)
    out = Path(args.out or f"recordings/linktest_{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    log = Log(out)
    rows = open(out / "link.csv", "w", buffering=1)
    rows.write("time,ping_ms,link,stream_pct\n")

    watching = ([f"ping {host}"] if host else []) + ([f"cable on {dev}"] if dev else []) \
        + ([f"stream at {rate / 1e6:g} MSPS"] if rate else [])
    print(f"Link test: {uri} ({'Ethernet' if is_ethernet(uri) else 'USB'}), "
          f"{', '.join(watching)}, "
          + (f"for {span(args.duration)}" if args.duration else "until Ctrl-C")
          + f".\nSaving to {out}/\n", flush=True)

    stop = threading.Event()
    stats = dict(samples=0, opens=0, errors=0, down_s=0.0, stalls=[])
    if rate:
        threading.Thread(target=stream, daemon=True,
                         args=(uri, rate, args.center * 1e6, args.gain, stats, log, stop)).start()

    rtts, lost, outages, short = [], 0, [], 0
    run, run_start = 0, None                  # current streak of lost pings
    link_drops, link = 0, carrier(dev) if dev else None
    t0 = tick = time.time()
    seen = 0
    try:
        while not args.duration or time.time() - t0 < args.duration:
            tick += 1
            now = time.time()
            rtt = ping(host) if host else None
            if host:
                if rtt is None:
                    lost += 1
                    run += 1
                    run_start = run_start or now
                    if run == OUTAGE_PINGS:
                        log("ping: no replies (outage started)", run_start)
                else:
                    rtts.append(rtt)
                    if run >= OUTAGE_PINGS:
                        outages.append((run_start, now - run_start))
                        log(f"ping: replies again after {now - run_start:.0f} s")
                    run, run_start = 0, None
            if dev:
                c = carrier(dev)
                if c != link:
                    if c == 0:
                        link_drops += 1
                    log(f"cable: link {'up' if c else 'DOWN'} on {dev}")
                    link = c
            pct = ""
            if rate:
                got, seen = stats["samples"] - seen, stats["samples"]
                pct = 100 * got / rate
                if pct < SHORT_PCT and time.time() - t0 > 3:
                    short += 1
                pct = f"{pct:.1f}"
            rows.write(f"{stamp(now)},{'' if rtt is None else rtt},"
                       f"{'' if link is None else link},{pct}\n")
            if TTY:
                status = [f"{time.time() - t0:5.0f} s"]
                if host:
                    status.append(f"ping {'---' if rtt is None else f'{rtt:.1f} ms'}, lost {lost}")
                if rate:
                    status.append(f"stream {pct}%, errors {stats['errors']}")
                print("\r\033[K" + "  |  ".join(status), end="", flush=True)
            time.sleep(max(0.0, tick - time.time()))
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(0.3)
    elapsed = time.time() - t0
    if run >= OUTAGE_PINGS:
        outages.append((run_start, time.time() - run_start))

    lines = [f"Link test {stamp(t0)}, {span(elapsed)}, {uri}" + (f" via {dev}" if dev else "")]
    if host:
        sent = len(rtts) + lost
        line = f"ping:    {sent} sent, {lost} lost ({100 * lost / max(sent, 1):.2f}%)"
        if rtts:
            line += (f"; round trip median {np.median(rtts):.2f} ms, 99% {np.percentile(rtts, 99):.2f} ms, "
                     f"max {max(rtts):.2f} ms")
        lines.append(line)
        if outages:
            longest = max(outages, key=lambda o: o[1])
            lines.append(f"outages: {len(outages)} ({OUTAGE_PINGS}+ s without a reply), "
                         f"{sum(o[1] for o in outages):.0f} s in all, longest {longest[1]:.0f} s "
                         f"at {stamp(longest[0])}")
        else:
            lines.append("outages: none")
    if dev:
        lines.append(f"cable:   {link_drops} link drop(s) on {dev}")
    if rate:
        lines.append(f"stream:  {rate / 1e6:g} MSPS, {100 * stats['samples'] / (rate * elapsed):.2f}% "
                     f"of the samples received; {stats['errors']} error(s), "
                     f"{max(stats['opens'] - 1, 0)} reconnect(s), {stats['down_s']:.1f} s disconnected")
        stalls = stats["stalls"]
        lines.append(f"         {len(stalls)} stall(s)" + (f", longest {max(stalls):.2f} s" if stalls else "")
                     + f"; {short} second(s) below {SHORT_PCT}% of the rate")
    drops = len(outages) + link_drops + stats["errors"]
    verdict = ("no drops" if drops == 0 and not stats["stalls"] else
               f"{drops} drop(s) in {span(elapsed)}; the times are in events.csv")
    if rate:
        got = stats["samples"] / (rate * elapsed)
        if got < 0.99:
            verdict += (f"; but the link can't sustain {rate / 1e6:g} MSPS: it delivered "
                        f"{100 * got:.0f}%, about {got * rate / 1e6:.1f} MSPS")
    lines.append(f"Verdict: {verdict}.")
    report = "\n".join(lines)
    print("\n\n" + report)
    (out / "summary.txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
