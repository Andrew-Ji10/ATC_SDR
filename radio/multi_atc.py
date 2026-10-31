#!/usr/bin/env python3
"""
Listen to several airband (AM) channels at once from one Pluto capture.

    python3 radio/multi_atc.py                         # SFO's own channels, to the speakers
    python3 radio/multi_atc.py --atis                  # also SFO ATIS, mixed in quietly
    python3 radio/multi_atc.py --preset norcal-usb --minutes 5 --no-audio \\
        --raw recordings/raw --clips recordings/clips
    python3 radio/multi_atc.py --channels "120.5 Tower, 121.8 Ground, 118.2 Clearance"
    python3 radio/multi_atc.py --list                  # presets and their channels

Every channel has its own squelch that learns that channel's noise floor, so you
only hear a channel while someone is transmitting on it. Channels are spread
across the stereo field (lowest frequency left, highest right). A status line
shows who is talking, and each transmission is logged with its time, channel,
length and strength.

Presets: sfo-airport (SFO clearance / tower / ground, departure, guard),
norcal-usb, norcal, and the areas south_bay, sfo, oak, bay (see
atcsdr/frequencies.py). Channels that don't fit one capture are covered by
rotating between capture windows (--dwell); see radio/bay_all.py for a survey.

--raw writes one continuous WAV per channel, aligned in time. --clips saves
each transmission as <MHz>MHz_<name>_<date>-<time>.wav/.mp3, --clean a cleaned
copy, --log a CSV of all transmissions.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import frequencies as fq
from atcsdr.airband import add_receiver_args, monitor


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    names = sorted(set(fq.PRESETS) | set(fq.AREAS))
    p.add_argument("--preset", choices=names, default="sfo-airport",
                   help="channel set (default sfo-airport)")
    p.add_argument("--channels", metavar="LIST",
                   help='comma-separated "MHz Name" list, instead of a preset')
    add_receiver_args(p, listen_default=True)
    p.add_argument("--minutes", type=float, help="stop after this long (default: Ctrl-C)")
    p.add_argument("--record", metavar="WAV", help="write the stereo mix to a WAV file")
    p.add_argument("--raw", metavar="DIR", help="write one continuous WAV per channel in DIR")
    p.add_argument("--clips", metavar="DIR", help="save each transmission as a clip in DIR")
    p.add_argument("--clean", metavar="DIR", help="save a cleaned copy of each clip in DIR")
    p.add_argument("--log", metavar="CSV", help="log every transmission to a CSV file")
    p.add_argument("--list", action="store_true", help="list the presets and exit")
    args = p.parse_args()

    if args.list:
        for name in names:
            chans = fq.preset_channels(name)
            print(f"{name} ({len(chans)}): " + ", ".join(f"{c.name} {c.mhz:g}" for c in chans))
        return
    freqs = (fq.parse_channels(args.channels) if args.channels
             else fq.preset_channels(args.preset, atis=args.atis))
    args.duration = args.minutes * 60 if args.minutes else None
    args.summary = None
    monitor(freqs, args)


if __name__ == "__main__":
    main()
