#!/usr/bin/env python3
"""
Listen to several airband (AM) channels at once from one Pluto capture.

    python3 multi_atc.py                       # SFO clearance, tower, ground, departure, guard
    python3 multi_atc.py --atis                # also the ATIS loop, mixed in quietly
    python3 multi_atc.py --clips clips/sfo     # also save every transmission per channel
    python3 multi_atc.py --channels "120.5 Tower, 121.8 Ground, 118.2 Clearance"
    python3 multi_atc.py --preset norcal --device ip:192.168.1.10 \
        --duration 300 --no-audio --raw recordings/raw --clips recordings/clips

Every channel has its own squelch that learns that channel's noise floor, so you
only hear a channel while someone is transmitting on it. Channels are spread
across the stereo field (lowest frequency left, highest right) so you can tell
them apart. A status line shows which channel is talking, and each transmission
is logged with its time, channel and length.

All the channels must fit in one capture. Over USB 2.0 the Pluto streams at most
about 8 MSPS (roughly 6.5 MHz of spectrum): SFO's airport channels (118.2-124.25
MHz) fit. The NorCal approach/departure preset spans 120.1-135.2 MHz and needs
about 19 MSPS, which only gigabit Ethernet (an ip: --device) can carry. If the
link or this computer can't keep up, the script says how many samples it lost.

--raw writes one continuous WAV per channel (squelch open or not), and --clips
saves each transmission as its own file named <MHz>_<name>_<date>-<time>.

Signal chain:
    Pluto (libiio, int16 IQ) -> blocks of N samples, 50% overlap
    -> one FFT per block -> for each channel, the bins around it, shaped to a
       +-5 kHz channel filter, inverse FFT at 48 kHz (an FFT filter bank)
    -> AM envelope, normalized by the carrier level -> per-channel squelch
    -> constant-power stereo pan -> sum -> speakers (and optional WAV / clips)
GNU Radio's Pluto source tops out near 6.5 MSPS on this machine, below what the
SFO channels need, so this script reads the Pluto directly instead.
"""

import argparse
import gc
import math
import os
import queue
import signal
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime

import numpy as np
import scipy.fft
from scipy.signal import lfilter

from capture_clips import find_lame, save_clip

AUDIO_RATE = 48_000
OUT_BLOCK = 1024             # output samples per FFT block (half are kept)
USB_MAX_RATE = 8_160_000     # highest sample rate the Pluto sustains over USB 2.0 here
NET_MAX_RATE = 30_720_000    # over gigabit Ethernet (an ip: URI); checked at run time
USABLE = 0.85                # fraction of the sampled band to use; measured flat to
                             # 94% at 7.68 MSPS, with the noise rising ~5 dB at the edges
DC_GUARD = 30e3              # keep channels this far from the LO (DC offset lives there)

PASS_HZ = 5_000              # channel filter: flat to +-5 kHz ...
STOP_HZ = 7_000              # ... and zero from +-7 kHz (channels are 25 kHz apart)

# San Francisco (KSFO) from AirNav. ATIS is a continuous loop, so it is opt-in.
SFO_CHANNELS = [
    (118.20, "Clearance"),
    (120.50, "Tower"),
    (120.90, "Departure"),
    (121.50, "Guard"),
    (121.80, "Ground"),
    (124.25, "Ground 2"),
]
SFO_ATIS = (118.85, "ATIS")

# NorCal TRACON approach / departure frequencies for SFO, OAK, SJC and PAO (AirNav).
NORCAL_CHANNELS = [
    (120.100, "SJC App"),
    (120.900, "Dep NW"),
    (121.300, "SJC Dep"),
    (124.525, "OAK Arr S"),
    (124.800, "OAK Arr"),
    (125.350, "App East"),
    (126.475, "SJC App 2"),
    (127.000, "App North"),
    (128.325, "SFO Arr E"),
    (128.575, "SFO Arr S"),
    (133.950, "App South"),
    (134.500, "App East 2"),
    (135.100, "Dep West"),
    (135.200, "SJC App W"),
]
# The lower half of NORCAL_CHANNELS: the most that fits in one USB 2.0 capture.
NORCAL_USB_CHANNELS = [c for c in NORCAL_CHANNELS if c[0] < 127]
PRESETS = {"sfo": SFO_CHANNELS, "norcal": NORCAL_CHANNELS, "norcal-usb": NORCAL_USB_CHANNELS}
ATIS_GAIN = 0.3

# Squelch, per channel. Frames are one output block (OUT_BLOCK / 2 samples, ~10.7 ms).
CALIBRATE_S = 2.0            # learn the noise floor this long before opening
HISTORY_S = 10.0             # noise floor = 20th percentile of this much quiet
OPEN_FRAMES = 2              # this many frames above the threshold open the squelch
HANG_S = 1.0                 # it closes after this much quiet (pauses don't split)
HYSTERESIS_DB = 3.0
PRE_ROLL_S = 0.3             # audio kept from just before the squelch opened
MIN_CLIP_S = 1.0


class FilterBank:
    """Split wideband IQ into channels at AUDIO_RATE with one FFT per block.

    Overlap-save with 50% overlap: each block of N = D * OUT_BLOCK samples is
    transformed once; for each channel the OUT_BLOCK bins around it are weighted
    by the channel filter and inverse-transformed, which also decimates by D.
    The middle half of each output block is kept, so blocks tile exactly and
    the filter's short impulse response never wraps around."""

    def __init__(self, rate, offsets_hz):
        self.decim = int(rate // AUDIO_RATE)
        self.n = self.decim * OUT_BLOCK
        self.hop = self.n // 2
        bin_hz = rate / self.n
        self.centers = [int(round(off / bin_hz)) for off in offsets_hz]
        f = np.fft.fftfreq(OUT_BLOCK, 1 / AUDIO_RATE)
        taper = np.clip((STOP_HZ - np.abs(f)) / (STOP_HZ - PASS_HZ), 0, 1)
        self.shape = (0.5 - 0.5 * np.cos(np.pi * taper)).astype(np.float32)
        window_bins = np.fft.fftfreq(OUT_BLOCK, 1 / OUT_BLOCK).astype(int)
        self.index = (np.array(self.centers)[:, None] + window_bins) % self.n
        self.buf = np.zeros(0, np.complex64)

    def process(self, iq):
        """Yield one (channels, OUT_BLOCK / 2) complex array per completed hop."""
        self.buf = np.concatenate([self.buf, iq])
        q = OUT_BLOCK // 4
        while len(self.buf) >= self.n:
            # scipy.fft keeps complex64 in single precision: ~2x faster than numpy here.
            spectrum = scipy.fft.fft(self.buf[: self.n])
            out = scipy.fft.ifft(spectrum[self.index] * self.shape, axis=1)
            self.buf = self.buf[self.hop:]
            yield out[:, q: q + OUT_BLOCK // 2].astype(np.complex64)


class Channel:
    """AM demodulator, self-calibrating squelch and transmission capture for one channel."""

    def __init__(self, freq, name, pan, gain=1.0, squelch=True, threshold_db=8.0):
        self.freq, self.name, self.gain, self.squelch = freq, name, gain, squelch
        self.threshold_db = threshold_db
        angle = (pan + 1) * math.pi / 4                  # constant-power pan, -1 left .. 1 right
        self.lr = np.array([math.cos(angle), math.sin(angle)], np.float32)
        frame_s = (OUT_BLOCK // 2) / AUDIO_RATE
        self.calibrate = int(CALIBRATE_S / frame_s)
        self.hang = int(HANG_S / frame_s)
        self.history = deque(maxlen=int(HISTORY_S / frame_s))
        self.pre_roll = deque(maxlen=max(1, int(PRE_ROLL_S / frame_s)))
        self.carrier_zi = np.zeros(1)
        self.carrier_a = 1 - math.exp(-1 / (0.05 * AUDIO_RATE))   # 50 ms carrier average
        self.floor_db = None
        self.level_db = -200.0
        self.loud = self.quiet = 0
        self.open = False
        self.opened_at = None
        self.clip = None
        self.muted = False
        self.raw = None              # wave writer for the continuous recording, if any

    def process(self, iq, now):
        """Demodulate one frame. Returns (audio, finished) where finished is
        (start time, audio) when a transmission just ended, else None."""
        env = np.abs(iq)
        carrier, self.carrier_zi = lfilter([self.carrier_a], [1, self.carrier_a - 1], env,
                                           zi=self.carrier_zi)
        # Carrier-normalized envelope: the modulation itself, the same loudness for
        # a strong tower and a weak aircraft.
        audio = ((env - carrier) / np.maximum(carrier, 1e-6)).astype(np.float32)
        db = 10 * math.log10(float(np.mean(env * env)) + 1e-20)
        self.level_db = db
        if self.raw:
            self.raw.writeframes((np.clip(audio * 0.5, -1, 1) * 32767).astype(np.int16).tobytes())
        if not self.squelch:
            return audio * self.gain, None

        finished = None
        if len(self.history) < self.calibrate:
            self.history.append(db)
        else:
            self.floor_db = float(np.percentile(self.history, 20))
            open_at = self.floor_db + self.threshold_db
            if not self.open:
                self.loud = self.loud + 1 if db > open_at else 0
                if self.loud >= OPEN_FRAMES:
                    self.open, self.quiet, self.opened_at = True, 0, now
                    self.clip = list(self.pre_roll)
                else:
                    self.history.append(db)
            else:
                self.quiet = self.quiet + 1 if db < open_at - HYSTERESIS_DB else 0
                if self.quiet >= self.hang:
                    self.open = False
                    self.loud = 0
                    kept = self.clip[: len(self.clip) - self.quiet + 10]
                    finished = (self.opened_at, np.concatenate(kept))
                    self.clip = None
        self.pre_roll.append(audio)
        if self.open:
            self.clip.append(audio)
            return audio * self.gain, finished
        return np.zeros_like(audio), finished


# -------------------------------------------------------------------- sources

def find_uri(device):
    try:
        import iio
    except ImportError:
        sys.exit("libiio Python bindings not found (conda install -c conda-forge libiio).")
    if device:
        return device
    found = [u for u, d in iio.scan_contexts().items() if "PlutoSDR" in d or "ad936" in d.lower()]
    return found[0] if found else "ip:192.168.2.1"


def open_pluto(uri, args, center, rate, stop, fatal=True):
    """Configure the Pluto and return a generator of int16 IQ. If it can't be
    opened: exit with a hint (fatal), or raise OSError (when reconnecting)."""
    import iio
    try:
        ctx = iio.Context(uri)
    except OSError as e:
        if not fatal:
            raise
        sys.exit(f"Could not open Pluto at {uri}: {e}\nTry `iio_info -s` to list devices. "
                 "Only one program can use the Pluto at a time.")
    phy = ctx.find_device("ad9361-phy")
    rx = ctx.find_device("cf-ad9361-lpc")
    phy.find_channel("altvoltage0", True).attrs["frequency"].value = str(int(center))
    ch = phy.find_channel("voltage0", False)
    ch.attrs["sampling_frequency"].value = str(int(rate))
    ch.attrs["rf_bandwidth"].value = str(int(rate))
    if args.gain in ("auto", "fast"):
        ch.attrs["gain_control_mode"].value = "slow_attack" if args.gain == "auto" else "fast_attack"
    else:
        ch.attrs["gain_control_mode"].value = "manual"
        ch.attrs["hardwaregain"].value = str(float(args.gain))
    for name in ("voltage0", "voltage1"):
        rx.find_channel(name, False).enabled = True
    rx.set_kernel_buffers_count(8)
    buf = iio.Buffer(rx, 1 << 19, False)
    print(f"Pluto at {uri}: {center / 1e6:.3f} MHz center, {rate / 1e6:g} MSPS, gain {args.gain}")

    def chunks():
        while not stop.is_set():
            buf.refill()
            yield np.frombuffer(buf.read(), dtype=np.int16).copy()

    return chunks()


# ----------------------------------------------------------------------- plan

def parse_channels(text):
    """'120.5 Tower, 121.8 Ground' -> [(120.5, 'Tower'), (121.8, 'Ground')]"""
    out = []
    for item in text.split(","):
        parts = item.split(None, 1)
        if parts:
            out.append((float(parts[0]), parts[1].strip() if len(parts) > 1 else f"{float(parts[0]):g}"))
    return out


def smooth(n):
    """True if n has no prime factors above 5 (fast FFT lengths)."""
    for p in (2, 3, 5):
        while n % p == 0:
            n //= p
    return n == 1


def plan_capture(freqs_mhz, max_rate):
    """Pick an LO and the lowest sample rate that holds every channel.
    Returns (center_hz, rate) or exits with an explanation."""
    lo_f, hi_f = min(freqs_mhz) * 1e6, max(freqs_mhz) * 1e6
    center = (lo_f + hi_f) / 2
    # Keep DC clear of every channel.
    while any(abs(f * 1e6 - center) < DC_GUARD for f in freqs_mhz):
        center += 2 * DC_GUARD
    half = max(abs(f * 1e6 - center) for f in freqs_mhz) + STOP_HZ
    for rate in range(2_400_000, max_rate + 1, 240_000):
        # The filter bank's FFT is (rate / 48k) * OUT_BLOCK long; keep its factors small.
        if half <= rate / 2 * USABLE and smooth(rate // AUDIO_RATE):
            return center, rate
    need = 2 * half / USABLE / 1e6
    sys.exit(f"These channels span {(hi_f - lo_f) / 1e6:.2f} MHz, which needs about "
             f"{need:.1f} MSPS; this link carries at most {max_rate / 1e6:.2f} MSPS. "
             "Connect the Pluto over Ethernet (--device ip:...), drop the outermost "
             "channels (--channels), or use a second SDR.")


# ----------------------------------------------------------------------- main

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--preset", choices=sorted(PRESETS), default="sfo",
                   help="sfo: SFO clearance/tower/ground/departure/guard (default); "
                        "norcal: Bay Area approach and departure, 14 channels (needs "
                        "Ethernet); norcal-usb: the 7 of those in 120.1-126.475 MHz")
    p.add_argument("--channels", metavar="LIST",
                   help='comma-separated "MHz Name" list, instead of a preset')
    p.add_argument("--atis", action="store_true",
                   help="also mix in SFO ATIS (118.85), quietly and without squelch")
    p.add_argument("--device", default="", help="Pluto libiio URI (default: auto-detect)")
    p.add_argument("--gain", default="40", help="RF gain in dB (0-73), or auto / fast")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above a channel's noise floor that opens its squelch (default 8)")
    p.add_argument("--volume", type=float, default=0.5, help="output volume (default 0.5)")
    p.add_argument("--mono", action="store_true", help="no stereo spread")
    p.add_argument("--no-audio", action="store_true", help="don't play audio (log / record only)")
    p.add_argument("--audio-device", default=None, help="output device name or number")
    p.add_argument("--record", metavar="WAV", help="also write the stereo mix to a WAV file")
    p.add_argument("--raw", metavar="DIR", help="write one continuous WAV per channel in DIR")
    p.add_argument("--clips", metavar="DIR", help="save each transmission as a clip in DIR")
    p.add_argument("--duration", type=float, metavar="SECONDS", help="stop after this long")
    p.add_argument("--no-mp3", action="store_true", help="clips as WAV only")
    return p.parse_args()


def _terminate(*_):
    raise KeyboardInterrupt


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, _terminate)
    chans = parse_channels(args.channels) if args.channels else list(PRESETS[args.preset])
    if args.atis:
        chans.append(SFO_ATIS)
    chans.sort()
    uri = find_uri(args.device)
    max_rate = NET_MAX_RATE if uri.startswith("ip:") else USB_MAX_RATE
    center, rate = plan_capture([f for f, _ in chans], max_rate)

    channels = []
    for i, (f, name) in enumerate(chans):
        pan = 0.0 if args.mono or len(chans) == 1 else -0.8 + 1.6 * i / (len(chans) - 1)
        is_atis = args.atis and (f, name) == SFO_ATIS
        channels.append(Channel(f, name, pan, ATIS_GAIN if is_atis else 1.0,
                                squelch=not is_atis, threshold_db=args.threshold))
    bank = FilterBank(rate, [c.freq * 1e6 - center for c in channels])

    mp3 = None
    if args.clips:
        os.makedirs(args.clips, exist_ok=True)
        mp3 = None if args.no_mp3 else find_lame()

    stop = threading.Event()
    chunks = open_pluto(uri, args, center, rate, stop)
    started = datetime.now()
    if args.raw:
        os.makedirs(args.raw, exist_ok=True)
        for c in channels:
            path = os.path.join(args.raw, f"{c.freq:.3f}MHz_{c.name.replace(' ', '_')}_"
                                          f"{started:%Y%m%d-%H%M%S}.wav")
            c.raw = wave.open(path, "wb")
            c.raw.setnchannels(1); c.raw.setsampwidth(2); c.raw.setframerate(AUDIO_RATE)
    received, lost, reconnects = [0], [0], [0]
    raw_q: queue.Queue = queue.Queue(maxsize=64)             # ~4 s of IQ at 7.68 MSPS
    audio_q: queue.Queue = queue.Queue(maxsize=200)           # ~2 s of stereo frames
    events: queue.Queue = queue.Queue()

    stream = None
    if not args.no_audio:
        import sounddevice as sd
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

        stream = sd.OutputStream(samplerate=AUDIO_RATE, channels=2, dtype="float32",
                                 device=args.audio_device, callback=callback, blocksize=1024)

    rec = None
    if args.record:
        rec = wave.open(args.record, "wb")
        rec.setnchannels(2); rec.setsampwidth(2); rec.setframerate(AUDIO_RATE)

    def reader():
        """Only reads the Pluto, so a slow moment in the DSP never makes it overflow.
        After a USB error it reopens the Pluto and reports how long it was gone."""
        source = chunks
        try:
            while not stop.is_set():
                try:
                    for raw in source:
                        received[0] += len(raw) // 2
                        try:
                            raw_q.put_nowait(raw)
                        except queue.Full:
                            lost[0] += len(raw) // 2
                    return
                except OSError as e:
                    error = str(e)
                # Reconnect outside the except block: the exception's traceback holds the
                # old connection, which keeps the Pluto's USB interface claimed.
                source = None
                gc.collect()
                down = time.time()
                print(f"\r\033[KUSB error ({error}); reconnecting...")
                while source is None and not stop.is_set():
                    time.sleep(1)
                    try:
                        source = open_pluto(uri, args, center, rate, stop, fatal=False)
                    except OSError:
                        gc.collect()
                if source is not None:
                    reconnects[0] += 1
                    print(f"\r\033[KReconnected after {time.time() - down:.1f} s.")
                    raw_q.put(("gap", time.time() - down))
        finally:
            raw_q.put(None)

    def dsp():
        frame_s = (OUT_BLOCK // 2) / AUDIO_RATE
        t = time.time()
        try:
            while (raw := raw_q.get()) is not None:
                if isinstance(raw, tuple):
                    # The Pluto was gone for `gap` seconds: keep the raw files aligned
                    # in time with silence, and restart the filter bank cleanly.
                    gap = raw[1]
                    for c in channels:
                        if c.raw:
                            c.raw.writeframes(bytes(2 * int(gap * AUDIO_RATE)))
                    bank.buf = bank.buf[:0]
                    t += gap
                    continue
                # Interleaved int16 I/Q -> complex64 without an intermediate copy.
                iq = raw.astype(np.float32).view(np.complex64) * np.float32(1 / 2048)
                for block in bank.process(iq):
                    t += frame_s
                    mix = np.zeros((OUT_BLOCK // 2, 2), np.float32)
                    for ch, x in zip(channels, block):
                        audio, finished = ch.process(x, t)
                        if not ch.muted:
                            mix += audio[:, None] * ch.lr
                        if finished:
                            events.put((ch, *finished))
                    mix = np.clip(mix * args.volume, -1, 1)
                    if rec:
                        rec.writeframes((mix * 32767).astype(np.int16).tobytes())
                    if stream is not None:
                        try:
                            audio_q.put_nowait(mix)
                        except queue.Full:          # SDR clock a little fast: drop a frame
                            pass
                # Keep the clock honest across USB hiccups.
                t = max(t, time.time() - 1.0)
        finally:
            stop.set()

    worker = threading.Thread(target=dsp, daemon=True)
    worker.start()
    threading.Thread(target=reader, daemon=True).start()
    if stream is not None:
        stream.start()

    names = "  ".join(f"{c.name} {c.freq:g}" for c in channels)
    print(f"Listening to {len(channels)} channels: {names}")
    print("Squelches calibrate for 2 s. Ctrl-C to stop.\n")
    clips = 0
    t_start = time.time()
    try:
        while not stop.is_set():
            if args.duration and time.time() - t_start >= args.duration:
                break
            try:
                ch, start, audio = events.get(timeout=0.25)
                secs = len(audio) / AUDIO_RATE
                line = (f"{datetime.fromtimestamp(start):%H:%M:%S}  {ch.name:<10} "
                        f"{ch.freq:7.3f}  {secs:5.1f} s")
                if args.clips and secs >= MIN_CLIP_S:
                    stamp = datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S")
                    base = os.path.join(args.clips, f"{ch.freq:.3f}MHz_{ch.name.replace(' ', '_')}_{stamp}")
                    paths = save_clip(audio, base, AUDIO_RATE, mp3, f"{ch.name} {ch.freq:.3f} MHz {stamp}")
                    clips += 1
                    line += f"  -> {paths[-1]}"
                print("\r\033[K" + line)
            except queue.Empty:
                pass
            status = "  ".join(
                f"{'●' if c.open else '·'} {c.name} "
                f"{'cal' if c.floor_db is None and c.squelch else f'{c.level_db - (c.floor_db or c.level_db):+3.0f}'}"
                for c in channels)
            print("\r\033[K" + status, end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=2)
        if stream is not None:
            stream.stop(); stream.close()
        if rec:
            rec.close()
        for c in channels:
            if c.raw:
                c.raw.close()
        elapsed = time.time() - t_start
        got = (received[0] - lost[0]) / (rate * elapsed) if elapsed > 0 else 1
        if reconnects[0]:
            print(f"\nThe Pluto dropped off USB {reconnects[0]} time(s); those gaps are "
                  "silence in the raw files.")
        print(f"\nStopped after {elapsed:.0f} s."
              f"{f' Saved {clips} clips to {args.clips}.' if args.clips else ''}"
              f"{f' Raw audio in {args.raw}.' if args.raw else ''}")
        if got < 0.98:
            print(f"warning: received only {100 * got:.0f}% of the samples; the link or "
                  "this computer couldn't keep up, so some audio is missing.")


if __name__ == "__main__":
    main()
