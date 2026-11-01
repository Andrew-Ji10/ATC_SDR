"""End-to-end test of the airband monitor with a simulated Pluto: window
rotation, squelch, clips, cleaned clips, raw files, log and summary."""

import argparse
import csv
import glob
import os
import time
import wave

import numpy as np
import pytest

from atcsdr import airband as A
from atcsdr import frequencies as fq


def old_receiver_speed(seconds=0.5):
    """How fast this computer runs atcsdr.airband's receiver on this test's busiest
    window, in x real time. The test streams in real time, and the fake radio makes
    its signal on the same computer, so it needs well over 1x (a Pi 5 manages ~1.3x;
    the whole-band receiver there is atcsdr.fastrx, tested in test_fastrx.py)."""
    rate, windows = A.plan(fq.area_channels("bay"), 7_680_000)
    w = max(windows, key=lambda w: len(w.channels))
    bank = A.FilterBank(rate, [f.mhz * 1e6 - w.center for f in w.channels])
    chans = [A.Channel(f) for f in w.channels]
    x = (np.random.default_rng(0).standard_normal(int(rate * seconds)) * 0.01).astype(np.complex64)
    t, now = time.perf_counter(), time.time()
    for block in bank.process(x):
        for ch, y in zip(chans, block):
            ch.process(y, now, now)
    return seconds / (time.perf_counter() - t)


class FakePluto:
    """Streams int16 IQ in real time around `center`, with AM transmissions on
    `talkers` (MHz -> (start s, length s) repeating every `period` s)."""

    def __init__(self, center, rate, talkers, period=6.0):
        self.center, self.rate, self.talkers, self.period = center, rate, talkers, period
        self.t0 = time.time()
        self.n = 0
        self.rng = np.random.default_rng(0)

    def tune(self, center):
        self.center = center

    def flush(self):
        pass

    def read(self, size=1 << 17):
        t = (self.n + np.arange(size)) / self.rate
        self.n += size
        x = (self.rng.normal(size=size) + 1j * self.rng.normal(size=size)) * 20
        for mhz, (start, length) in self.talkers.items():
            off = mhz * 1e6 - self.center
            if abs(off) > self.rate / 2 * A.USABLE:
                continue                          # not in this window
            on = ((t % self.period) >= start) & ((t % self.period) < start + length)
            x += on * 400 * (1 + 0.5 * np.sin(2 * np.pi * 600 * t)) * np.exp(2j * np.pi * off * t)
        # Real-time pace, like the hardware.
        time.sleep(max(0, self.t0 + self.n / self.rate - time.time()))
        out = np.empty(2 * size, np.int16)
        out[0::2], out[1::2] = x.real, x.imag
        return out

    def reconnect(self, stop):
        return 0.0


def test_bay_survey_with_window_rotation(tmp_path, monkeypatch):
    speed = old_receiver_speed()
    if speed < 2.0:
        pytest.skip(f"this computer runs the old receiver at {speed:.1f}x real time; "
                    "this real-time test needs 2x or more")
    talkers = {120.5: (1.0, 2.0),      # SFO Twr, window 1
               128.325: (0.5, 2.5),    # SFO Arr E, window 2
               134.5: (2.0, 1.5)}      # App East 2, window 3
    monkeypatch.setattr(A, "find_uri", lambda device="": "usb:fake")
    monkeypatch.setattr(A, "open_radio",
                        lambda uri, center, rate, gain: FakePluto(center, rate, talkers))
    out = tmp_path
    opts = argparse.Namespace(
        device="", gain="40", threshold=8.0, dwell=6.0, duration=19.0, listen=False,
        volume=0.5, mono=False, audio_device=None, record=None, raw=str(out / "raw"),
        clips=str(out / "clips"), clean=str(out / "clips_clean"), strength=0.9, no_mp3=True,
        log=str(out / "transmissions.csv"), summary=str(out / "summary.txt"))
    channels = A.monitor(fq.area_channels("bay"), opts)

    by_mhz = {c.mhz: c for c in channels}
    for mhz in talkers:                    # each talker heard while its window was up
        assert by_mhz[mhz].transmissions >= 1, mhz
    quiet = [c for c in channels if c.mhz not in talkers]
    assert sum(c.transmissions for c in quiet) == 0           # no false openings
    # Each channel was monitored roughly a third of the time (3 windows).
    for c in channels:
        assert 3 < c.monitored_s < 10, (c.name, c.monitored_s)

    rows = list(csv.DictReader(open(out / "transmissions.csv")))
    assert {float(r["mhz"]) for r in rows} <= set(talkers)
    clips = glob.glob(str(out / "clips" / "*.wav"))
    assert len(clips) == sum(c.transmissions for c in channels)
    assert len(glob.glob(str(out / "clips_clean" / "*.wav"))) == len(clips)
    # Raw files: one per channel, all the same length (the whole run).
    raws = glob.glob(str(out / "raw" / "*.wav"))
    assert len(raws) == len(channels)
    lengths = {wave.open(p).getnframes() for p in raws}
    assert len(lengths) == 1
    assert abs(max(lengths) / A.AUDIO_RATE - 19) < 1.5
    assert "3 of 43 channels had traffic" in open(out / "summary.txt").read()
