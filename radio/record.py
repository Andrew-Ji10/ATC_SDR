#!/usr/bin/env python3
"""
Timed airband recording: monitor an area's (or any) ATC channels for a set time,
save every transmission, then exit. Built for unattended runs (a Raspberry Pi,
cron, systemd): no audio output, and a plain log when stdout isn't a terminal.

    python3 radio/record.py --duration 2h                    # all of the Bay
    python3 radio/record.py --area sfo --duration 1h30m
    python3 radio/record.py --channels "120.5 SFO Twr, 121.5 Guard" --duration 45m
    python3 radio/record.py --duration 8h --max-rate 4       # lighter on a Pi's CPU
    python3 radio/record.py --area oak --list                # channels and plan only

--duration takes 90 (minutes), 90s, 45m, 2h, 1h30m or 1d; 0 = until Ctrl-C or SIGTERM.
Output, in recordings/<area>_<date>-<time>/ (or --out): clips/, clips_clean/,
transmissions.csv and summary.txt, as with the area scripts.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import frequencies as fq
from atcsdr.airband import (add_receiver_args, describe_plan, find_uri, max_rate, monitor,
                            parse_duration, plan)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    names = sorted(set(fq.PRESETS) | set(fq.AREAS))
    p.add_argument("--area", choices=names, default="bay",
                   help="channel set: an area or a multi_atc.py preset (default bay)")
    p.add_argument("--channels", metavar="LIST",
                   help='comma-separated "MHz Name" list, instead of --area')
    p.add_argument("--duration", type=parse_duration, default=parse_duration("1h"),
                   help="how long to record (default 1h); 0 = until stopped")
    add_receiver_args(p, listen_default=False)
    p.add_argument("--out", help="output folder (default recordings/<area>_<time>)")
    p.add_argument("--raw", action="store_true",
                   help="also keep one continuous WAV per channel (large: ~5.8 MB/min each)")
    p.add_argument("--no-clean", action="store_true", help="don't save cleaned copies of the clips")
    p.add_argument("--list", action="store_true", help="print the channels and the plan, then exit")
    args = p.parse_args()

    freqs = (fq.parse_channels(args.channels) if args.channels
             else fq.preset_channels(args.area, atis=args.atis))
    label = "custom" if args.channels else args.area
    if args.list:
        uri = find_uri(args.device)
        rate, windows = plan(freqs, max_rate(uri, args.max_rate))
        print(describe_plan(rate, windows, args.dwell, uri))
        return

    out = args.out or os.path.join("recordings", f"{label}_{datetime.now():%Y%m%d-%H%M}")
    opts = argparse.Namespace(**{
        **vars(args), "duration": args.duration or None, "record": None,
        "raw": os.path.join(out, "raw") if args.raw else None,
        "clips": os.path.join(out, "clips"),
        "clean": None if args.no_clean else os.path.join(out, "clips_clean"),
        "log": os.path.join(out, "transmissions.csv"),
        "summary": os.path.join(out, "summary.txt")})
    until = (f"for {args.duration / 60:g} min" if args.duration else "until stopped")
    print(f"Recording {label} ({len(freqs)} channels) {until}, started "
          f"{datetime.now():%Y-%m-%d %H:%M:%S}.\nSaving to {out}/\n", flush=True)
    monitor(freqs, opts)


if __name__ == "__main__":
    main()
