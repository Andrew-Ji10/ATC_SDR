#!/usr/bin/env python3
"""
Decode ADS-B (1090 MHz) to the terminal and to log files, without a map.

    python3 adsb/adsb_decode.py --ref 37.43 -122.17            # Pluto, live table
    python3 adsb/adsb_decode.py --minutes 30 --out recordings/adsb_evening
    python3 adsb/adsb_decode.py --messages                     # print every message
    python3 adsb/adsb_decode.py --hex recordings/adsb_x/messages.txt   # decode a log again
    python3 adsb/adsb_decode.py --network localhost:30002      # dump1090 / readsb feed

The terminal shows a table of aircraft (callsign, altitude, speed, track, climb
rate, position, range from --ref, messages, age), refreshed every second. With
--out DIR it also writes:
    positions.csv   every position fix (re-plot with adsb_plot.py --track-csv,
                    animate with animate_tracks.py)
    messages.txt    every valid message as '<unix time> <hex>' (decode again with --hex)
    decoded.jsonl   every decoded message, one JSON object per line
    aircraft.csv    one row per aircraft at the end: first / last seen, messages, ...

--ref (your location) lets single position messages decode right away and adds
a range column; without it pyModeS waits for a few even/odd position pairs.
"""

import argparse
import csv
import json
import math
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import adsb


def _terminate(*_):
    raise KeyboardInterrupt


def range_nm(ref, lat, lon):
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(ref[0]), math.radians(lat)
    dp, dl = p2 - p1, math.radians(lon - ref[1])
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(a))


def fmt(v, spec, width):
    return f"{v:{spec}}".rjust(width) if v is not None else "".rjust(width)


def table(tracker, ref, now):
    """The aircraft table, most recently heard first."""
    with tracker.lock:
        planes = sorted(tracker.aircraft.values(), key=lambda a: -a.last_seen)
    rows = [f"{'ICAO':<7}{'Flight':<9}{'Alt ft':>7}{'Kt':>5}{'Trk':>5}{'fpm':>7}"
            f"{'Lat':>10}{'Lon':>11}" + (f"{'nm':>6}" if ref else "") + f"{'Msgs':>6}{'Age':>5}"]
    for a in planes:
        dist = range_nm(ref, a.lat, a.lon) if ref and a.lat is not None else None
        rows.append(f"{a.icao:<7}{a.callsign:<9.9}{fmt(a.altitude, '.0f', 7)}"
                    f"{fmt(a.speed, '.0f', 5)}{fmt(a.track, '.0f', 5)}{fmt(a.vrate, '+.0f', 7)}"
                    f"{fmt(a.lat, '.4f', 10)}{fmt(a.lon, '.4f', 11)}"
                    + (fmt(dist, '.1f', 6) if ref else "")
                    + f"{a.messages:6d}{now - a.last_seen:4.0f}s")
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    adsb.add_source_args(p)
    p.add_argument("--ref", nargs=2, type=float, metavar=("LAT", "LON"), help="receiver position")
    p.add_argument("--out", metavar="DIR", help="write positions.csv, messages.txt, "
                                                 "decoded.jsonl and aircraft.csv here")
    p.add_argument("--minutes", type=float, help="stop after this long (default: Ctrl-C)")
    p.add_argument("--messages", action="store_true",
                   help="print every decoded message instead of the table")
    args = p.parse_args()
    signal.signal(signal.SIGTERM, _terminate)

    files = {}
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        files["hex"] = open(os.path.join(args.out, "messages.txt"), "w", buffering=1)
        files["json"] = open(os.path.join(args.out, "decoded.jsonl"), "w", buffering=1)
    tracker = adsb.Tracker(args.ref, os.path.join(args.out, "positions.csv") if args.out else None)

    def on_message(t, msg, d):
        if files:
            files["hex"].write(f"{t:.3f} {msg}\n")
            files["json"].write(json.dumps({"time": round(t, 3), "hex": msg, **d},
                                           default=str) + "\n")
        if args.messages:
            keys = ("callsign", "altitude", "groundspeed", "track", "vertical_rate",
                    "latitude", "longitude")
            info = "  ".join(f"{k}={d[k]:.5g}" if isinstance(d[k], float) else f"{k}={d[k]}"
                             for k in keys if d.get(k) is not None)
            print(f"{datetime.fromtimestamp(t):%H:%M:%S.%f}"[:-3]
                  + f"  {d['icao']}  DF{d.get('df')} TC{d.get('typecode')}  {info}")

    stop, done = threading.Event(), threading.Event()
    source_q = adsb.new_queue()
    adsb.start_source(args, source_q, stop)

    def consume():
        adsb.consume(source_q, tracker, on_message)
        done.set()
    threading.Thread(target=consume, daemon=True).start()

    t0 = time.time()
    live = not (args.hex or args.iq)
    try:
        while not done.wait(1.0):
            if args.minutes and time.time() - t0 >= args.minutes * 60:
                break
            if not args.messages:
                rows = table(tracker, args.ref, time.time() if live else tracker.now)
                height = shutil.get_terminal_size((100, 30)).lines - 3
                header = (f"{datetime.now():%H:%M:%S}  {tracker.total} messages, "
                          f"{len(tracker.aircraft)} aircraft  (Ctrl-C to stop)")
                print("\033[H\033[J" + header + "\n" + "\n".join(rows[:height]), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if not live:
            done.wait(2)
        tracker.close()
        print("\n".join(table(tracker, args.ref, time.time() if live else tracker.now)))
        print(f"\nDone: {tracker.total} messages from {len(tracker.aircraft)} aircraft "
              f"in {time.time() - t0:.0f} s.")
        if args.out:
            for f in files.values():
                f.close()
            with open(os.path.join(args.out, "aircraft.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["icao", "callsign", "first_seen", "last_seen", "messages",
                            "fixes", "last_altitude_ft", "last_groundspeed_kt"])
                for a in sorted(tracker.aircraft.values(), key=lambda a: a.first_seen):
                    w.writerow([a.icao, a.callsign,
                                f"{datetime.fromtimestamp(a.first_seen):%Y-%m-%d %H:%M:%S}",
                                f"{datetime.fromtimestamp(a.last_seen):%Y-%m-%d %H:%M:%S}",
                                a.messages, len(a.path), a.altitude, a.speed])
            print(f"Logs in {args.out}/")


if __name__ == "__main__":
    main()
