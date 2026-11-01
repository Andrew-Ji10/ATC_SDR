"""Fast multichannel AM receiver: the whole band in one capture window.

A polyphase channelizer splits `rate` into rate / 25 kHz channels, exactly on
the airband grid (20.0 MSPS: 800 channels; 21.6 MSPS: 864), compiled with numba
and spread over the cores it's given. The channels we want are kept, AM
demodulated at 25 kHz and squelched in 16 ms frames, as atcsdr.airband does.

    int16 I/Q -> complex -> polyphase filter (12 taps a branch) -> M-point FFT
    -> our channels at 25 kHz -> AM envelope / carrier -> frames -> squelch

On the Pi 5 (radio/dsp_bench.py): the channelizer and demodulator run at about
3x real time at 21.6 MSPS on 2 cores; today's receiver manages 0.66x.
"""

import math
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import scipy.fft
import scipy.signal

from . import airband as A

SPACING = 25_000             # airband channel grid = each channel's sample rate
AUDIO_RATE = SPACING
TAPS = 12                    # polyphase filter taps per branch (~60 dB from 12.5 kHz)
FRAME = 400                  # samples per squelch frame (16 ms, like atcsdr.airband)
FRAME_S = FRAME / AUDIO_RATE

try:
    import numba
except ImportError:
    numba = None


def plan_center(freqs, rate):
    """The LO for one window holding all `freqs` (Freq) at `rate`: on the 25 kHz
    grid, clear of every channel, as central as possible. Returns (center Hz,
    how far the outermost channel's filter edge reaches, as a fraction of the
    way to the band edge)."""
    lo, hi = freqs[0].mhz * 1e6, freqs[-1].mhz * 1e6
    mid = round((lo + hi) / 2 / SPACING) * SPACING
    best = None
    for k in range(-400, 401):
        c = mid + k * SPACING
        if all(abs(f.mhz * 1e6 - c) >= A.DC_GUARD for f in freqs):
            reach = (max(abs(lo - c), abs(hi - c)) + A.STOP_HZ) / (rate / 2)
            if best is None or reach < best[1]:
                best = (c, reach)
    if best is None or best[1] >= 1:
        raise ValueError(f"{freqs[0].mhz:.3f}-{freqs[-1].mhz:.3f} MHz doesn't fit {rate / 1e6:g} MSPS")
    return best


if numba:
    @numba.njit(parallel=True, fastmath=True, cache=True)
    def _to_complex(raw, out):
        for i in numba.prange(out.shape[0]):
            out[i] = complex(raw[2 * i], raw[2 * i + 1])

    @numba.njit(parallel=True, fastmath=True, cache=True)
    def _poly(X, H, out):
        # X: (F+T-1, M) complex64; out: (F, M). Frames across cores; the branch
        # loop innermost, over contiguous memory, so it compiles to SIMD.
        taps, branches = H.shape
        for f in numba.prange(out.shape[0]):
            for p in range(branches):
                out[f, p] = 0
            for t in range(taps):
                row = f + taps - 1 - t
                for p in range(branches):
                    out[f, p] += X[row, p] * H[t, p]

    @numba.njit(parallel=True, fastmath=True, cache=True)
    def _am(y, state, a, audio, env2):
        # Envelope normalized by a one-pole carrier average (atcsdr.airband.Channel's
        # demodulator), and the envelope squared for the squelch's frame power.
        for c in numba.prange(y.shape[0]):
            s = state[c]
            for i in range(y.shape[1]):
                e = abs(y[c, i])
                if s == 0:
                    s = e
                s += a * (e - s)
                audio[c, i] = (e - s) / max(s, np.float32(1e-6))
                env2[c, i] = e * e
            state[c] = s


class Channelizer:
    """int16 I/Q chunks in, our channels at 25 kHz out (C, n), streaming."""

    def __init__(self, rate, center, freqs, threads=2):
        if numba is None:
            raise RuntimeError("numba is needed (pip install numba; pi/setup.sh installs it)")
        if rate % SPACING:
            raise ValueError(f"the rate must be a multiple of {SPACING} Hz")
        self.M = rate // SPACING
        h = scipy.signal.firwin(self.M * TAPS, 8_750, fs=rate, window=("kaiser", 6.0))
        # Branch p, tap t = h[t*M + M-1-p]: with an FFT across branches, output k is
        # the channel at center + k * 25 kHz. 1/2048 folds in the int16 scale.
        self.H = (h.reshape(TAPS, self.M)[:, ::-1] / 2048).astype(np.float32).copy()
        self.bins = np.array([round((f.mhz * 1e6 - center) / SPACING) % self.M for f in freqs])
        self.hist = (TAPS - 1) * self.M
        self.buf = np.zeros(self.hist, np.complex64)     # filter history + leftover samples
        self.threads = threads
        numba.set_num_threads(threads)
        self.pool = ThreadPoolExecutor(threads)

    def reset(self):
        self.buf = np.zeros(self.hist, np.complex64)

    def _fft(self, out):
        if self.threads == 1:
            return np.ascontiguousarray(scipy.fft.fft(out, axis=1, overwrite_x=True)[:, self.bins].T)
        parts = np.array_split(np.arange(out.shape[0]), self.threads)
        rows = self.pool.map(lambda r: scipy.fft.fft(out[r[0]: r[-1] + 1], axis=1)[:, self.bins], parts)
        return np.ascontiguousarray(np.concatenate(list(rows)).T)

    def process(self, raw):
        x = np.empty(len(raw) // 2, np.complex64)
        _to_complex(raw, x)
        buf = np.concatenate([self.buf, x])
        frames = (len(buf) - self.hist) // self.M
        if frames <= 0:
            self.buf = buf
            return np.zeros((len(self.bins), 0), np.complex64)
        out = np.empty((frames, self.M), np.complex64)
        _poly(buf[: (frames + TAPS - 1) * self.M].reshape(-1, self.M), self.H, out)
        self.buf = buf[frames * self.M:]
        return self._fft(out)


@dataclass
class Transmission:
    channel: "Squelch"
    start: float                 # unix time of the first audio sample
    audio: np.ndarray            # at AUDIO_RATE
    peak_db: float

    @property
    def seconds(self):
        return len(self.audio) / AUDIO_RATE


class Squelch:
    """One channel's self-calibrating squelch, on 16 ms frames of (power dB, audio):
    atcsdr.airband.Channel's logic, with the demodulation done for all channels at once."""

    def __init__(self, freq, threshold_db=8.0):
        self.freq, self.mhz, self.name, self.kind = freq, freq.mhz, freq.name, freq.kind
        self.squelch = freq.kind != "atis"
        self.threshold_db = threshold_db
        self.calibrate = int(A.CALIBRATE_S / FRAME_S)
        self.hang = int(A.HANG_S / FRAME_S)
        self.history = deque(maxlen=int(A.HISTORY_S / FRAME_S))
        self.pre_roll = deque(maxlen=max(1, int(A.PRE_ROLL_S / FRAME_S)))
        self.floor_db, self.floor_age, self.level_db = None, 0, -200.0
        self.loud = self.quiet = 0
        self.clip, self.opened_at, self.peak_db = None, None, 0.0
        self.monitored_s = 0.0
        self.transmissions = self.blips = 0
        self.talk_s, self.best_db = 0.0, None

    @property
    def open(self):
        return self.clip is not None

    def frame(self, db, audio, now):
        """One frame ending at unix time `now`. Returns a finished Transmission or None."""
        self.monitored_s += FRAME_S
        self.level_db = db
        finished = None
        if not self.squelch:
            return None
        if len(self.history) < self.calibrate:
            self.history.append(db)
        else:
            self.floor_age -= 1
            if self.floor_db is None or self.floor_age <= 0:
                self.floor_db = float(np.percentile(self.history, 20))
                self.floor_age = A.FLOOR_EVERY
            open_at = self.floor_db + self.threshold_db
            if not self.open:
                self.loud = self.loud + 1 if db > open_at else 0
                if self.loud >= A.OPEN_FRAMES:
                    self.clip, self.quiet = list(self.pre_roll), 0
                    self.opened_at = now - FRAME_S * (len(self.clip) + 1)
                    self.peak_db = db - self.floor_db
                else:
                    self.history.append(db)
            else:
                self.peak_db = max(self.peak_db, db - self.floor_db)
                self.quiet = self.quiet + 1 if db < open_at - A.HYSTERESIS_DB else 0
                if self.quiet >= self.hang:
                    finished = self.end()
        self.pre_roll.append(audio)
        if self.open:
            self.clip.append(audio)
        return finished

    def end(self):
        if not self.open:
            return None
        kept = self.clip[: max(1, len(self.clip) - self.quiet + A.TAIL_FRAMES)]
        tx = Transmission(self, self.opened_at, np.concatenate(kept), self.peak_db)
        self.clip, self.loud, self.quiet = None, 0, 0
        self.pre_roll.clear()
        return tx


class Receiver:
    """The whole chain for one window: process() raw chunks, get finished transmissions."""

    def __init__(self, rate, freqs, threshold_db=8.0, threads=2):
        self.rate = int(rate)
        self.center, self.edge = plan_center(freqs, self.rate)
        self.chan = Channelizer(self.rate, self.center, freqs, threads)
        self.squelches = [Squelch(f, threshold_db) for f in freqs]
        n = len(freqs)
        self.state = np.zeros(n, np.float32)
        self.a = np.float32(1 - math.exp(-1 / (0.05 * AUDIO_RATE)))   # 50 ms carrier average
        self.pend_audio = np.zeros((n, 0), np.float32)
        self.pend_env2 = np.zeros((n, 0), np.float32)

    def process(self, raw, t_end):
        """A chunk of interleaved int16 I/Q whose last sample is at unix time t_end."""
        y = self.chan.process(raw)
        if y.shape[1]:
            audio = np.empty(y.shape, np.float32)
            env2 = np.empty(y.shape, np.float32)
            _am(y, self.state, self.a, audio, env2)
            self.pend_audio = np.concatenate([self.pend_audio, audio], axis=1)
            self.pend_env2 = np.concatenate([self.pend_env2, env2], axis=1)
        n = self.pend_audio.shape[1] // FRAME
        done = []
        if not n:
            return done
        power = self.pend_env2[:, : n * FRAME].reshape(len(self.squelches), n, FRAME).mean(axis=2)
        db = 10 * np.log10(power + 1e-20)
        held = self.pend_audio.shape[1] - n * FRAME        # samples after the last whole frame
        for k in range(n):
            now = t_end - (held + (n - 1 - k) * FRAME) / AUDIO_RATE
            frame = self.pend_audio[:, k * FRAME: (k + 1) * FRAME]
            for c, sq in enumerate(self.squelches):
                tx = sq.frame(float(db[c, k]), frame[c].copy(), now)
                if tx is not None:
                    done.append(tx)
        self.pend_audio = self.pend_audio[:, n * FRAME:]
        self.pend_env2 = self.pend_env2[:, n * FRAME:]
        return done

    def gap(self):
        """The stream broke (lost chunks, a reconnect): close what's open, start the
        filters afresh. Returns the transmissions that were open."""
        done = [tx for sq in self.squelches if (tx := sq.end()) is not None]
        self.chan.reset()
        self.pend_audio = self.pend_audio[:, :0]
        self.pend_env2 = self.pend_env2[:, :0]
        return done

    def finish(self):
        return [tx for sq in self.squelches if (tx := sq.end()) is not None]
