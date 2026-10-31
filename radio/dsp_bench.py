#!/usr/bin/env python3
"""
DSP benchmark for a full-band receiver: can this computer channelize all of the
Bay's airband channels (118.000-135.650 MHz, 43 channels) in ONE capture window
at 21.6 MSPS, and how much do compiled code and more cores help?

    python3 radio/dsp_bench.py                    # about 3 minutes
    python3 radio/dsp_bench.py --sustain 300      # longer thermal test
    python3 radio/dsp_bench.py --quick            # shorter runs, no thermal test

It compares, on synthetic IQ (no radio needed):
  1. today's receiver (one large FFT per block, then a small inverse FFT and
     Python demodulation per channel), at 10.8 MSPS (today's 2-window plan,
     busiest window) and at 21.6 MSPS (one window)
  2. a polyphase filter bank: 21.6 MSPS / 25 kHz = 864 channels exactly on the
     airband grid; a 12-tap filter per branch, then small 864-point FFTs.
     In numpy, and compiled with numba on 1, 2, 4 ... cores
  3. demodulation of the 43 channels: today's Python loop vs compiled
  4. the whole proposed pipeline (int16 IQ in -> 43 demodulated channels out)
It also checks the polyphase channelizer is right (a tone comes out of its own
channel only) and runs the best setup for --sustain seconds while watching the
CPU temperature and throttling, since four busy cores heat a Pi 5 quickly.

Real time needs 1x; the recorder wants 1.5x or more (it also reads the radio and
saves clips). Writes report.txt to recordings/dspbench_<date>-<time>/ (or --out).
numba is optional (pip install numba; pi/setup.sh installs it).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import scipy
import scipy.fft
import scipy.signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                    # for atcsdr
sys.path.insert(0, str(ROOT / "radio"))          # for characterize.bench (today's receiver)

from atcsdr import airband as A
from atcsdr import frequencies as fq
from characterize import bench as current_bench

try:
    import numba
except ImportError:
    numba = None

RATE = 21_600_000
SPACING = 25_000             # airband channel grid
M = RATE // SPACING          # 864 polyphase branches = channels
T = 12                       # filter taps per branch (~60 dB stopband from 12.5 kHz)
CENTER = 126_825_000         # on the grid, between channels, all 43 within 85% of the band
FRAME = 400                  # samples per squelch frame at 25 kHz (16 ms, like the receiver)
CHUNK = 2500                 # frames per processing chunk (0.1 s, like a Pluto buffer)
SCALE = 1 / 2048             # int16 counts -> +-1
NEED = 1.5


class Report:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def section(self, title):
        self("")
        self(f"== {title}")


def _read(path):
    try:
        return Path(path).read_text().strip("\x00\n ")
    except OSError:
        return None


def temp_c():
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    return int(t) / 1000 if t else None


def throttled():
    if not shutil.which("vcgencmd"):
        return None
    out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout
    return int(out.split("=")[-1], 16) if "=" in out else None


# ------------------------------------------------------------------ channelizer

def prototype():
    """The polyphase filter: flat to +-5 kHz, ~60 dB down from 12.5 kHz (where the
    next channel's band starts to alias in), split into T taps x M branches."""
    h = scipy.signal.firwin(M * T, 8_750, fs=RATE, window=("kaiser", 6.0)).astype(np.float32)
    # Branch p, tap t = h[t*M + M-1-p]: with an FFT across branches, output k is
    # the channel at CENTER + k * 25 kHz (k < 0 wraps to M + k).
    return h.reshape(T, M)[:, ::-1].copy()


def channel_bins(freqs):
    return np.array([round((f.mhz * 1e6 - CENTER) / SPACING) % M for f in freqs])


def poly_numpy(raw, H):
    """int16 I/Q, (F+T-1)*M pairs -> (F, M) polyphase outputs (before the FFT)."""
    X = raw.astype(np.float32).view(np.complex64).reshape(-1, M)
    F = X.shape[0] - (T - 1)
    acc = np.zeros((F, M), np.complex64)
    for t in range(T):
        acc += X[T - 1 - t: T - 1 - t + F] * H[t]
    return acc


if numba:
    @numba.njit(parallel=True, fastmath=True, cache=True)
    def _poly(X, H, out):
        # X: (F+T-1, M) complex64; out: (F, M). Frames split across cores; the branch
        # loop innermost, over contiguous memory, so it compiles to SIMD (4x faster
        # than reading the int16 pairs directly).
        taps, branches = H.shape
        for f in numba.prange(out.shape[0]):
            for p in range(branches):
                out[f, p] = 0
            for t in range(taps):
                row = f + taps - 1 - t
                for p in range(branches):
                    out[f, p] += X[row, p] * H[t, p]

    @numba.njit(parallel=True, fastmath=True, cache=True)
    def _demod(y, state, a, audio, fpow):
        # y: (C, N) complex64 channels at 25 kHz. AM envelope normalized by a one-pole
        # carrier average (as atcsdr.airband.Channel does), plus power per 16 ms frame.
        for c in numba.prange(y.shape[0]):
            s = state[c]
            for k in range(y.shape[1] // FRAME):
                acc = np.float32(0.0)
                for i in range(k * FRAME, (k + 1) * FRAME):
                    e = abs(y[c, i])
                    s += a * (e - s)
                    audio[c, i] = (e - s) / max(s, np.float32(1e-6))
                    acc += e * e
                fpow[c, k] = acc / FRAME
            state[c] = s


def poly_numba(raw, H, out):
    _poly(raw.astype(np.float32).view(np.complex64).reshape(-1, M), H, out)
    return out


def fft_select(acc, bins, pool, threads):
    """FFT across branches, keeping only our channels: (F, M) -> (C, F)."""
    if threads == 1:
        return np.ascontiguousarray(scipy.fft.fft(acc, axis=1, overwrite_x=True)[:, bins].T)
    parts = np.array_split(np.arange(acc.shape[0]), threads)
    out = pool.map(lambda r: scipy.fft.fft(acc[r[0]: r[-1] + 1], axis=1)[:, bins], parts)
    return np.ascontiguousarray(np.concatenate(list(out)).T)


def synthetic(frames, seed=0):
    """int16 I/Q noise, (frames + T - 1) * M pairs."""
    n = 2 * (frames + T - 1) * M
    return (np.random.default_rng(seed).standard_normal(n) * 30).astype(np.int16)


def chunks(raw, frames):
    """Views of `raw` for each CHUNK of frames, each with its T-1 frames of history."""
    for f0 in range(0, frames - CHUNK + 1, CHUNK):
        yield raw[2 * f0 * M: 2 * (f0 + CHUNK + T - 1) * M]


# ------------------------------------------------------------------ tests

def check_channelizer(rep, H, poly):
    """Tones 1 kHz above three channels: each must come out of its own channel, with
    the neighbours (and everything else) far below."""
    freqs = [f for f in fq.area_channels("bay") if f.mhz in (118.0, 124.525, 135.65)]
    frames = 400
    n = np.arange((frames + T - 1) * M)
    x = sum(np.exp(2j * np.pi * (f.mhz * 1e6 + 1_000 - CENTER) * n / RATE) for f in freqs) * 600
    raw = np.empty(2 * len(n), np.int16)
    raw[0::2], raw[1::2] = x.real, x.imag
    out = np.empty((frames, M), np.complex64)
    p = np.mean(np.abs(scipy.fft.fft(poly(raw, H, out) if poly is poly_numba else poly(raw, H),
                                     axis=1)[T:]) ** 2, axis=0)
    ok = True
    for f, b in zip(freqs, channel_bins(freqs)):
        around = [(b + d) % M for d in range(-3, 4)]
        peak = around[int(np.argmax(p[around]))]
        near = max(p[(b - 1) % M], p[(b + 1) % M])
        db = 10 * np.log10(near / p[b])
        good = peak == b and db < -50
        ok &= good
        rep(f"  tone at {f.mhz:.3f} MHz + 1 kHz -> channel {'ok' if peak == b else 'WRONG'}, "
            f"neighbours {db:5.0f} dB  {'ok' if good else 'FAIL'}")
    return ok


def time_it(fn, signal_s, repeat=1):
    best = None
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    return signal_s / best


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=float, default=3.0, help="signal seconds per test (default 3)")
    p.add_argument("--sustain", type=float, default=60.0,
                   help="seconds of the thermal test, 0 = skip (default 60)")
    p.add_argument("--quick", action="store_true", help="1 s tests, no thermal test")
    p.add_argument("--out", help="output folder (default recordings/dspbench_<time>)")
    args = p.parse_args()
    if args.quick:
        args.seconds, args.sustain = 1.0, 0.0

    rep = Report()
    bay = fq.area_channels("bay")
    bins = channel_bins(bay)
    cores = os.cpu_count()
    thread_counts = sorted({1, 2, 4, cores} & set(range(1, cores + 1)))
    rep(f"DSP benchmark, {datetime.now():%Y-%m-%d %H:%M}: {len(bay)} channels, "
        f"{bay[0].mhz:.3f}-{bay[-1].mhz:.3f} MHz, one window at {RATE / 1e6:g} MSPS")
    rep(f"board: {_read('/proc/device-tree/model') or sys.platform}, {cores} cores; Python "
        f"{sys.version.split()[0]}, numpy {np.__version__}, scipy {scipy.__version__}, "
        f"numba {numba.__version__ if numba else 'not installed'}")
    t = temp_c()
    if t:
        rep(f"CPU temperature at start: {t:.0f} C")

    H = prototype()
    frames = int(args.seconds * SPACING)
    frames -= frames % CHUNK
    sig_s = frames / SPACING
    raw = synthetic(frames)
    results = {}

    # 1. Today's receiver.
    rep.section("1. Today's receiver (big FFT + per-channel IFFT + Python demodulation)")
    for rate in (10_800_000, RATE):
        r, windows = A.plan(bay, rate)
        speed, n = current_bench(bay, r, windows, args.seconds)
        results[f"current {r / 1e6:g}"] = speed
        rep(f"  {r / 1e6:5.2f} MSPS, {len(windows)} window(s), {n} ch in the busiest: {speed:5.2f}x"
            + ("" if speed >= NEED else "   too slow"))

    # 2. Channelizer alone.
    rep.section(f"2. Polyphase channelizer alone ({M} channels x 25 kHz, int16 in, 43 out)")
    pool = ThreadPoolExecutor(max(thread_counts))

    def run_numpy():
        for c in chunks(raw, frames):
            fft_select(poly_numpy(c, H), bins, pool, 1)
    results["pfb numpy"] = time_it(run_numpy, sig_s)
    rep(f"  numpy, 1 core:            {results['pfb numpy']:6.2f}x")
    if numba:
        out = np.empty((CHUNK, M), np.complex64)
        poly_numba(raw[: 2 * (10 + T - 1) * M], H, out[:10])          # compile (cached after)
        for n in thread_counts:
            numba.set_num_threads(n)

            def run_numba():
                for c in chunks(raw, frames):
                    fft_select(poly_numba(c, H, out), bins, pool, n)
            results[f"pfb numba {n}"] = time_it(run_numba, sig_s, repeat=2)
            rep(f"  numba, {n} core(s):         {results[f'pfb numba {n}']:6.2f}x")
    else:
        rep("  numba: not installed (pip install numba), skipped")

    rep.section("   correctness")
    ok = check_channelizer(rep, H, poly_numba if numba else poly_numpy)
    if numba:
        c = next(chunks(raw, frames))
        same = np.allclose(poly_numpy(c, H), poly_numba(c, H, np.empty((CHUNK, M), np.complex64)),
                           rtol=1e-4, atol=1e-3)
        ok &= same
        rep(f"  numba matches numpy: {'ok' if same else 'FAIL'}")

    # 3. Demodulation.
    rep.section(f"3. Demodulating {len(bay)} channels")
    chans = [A.Channel(f) for f in bay]
    audio_s = 2.0
    blocks = [np.random.default_rng(1).standard_normal((len(bay), A.KEEP)).astype(np.complex64)
              for _ in range(int(audio_s / A.FRAME_S))]

    def run_python():
        now = time.time()
        for blk in blocks:
            mix = np.zeros((A.KEEP, 2), np.float32)
            for ch, y in zip(chans, blk):
                audio, _ = ch.process(y, now, now)
                mix += audio[:, None] * ch.lr
    results["demod python"] = time_it(run_python, audio_s)
    rep(f"  today's Python loop (48 kHz): {results['demod python']:6.1f}x")
    if numba:
        y = np.random.default_rng(2).standard_normal((len(bay), int(audio_s * SPACING))).astype(np.complex64)
        state = np.ones(len(bay), np.float32)
        audio = np.empty(y.shape, np.float32)
        fpow = np.empty((len(bay), y.shape[1] // FRAME), np.float32)
        a = np.float32(1 - np.exp(-1 / (0.05 * SPACING)))
        _demod(y[:, :FRAME], state, a, audio[:, :FRAME], fpow[:, :1])  # compile
        for n in thread_counts:
            numba.set_num_threads(n)
            results[f"demod numba {n}"] = time_it(lambda: _demod(y, state, a, audio, fpow), audio_s, 3)
            rep(f"  numba, {n} core(s) (25 kHz):  {results[f'demod numba {n}']:6.1f}x")

    # 4. The whole proposed pipeline.
    best_n, best = None, 0.0
    if numba:
        rep.section("4. Whole proposed pipeline (int16 IQ -> channelize -> demodulate), 21.6 MSPS")
        state = np.ones(len(bay), np.float32)
        audio = np.empty((len(bay), CHUNK), np.float32)
        fpow = np.empty((len(bay), CHUNK // FRAME), np.float32)
        out = np.empty((CHUNK, M), np.complex64)

        def pipeline(src, n_chunks=None):
            for i, c in enumerate(chunks(src, frames)):
                if n_chunks is not None and i >= n_chunks:
                    break
                y = fft_select(poly_numba(c, H, out), bins, pool, numba.get_num_threads())
                _demod(y, state, a, audio, fpow)
        for n in thread_counts:
            numba.set_num_threads(n)
            speed = time_it(lambda: pipeline(raw), sig_s, repeat=2)
            results[f"pipeline {n}"] = speed
            if speed > best:
                best_n, best = n, speed
            rep(f"  {n} core(s): {speed:6.2f}x" + ("" if speed >= NEED else "   too slow"))

        # Where the time goes, at the best core count.
        numba.set_num_threads(best_n)
        spent = dict(convert=0.0, polyphase=0.0, fft=0.0, demod=0.0)
        for c in chunks(raw, frames):
            t1 = time.perf_counter(); X = c.astype(np.float32).view(np.complex64).reshape(-1, M)
            t2 = time.perf_counter(); _poly(X, H, out)
            t3 = time.perf_counter(); y = fft_select(out, bins, pool, best_n)
            t4 = time.perf_counter(); _demod(y, state, a, audio, fpow)
            t5 = time.perf_counter()
            for k, dt in zip(spent, (t2 - t1, t3 - t2, t4 - t3, t5 - t4)):
                spent[k] += dt
        total = sum(spent.values())
        rep(f"  time on {best_n} cores: " + ", ".join(f"{k} {100 * v / total:.0f}%" for k, v in spent.items()))

        # 5. Sustained, for heat.
        if args.sustain > 0:
            rep.section(f"5. Sustained: {best_n} cores for {args.sustain:g} s (heat and throttling)")
            numba.set_num_threads(best_n)
            per = []
            t0, th0 = time.time(), throttled()
            while time.time() - t0 < args.sustain:
                per.append(time_it(lambda: pipeline(raw), sig_s))
                t = temp_c()
                if sys.stdout.isatty():
                    print(f"\r  {time.time() - t0:5.0f} s  {per[-1]:5.2f}x" + (f"  {t:.0f} C" if t else ""),
                          end="", flush=True)
            if sys.stdout.isatty():
                print()
            th1, t = throttled(), temp_c()
            first, last = np.mean(per[: max(1, len(per) // 5)]), np.mean(per[-max(1, len(per) // 5):])
            rep(f"  speed first fifth {first:.2f}x, last fifth {last:.2f}x"
                + (f"; temperature now {t:.0f} C" if t else ""))
            if th1 is not None:
                newly = th1 & ~(th0 or 0)
                rep("  throttling: " + ("THROTTLED or capped during the run: fit the active cooler"
                                        if th1 & 0x6 or newly & 0x60000 else "none"))
            if last < 0.85 * first:
                rep("  -> it slowed down as it heated up: needs better cooling to hold this speed")
            results["sustained"] = last

    # Verdict.
    rep.section("Verdict")
    rep(f"channelizer correct: {'yes' if ok else 'NO (see above)'}")
    cur = results[f"current {RATE / 1e6:g}"]
    rep(f"today's receiver at 21.6 MSPS (one window): {cur:.2f}x -> "
        + ("enough" if cur >= NEED else "not enough"))
    if numba:
        hold = results.get("sustained", best)
        rep(f"proposed pipeline: {best:.2f}x on {best_n} cores ({best / cur:.1f}x today's)"
            + (f", {hold:.2f}x sustained" if "sustained" in results else "") + " -> "
            + ("one window over the whole band is feasible on this computer."
               if hold >= NEED else "not enough for one window here; it would need native code or fewer channels."))
    else:
        rep("install numba (pip install numba) to test the proposed pipeline.")
    rep("The radio link must also carry 21.6 MSPS: pi/linktest.sh 2m --rate 21.6")

    out_dir = Path(args.out or ROOT / f"recordings/dspbench_{datetime.now():%Y%m%d-%H%M%S}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text("\n".join(rep.lines) + "\n")
    print(f"\nSaved {out_dir}/report.txt")
    pool.shutdown()


if __name__ == "__main__":
    main()
