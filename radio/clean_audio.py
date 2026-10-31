#!/usr/bin/env python3
"""
Clean up recorded AM (ATC) clips: less static, more even loudness.

    python3 radio/clean_audio.py recordings/norcal_20261014-0022
    python3 radio/clean_audio.py recordings/sfo_20261016-1400 --strength 0.7

For a recording folder (with clips/), every clip is cleaned and saved under the
same name in clips_clean/: voice band-pass (300-3000 Hz), spectral noise
reduction using the gaps between words as the noise sample, and loudness
normalization. See atcsdr/audio.py. The area scripts (radio/sfo.py, ...) do this
already as they record; this is for older recordings or another --strength.

--strength sets how much of the static is removed (0-1, default 0.9). Higher is
quieter but can leave a watery, "musical" sound on weak transmissions.
"""

import argparse
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr import audio


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", help="recording folder with clips/ (or a folder of clips)")
    p.add_argument("--strength", type=float, default=0.9,
                   help="fraction of the static to remove, 0-1 (default 0.9)")
    p.add_argument("--out", help="output folder (default <folder>/clips_clean)")
    p.add_argument("--no-mp3", action="store_true", help="save WAV only")
    args = p.parse_args()

    clips_dir = os.path.join(args.folder, "clips")
    if not os.path.isdir(clips_dir):
        clips_dir = args.folder
    out_dir = args.out or os.path.join(os.path.dirname(clips_dir.rstrip("/")) or ".", "clips_clean")
    paths = sorted(p for p in glob.glob(os.path.join(clips_dir, "*.wav"))
                   if audio.CLIP_NAME.match(os.path.basename(p)))
    if not paths:
        sys.exit(f"No clips (<MHz>MHz_<name>_<date>-<time>.wav) found in {clips_dir}.")
    os.makedirs(out_dir, exist_ok=True)
    mp3 = None if args.no_mp3 else audio.find_lame()

    print(f"{'clip':<44} {'speech over static, dB':>22}")
    for path in paths:
        m = audio.CLIP_NAME.match(os.path.basename(path))
        x, rate = audio.load_wav(path)
        y = audio.clean(x, rate, args.strength)
        title = f"{m['name'].replace('_', ' ')} {m['freq']} MHz {m['stamp']} (cleaned)"
        audio.save_clip(y, os.path.join(out_dir, os.path.basename(path)[:-4]), rate, mp3, title)
        before, after = audio.speech_to_gap_db(x, rate), audio.speech_to_gap_db(y, rate)
        print(f"{os.path.basename(path):<44} {before:9.1f} -> {after:5.1f}")
    print("(speech over static: loudest 10% of 50 ms frames vs quietest 10%, voice band only;\n"
          " 'before' already includes the band-pass, so the gain shown is the noise reduction)")
    print(f"\nSaved {len(paths)} cleaned clips to {out_dir}")


if __name__ == "__main__":
    main()
