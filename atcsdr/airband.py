"""Many airband (AM) channels from one Pluto: channelize, demodulate, squelch,
mix, record, and log every transmission.

Signal chain:
    Pluto (libiio, int16 IQ) -> blocks of N samples, 25% overlap
    -> one FFT per block -> for each channel, the bins around it, shaped to a
       +-5 kHz channel filter, inverse FFT at 48 kHz (an FFT filter bank)
    -> AM envelope, normalized by the carrier level -> per-channel squelch
    -> constant-power stereo pan -> sum -> speakers / WAV; clips per transmission

Capture windows: the channels must fit in what the link can stream. Over USB
2.0 that is ~8 MSPS, about 6.5 MHz of spectrum; over gigabit Ethernet ~30 MSPS.
plan() splits the channel list into the fewest windows that each fit. With one
window every channel is monitored all the time. With several, the Pluto
rotates between them, `dwell` seconds each, and each channel is monitored
1/len(windows) of the time; the summary says how long each channel was heard.
"""

import argparse
import math
import os
import queue
import shutil
import signal
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import scipy.fft
from scipy.signal import lfilter

from . import audio as audiofile
from . import frequencies as fq
from . import pluto
from .pluto import FALLBACK_URI, is_ethernet, to_complex
from .rtl import RTL_MAX_RATE, is_rtl, open_rtl

AUDIO_RATE = 48_000
OUT_BLOCK = 1024             # output samples per FFT block ...
KEEP = 3 * OUT_BLOCK // 4    # ... of which the middle 3/4 are kept (2.7 ms margin each
                             # side; the channel filter's impulse response is ~0.5 ms)
FRAME_S = KEEP / AUDIO_RATE  # 16 ms per demodulated frame
USABLE = 0.85                # fraction of the sampled band to use; measured flat to
                             # 94% at 7.68 MSPS, with the noise rising ~5 dB at the edges
DC_GUARD = 10e3              # keep channels this far from the LO, where the DC offset
                             # lives: past the +-7 kHz channel filter, and small enough
                             # that the LO fits between channels on a 25 kHz grid
PASS_HZ = 5_000              # channel filter: flat to +-5 kHz ...
STOP_HZ = 7_000              # ... and zero from +-7 kHz (channels are 25 kHz apart)
ATIS_GAIN = 0.3              # ATIS loops play quietly and without squelch

# Squelch, per channel.
CALIBRATE_S = 2.0            # learn the noise floor this long before opening
HISTORY_S = 10.0             # noise floor = 20th percentile of this much quiet
OPEN_FRAMES = 2              # this many frames above the threshold open the squelch
HANG_S = 1.0                 # it closes after this much quiet (pauses don't split)
HYSTERESIS_DB = 3.0
FLOOR_EVERY = 10             # recompute the noise floor every this many frames (~0.1 s)
PRE_ROLL_S = 0.3             # audio kept from just before the squelch opened
TAIL_FRAMES = 10             # ~0.1 s kept after the last loud frame
MIN_CLIP_S = 1.0             # shorter openings are logged as blips, not saved


# ------------------------------------------------------------------ DSP

class FilterBank:
    """Split wideband IQ into channels at AUDIO_RATE with one FFT per block.

    Overlap-save with 25% overlap: each block of N = D * OUT_BLOCK samples is
    transformed once; for each channel the OUT_BLOCK bins around it are weighted
    by the channel filter and inverse-transformed, which also decimates by D.
    The middle 3/4 of each output block is kept, so blocks tile exactly and
    the filter's short impulse response never wraps around."""

    def __init__(self, rate, offsets_hz):
        self.decim = int(rate // AUDIO_RATE)
        self.n = self.decim * OUT_BLOCK
        self.hop = self.decim * KEEP
        bin_hz = rate / self.n
        centers = np.array([int(round(off / bin_hz)) for off in offsets_hz])
        f = np.fft.fftfreq(OUT_BLOCK, 1 / AUDIO_RATE)
        taper = np.clip((STOP_HZ - np.abs(f)) / (STOP_HZ - PASS_HZ), 0, 1)
        self.shape = (0.5 - 0.5 * np.cos(np.pi * taper)).astype(np.float32)
        window_bins = np.fft.fftfreq(OUT_BLOCK, 1 / OUT_BLOCK).astype(int)
        self.index = (centers[:, None] + window_bins) % self.n
        self.buf = np.zeros(0, np.complex64)

    def reset(self):
        self.buf = self.buf[:0]

    def process(self, iq):
        """Yield one (channels, KEEP) complex array per completed hop."""
        self.buf = np.concatenate([self.buf, iq])
        q = (OUT_BLOCK - KEEP) // 2
        while len(self.buf) >= self.n:
            # scipy.fft keeps complex64 in single precision: ~2x faster than numpy here.
            spectrum = scipy.fft.fft(self.buf[: self.n])
            out = scipy.fft.ifft(spectrum[self.index] * self.shape, axis=1)
            self.buf = self.buf[self.hop:]
            yield out[:, q: q + KEEP].astype(np.complex64)


@dataclass
class Transmission:
    channel: "Channel"
    start: float                 # unix time
    audio: np.ndarray
    peak_db: float               # loudest frame, dB above the channel's noise floor

    @property
    def seconds(self):
        return len(self.audio) / AUDIO_RATE


class Channel:
    """AM demodulator, self-calibrating squelch and transmission capture for one
    channel, plus what it heard (for the summary)."""

    def __init__(self, freq, pan=0.0, threshold_db=8.0):
        self.freq = freq
        self.mhz, self.name, self.kind = freq.mhz, freq.name, freq.kind
        self.squelch = freq.kind != "atis"
        self.gain = ATIS_GAIN if freq.kind == "atis" else 1.0
        self.threshold_db = threshold_db
        angle = (pan + 1) * math.pi / 4                  # constant-power pan, -1 left .. 1 right
        self.lr = np.array([math.cos(angle), math.sin(angle)], np.float32)
        self.calibrate = int(CALIBRATE_S / FRAME_S)
        self.hang = int(HANG_S / FRAME_S)
        self.history = deque(maxlen=int(HISTORY_S / FRAME_S))
        self.pre_roll = deque(maxlen=max(1, int(PRE_ROLL_S / FRAME_S)))
        self.carrier_zi = np.zeros(1)
        self.carrier_a = 1 - math.exp(-1 / (0.05 * AUDIO_RATE))   # 50 ms carrier average
        self.floor_db = None
        self.floor_age = 0
        self.level_db = -200.0
        self.loud = self.quiet = 0
        self.clip = None             # frames of the transmission in progress, or None
        self.opened_at = None
        self.peak_db = 0.0
        self.raw = None              # wave writer for the continuous recording, if any
        self.raw_samples = 0
        # What this channel heard, for the summary.
        self.monitored_s = 0.0
        self.transmissions = 0
        self.blips = 0
        self.talk_s = 0.0
        self.best_db = None

    @property
    def open(self):
        return self.clip is not None

    def process(self, iq, now, t0):
        """Demodulate one frame ending at unix time `now` (session start `t0`).
        Returns (audio to mix, finished Transmission or None)."""
        self.monitored_s += FRAME_S
        env = np.abs(iq)
        carrier, self.carrier_zi = lfilter([self.carrier_a], [1, self.carrier_a - 1], env,
                                           zi=self.carrier_zi)
        # Carrier-normalized envelope: the modulation itself, the same loudness for
        # a strong tower and a weak aircraft.
        audio = ((env - carrier) / np.maximum(carrier, 1e-6)).astype(np.float32)
        db = 10 * math.log10(float(np.mean(env * env)) + 1e-20)
        self.level_db = db
        self.write_raw(audio, now - FRAME_S - t0)
        if not self.squelch:
            return audio * self.gain, None

        finished = None
        if len(self.history) < self.calibrate:
            self.history.append(db)
        else:
            self.floor_age -= 1
            if self.floor_db is None or self.floor_age <= 0:
                self.floor_db = float(np.percentile(self.history, 20))
                self.floor_age = FLOOR_EVERY
            open_at = self.floor_db + self.threshold_db
            if not self.open:
                self.loud = self.loud + 1 if db > open_at else 0
                if self.loud >= OPEN_FRAMES:
                    self.clip, self.quiet, self.opened_at = list(self.pre_roll), 0, now
                    self.peak_db = db - self.floor_db
                else:
                    self.history.append(db)
            else:
                self.peak_db = max(self.peak_db, db - self.floor_db)
                self.quiet = self.quiet + 1 if db < open_at - HYSTERESIS_DB else 0
                if self.quiet >= self.hang:
                    finished = self.end()
        self.pre_roll.append(audio)
        if self.open:
            self.clip.append(audio)
            return audio * self.gain, finished
        return np.zeros_like(audio), finished

    def end(self):
        """Close the transmission in progress (squelch closed, or the window is
        moving on). Returns it, or None if none was open."""
        if not self.open:
            return None
        kept = self.clip[: max(1, len(self.clip) - self.quiet + TAIL_FRAMES)]
        tx = Transmission(self, self.opened_at, np.concatenate(kept), self.peak_db)
        self.clip, self.loud, self.quiet = None, 0, 0
        self.pre_roll.clear()
        return tx

    def write_raw(self, audio, at_s):
        """Append to the raw file at `at_s` seconds into the session, padding with
        silence for any time this channel wasn't monitored, so every channel's
        raw file lines up with the clock."""
        if not self.raw:
            return
        gap = int(at_s * AUDIO_RATE) - self.raw_samples
        if gap > 0:
            self.raw.writeframes(bytes(2 * gap))
            self.raw_samples += gap
        self.raw.writeframes((np.clip(audio * 0.5, -1, 1) * 32767).astype(np.int16).tobytes())
        self.raw_samples += len(audio)


# ------------------------------------------------------------------ planning

@dataclass
class Window:
    center: float                # LO, Hz
    channels: list = field(default_factory=list)    # Freq

    def span(self):
        return self.channels[0].mhz, self.channels[-1].mhz


def smooth(n):
    """True if n has no prime factors above 5 (fast FFT lengths)."""
    for p in (2, 3, 5):
        while n % p == 0:
            n //= p
    return n == 1


def rates(limit):
    """Sample rates the filter bank can use, up to `limit`: multiples of 240 kHz
    whose FFT length ((rate / 48 kHz) * OUT_BLOCK) has only small factors."""
    return [r for r in range(2_400_000, limit + 1, 240_000) if smooth(r // AUDIO_RATE)]


def fit_center(freqs, rate):
    """An LO that holds all `freqs` (MHz) at `rate` with none at DC, or None."""
    lo, hi = freqs[0] * 1e6, freqs[-1] * 1e6
    reach = rate / 2 * USABLE - STOP_HZ
    mid = (lo + hi) / 2
    for shift in [0] + [s * k * 10e3 for k in range(1, 40) for s in (1, -1)]:
        c = mid + shift
        if (max(abs(lo - c), abs(hi - c)) <= reach
                and all(abs(f * 1e6 - c) >= DC_GUARD for f in freqs)):
            return c
    return None


def plan(channels, limit):
    """Split `channels` (sorted Freq) into the fewest capture windows that fit at
    one sample rate <= `limit`. Returns (rate, [Window])."""
    usable = rates(limit)
    if not usable:
        sys.exit(f"No usable sample rate at or below {limit / 1e6:.2f} MSPS.")
    # Everything in one window, at the lowest rate that holds it?
    mhz = [c.mhz for c in channels]
    for rate in usable:
        center = fit_center(mhz, rate)
        if center is not None:
            return rate, [Window(center, list(channels))]
    # Otherwise windows at the highest rate, filled greedily from the bottom.
    rate, windows, current = usable[-1], [], []
    for ch in channels:
        if current and fit_center([c.mhz for c in current + [ch]], rate) is None:
            windows.append(Window(fit_center([c.mhz for c in current], rate), current))
            current = []
        current.append(ch)
    windows.append(Window(fit_center([c.mhz for c in current], rate), current))
    return rate, windows


def describe_plan(rate, windows, dwell, uri):
    lines = [f"{sum(len(w.channels) for w in windows)} channels at {rate / 1e6:g} MSPS "
             f"({link_name(uri)})."]
    if len(windows) == 1:
        lines.append("All channels fit in one capture: every channel is monitored all the time.")
    else:
        lines.append(f"They need {len(windows)} capture windows, so the radio rotates "
                     f"between them, {dwell:g} s each: each channel is monitored about "
                     f"{100 / len(windows):.0f}% of the time.")
        if not is_ethernet(uri) and not is_rtl(uri):
            lines.append("Over Ethernet (--device ip:...) they fit in one capture.")
    for i, w in enumerate(windows, 1):
        lo, hi = w.span()
        names = ", ".join(f"{c.name} {c.mhz:g}" for c in w.channels)
        lines.append(f"  window {i}: {lo:.3f}-{hi:.3f} MHz ({len(w.channels)} ch): {names}")
    return "\n".join(lines)


# ------------------------------------------------------------------ monitor

def find_uri(device):
    return device if is_rtl(device) else pluto.find_uri(device)


def max_rate(uri):
    return RTL_MAX_RATE if is_rtl(uri) else pluto.max_rate(uri)


def link_name(uri):
    return "RTL-SDR" if is_rtl(uri) else "Ethernet" if is_ethernet(uri) else "USB"


def open_radio(uri, center, rate, gain):
    return open_rtl(uri, center, rate, gain) if is_rtl(uri) else pluto.open_pluto(uri, center, rate, gain)


def _terminate(*_):
    raise KeyboardInterrupt


def monitor(freqs, opts):
    """Run the receiver until opts.duration (s) passes or Ctrl-C.

    opts fields: device, gain, threshold, dwell, duration, listen, volume, mono,
    audio_device, record (mix WAV), raw (dir), clips (dir), clean (dir),
    strength, no_mp3, log (CSV path), summary (text path). Returns the Channels."""
    signal.signal(signal.SIGTERM, _terminate)
    uri = find_uri(opts.device)
    rate, windows = plan(fq.dedupe(freqs), max_rate(uri))
    print(describe_plan(rate, windows, opts.dwell, uri))

    channels = {}
    all_freqs = [c for w in windows for c in w.channels]
    for i, f in enumerate(all_freqs):
        pan = 0.0 if opts.mono or len(all_freqs) == 1 else -0.8 + 1.6 * i / (len(all_freqs) - 1)
        channels[f] = Channel(f, pan, opts.threshold)
    win_channels = [[channels[f] for f in w.channels] for w in windows]
    banks = [FilterBank(rate, [f.mhz * 1e6 - w.center for f in w.channels]) for w in windows]

    for d in (opts.raw, opts.clips, opts.clean):
        if d:
            os.makedirs(d, exist_ok=True)
    mp3 = None if opts.no_mp3 else audiofile.find_lame()
    started = datetime.now()
    if opts.raw:
        for c in channels.values():
            c.raw = wave.open(audiofile.clip_base(opts.raw, c.mhz, c.name,
                                                  f"{started:%Y%m%d-%H%M%S}") + ".wav", "wb")
            c.raw.setnchannels(1); c.raw.setsampwidth(2); c.raw.setframerate(AUDIO_RATE)
    log = None
    if opts.log:
        os.makedirs(os.path.dirname(opts.log) or ".", exist_ok=True)
        log = open(opts.log, "w", buffering=1)
        log.write("time,mhz,name,kind,seconds,peak_db,clip\n")

    radio = open_radio(uri, windows[0].center, rate, opts.gain)
    print(f"{'RTL-SDR' if is_rtl(uri) else 'Pluto'} at {uri}, gain {opts.gain}. "
          "Squelches calibrate for 2 s. Ctrl-C to stop.\n")

    stop = threading.Event()
    raw_q: queue.Queue = queue.Queue(maxsize=64)    # ~4 s of IQ at 7.68 MSPS
    audio_q: queue.Queue = queue.Queue(maxsize=200)
    events: queue.Queue = queue.Queue()
    stats = dict(received=0, lost=0, dead_s=0.0, reconnects=0)
    state = dict(window=0, next_switch=None)
    t0 = time.time()

    def reader():
        """Only reads (and retunes) the Pluto, so a slow moment in the DSP never
        makes it overflow. Reconnects after a USB or network error."""
        win, switch_at = 0, time.time() + opts.dwell
        raw_q.put(("window", 0, time.time()))
        try:
            while not stop.is_set():
                error = None
                try:
                    if len(windows) > 1 and time.time() >= switch_at:
                        t = time.time()
                        win = (win + 1) % len(windows)
                        radio.tune(windows[win].center)
                        radio.flush()
                        stats["dead_s"] += time.time() - t
                        switch_at = time.time() + opts.dwell
                        raw_q.put(("window", win, time.time()))
                    state.update(window=win, next_switch=switch_at)
                    raw = radio.read()
                except OSError as e:
                    error = str(e)
                if error is None:
                    stats["received"] += len(raw) // 2
                    try:
                        raw_q.put_nowait(raw)
                    except queue.Full:
                        stats["lost"] += len(raw) // 2
                    continue
                # Reconnect outside the except block (see Pluto).
                print(f"\r\033[KConnection error ({error}); reconnecting...")
                gone = radio.reconnect(stop)
                if gone is None:
                    break
                stats["reconnects"] += 1
                stats["dead_s"] += gone
                print(f"\r\033[KReconnected after {gone:.1f} s.")
                switch_at = time.time() + opts.dwell
                raw_q.put(("gap", time.time()))
        finally:
            raw_q.put(None)

    def dsp():
        active, t = None, time.time()
        try:
            while (item := raw_q.get()) is not None:
                if isinstance(item, tuple):
                    # Retuned or reconnected: what was open on this window ends here.
                    if active is not None:
                        for ch in win_channels[active]:
                            if (tx := ch.end()) is not None:
                                events.put(tx)
                    if item[0] == "window":
                        active = item[1]
                    banks[active].reset()
                    t = max(t, item[-1])
                    continue
                for block in banks[active].process(to_complex(item)):
                    t += FRAME_S
                    mix = np.zeros((KEEP, 2), np.float32)
                    for ch, x in zip(win_channels[active], block):
                        audio, finished = ch.process(x, t, t0)
                        mix += audio[:, None] * ch.lr
                        if finished is not None:
                            events.put(finished)
                    mix = np.clip(mix * opts.volume, -1, 1)
                    if rec:
                        rec.writeframes((mix * 32767).astype(np.int16).tobytes())
                    if stream is not None:
                        try:
                            audio_q.put_nowait(mix)
                        except queue.Full:          # SDR clock a little fast: drop a frame
                            pass
                t = max(t, time.time() - 1.0)       # keep the clock honest across hiccups
        finally:
            if active is not None:
                for ch in win_channels[active]:
                    if (tx := ch.end()) is not None:
                        events.put(tx)
            stop.set()

    stream = _audio_stream(opts, audio_q) if opts.listen else None
    rec = None
    if opts.record:
        rec = wave.open(opts.record, "wb")
        rec.setnchannels(2); rec.setsampwidth(2); rec.setframerate(AUDIO_RATE)

    worker = threading.Thread(target=dsp, daemon=True)
    worker.start()
    threading.Thread(target=reader, daemon=True).start()
    if stream is not None:
        stream.start()

    def handle(tx):
        ch = tx.channel
        when = datetime.fromtimestamp(tx.start)
        line = f"{when:%H:%M:%S}  {ch.name:<11} {ch.mhz:8.3f}  {tx.seconds:5.1f} s  {tx.peak_db:+5.1f} dB"
        clip = ""
        if tx.seconds < MIN_CLIP_S:
            ch.blips += 1
            line += "  (blip)"
        else:
            ch.transmissions += 1
            ch.talk_s += tx.seconds
            ch.best_db = max(ch.best_db or tx.peak_db, tx.peak_db)
            stamp = f"{when:%Y%m%d-%H%M%S}"
            title = f"{ch.name} {ch.mhz:.3f} MHz {stamp}"
            if opts.clips:
                clip = audiofile.save_clip(tx.audio, audiofile.clip_base(
                    opts.clips, ch.mhz, ch.name, stamp), AUDIO_RATE, mp3, title)[0]
                line += f"  -> {clip}"
            if opts.clean:
                audiofile.save_clip(audiofile.clean(tx.audio, AUDIO_RATE, opts.strength),
                                    audiofile.clip_base(opts.clean, ch.mhz, ch.name, stamp),
                                    AUDIO_RATE, mp3, title + " (cleaned)")
        if log:
            log.write(f"{when:%Y-%m-%d %H:%M:%S},{ch.mhz:.3f},{ch.name},{ch.kind},"
                      f"{tx.seconds:.1f},{tx.peak_db:.1f},{clip}\n")
        print("\r\033[K" + line)

    try:
        while not stop.is_set():
            if opts.duration and time.time() - t0 >= opts.duration:
                break
            try:
                handle(events.get(timeout=0.25))
            except queue.Empty:
                pass
            print("\r\033[K" + _status(windows, win_channels, state, time.time() - t0,
                                       opts.duration), end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=3)
        while not events.empty():
            handle(events.get())
        if stream is not None:
            stream.stop(); stream.close()
        if rec:
            rec.close()
        elapsed = time.time() - t0
        # Pad every raw file to the same end, so they all line up.
        end = max([elapsed] + [c.raw_samples / AUDIO_RATE for c in channels.values() if c.raw])
        for c in channels.values():
            if c.raw:
                c.write_raw(np.zeros(0, np.float32), end)
                c.raw.close()
        if log:
            log.close()
        report = summary(list(channels.values()), elapsed, started)
        print("\n\n" + report)
        if opts.summary:
            with open(opts.summary, "w") as f:
                f.write(report + "\n")
        live = elapsed - stats["dead_s"]
        got = (stats["received"] - stats["lost"]) / (rate * live) if live > 0 else 1
        if stats["reconnects"]:
            print(f"The radio disconnected {stats['reconnects']} time(s); the raw files "
                  "have silence for those gaps.")
        if got < 0.97:
            print(f"warning: received only {100 * got:.0f}% of the samples; the link or "
                  "this computer couldn't keep up, so some audio is missing.")
    return list(channels.values())


def _audio_stream(opts, audio_q):
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("sounddevice not found. Install it with: pip install sounddevice "
                 "(or run without audio).")
    pending = [np.zeros((0, 2), np.float32)]

    def callback(out, frames, _time, _status):
        have = pending[0]
        while len(have) < frames:
            try:
                have = np.concatenate([have, audio_q.get_nowait()])
            except queue.Empty:
                break
        n = min(frames, len(have))
        out[:n] = have[:n]
        out[n:] = 0
        pending[0] = have[n:]

    return sd.OutputStream(samplerate=AUDIO_RATE, channels=2, dtype="float32",
                           device=opts.audio_device, callback=callback, blocksize=1024)


def _status(windows, win_channels, state, elapsed, duration):
    """One status line, cut to the terminal width: the active window, the
    channels talking right now, and when the window changes."""
    i = state["window"]
    chans = win_channels[i]
    parts = []
    if len(windows) > 1:
        lo, hi = windows[i].span()
        left = max(0, (state["next_switch"] or time.time()) - time.time())
        parts.append(f"window {i + 1}/{len(windows)} {lo:.1f}-{hi:.1f} MHz, next in {left:2.0f}s")
    if any(c.floor_db is None and c.squelch for c in chans):
        parts.append("calibrating")
    talking = [f"● {c.name} {c.level_db - c.floor_db:+.0f}" for c in chans
               if c.open and c.floor_db is not None]
    parts.append("  ".join(talking) if talking else f"{len(chans)} ch quiet")
    parts.append(f"{elapsed:.0f}" + (f"/{duration:.0f} s" if duration else " s"))
    return "  |  ".join(parts)[: shutil.get_terminal_size((120, 20)).columns - 1]


def summary(channels, elapsed, started):
    """What every channel heard, as a text table."""
    rows = [f"Summary: {started:%Y-%m-%d %H:%M:%S}, {elapsed / 60:.1f} min",
            f"{'MHz':>8}  {'Channel':<11} {'Kind':<10} {'Heard':>7} {'Tx':>4} {'Talk':>7} "
            f"{'Blips':>5} {'Peak':>6}"]
    for c in sorted(channels, key=lambda c: c.mhz):
        peak = f"{c.best_db:+5.1f}" if c.best_db is not None else "    -"
        rows.append(f"{c.mhz:8.3f}  {c.name:<11} {c.kind:<10} {c.monitored_s:6.0f}s "
                    f"{c.transmissions:4d} {c.talk_s:6.0f}s {c.blips:5d} {peak:>6}")
    active = [c for c in channels if c.transmissions]
    rows.append(f"{len(active)} of {len(channels)} channels had traffic; "
                f"{sum(c.transmissions for c in channels)} transmissions, "
                f"{sum(c.talk_s for c in channels):.0f} s of talk. "
                "Heard = time monitored; Peak = loudest transmission, dB above that "
                "channel's noise floor.")
    return "\n".join(rows)


# ------------------------------------------------------------------ CLIs

def add_receiver_args(p, listen_default):
    p.add_argument("--device", default="",
                   help="Pluto libiio URI (e.g. ip:pluto.local), or rtl / rtl:N for an RTL-SDR "
                        "(default: auto-detect a Pluto)")
    p.add_argument("--gain", default="40", help="RF gain in dB (0-73), or auto / fast (default 40)")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above a channel's noise floor that opens its squelch (default 8)")
    p.add_argument("--dwell", type=float, default=30.0,
                   help="seconds per capture window when the channels need several (default 30)")
    p.add_argument("--atis", action="store_true", help="include ATIS, mixed in quietly, no squelch")
    if listen_default:
        p.add_argument("--no-audio", dest="listen", action="store_false",
                       help="don't play audio (log / record only)")
    else:
        p.add_argument("--listen", action="store_true", help="also play the audio")
    p.add_argument("--volume", type=float, default=0.5, help="output volume (default 0.5)")
    p.add_argument("--mono", action="store_true", help="no stereo spread")
    p.add_argument("--audio-device", default=None, help="output device name or number")
    p.add_argument("--strength", type=float, default=0.9,
                   help="noise reduction for cleaned clips, 0-1 (default 0.9)")
    p.add_argument("--no-mp3", action="store_true", help="clips as WAV only")


def area_main(area, default_minutes=10):
    """CLI for an area test script (radio/south_bay.py etc.): record every
    transmission on the area's channels into recordings/<area>_<time>/."""
    a = fq.AREAS[area]
    p = argparse.ArgumentParser(
        description=f"{a['title']}. Monitors the tower, ground, clearance and approach / "
                    f"departure channels, saves each transmission (raw and cleaned) and "
                    f"ends with a summary of what was heard.",
        epilog="Output: recordings/<area>_<time>/clips, clips_clean, transmissions.csv, "
               "summary.txt (and raw/ with --raw).")
    add_receiver_args(p, listen_default=False)
    p.add_argument("--minutes", type=float, default=default_minutes,
                   help=f"how long to listen (default {default_minutes:g}); 0 = until Ctrl-C")
    p.add_argument("--out", help="output folder (default recordings/<area>_<time>)")
    p.add_argument("--raw", action="store_true",
                   help="also keep one continuous WAV per channel (large: ~5.8 MB/min each)")
    p.add_argument("--no-clean", action="store_true", help="don't save cleaned copies of the clips")
    p.add_argument("--list", action="store_true", help="print the channels and the plan, then exit")
    args = p.parse_args()

    freqs = fq.area_channels(area, atis=args.atis)
    if args.list:
        uri = find_uri(args.device)
        print(a["title"])
        if uri == FALLBACK_URI and not args.device:
            print("(No Pluto found; planning for a USB connection.)")
        rate, windows = plan(freqs, max_rate(uri))
        print(describe_plan(rate, windows, args.dwell, uri))
        return
    out = args.out or os.path.join("recordings", f"{area}_{datetime.now():%Y%m%d-%H%M}")
    opts = argparse.Namespace(**{
        **vars(args), "duration": args.minutes * 60 or None, "record": None,
        "raw": os.path.join(out, "raw") if args.raw else None,
        "clips": os.path.join(out, "clips"),
        "clean": None if args.no_clean else os.path.join(out, "clips_clean"),
        "log": os.path.join(out, "transmissions.csv"),
        "summary": os.path.join(out, "summary.txt")})
    print(f"{a['title']}\nSaving to {out}/\n")
    monitor(freqs, opts)
