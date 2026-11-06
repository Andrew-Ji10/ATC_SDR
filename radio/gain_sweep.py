#!/usr/bin/env python3
"""
Gain sweep on live traffic: which RF gain hears the airband best with this
Pluto, antenna and spot?

    python3 radio/gain_sweep.py                                   # Pluto on USB, 10 min
    python3 radio/gain_sweep.py --device packed:192.168.3.2       # on the Pi: the whole band at once
    python3 radio/gain_sweep.py --gains 40 45 50 55 60 --duration 20m
    python3 radio/gain_sweep.py --slot 60 --duration 40m          # whole transmissions per gain, to listen to
    python3 radio/gain_sweep.py --source synthetic --duration 2m  # no radio, checks the analysis
    python3 radio/gain_sweep.py --reanalyze recordings/gain_sweep_<time>/ --threshold 10

Running each gain for its own few minutes would mostly measure who happened to
be talking. So this switches the gain every --slot seconds (2 s), in a fresh
random order each round, all through the run: every gain hears the same
traffic, and one transmission (or an ATIS, which never stops) is usually heard
at several gains.

For every transmission it measures the signal-to-noise ratio at each gain it
was heard at, against that gain's own noise floor, and fits
    SNR = (that transmission's strength) + (the gain's effect)
so a strong station and a weak one count alike. The result is how many dB more
(or less) above the noise traffic comes out at each gain than at --ref-gain
(40, the usual setting), with a standard error. Gain helps until the
antenna's own noise dominates the receiver's noise; past that, it lifts signal
and noise alike (no change), and too much of it overloads the ADC or makes
intermodulation (the SNR falls, and steady false signals, "spurs", appear).

Also per gain: the ADC level and clipping, the noise floor (and how fast it
rises with gain), how much of each transmission kept the squelch open, short
false openings (blips) per minute, and channels with a steady signal that
isn't an ATIS (spurs, interference).

The recommendation is the lowest gain within 1 dB of the best SNR that keeps
the ADC out of clipping: as good as the best, with more room for a strong
nearby transmitter.

Clips: every transmission is saved once per gain it was heard at, cut at
each gain change, named so the clips of one transmission sort together
(tx0012_SFO_Twr_120.500_143205_g40.mp3, ..._g50.mp3): the same voice at each
gain, to compare by ear. Short slots give the best numbers; long ones
(--slot 60 --duration 40m) give whole transmissions at one gain, to listen
to, with every gain still coming round each cycle. Clips need every frame
demodulated: a computer too slow for that at the whole band (a Pi at 21.6
MSPS) measures without clips; --area sfo or a lower --max-rate brings them back.

Writes, to recordings/gain_sweep_<time>/ (or --out): report.txt, per_gain.csv,
cells.csv (each transmission's SNR at each gain), slots.csv (every slot: gain,
window, ADC), clips/ and clips.csv, and levels.npz (every channel's level in every frame, so
--reanalyze can rerun the analysis, e.g. with another --threshold).
"""

import argparse
import json
import math
import queue
import shutil
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import scipy.fft
from scipy.signal import lfilter, resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atcsdr import airband as A
from atcsdr import audio as audiofile
from atcsdr import frequencies as fq
from atcsdr import pluto
from atcsdr.airband import parse_duration
from atcsdr.frequencies import Freq
from atcsdr.pluto import to_complex

CLIP_LEVEL = 2000            # |I| or |Q| at least this counts as clipped (12-bit, as int16)
CLIP_OK = 1e-4               # at most this fraction clipped, in every window, for headroom
RMS_OK = -15.0               # and the ADC level (dBFS) under this
SETTLE_READS = 1             # reads dropped after each gain change, past the flush
STEADY_DB = 6.0              # a channel floor this far above the window's median is a signal
HANG_S = 1.5                 # active frames closer than this belong to one transmission
MIN_SPAN_S = 1.0             # shorter openings are blips, not transmissions
MIN_CELL_FRAMES = 8          # frames of a transmission at one gain needed to measure it there
WEAK_DB = 15.0               # weak transmissions: under this SNR at the reference gain
NEAR_DB = 1.0                # "as good as the best": within this many dB
MIN_EVENTS = 5               # paired transmissions needed to trust the SNR fit
CPU_BUDGET = 0.3             # analyse at most this fraction of real time (the rest: reading)
TTY = sys.stdout.isatty()
CLEAR = "\r\033[K" if TTY else ""


def _terminate(*_):
    raise KeyboardInterrupt


# ------------------------------------------------------------------ measuring

class Channelizer:
    """The receiver's own channel filter (atcsdr.airband.FilterBank: overlap-save,
    flat to +-5 kHz, zero from +-7 kHz), one 16 ms frame at a time: each
    channel's level (dB re full scale, the squelch's measure) and, if asked,
    its AM audio, demodulated as the receiver does it: the envelope normalized
    by its carrier, so a strong and a weak station play equally loud and only
    the noise behind them differs. A frame is `n` samples; the next one starts
    `hop` samples later."""

    def __init__(self, rate, window):
        self.bank = A.FilterBank(rate, [c.mhz * 1e6 - window.center for c in window.channels])
        self.n, self.hop = self.bank.n, self.bank.hop
        self.frame_s = A.FRAME_S
        self.q = (A.OUT_BLOCK - A.KEEP) // 2
        self.a = 1 - math.exp(-1 / (0.05 * A.AUDIO_RATE))       # 50 ms carrier average
        self.zi = None
        self.offset_db = 20 * math.log10(self.bank.decim)      # the bank's output is decim x the input

    def reset(self):
        self.zi = None

    def process(self, raw, audio=True):
        """raw: one frame (2 * n int16, interleaved I/Q) -> (dB per channel,
        (channels, KEEP) audio or None)."""
        spectrum = scipy.fft.fft(to_complex(raw))
        out = scipy.fft.ifft(spectrum[self.bank.index] * self.bank.shape, axis=1)[:, self.q: self.q + A.KEEP]
        env = np.abs(out)
        level = (10 * np.log10(np.mean(env * env, axis=1) + 1e-20) - self.offset_db).astype(np.float32)
        if not audio:
            return level, None
        if self.zi is None:                                     # start from this frame's carrier
            self.zi = (1 - self.a) * env.mean(axis=1, keepdims=True)
        carrier, self.zi = lfilter([self.a], [1, self.a - 1], env, axis=1, zi=self.zi)
        return level, ((env - carrier) / np.maximum(carrier, 1e-6)).astype(np.float32)


def choose_stride(chz, audio=True, budget=CPU_BUDGET):
    """Analyse every Nth frame, so that analysis takes at most `budget` of real
    time and reading the radio never falls behind. Clips need every frame (1)."""
    raw = (np.random.default_rng(0).standard_normal(2 * chz.n) * 30).astype(np.int16)
    chz.process(raw, audio)                            # warm up the FFT plan
    t, k = time.perf_counter(), 6
    for _ in range(k):
        chz.process(raw, audio)
    chz.reset()
    per_frame = (time.perf_counter() - t) / k
    return max(1, math.ceil(per_frame / (chz.frame_s * budget)))


# ------------------------------------------------------------------ clips

class Clips:
    """Cuts every transmission into one clip per gain it was heard at, named so
    the clips of one transmission sort together:
        tx0012_SFO_Twr_120.500_143205_g40.mp3, tx0012_..._g50.mp3
    The squelch here is the receiver's (opens 2 frames above floor + threshold,
    closes after 1 s of quiet, keeps 0.3 s before), with a noise floor learned
    per gain (and per window). A clip also ends at every gain change, so each
    one is at a single gain. An ATIS gets --atis-clips clips per gain, of at
    most ATIS_CLIP_S; a traffic channel with a steady signal (a spur) none."""

    FLOOR_FRAMES = int(30 / A.FRAME_S)     # floor from the last 30 s heard at that gain
    CAL_FRAMES = int(1.0 / A.FRAME_S)      # no clips until a gain has been heard this long
    PRE = int(A.PRE_ROLL_S / A.FRAME_S)
    HANG = int(A.HANG_S / A.FRAME_S)
    TX_GAP_S = 2.5                         # clips this close (across a gain change): one transmission
    ATIS_CLIP_S = 15.0

    def __init__(self, windows, gains, threshold, saver, atis_per_gain=1):
        self.windows, self.gains, self.threshold, self.saver = windows, gains, threshold, saver
        self.atis_per_gain = atis_per_gain
        self.hist = {}
        self.floor = {}
        self.last = {}                     # (window, channel) -> (end time, tx id)
        self.atis_tx = {}
        self.atis_saved = {}
        self.next_tx = 0
        self.saved = 0

    def start_slot(self, wi, gi):
        self.wi, self.gi, self.age = wi, gi, 0
        self.hist.setdefault((wi, gi), deque(maxlen=self.FLOOR_FRAMES))
        self.st = [dict(pre=deque(maxlen=self.PRE), seg=None, loud=0, quiet=0)
                   for _ in self.windows[wi].channels]

    def _floors(self):
        lv = np.array(self.hist[self.wi, self.gi])
        own = np.percentile(lv, 20, axis=0)
        atis = np.array([c.kind == "atis" for c in self.windows[self.wi].channels])
        med = float(np.median(own[~atis])) if (~atis).any() else float(np.median(own))
        steady = (own > med + STEADY_DB) | atis
        self.floor[self.wi, self.gi] = (np.where(steady, med, own), steady & ~atis)

    def frame(self, t, level, audio):
        key = (self.wi, self.gi)
        h = self.hist[key]
        h.append(level)
        self.age += 1
        if len(h) >= self.CAL_FRAMES and (key not in self.floor or self.age % 60 == 0):
            self._floors()
        chans = self.windows[self.wi].channels
        if key not in self.floor:
            for s, a in zip(self.st, audio):
                s["pre"].append(a)
            return
        floor, spur = self.floor[key]
        for ci, (s, ch) in enumerate(zip(self.st, chans)):
            d = level[ci] - floor[ci]
            if spur[ci]:
                pass
            elif s["seg"] is None:
                s["loud"] = s["loud"] + 1 if d > self.threshold else 0
                if s["loud"] >= 2:
                    s["seg"] = dict(start=t - len(s["pre"]) * A.FRAME_S, chunks=list(s["pre"]), levels=[])
                    s["quiet"] = 0
            else:
                s["seg"]["chunks"].append(audio[ci])
                s["seg"]["levels"].append(level[ci])
                s["quiet"] = s["quiet"] + 1 if d < self.threshold - A.HYSTERESIS_DB else 0
                too_long = ch.kind == "atis" and len(s["seg"]["chunks"]) * A.FRAME_S >= self.ATIS_CLIP_S
                if s["quiet"] >= self.HANG or too_long:
                    self._close(ci, cut=False)
            s["pre"].append(audio[ci])

    def end_slot(self):
        for ci in range(len(self.st)):
            self._close(ci, cut=True)

    def _close(self, ci, cut):
        s = self.st[ci]
        seg, s["seg"], s["loud"] = s["seg"], None, 0
        if seg is None:
            return
        chunks = seg["chunks"][: max(1, len(seg["chunks"]) - s["quiet"] + A.TAIL_FRAMES)]
        s["quiet"] = 0
        seconds = len(chunks) * A.FRAME_S
        if seconds < A.MIN_CLIP_S or not seg["levels"]:
            return
        wi, gi, ch = self.wi, self.gi, self.windows[self.wi].channels[ci]
        if ch.kind == "atis":
            k = (wi, ci, gi)
            if self.atis_saved.get(k, 0) >= self.atis_per_gain:
                return
            self.atis_saved[k] = self.atis_saved.get(k, 0) + 1
            if (wi, ci) not in self.atis_tx:
                self.atis_tx[wi, ci], self.next_tx = self.next_tx, self.next_tx + 1
            tx = self.atis_tx[wi, ci]
        else:
            prev = self.last.get((wi, ci))
            if prev and seg["start"] - prev[0] < self.TX_GAP_S:
                tx = prev[1]
            else:
                tx, self.next_tx = self.next_tx, self.next_tx + 1
        self.last[wi, ci] = (seg["start"] + seconds, tx)
        floor = self.floor[wi, gi][0][ci]
        mean_db = 10 * math.log10(float(np.mean(10 ** (np.array(seg["levels"], np.float64) / 10))))
        self.saved += 1
        self.saver.put(dict(tx=tx, window=wi + 1, mhz=ch.mhz, name=ch.name, kind=ch.kind,
                            gain=self.gains[gi], start=seg["start"], seconds=seconds,
                            snr_db=snr_db(mean_db, floor), cut=int(cut)), np.concatenate(chunks))


class ClipSaver(threading.Thread):
    """Writes the clips (MP3 at 16 kHz, or WAV without lame) and clips.csv off
    the radio's thread."""

    def __init__(self, folder, log):
        super().__init__(daemon=True)
        self.folder, self.q = Path(folder), queue.Queue()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lame = audiofile.find_lame()
        self.log = open(log, "w", buffering=1)
        self.log.write("tx,window,mhz,name,kind,gain,time,seconds,snr_db,cut_at_gain_change,file\n")
        self.rows = []

    def put(self, row, audio):
        self.q.put((row, audio))

    def run(self):
        while (item := self.q.get()) is not None:
            row, audio = item
            when = datetime.fromtimestamp(row["start"])
            stem = (f"tx{row['tx']:04d}_{row['name'].replace(' ', '_')}_{row['mhz']:.3f}_{when:%H%M%S}"
                    f"_g{row['gain']:g}")
            title = f"{row['name']} {row['mhz']:.3f} MHz, gain {row['gain']:g} dB, {when:%H:%M:%S}"
            path = audiofile.save_mp3(resample_poly(audio, 1, 3), str(self.folder / f"{stem}.mp3"), 16_000,
                                      self.lame, title, f"gain sweep, transmission {row['tx']}")
            row = dict(row, file=Path(path).name, time=f"{when:%Y-%m-%d %H:%M:%S}")
            self.rows.append(row)
            self.log.write(f"{row['tx']},{row['window']},{row['mhz']:.3f},{row['name']},{row['kind']},"
                           f"{row['gain']:g},{row['time']},{row['seconds']:.1f},{row['snr_db']:.1f},"
                           f"{row['cut']},{row['file']}\n")
        self.log.close()

    def finish(self):
        self.q.put(None)
        self.join()
        return self.rows


# ------------------------------------------------------------------ synthetic radio

class SyntheticRadio:
    """A Pluto stand-in whose gain matters the way a real one's does: the
    antenna's noise and the stations scale with gain, the receiver's own noise
    (ADC_DBFS) doesn't, so SNR rises with gain until the antenna's noise
    dominates (about KNEE_GAIN), then levels off; a strong carrier off any
    channel (BLOCKER) clips the ADC at the top gains, which spreads its power
    over the band and lowers every channel's SNR. realtime=False runs as fast as
    the CPU allows, on its own clock (clock())."""

    READ = 1 << 17
    ADC_DBFS = -45.0                 # the receiver's own noise, whatever the gain
    KNEE_GAIN = 45.0                 # gain at which the antenna noise equals it
    BLOCKER_FS_GAIN = 66.0           # gain at which the blocker reaches full scale
    # MHz, every s, talk s, SNR (dB) once antenna noise dominates, tone Hz
    STATIONS = [(120.500, 9, 3, 10, 600), (121.800, 13, 4, 25, 800), (118.850, 1, 1, 18, 450)]
    BLOCKER_MHZ = 119.500

    def __init__(self, center, rate, gain=40.0, realtime=True, seed=0):
        self.center, self.rate, self.realtime = center, rate, realtime
        self.gain = float(gain)
        rng = np.random.default_rng(seed)
        self.rng = rng
        n = self.READ
        self.pool = [((rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2)).astype(np.complex64)
                     for _ in range(8)]
        self.t = self.t0 = time.time()

    def clock(self):
        return time.time() if self.realtime else self.t

    def _noise(self):
        return self.pool[self.rng.integers(len(self.pool))]

    def read(self):
        n = self.READ
        if self.realtime:
            now = time.time()
            if now - self.t > 0.5:
                self.t = now
            time.sleep(max(0.0, self.t - now))
        t = (self.t - self.t0) + np.arange(n) / self.rate
        self.t += n / self.rate
        ant = 10 ** ((self.ADC_DBFS + self.gain - self.KNEE_GAIN) / 20)      # antenna noise rms
        x = self._noise() * np.float32(ant) + self._noise() * np.float32(10 ** (self.ADC_DBFS / 20))
        share = 12e3 / self.rate                                             # a channel's share of the noise
        for mhz, every, talk, snr, tone in self.STATIONS:
            off = mhz * 1e6 - self.center
            if abs(off) < self.rate / 2:
                on = (t % every) < talk
                a = ant * math.sqrt(share) * 10 ** (snr / 20)
                x = x + (on * a * (1 + 0.5 * np.cos(2 * np.pi * tone * t))
                         * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        off = self.BLOCKER_MHZ * 1e6 - self.center
        if abs(off) < self.rate / 2:
            x = x + (10 ** ((self.gain - self.BLOCKER_FS_GAIN) / 20)
                     * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        raw = np.empty(2 * n, np.int16)
        raw[0::2] = np.clip(x.real * 2048, -2047, 2047)
        raw[1::2] = np.clip(x.imag * 2048, -2047, 2047)
        return raw

    def set_gain(self, gain):
        self.gain = float(gain)

    def tune(self, center):
        self.center = center
        self.flush()

    def flush(self):
        if self.realtime:
            time.sleep(0.3)
        else:
            self.t += 0.3            # about what a real flush costs

    def close(self):
        pass

    def reconnect(self, stop):
        return 0.0


# ------------------------------------------------------------------ the sweep

class Sweep:
    """Rotates the gain every slot, through every capture window, and keeps
    every analysed frame's level for each channel."""

    def __init__(self, radio, rate, windows, gains, slot_s, seed=0, stride=None, clips=None):
        self.radio, self.rate, self.windows, self.gains = radio, rate, windows, list(gains)
        self.slot_s = slot_s
        self.clock = getattr(radio, "clock", time.time)
        self.meters = [Channelizer(rate, w) for w in windows]
        self.stride = stride or choose_stride(self.meters[0], audio=clips is not None)
        self.clips = clips if self.stride == 1 else None       # clips need every frame
        self.rng = np.random.default_rng(seed)
        self.frames = [dict(level=[], t=[], gain=[], slot=[]) for _ in windows]
        self.slots = []              # dict per slot: window, gain, t, seconds, frames, adc_rms_dbfs, clipped
        self.rounds = 0
        self.reconnects = 0
        self.never = _Never()

    def read(self):
        while True:
            try:
                return self.radio.read()
            except OSError as e:
                print(f"{CLEAR}radio: {e}; reconnecting...", flush=True)
                gone = self.radio.reconnect(self.never)
                self.reconnects += 1
                print(f"{CLEAR}radio back after {gone:.1f} s.", flush=True)
                raise _SlotLost() from None

    def run(self, deadline=None, status=None):
        """Until `deadline` (clock time), or forever. Ctrl-C stops it; what was
        measured so far is kept."""
        while True:
            for wi, w in enumerate(self.windows):
                if len(self.windows) > 1 or not self.slots:
                    self.radio.tune(w.center)
                for gi in self.rng.permutation(len(self.gains)):
                    if deadline and self.clock() >= deadline:
                        return
                    try:
                        self.slot(wi, int(gi), deadline, status)
                    except _SlotLost:
                        self.radio.tune(w.center)
            self.rounds += 1

    def slot(self, wi, gi, deadline, status):
        chz, fr, s, clips = self.meters[wi], self.frames[wi], len(self.slots), self.clips
        self.radio.set_gain(self.gains[gi])
        self.radio.flush()
        for _ in range(SETTLE_READS):
            self.read()
        chz.reset()
        if clips:
            clips.start_slot(wi, gi)
        t0 = self.clock()
        carry = np.zeros(0, np.int16)
        k = clipped = total = 0
        power = 0.0
        need, step = 2 * chz.n, 2 * chz.hop
        try:
            while self.clock() - t0 < self.slot_s and not (deadline and self.clock() >= deadline):
                carry = np.concatenate([carry, self.read()])
                while len(carry) >= need:
                    frame, carry = carry[:need], carry[step:]
                    if k % self.stride == 0:
                        level, audio = chz.process(frame, audio=clips is not None)
                        t = t0 + k * chz.frame_s
                        fr["level"].append(level)
                        fr["t"].append(t)
                        fr["gain"].append(gi)
                        fr["slot"].append(s)
                        if clips:
                            clips.frame(t, level, audio)
                        new = frame[:step]                     # each sample counted once
                        clipped += int(np.count_nonzero(np.abs(new) >= CLIP_LEVEL))
                        total += len(new)
                        f32 = new.astype(np.float32)
                        power += float(f32 @ f32)
                    k += 1
                if status:
                    status(self, wi, gi)
        finally:
            if clips:
                clips.end_slot()
        rms = math.sqrt(power / max(total, 1)) / 2048
        self.slots.append(dict(window=wi, gain=self.gains[gi], t=t0, seconds=self.clock() - t0,
                               frames=total // step, adc_rms_dbfs=20 * math.log10(rms + 1e-12),
                               clipped=clipped / max(total, 1)))

    def data(self):
        """Everything measured, as plain arrays (see save_levels())."""
        out = []
        for w, fr in zip(self.windows, self.frames):
            nch = len(w.channels)
            out.append(dict(level=np.array(fr["level"], np.float32).reshape(-1, nch),
                            t=np.array(fr["t"], np.float64), gain=np.array(fr["gain"], np.int16),
                            slot=np.array(fr["slot"], np.int32), channels=list(w.channels)))
        return out


class _Never:
    """A stop flag that's never set: reconnect until it works (Ctrl-C ends it)."""

    def is_set(self):
        return False


class _SlotLost(Exception):
    pass


# ------------------------------------------------------------------ analysis

def floors(level, gain_idx, n_gains, atis):
    """Per gain, each channel's noise floor (dB) and whether it carries a steady
    signal. The floor is the 20th percentile of the channel's level, as the
    squelch measures it, except for a channel well above the window's median
    (STEADY_DB), whose own floor is its signal: there the median stands in. An
    ATIS is always taken as steady (it never stops)."""
    nch = level.shape[1]
    floor = np.full((n_gains, nch), np.nan)
    steady = np.zeros((n_gains, nch), bool)
    median = np.full(n_gains, np.nan)
    for g in range(n_gains):
        sel = gain_idx == g
        if sel.sum() < 20:
            continue
        own = np.percentile(level[sel], 20, axis=0)
        med = float(np.median(own[~atis])) if (~atis).any() else float(np.median(own))
        st = (own > med + STEADY_DB) | atis
        floor[g] = np.where(st, med, own)
        steady[g], median[g] = st, med
    return floor, steady, median


def spans(times, hang=HANG_S):
    """Group sorted times into runs with gaps under `hang`: [(first, last)]."""
    if len(times) == 0:
        return []
    cut = np.nonzero(np.diff(times) > hang)[0]
    starts = np.r_[0, cut + 1]
    ends = np.r_[cut, len(times) - 1]
    return [(times[a], times[b]) for a, b in zip(starts, ends)]


def snr_db(mean_power_db, floor_db):
    """Signal-to-noise from (signal + noise) and noise, in dB (floored at -10)."""
    ratio = 10 ** ((mean_power_db - floor_db) / 10) - 1
    return 10 * math.log10(max(ratio, 0.1))


def measure(data, gains, threshold, slots=()):
    """Per window, floors; and every transmission's SNR at each gain it was heard
    at ("cells"; a cell is "clipped" if any of it comes from a slot whose ADC
    clipped more than CLIP_OK). Returns (cells, per-window floor info, blips per
    gain, spur channels per gain)."""
    G = len(gains)
    over = np.array([s["clipped"] > CLIP_OK for s in slots] + [False], bool)
    cells, info = [], []
    blips = np.zeros(G)
    spurs = [set() for _ in range(G)]
    event = 0
    for wi, d in enumerate(data):
        level, t, gidx, chans = d["level"], d["t"], d["gain"], d["channels"]
        atis = np.array([c.kind == "atis" for c in chans])
        floor, steady, median = floors(level, gidx, G, atis)
        info.append(dict(floor=floor, steady=steady, median=median))
        if not len(t):
            continue
        fl = floor[gidx]                                     # (frames, channels)
        active = (level - fl) > threshold

        def cell(ci, ch, lo, hi, a, b, g):
            m = gidx[lo:hi] == g
            if m.sum() < MIN_CELL_FRAMES or np.isnan(floor[g, ci]):
                return
            lv = level[lo:hi, ci][m]
            slot = d["slot"][lo:hi][m]
            mean_db = 10 * math.log10(float(np.mean(10 ** (lv.astype(np.float64) / 10))))
            cells.append(dict(event=event, window=wi, mhz=ch.mhz, name=ch.name, kind=ch.kind,
                              start=float(a), seconds=float(b - a), gain=g, frames=int(m.sum()),
                              snr_db=snr_db(mean_db, floor[g, ci]),
                              open_frac=float(active[lo:hi, ci][m].mean()),
                              clipped=bool(over[np.clip(slot, 0, len(over) - 1)].any())))

        for ci, ch in enumerate(chans):
            if ch.kind == "atis":
                # It never stops: once it's received at some gain, measure it at
                # every gain, including those where it's too weak for the squelch.
                received = any(active[gidx == g, ci].mean() > 0.5 for g in range(G) if (gidx == g).any())
                if received:
                    for g in range(G):
                        cell(ci, ch, 0, len(t), t[0], t[-1], g)
                    event += 1
                continue
            # A steady signal on a traffic channel (a spur, interference) at some
            # gains: those gains don't count for this channel, the others still do.
            bad = steady[:, ci]
            for g in np.nonzero(bad)[0]:
                spurs[g].add(ch)
            act = np.nonzero(active[:, ci] & ~bad[gidx])[0]
            for a, b in spans(t[act]):
                if b - a < MIN_SPAN_S:
                    blips[gidx[act[np.searchsorted(t[act], a)]]] += 1
                    continue
                lo, hi = np.searchsorted(t, a), np.searchsorted(t, b, side="right")
                for g in range(G):
                    if not bad[g]:
                        cell(ci, ch, lo, hi, a, b, g)
                event += 1
    return cells, info, blips, spurs


def fit(cells, n_gains, ref):
    """Least squares SNR[event, gain] = strength[event] + effect[gain], with
    effect[ref] = 0, on the events heard at two gains or more, from the cells
    whose ADC didn't clip: an overloaded ADC's readings are garbage, and through
    the events they share they would skew every other gain's estimate. A gain
    heard only clipped is then scored on its own against those strengths
    (overloaded[gain] = True). Returns (effect, standard error, events per gain,
    strength per event id, overloaded)."""
    effect, se, n, strength = _fit([c for c in cells if not c.get("clipped")], n_gains, ref)
    overloaded = np.zeros(n_gains, bool)
    for g in range(n_gains):
        if not np.isnan(effect[g]):
            continue
        r = [c["snr_db"] - strength[c["event"]] - effect[ref] for c in cells
             if c["gain"] == g and c.get("clipped") and c["event"] in strength]
        if r and not np.isnan(effect[ref]):
            effect[g] = float(np.mean(r))
            se[g] = float(np.std(r, ddof=1) / math.sqrt(len(r))) if len(r) > 1 else np.nan
            n[g], overloaded[g] = len(r), True
    return effect, se, n, strength, overloaded


def _fit(cells, n_gains, ref):
    by_event = {}
    for c in cells:
        by_event.setdefault(c["event"], []).append(c)
    events = [e for e, cs in by_event.items() if len({c["gain"] for c in cs}) >= 2]
    nan = np.full(n_gains, np.nan)
    if not events:
        return nan, nan, np.zeros(n_gains, int), {}
    rows = [c for e in events for c in by_event[e]]
    col = {e: i for i, e in enumerate(events)}
    others = [g for g in range(n_gains) if g != ref]
    gcol = {g: len(events) + i for i, g in enumerate(others)}
    X = np.zeros((len(rows), len(events) + len(others)))
    y = np.array([c["snr_db"] for c in rows])
    for r, c in enumerate(rows):
        X[r, col[c["event"]]] = 1
        if c["gain"] != ref:
            X[r, gcol[c["gain"]]] = 1
    used = X.any(axis=0)                                  # gains never heard drop out
    beta, *_ = np.linalg.lstsq(X[:, used], y, rcond=None)
    full = np.zeros(X.shape[1])
    full[used] = beta
    dof = len(rows) - int(used.sum())
    resid = y - X[:, used] @ beta
    s2 = float(resid @ resid) / dof if dof > 0 else np.nan
    cov = np.linalg.pinv(X[:, used].T @ X[:, used]) * s2
    se_full = np.full(X.shape[1], np.nan)
    se_full[used] = np.sqrt(np.clip(np.diag(cov), 0, None))
    effect, se = nan.copy(), nan.copy()
    n = np.zeros(n_gains, int)
    for c in rows:
        n[c["gain"]] += 1
    heard_ref = n[ref] > 0
    for g in range(n_gains):
        if g == ref:
            effect[g], se[g] = (0.0, 0.0) if heard_ref else (np.nan, np.nan)
        elif used[gcol[g]] and heard_ref:
            effect[g], se[g] = full[gcol[g]], se_full[gcol[g]]
    strength = {e: full[col[e]] for e in events}
    return effect, se, n, strength


def analyze(data, slots, gains, threshold, ref_gain):
    """The per-gain table and the recommendation. Returns (rows, cells, notes)."""
    G = len(gains)
    ref = int(np.argmin([abs(g - ref_gain) for g in gains]))
    cells, info, blips, spurs = measure(data, gains, threshold, slots)
    effect, se, n_all, strength, overloaded = fit(cells, G, ref)
    weak_events = {e for e, s in strength.items() if s < WEAK_DB}
    weak, weak_se, n_weak, _, _ = fit([c for c in cells if c["event"] in weak_events], G, ref)
    rows = []
    for g in range(G):
        sl = [s for s in slots if s["gain"] == gains[g]]
        minutes = sum(s["seconds"] for s in sl) / 60
        floor_med = np.nanmedian([i["median"][g] for i in info]) if info else np.nan
        cg = [c for c in cells if c["gain"] == g]
        rows.append(dict(
            gain=gains[g], minutes=minutes,
            adc_rms_dbfs=max((s["adc_rms_dbfs"] for s in sl), default=np.nan),
            clipped=max((s["clipped"] for s in sl), default=np.nan),
            floor_db=float(floor_med),
            snr_vs_ref=effect[g], snr_se=se[g], n=int(n_all[g]), overloaded=bool(overloaded[g]),
            weak_vs_ref=weak[g], weak_se=weak_se[g], n_weak=int(n_weak[g]),
            open_pct=100 * float(np.average([c["open_frac"] for c in cg], weights=[c["frames"] for c in cg]))
            if cg else np.nan,
            blips_per_min=blips[g] / minutes if minutes else np.nan,
            spurs=len(spurs[g]), spur_names=sorted(f"{c.name} {c.mhz:g}" for c in spurs[g])))
    for i in range(G - 1):
        a, b = rows[i], rows[i + 1]
        rows[i]["slope"] = (b["floor_db"] - a["floor_db"]) / (b["gain"] - a["gain"])
    if rows:
        rows[-1]["slope"] = np.nan
    return rows, cells, recommend(rows, cells, gains, ref)


def recommend(rows, cells, gains, ref):
    """(gain or None, lines of explanation)."""
    headroom = [r for r in rows if r["clipped"] <= CLIP_OK and r["adc_rms_dbfs"] < RMS_OK]
    events = len({c["event"] for c in cells})
    paired = max((r["n"] for r in rows), default=0)
    lines = []
    if not headroom:
        lines.append("The ADC clips even at the lowest gain tried: a very strong signal is in the band "
                     "(a transmitter nearby?). Try lower gains (--gains 10 20 30), or an airband filter.")
        return None, lines
    usable = [r for r in headroom if not np.isnan(r["snr_vs_ref"]) and not r["overloaded"]]
    if len(usable) >= 2 and paired >= MIN_EVENTS:
        best = max(usable, key=lambda r: r["snr_vs_ref"])
        pick = min((r for r in usable if r["snr_vs_ref"] >= best["snr_vs_ref"] - NEAR_DB),
                   key=lambda r: r["gain"])
        lines.append(f"Best SNR with ADC headroom: {best['gain']:g} dB ({best['snr_vs_ref']:+.1f} dB vs "
                     f"{gains[ref]:g}). The lowest gain within {NEAR_DB:g} dB of it: {pick['gain']:g} dB.")
        over = [r for r in rows if r not in headroom]
        if over:
            lines.append("No ADC headroom (clipping, or too hot): " + ", ".join(f"{r['gain']:g}" for r in over)
                         + " dB.")
        if pick["gain"] == max(r["gain"] for r in rows) and pick in headroom:
            lines.append("That's the highest gain tried: SNR may still rise above it. Try higher gains "
                         f"(--gains {pick['gain']:g} {pick['gain'] + 5:g} {pick['gain'] + 10:g} ...).")
        return pick["gain"], lines
    # Too little traffic for the paired fit: fall back on the noise floor's knee.
    lines.append(f"Too little traffic to compare SNR ({events} transmissions, {paired} heard at two gains or "
                 "more): this falls back on the noise floor alone. Run longer (--duration 30m) or at a "
                 "busier time.")
    knee = next((r["gain"] for r in rows if not np.isnan(r.get("slope", np.nan)) and r["slope"] >= 0.7), None)
    if knee is None:
        pick = max(headroom, key=lambda r: r["gain"])["gain"]
        lines.append(f"The receiver's own noise dominates at every gain tried: the highest with headroom, "
                     f"{pick:g} dB.")
    else:
        want = knee + 6
        pick = min([r["gain"] for r in headroom if r["gain"] >= want] or [max(r["gain"] for r in headroom)])
        lines.append(f"The antenna's noise dominates from about {knee:g} dB: {pick:g} dB.")
    return pick, lines


# ------------------------------------------------------------------ report

def _f(x, fmt, width):
    return f"{'-':>{width}}" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:{fmt}}".rjust(width)


def report(rows, cells, rec, gains, ref_gain, header):
    gain, why = rec
    ref = gains[int(np.argmin([abs(g - ref_gain) for g in gains]))]
    out = list(header) + [""]
    out.append(f"{'gain':>5} {'min':>4} {'ADC rms':>8} {'clipped':>9} {'floor':>6} {'slope':>6}  "
               f"{'SNR vs ' + format(ref, 'g'):>12} {'(n)':>5}  {'weak':>12} {'(n)':>4}  "
               f"{'open':>5} {'blips/min':>9} {'spurs':>5}")
    for r in rows:
        def pm(x, e):
            star = "*" if r["overloaded"] else ""
            return "-" if np.isnan(x) else "ref" if r["gain"] == ref else f"{star}{x:+.1f} ±{e:.1f}"
        snr, weak = pm(r["snr_vs_ref"], r["snr_se"]), pm(r["weak_vs_ref"], r["weak_se"])
        mark = "  <-" if gain is not None and r["gain"] == gain else ""
        out.append(f"{r['gain']:5g} {r['minutes']:4.1f} {_f(r['adc_rms_dbfs'], '.1f', 8)} "
                   f"{_f(100 * r['clipped'], '.4f', 8)}% {_f(r['floor_db'], '.1f', 6)} "
                   f"{_f(r.get('slope', np.nan), '.2f', 6)}  {snr:>12} {r['n']:>5}  {weak:>12} {r['n_weak']:>4}  "
                   f"{_f(r['open_pct'], '.0f', 4)}% {_f(r['blips_per_min'], '.1f', 9)} {r['spurs']:5d}{mark}")
    out += [
        "",
        "min: minutes listened at that gain. ADC rms (dBFS) and clipped: the worst slot. floor: median channel",
        "noise floor, relative dB; slope: dB of floor per dB of gain to the next row (near 0: the receiver's own",
        "noise dominates, so more gain helps; near 1: the antenna's does, so it no longer does).",
        f"SNR vs {ref:g}: dB more above the noise than at {ref:g} dB, on the same transmissions (± standard",
        f"error; n: transmissions heard at that gain). weak: only transmissions under {WEAK_DB:g} dB SNR, the ones",
        "the gain decides whether you hear. open: the share of each transmission the squelch stayed open.",
        "blips: openings too short to be a transmission. spurs: channels with a steady non-ATIS signal.",
        "SNR is fitted only on slots whose ADC didn't clip; * marks a gain heard only clipped, scored apart.",
    ]
    spur_list = sorted({s for r in rows for s in r["spur_names"]})
    if spur_list:
        out.append("Steady signals (interference, or a stuck carrier): " + ", ".join(
            f"{s} (at {', '.join(format(r['gain'], 'g') for r in rows if s in r['spur_names'])})"
            for s in spur_list))
    events = {}
    for c in cells:
        events.setdefault((c["name"], c["mhz"], c["kind"]), set()).add(c["event"])
    traffic = sorted(((k, len(v)) for k, v in events.items() if k[2] != "atis"), key=lambda kv: -kv[1])
    atis = [k for k in events if k[2] == "atis"]
    out.append("")
    out.append(f"Heard: {sum(n for _, n in traffic)} transmissions on {len(traffic)} channels"
               + (": " + ", ".join(f"{k[0]} {n}" for k, n in traffic[:12]) if traffic else "")
               + (" ..." if len(traffic) > 12 else ""))
    if atis:
        out.append("ATIS (steady, so measured at every gain): SNR in dB")
        out.append("  " + " " * 20 + "".join(f"{g:>7g}" for g in gains))
        for name, mhz, _ in sorted(atis, key=lambda k: k[1]):
            per = {}
            for c in cells:
                if c["name"] == name and c["mhz"] == mhz:
                    per.setdefault(c["gain"], []).append(c["snr_db"])
            out.append(f"  {name:<11} {mhz:8.3f}" + "".join(
                _f(float(np.mean(per[g])) if g in per else np.nan, ".1f", 7) for g in range(len(gains))))
    out += ["", "== Recommendation"] + why
    if gain is not None:
        out.append(f"-> --gain {gain:g}  (radio/record.py, radio/atc_listen.py, pi/record.sh)")
    return "\n".join(out)


def clip_summary(rows, gains):
    """Clips per gain, and the transmissions heard at several gains (the A/B
    sets worth listening to)."""
    out = ["== Clips (clips/, listed in clips.csv)"]
    if not rows:
        return "\n".join(out + ["None saved: no transmission was heard."])
    per = {g: sum(1 for r in rows if r["gain"] == g) for g in gains}
    out.append("Per gain: " + ", ".join(f"{g:g} dB: {n}" for g, n in per.items()))
    tx = {}
    for r in rows:
        tx.setdefault(r["tx"], []).append(r)
    multi = sorted((v for v in tx.values() if len({r["gain"] for r in v}) >= 2),
                   key=lambda v: (-len({r["gain"] for r in v}), v[0]["start"]))
    out.append(f"{len(multi)} of {len(tx)} transmissions have clips at two gains or more: the same voice at each "
               "gain, so the gain is the only difference. They sort together by name (txNNNN_...). Best sets:")
    for v in multi[:10]:
        r0 = v[0]
        out.append(f"  tx{r0['tx']:04d} {r0['name']:<11} {r0['mhz']:8.3f}  "
                   + ", ".join(f"{r['gain']:g} dB ({r['snr_db']:.0f} dB SNR)"
                               for r in sorted(v, key=lambda r: r["gain"])))
    out.append("Clips play equally loud (as the receiver's do): listen for the hiss behind the voice, and for "
               "distortion or other stations bleeding in at the high gains.")
    return "\n".join(out)


def write_csvs(out, rows, cells, slots):
    with open(out / "per_gain.csv", "w") as f:
        keys = ["gain", "minutes", "adc_rms_dbfs", "clipped", "floor_db", "slope", "snr_vs_ref", "snr_se", "n",
                "weak_vs_ref", "weak_se", "n_weak", "open_pct", "blips_per_min", "spurs"]
        f.write(",".join(keys) + "\n")
        for r in rows:
            f.write(",".join(f"{r.get(k, np.nan):.6g}" for k in keys) + "\n")
    with open(out / "cells.csv", "w") as f:
        keys = ["event", "window", "mhz", "name", "kind", "start", "seconds", "gain", "frames", "snr_db", "open_frac"]
        f.write(",".join(keys) + "\n")
        for c in cells:
            f.write(",".join(str(round(c[k], 4)) if isinstance(c[k], float) else str(c[k]) for k in keys) + "\n")
    with open(out / "slots.csv", "w") as f:
        f.write("time,window,gain,seconds,frames,adc_rms_dbfs,clipped\n")
        for s in slots:
            f.write(f"{datetime.fromtimestamp(s['t']):%Y-%m-%d %H:%M:%S.%f},{s['window'] + 1},{s['gain']:g},"
                    f"{s['seconds']:.2f},{s['frames']},{s['adc_rms_dbfs']:.2f},{s['clipped']:.6f}\n")


def save_levels(path, data, slots, gains, meta):
    arrays = {}
    for i, d in enumerate(data):
        for k in ("level", "t", "gain", "slot"):
            arrays[f"w{i}_{k}"] = d[k]
    meta = dict(meta, gains=list(gains), slots=slots,
                windows=[[[c.mhz, c.name, c.kind] for c in d["channels"]] for d in data])
    np.savez_compressed(path, meta=np.array(json.dumps(meta)), **arrays)


def load_levels(path):
    z = np.load(path)
    meta = json.loads(str(z["meta"]))
    data = []
    for i, chans in enumerate(meta["windows"]):
        d = {k: z[f"w{i}_{k}"] for k in ("level", "t", "gain", "slot")}
        d["channels"] = [Freq(m, n, k) for m, n, k in chans]
        data.append(d)
    return data, meta


# ------------------------------------------------------------------ main

def status_line(sweep, start, duration):
    def show(sw, wi, gi):
        if not TTY:
            return
        lo, hi = sw.windows[wi].span()
        fr = sw.frames[wi]
        last = fr["level"][-1] if fr["level"] else None
        elapsed = sw.clock() - start
        parts = [f"round {sw.rounds + 1}", f"gain {sw.gains[gi]:g} dB"]
        if len(sw.windows) > 1:
            parts.append(f"window {wi + 1}/{len(sw.windows)} {lo:.1f}-{hi:.1f} MHz")
        if sw.slots:
            parts.append(f"ADC {sw.slots[-1]['adc_rms_dbfs']:+.0f} dBFS")
        if last is not None:
            med = float(np.median(last))
            loud = [f"{c.name} {lv - med:+.0f}" for c, lv in zip(sw.windows[wi].channels, last) if lv - med > 10]
            parts.append("  ".join(loud[:4]) or "quiet")
        parts.append(f"{elapsed:.0f}" + (f"/{duration:.0f} s" if duration else " s"))
        print(CLEAR + "  |  ".join(parts)[: shutil.get_terminal_size((120, 20)).columns - 1], end="", flush=True)
    return show


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    names = sorted(set(fq.PRESETS) | set(fq.AREAS))
    p.add_argument("--device", default="", help="the radio, e.g. packed:192.168.3.2 or ip:192.168.3.2 "
                                                "(default: a Pluto on USB)")
    p.add_argument("--source", choices=["radio", "synthetic"], default="radio", help="default radio")
    p.add_argument("--area", choices=names, default="bay", help="channel set, ATIS included (default bay)")
    p.add_argument("--gains", type=float, nargs="+", default=[30, 35, 40, 45, 50, 55, 60, 65],
                   help="gains to compare, dB (default 30 35 ... 65)")
    p.add_argument("--ref-gain", type=float, default=40.0,
                   help="SNR is reported relative to this gain (default 40, the usual setting)")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("10m"),
                   help="how long: 90s, 10m, 1h (default 10m); 0 = until Ctrl-C")
    p.add_argument("--slot", type=float, default=2.0, help="seconds at each gain before switching (default 2)")
    p.add_argument("--max-rate", type=float, help="cap the sample rate, MSPS (default: the link's maximum)")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above the noise floor that counts as a signal, as the squelch (default 8)")
    p.add_argument("--no-clips", action="store_true", help="don't save audio clips")
    p.add_argument("--atis-clips", type=int, default=1, help="clips of each ATIS per gain (default 1)")
    p.add_argument("--seed", type=int, default=0, help="seed for the gain order")
    p.add_argument("--out", help="output folder (default recordings/gain_sweep_<time>)")
    p.add_argument("--reanalyze", metavar="DIR", help="rerun the analysis on a saved run's levels.npz")
    args = p.parse_args()
    signal.signal(signal.SIGTERM, _terminate)

    if args.reanalyze:
        out = Path(args.reanalyze)
        data, meta = load_levels(out / "levels.npz")
        gains = meta["gains"]
        rows, cells, rec = analyze(data, meta["slots"], gains, args.threshold, args.ref_gain)
        header = meta["header"] + [f"Reanalysed with --threshold {args.threshold:g}, --ref-gain {args.ref_gain:g}."]
        text = report(rows, cells, rec, gains, args.ref_gain, header)
        print(text)
        (out / f"report_threshold{args.threshold:g}.txt").write_text(text + "\n")
        return

    gains = sorted(set(args.gains))
    if len(gains) < 2:
        sys.exit("Give at least two gains to compare.")
    freqs = fq.area_channels(args.area, atis=True) if args.area in fq.AREAS else fq.preset_channels(args.area, True)
    uri = A.find_uri(args.device) if args.source == "radio" else "synthetic"
    limit = (A.max_rate(uri, args.max_rate) if args.source == "radio"
             else int((args.max_rate or pluto.USB_MAX_RATE / 1e6) * 1e6))
    rate, windows = A.plan(freqs, limit)
    out = Path(args.out or ROOT / f"recordings/gain_sweep_{datetime.now():%Y%m%d-%H%M}")
    out.mkdir(parents=True, exist_ok=True)

    if args.source == "radio":
        kw = {} if pluto.is_packed(uri) else dict(kernel_buffers=4)     # a quicker flush at each gain change
        try:
            radio = pluto.make(uri, windows[0].center, rate, gains[0], **kw)
        except OSError as e:
            sys.exit(f"Could not open the radio at {uri}: {e}\nTry `iio_info -s` to list devices. "
                     "Only one program can use the Pluto at a time.")
    else:
        radio = SyntheticRadio(windows[0].center, rate, gains[0])

    saver = None if args.no_clips else ClipSaver(out / "clips", out / "clips.csv")
    clips = saver and Clips(windows, gains, args.threshold, saver, args.atis_clips)
    sweep = Sweep(radio, rate, windows, gains, args.slot, seed=args.seed, clips=clips)
    m = sweep.meters[0]
    if saver and sweep.clips is None:
        print(f"Clips off: this computer can't demodulate every frame at {rate / 1e6:g} MSPS. For clips, "
              "use fewer channels (--area sfo) or a lower --max-rate.")
        saver = None
    if saver:
        saver.start()
    started = datetime.now()
    header = [f"Gain sweep, {started:%Y-%m-%d %H:%M}: {args.area} ({len(freqs)} channels with ATIS), "
              f"radio {uri} ({A.link_name(uri) if args.source == 'radio' else 'synthetic'}), "
              f"{rate / 1e6:g} MSPS, {len(windows)} window(s)",
              f"Gains {', '.join(format(g, 'g') for g in gains)} dB, {args.slot:g} s each in a random order per round"
              + (f", per window" if len(windows) > 1 else "")
              + f"; analysing every {'' if sweep.stride == 1 else str(sweep.stride) + 'th '}frame "
                f"({1000 * m.frame_s:.0f} ms); threshold {args.threshold:g} dB."]
    print("\n".join(header))
    print(f"Saving to {out}/\n" + ("Ctrl-C stops it early; the report is still written.\n" if args.duration else
                                  "Ctrl-C to stop and write the report.\n"), flush=True)
    t0 = sweep.clock()
    try:
        sweep.run(t0 + args.duration if args.duration else None, status_line(sweep, t0, args.duration))
    except KeyboardInterrupt:
        pass
    finally:
        radio.close()
        clip_rows = saver.finish() if saver else None
    elapsed = sweep.clock() - t0
    header.append(f"Ran {elapsed / 60:.1f} min: {sweep.rounds} full rounds, {len(sweep.slots)} slots"
                  + (f", {sweep.reconnects} reconnect(s)" if sweep.reconnects else "") + ".")
    data = sweep.data()
    save_levels(out / "levels.npz", data, sweep.slots, gains, dict(header=header, rate=rate, uri=uri))
    rows, cells, rec = analyze(data, sweep.slots, gains, args.threshold, args.ref_gain)
    write_csvs(out, rows, cells, sweep.slots)
    text = report(rows, cells, rec, gains, args.ref_gain, header)
    if clip_rows is not None:
        text += "\n\n" + clip_summary(clip_rows, gains)
    print(CLEAR + "\n" + text)
    (out / "report.txt").write_text(text + "\n")


if __name__ == "__main__":
    main()
