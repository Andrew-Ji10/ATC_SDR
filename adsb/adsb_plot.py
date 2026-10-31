#!/usr/bin/env python3
"""
Receive ADS-B (1090 MHz), decode it and plot aircraft tracks on a live map.

    python3 adsb/adsb_plot.py --ref 37.43 -122.17      # Pluto; --ref = your lat/lon
    python3 adsb/adsb_plot.py --csv tracks.csv --save map.png
    python3 adsb/adsb_plot.py --network localhost:30002     # dump1090 / readsb feed
    python3 adsb/adsb_plot.py --iq capture.cs16             # recorded IQ
    python3 adsb/adsb_plot.py --track-csv samples/adsb/tracks.csv --label-every 0 \\
        --no-plot --save map.png                             # re-plot a saved session

--ref (your location) centers the map and lets single position messages decode
right away; without it pyModeS waits for a few even/odd position pairs per
aircraft. Every fix is a dot; --label-every sets how often fixes get a time
label (default about ten per track; 0 = all, with --save enlarging the image
until none overlap). For a text-only decoder, see adsb/adsb_decode.py; to
replay a session as a video, adsb/animate_tracks.py.
"""

import argparse
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import adsb, adsb_map


def _terminate(*_):
    raise KeyboardInterrupt


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    adsb.add_source_args(p)
    p.add_argument("--track-csv", metavar="FILE", help="re-plot positions from a --csv log")
    p.add_argument("--ref", nargs=2, type=float, metavar=("LAT", "LON"), help="receiver position")
    p.add_argument("--csv", metavar="FILE", help="log decoded positions to CSV")
    p.add_argument("--save", metavar="PNG", help="save the map when the window closes")
    p.add_argument("--label-every", type=float, metavar="SECONDS",
                   help="timestamp a fix every SECONDS along each track; 0 = every fix "
                        "(default: about ten per track)")
    p.add_argument("--no-plot", action="store_true",
                   help="decode and log only, no window (--save still writes the map at the end)")
    args = p.parse_args()
    # `kill` stops the script like Ctrl-C does, so the CSV and --save map are written.
    signal.signal(signal.SIGTERM, _terminate)

    tracker = adsb.Tracker(args.ref, args.csv)
    stop, done = threading.Event(), threading.Event()
    if args.track_csv:
        tracker.load_csv(args.track_csv)
        done.set()
    else:
        source_q = adsb.new_queue()
        adsb.start_source(args, source_q, stop)

        def consume():
            adsb.consume(source_q, tracker)
            done.set()
        threading.Thread(target=consume, daemon=True).start()

    try:
        if args.no_plot:
            while not done.wait(5):
                _, total, planes = tracker.snapshot()
                print(f"{total} messages, {len(planes)} aircraft, "
                      f"{sum(1 for p in planes if p[6])} with position", flush=True)
        else:
            adsb_map.show_live(tracker, args.ref, args.track_csv is not None,
                               args.label_every, args.save)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if args.hex or args.iq:
            done.wait(2)
        _, total, planes = tracker.snapshot()
        print(f"Done: {total} messages from {len(planes)} aircraft.")
        if args.save and args.no_plot:
            import matplotlib
            matplotlib.use("Agg")
            fig, draw = adsb_map.build_map(tracker, args.ref, args.track_csv is not None,
                                           args.label_every)
            adsb_map.save_map(fig, draw, args.save, args.label_every == 0)
        tracker.close()


if __name__ == "__main__":
    main()
