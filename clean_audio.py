#!/usr/bin/env python3
"""
Clean up recorded AM (ATC) clips: less static, more even loudness.

    python3 clean_audio.py recordings/norcal_20261014-0022
    python3 clean_audio.py recordings/norcal_20261014-0022 --strength 0.7

For a recording folder made by multi_atc.py, every clip in clips/ is cleaned and
saved under the same name in clips_clean/. Each clip:

  1. Voice band-pass, 300-3000 Hz: removes the hiss above speech and the rumble
     below it.
  2. Spectral noise reduction (noisereduce), with the static measured in the
     quietest 20% of the clip: the gaps between words, while the carrier is on.
     (The raw files' quiet stretches have no carrier, so their static is
     different and much stronger relative to the signal; as a profile they
     removed 10-19 dB of the speech itself.)
  3. Loudness normalization to a common level, with a peak limit.

--strength sets how much of the static is removed (0-1, default 0.9). Higher
is quieter but can leave a watery, "musical" sound on weak transmissions.
"""

import argparse
import glob
import os
import re
import sys
import wave

import numpy as np
import noisereduce
from scipy.signal import butter, sosfiltfilt

from capture_clips import find_lame, save_clip

VOICE_BAND = (300, 3000)
TARGET_RMS_DB = -20.0        # loudness after cleaning, dB re full scale
PEAK = 0.95
NAME_RE = re.compile(r"^(?P<freq>\d+\.\d+)MHz_(?P<name>.+)_(?P<stamp>\d{8}-\d{6})\.wav$")


def load_wav(path):
    with wave.open(path) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    return pcm.astype(np.float32) / 32767, rate


def band_pass(x, rate):
    sos = butter(4, VOICE_BAND, btype="bandpass", fs=rate, output="sos")
    return sosfiltfilt(sos, x).astype(np.float32)


def quietest_part(x, rate, frac=0.2):
    """Noise sample: the quietest 20% of 50 ms frames of the clip."""
    n = int(0.05 * rate)
    frames = x[: len(x) // n * n].reshape(-1, n)
    order = np.argsort((frames ** 2).mean(axis=1))
    return frames[order[: max(1, int(len(order) * frac))]].ravel()


def normalize(x):
    rms = np.sqrt(np.mean(x ** 2)) + 1e-9
    y = x * (10 ** (TARGET_RMS_DB / 20) / rms)
    peak = np.max(np.abs(y)) + 1e-9
    return y * (PEAK / peak) if peak > PEAK else y


def clean(x, rate, noise, strength):
    x = band_pass(x, rate)
    noise = band_pass(noise, rate)
    y = noisereduce.reduce_noise(y=x, sr=rate, y_noise=noise, stationary=True,
                                 prop_decrease=strength)
    return normalize(y.astype(np.float32))


def speech_to_gap_db(x, rate):
    """Loudest 10% of 50 ms frames over the quietest 10%, in dB, within the voice
    band: how far the speech stands above the static between words."""
    x = band_pass(x, rate)
    n = int(0.05 * rate)
    e = (x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1) + 1e-20
    return 10 * np.log10(np.percentile(e, 90) / np.percentile(e, 10))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", help="recording folder with clips/ from multi_atc.py")
    p.add_argument("--strength", type=float, default=0.9,
                   help="fraction of the static to remove, 0-1 (default 0.9)")
    p.add_argument("--out", help="output folder (default <folder>/clips_clean)")
    p.add_argument("--no-mp3", action="store_true", help="save WAV only")
    args = p.parse_args()

    clips_dir = os.path.join(args.folder, "clips")
    out_dir = args.out or os.path.join(args.folder, "clips_clean")
    clip_paths = sorted(p for p in glob.glob(os.path.join(clips_dir, "*.wav"))
                        if NAME_RE.match(os.path.basename(p)))
    if not clip_paths:
        sys.exit(f"No clips found in {clips_dir}.")
    os.makedirs(out_dir, exist_ok=True)
    mp3 = None if args.no_mp3 else find_lame()

    print(f"{'clip':<44} {'speech over static, dB':>22}")
    for path in clip_paths:
        m = NAME_RE.match(os.path.basename(path))
        x, rate = load_wav(path)
        y = clean(x, rate, quietest_part(x, rate), args.strength)
        base = os.path.join(out_dir, os.path.basename(path)[:-4])
        title = f"{m['name'].replace('_', ' ')} {m['freq']} MHz {m['stamp']} (cleaned)"
        save_clip(y, base, rate, mp3, title)
        before, after = speech_to_gap_db(x, rate), speech_to_gap_db(y, rate)
        print(f"{os.path.basename(path):<44} {before:9.1f} -> {after:5.1f}")
    print("(speech over static: loudest 10% of 50 ms frames vs quietest 10%, voice band only;\n"
          " 'before' already includes the band-pass, so the gain shown is the noise reduction)")
    print(f"\nSaved {len(clip_paths)} cleaned clips to {out_dir}")


if __name__ == "__main__":
    main()
