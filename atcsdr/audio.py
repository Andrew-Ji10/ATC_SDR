"""Audio files and clean-up for recorded AM (ATC) transmissions.

Clip files are named <MHz>MHz_<name>_<YYYYmmdd-HHMMSS>.wav (plus .mp3).

clean() reduces the static in a clip:
  1. Voice band-pass, 300-3000 Hz: removes the hiss above speech and the rumble
     below it.
  2. Spectral noise reduction (noisereduce), with the static measured in the
     quietest 20% of the clip: the gaps between words, while the carrier is on.
     (A channel's quiet stretches between transmissions have no carrier, so
     their static is different; used as the noise sample they removed 10-19 dB
     of the speech itself.)
  3. Loudness normalization to a common level, with a peak limit.
"""

import os
import re
import shutil
import subprocess
import sys
import wave

import numpy as np
from scipy.signal import butter, sosfiltfilt

VOICE_BAND = (300, 3000)
TARGET_RMS_DB = -20.0        # loudness after cleaning, dB re full scale
PEAK = 0.95
CLIP_NAME = re.compile(r"^(?P<freq>\d+\.\d+)MHz_(?P<name>.+)_(?P<stamp>\d{8}-\d{6})\.wav$")


def clip_base(out_dir, mhz, name, stamp):
    """Path (without extension) for a clip: <MHz>MHz_<name>_<stamp>."""
    return os.path.join(out_dir, f"{mhz:.3f}MHz_{name.replace(' ', '_')}_{stamp}")


def find_lame():
    """The MP3 encoder: this Python environment's (conda installs it there), else PATH."""
    local = os.path.join(os.path.dirname(sys.executable), "lame")
    return local if os.path.exists(local) else shutil.which("lame")


def save_clip(audio, path_base, rate, mp3, title):
    """Write audio (float, +-1) as <path_base>.wav, and .mp3 if `mp3` is the
    path of lame. Returns the paths written."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    wav_path = path_base + ".wav"
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    paths = [wav_path]
    if mp3:
        mp3_path = path_base + ".mp3"
        subprocess.run([mp3, "--quiet", "-b", "128", "--tt", title, wav_path, mp3_path],
                       check=True)
        paths.append(mp3_path)
    return paths


def save_mp3(audio, path, rate, lame, title, comment="", kbps=32):
    """Write audio (float, +-1) straight to an MP3 at `path` (mono, `kbps`), with
    an ID3 title and comment; no WAV. Without lame, a WAV next to it instead.
    Returns the path written."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    if not lame:
        wav_path = os.path.splitext(path)[0] + ".wav"
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        return wav_path
    subprocess.run([lame, "--quiet", "-r", "-s", f"{rate / 1000:g}", "--bitwidth", "16", "--signed",
                    "--little-endian", "-m", "m", "-b", str(kbps), "--tt", title, "--tc", comment,
                    "-", path], input=pcm.tobytes(), check=True)
    return path


def load_wav(path):
    with wave.open(path) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    return pcm.astype(np.float32) / 32767, rate


def band_pass(x, rate):
    sos = butter(4, VOICE_BAND, btype="bandpass", fs=rate, output="sos")
    return sosfiltfilt(sos, x).astype(np.float32)


def quietest_part(x, rate, frac=0.2):
    """Noise sample: the quietest 20% of 50 ms frames."""
    n = int(0.05 * rate)
    frames = x[: len(x) // n * n].reshape(-1, n)
    order = np.argsort((frames ** 2).mean(axis=1))
    return frames[order[: max(1, int(len(order) * frac))]].ravel()


def normalize(x):
    rms = np.sqrt(np.mean(x ** 2)) + 1e-9
    y = x * (10 ** (TARGET_RMS_DB / 20) / rms)
    peak = np.max(np.abs(y)) + 1e-9
    return y * (PEAK / peak) if peak > PEAK else y


def clean(x, rate, strength=0.9):
    """Cleaned copy of clip `x` (see the module docstring). `strength` is the
    fraction of the static removed, 0-1; higher is quieter but can sound watery."""
    try:
        import noisereduce
    except ImportError:
        sys.exit("noisereduce not found. Install it with: pip install noisereduce")
    noise = band_pass(quietest_part(x, rate), rate)
    y = noisereduce.reduce_noise(y=band_pass(x, rate), sr=rate, y_noise=noise,
                                 stationary=True, prop_decrease=strength)
    return normalize(y.astype(np.float32))


def speech_to_gap_db(x, rate):
    """Loudest 10% of 50 ms frames over the quietest 10%, in dB, within the voice
    band: how far the speech stands above the static between words."""
    x = band_pass(x, rate)
    n = int(0.05 * rate)
    e = (x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1) + 1e-20
    return 10 * np.log10(np.percentile(e, 90) / np.percentile(e, 10))
