#!/usr/bin/env python3
"""
Listen to one broadcast FM or airband (ATC) AM channel with an SDR.

    python3 radio/listen.py 88.5              # broadcast FM (mode auto-detected)
    python3 radio/listen.py 118.6 --squelch -45   # ATC tower, AM with squelch
    python3 radio/listen.py 162.55 --mode nbfm    # NOAA weather radio
    python3 radio/listen.py 120.5 --record sfo_tower.wav
    python3 radio/listen.py 88.5 --sdr osmosdr    # RTL-SDR instead of Pluto

While running, type commands at the prompt:
    <MHz>        retune (e.g. 121.8)
    v <0-5>      volume
    g <dB>|auto  RF gain
    sq <dB>      squelch threshold (AM / NBFM only), e.g. sq -45
    s            print current channel power (use it to pick a squelch level)
    q            quit

For a spectrum display and click-to-tune, run radio/gui.py. The receiver itself
is in atcsdr/gr_receiver.py.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr.gr_receiver import add_sdr_args, auto_mode, open_receiver, warn_pluto_range


def control_loop(rx):
    print("Commands: <MHz> | v <vol> | g <dB|auto|fast> | sq <dB> | s | q")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        cmd, *rest = line.split()
        arg = rest[0] if rest else None
        try:
            if cmd in ("q", "quit", "exit"):
                return
            elif cmd == "s":
                print(f"channel power: {rx.power_db():.1f} dB")
            elif cmd == "v" and arg:
                rx.set_volume(float(arg))
            elif cmd == "g" and arg:
                rx.set_gain(arg)
            elif cmd == "sq" and arg:
                rx.set_squelch(float(arg))
            else:
                rx.set_freq(float(cmd) * 1e6)
                print(f"tuned to {rx.freq / 1e6:.4f} MHz")
        except ValueError as e:
            print(f"error: {e}")


def main():
    p = argparse.ArgumentParser(
        description="Listen to broadcast FM or airband ATC with an SDR.")
    p.add_argument("freq", type=float, help="frequency in MHz, e.g. 88.5 or 118.6")
    add_sdr_args(p)
    args = p.parse_args()

    freq = args.freq * 1e6
    mode = auto_mode(freq) if args.mode == "auto" else args.mode
    warn_pluto_range(args, freq)

    try:
        rx = open_receiver(args, freq, mode)
    except RuntimeError as e:
        sys.exit(str(e))

    print(f"Listening on {args.freq:.4f} MHz [{mode.upper()}], gain {args.gain}")
    rx.start()
    try:
        if sys.stdin.isatty():
            control_loop(rx)
        else:
            rx.wait()
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        rx.wait()


if __name__ == "__main__":
    main()
