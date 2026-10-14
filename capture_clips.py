#!/usr/bin/env python3
"""
Capture individual transmissions from an AM (ATC) or NBFM channel as clips.

    python3 capture_clips.py 120.1                  # wait for 1 transmission
    python3 capture_clips.py 120.1 --clips 5 --max-time 900
    python3 capture_clips.py 118.6 --listen         # also play audio while waiting

How it works:
    - The tuned channel's power is measured in 20 ms frames.
    - The noise floor is the 20th percentile of the last 10 s of quiet frames
      (the first 2 s are spent calibrating).
    - A clip opens when the power stays THRESHOLD dB above the floor for 40 ms,
      and includes PRE_ROLL seconds of audio from before the trigger.
    - It closes after HANG seconds below (threshold - 3 dB), so short pauses
      between words don't split a transmission. Trailing silence is trimmed.
    - Each clip is saved as WAV (lossless, best for transcription) and MP3.
"""

import argparse
import os
import queue
import shutil
import subprocess
import sys
import time
import wave
from collections import deque
from datetime import datetime

import numpy as np
from gnuradio import gr

import sdr_receiver as sr

FRAME_S = 0.02
CALIBRATE_S = 2.0
HISTORY_S = 10.0
TRIGGER_FRAMES = 2
HYSTERESIS_DB = 3.0
TAIL_S = 0.3


class ClipDetector(gr.sync_block):
    """Inputs: demodulated audio (float) and channel IQ (complex), both at AUDIO_RATE."""

    def __init__(self, rate, threshold_db, hang_s, pre_roll_s, min_s, max_s, clips_out):
        gr.sync_block.__init__(self, "clip_detector",
                               in_sig=[np.float32, np.complex64], out_sig=None)
        self.frame = int(rate * FRAME_S)
        self.threshold_db = threshold_db
        self.hang_frames = int(hang_s / FRAME_S)
        self.min_frames = int(min_s / FRAME_S)
        self.max_frames = int(max_s / FRAME_S)
        self.clips_out = clips_out
        self.audio_buf = np.zeros(0, np.float32)
        self.power_buf = np.zeros(0, np.float32)
        self.history = deque(maxlen=int(HISTORY_S / FRAME_S))
        self.pre_roll = deque(maxlen=max(1, int(pre_roll_s / FRAME_S)))
        self.active = None
        self.loud = 0
        self.quiet = 0
        self.peak_db = 0.0
        self.floor_db = None
        self.level_db = -200.0

    def work(self, input_items, output_items):
        audio, iq = input_items
        self.audio_buf = np.concatenate([self.audio_buf, audio])
        self.power_buf = np.concatenate([self.power_buf, (iq.real ** 2 + iq.imag ** 2)])
        n = len(self.audio_buf) // self.frame
        for i in range(n):
            sl = slice(i * self.frame, (i + 1) * self.frame)
            self._frame(self.audio_buf[sl].copy(), float(self.power_buf[sl].mean()))
        self.audio_buf = self.audio_buf[n * self.frame:]
        self.power_buf = self.power_buf[n * self.frame:]
        return len(audio)

    def _frame(self, audio, power):
        db = 10 * np.log10(power + 1e-20)
        self.level_db = db
        if len(self.history) < CALIBRATE_S / FRAME_S:
            self.history.append(db)
            self.pre_roll.append(audio)
            return
        self.floor_db = float(np.percentile(self.history, 20))
        open_at = self.floor_db + self.threshold_db

        if self.active is None:
            self.pre_roll.append(audio)
            self.loud = self.loud + 1 if db > open_at else 0
            if self.loud >= TRIGGER_FRAMES:
                self.active = list(self.pre_roll)
                self.quiet = 0
                self.peak_db = db
            else:
                self.history.append(db)
            return

        self.active.append(audio)
        self.peak_db = max(self.peak_db, db)
        self.quiet = self.quiet + 1 if db < open_at - HYSTERESIS_DB else 0
        if self.quiet >= self.hang_frames or len(self.active) >= self.max_frames:
            frames = self.active[:len(self.active) - self.quiet + int(TAIL_S / FRAME_S)]
            if len(frames) >= self.min_frames:
                self.clips_out.put((np.concatenate(frames), self.peak_db - self.floor_db))
            self.active = None
            self.loud = 0
            self.pre_roll.clear()


def save_clip(audio, path_base, rate, mp3, title):
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


def find_lame():
    # Prefer the encoder from this Python's environment (conda installs it there).
    local = os.path.join(os.path.dirname(sys.executable), "lame")
    return local if os.path.exists(local) else shutil.which("lame")


def main():
    p = argparse.ArgumentParser(description="Capture ATC transmissions as individual clips.")
    p.add_argument("freq", type=float, help="frequency in MHz, e.g. 120.1")
    p.add_argument("--clips", type=int, default=1, help="stop after this many clips (default 1)")
    p.add_argument("--max-time", type=float, default=600,
                   help="give up after this many seconds (default 600)")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above the noise floor that starts a clip (default 8)")
    p.add_argument("--hang", type=float, default=1.5,
                   help="seconds of quiet that end a clip (default 1.5)")
    p.add_argument("--pre-roll", type=float, default=0.3,
                   help="seconds kept from before the trigger (default 0.3)")
    p.add_argument("--min-length", type=float, default=1.0,
                   help="discard clips shorter than this many seconds (default 1)")
    p.add_argument("--max-length", type=float, default=30.0,
                   help="cut clips longer than this many seconds (default 30)")
    p.add_argument("--out-dir", default="clips", help="where to save clips (default clips/)")
    p.add_argument("--listen", action="store_true", help="also play the audio")
    p.add_argument("--no-mp3", action="store_true", help="save WAV only")
    sr.add_sdr_args(p)
    p.set_defaults(gain="40")
    args = p.parse_args()

    freq = args.freq * 1e6
    mode = sr.auto_mode(freq) if args.mode == "auto" else args.mode
    if mode == "wfm":
        sys.exit("Clip capture is for AM / NBFM channels; broadcast FM never goes quiet.")
    mp3 = None if args.no_mp3 else find_lame()
    if not args.no_mp3 and mp3 is None:
        print("note: `lame` not found, saving WAV only "
              "(conda install -c conda-forge lame)", file=sys.stderr)
    os.makedirs(args.out_dir, exist_ok=True)

    try:
        rx = sr.open_receiver(args, freq, mode)
    except RuntimeError as e:
        sys.exit(str(e))
    rx.set_muted(not args.listen)

    clips = queue.Queue()
    detector = ClipDetector(sr.AUDIO_RATE, args.threshold, args.hang, args.pre_roll,
                            args.min_length, args.max_length, clips)
    rx.connect(rx.mix, (detector, 0))
    rx.connect(rx.chans[mode], (detector, 1))

    print(f"Listening on {args.freq:.4f} MHz [{mode.upper()}], gain {args.gain} dB. "
          f"Waiting for {args.clips} transmission(s), up to {args.max_time:.0f} s. "
          "Ctrl-C to stop.")
    rx.start()
    start = time.time()
    saved = 0
    try:
        while saved < args.clips and time.time() - start < args.max_time:
            try:
                audio, snr = clips.get(timeout=1.0)
            except queue.Empty:
                state = ("calibrating" if detector.floor_db is None else
                         "RECORDING" if detector.active is not None else "waiting")
                floor = "" if detector.floor_db is None else f", floor {detector.floor_db:.1f} dB"
                print(f"\r  {time.time() - start:5.0f}s  {state:<11} "
                      f"level {detector.level_db:6.1f} dB{floor}   ", end="", flush=True)
                continue
            saved += 1
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = os.path.join(args.out_dir, f"{args.freq:.3f}MHz_{stamp}")
            title = f"{args.freq:.3f} MHz {mode.upper()} {stamp}"
            paths = save_clip(audio, base, sr.AUDIO_RATE, mp3, title)
            print(f"\n  clip {saved}/{args.clips}: {len(audio) / sr.AUDIO_RATE:.1f} s, "
                  f"peak {snr:.0f} dB above noise -> {', '.join(paths)}")
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        rx.wait()
    if saved < args.clips:
        print(f"\nStopped with {saved} of {args.clips} clip(s) after "
              f"{time.time() - start:.0f} s.")


if __name__ == "__main__":
    main()
