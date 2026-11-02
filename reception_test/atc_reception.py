#!/usr/bin/env python3
"""
ATC reception test for an RTL-SDR: every Bay Area channel, ATIS included,
scanned the way a scanner radio does it.

    python3 reception_test/atc_reception.py                          # 20 minutes
    python3 reception_test/atc_reception.py --duration 1h --gain 35
    python3 reception_test/atc_reception.py --source synthetic --duration 2m   # no dongle

The RTL-SDR takes in about 2 MHz at a time, so the 51 channels (118.000-135.650
MHz) are split into 6 capture windows.

1. Calibrate: each window in turn, for a few seconds, to learn every channel's
   noise floor and the ADC level (clipping means too much gain).
2. Scan: hop from window to window, looking at each for a moment (--look).
   When a channel's squelch opens, stay until every channel in that window has
   been quiet for --linger seconds (so the reply is caught too), or for at most
   --max-visit. Each transmission is saved as a clip, plain and cleaned.

An ATIS broadcasts nonstop, so each one is recorded once (--atis-seconds of it)
and from then on doesn't hold the scan. A channel that stays open for a whole
--max-visit is a steady carrier or interference, and is ignored after that.

Saves, in recordings/atc_reception_<time>/: calibration.csv (each channel's
noise floor, each window's ADC level), visits.csv (every stop: window, seconds,
channels heard), transmissions.csv, clips/, clips_clean/ and summary.txt (what
each channel heard, and the channels never heard).
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
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
except ModuleNotFoundError as e:
    sys.exit(f"{e.name} not found: this Python ({sys.executable}) isn't the sdr environment.\n"
             "Run 'conda activate sdr' first (set it up with: bash reception_test/setup.sh).")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atcsdr import airband as A
from atcsdr import audio as audiofile
from atcsdr import frequencies as fq
from atcsdr.airband import parse_duration
from atcsdr.pluto import to_complex
from atcsdr.rtl import READ_SAMPLES, RTL_MAX_RATE

CAL_S = 3.0                  # calibration listen per window
SETTLE_READS = 1             # reads dropped after each retune, while the tuner settles
STEADY_DB = 6.0              # a floor this far above its window's median is a signal, not noise
CLIP_LEVEL = 2032            # |sample| at the 8-bit ADC's limits, as scaled by atcsdr/rtl.py
CLIPPING_WARN = 0.001        # warn above 0.1% of samples clipped
TTY = sys.stdout.isatty()
CLEAR = "\r\033[K" if TTY else ""


def _terminate(*_):
    raise KeyboardInterrupt


def level_db(x):
    """A frame's power in dB, the same measure the squelch uses."""
    return 10 * math.log10(float(np.mean(np.abs(x) ** 2)) + 1e-20)


class SyntheticRadio:
    """Noise plus a few simulated stations, for checking the scanner without a
    dongle: SFO Twr talks 4 s in every 15, App South 3 s in every 20, and SFO
    ATIS never stops. realtime=False runs as fast as the CPU allows, on its own
    clock (clock())."""

    STATIONS = [(120.500, 15, 4, 600), (133.950, 20, 3, 900), (118.850, 1, 1, 450)]  # MHz, every s, talk s, tone Hz

    def __init__(self, center, rate, realtime=True, seed=0):
        self.center, self.rate, self.realtime = center, rate, realtime
        self.rng = np.random.default_rng(seed)
        self.t = time.time()             # sample clock: the time of the next sample
        self.t0 = self.t

    def clock(self):
        return time.time() if self.realtime else self.t

    def read(self):
        if self.realtime:
            now = time.time()
            if now - self.t > 0.5:       # after a retune: carry on from now
                self.t = now
            time.sleep(max(0.0, self.t - now))
        t = (self.t - self.t0) + np.arange(READ_SAMPLES) / self.rate
        self.t += READ_SAMPLES / self.rate
        x = (self.rng.normal(size=READ_SAMPLES) + 1j * self.rng.normal(size=READ_SAMPLES)) * 0.01
        for mhz, every, talk, tone in self.STATIONS:
            off = mhz * 1e6 - self.center
            if abs(off) < self.rate / 2:
                on = (t % every) < talk
                x += on * 0.05 * (1 + 0.5 * np.cos(2 * np.pi * tone * t)) * np.exp(2j * np.pi * off * t)
        raw = np.empty(2 * READ_SAMPLES, np.int16)
        raw[0::2] = np.clip(x.real * 2048, -2047, 2047)
        raw[1::2] = np.clip(x.imag * 2048, -2047, 2047)
        return raw

    def tune(self, center):
        self.center = center
        if self.realtime:
            time.sleep(0.3)              # about what restarting rtl_sdr takes
        else:
            self.t += 0.3

    def close(self):
        pass

    def reconnect(self, stop):
        return 0.0


class Saver(threading.Thread):
    """Writes the clips, cleaned copies and transmissions.csv off the scan
    thread, so cleaning a clip never stalls the radio."""

    def __init__(self, opts):
        super().__init__(daemon=True)
        self.opts, self.q = opts, queue.Queue()
        self.mp3 = None if opts.no_mp3 else audiofile.find_lame()
        for d in (opts.clips, opts.clean):
            if d:
                os.makedirs(d, exist_ok=True)
        self.log = open(opts.log, "w", buffering=1)
        self.log.write("time,mhz,name,kind,seconds,peak_db,cut_short,clip\n")

    def run(self):
        while (item := self.q.get()) is not None:
            tx, cut = item
            ch, when = tx.channel, datetime.fromtimestamp(tx.start)
            stamp = f"{when:%Y%m%d-%H%M%S}"
            title = f"{ch.name} {ch.mhz:.3f} MHz {stamp}"
            clip = audiofile.save_clip(tx.audio, audiofile.clip_base(self.opts.clips, ch.mhz, ch.name, stamp),
                                       A.AUDIO_RATE, self.mp3, title)[-1]
            if self.opts.clean:
                audiofile.save_clip(audiofile.clean(tx.audio, A.AUDIO_RATE, self.opts.strength),
                                    audiofile.clip_base(self.opts.clean, ch.mhz, ch.name, stamp),
                                    A.AUDIO_RATE, self.mp3, title + " (cleaned)")
            self.log.write(f"{when:%Y-%m-%d %H:%M:%S},{ch.mhz:.3f},{ch.name},{ch.kind},{tx.seconds:.1f},"
                           f"{tx.peak_db:.1f},{int(cut)},{clip}\n")
            print(f"{CLEAR}{when:%H:%M:%S}  {ch.name:<11} {ch.mhz:8.3f}  {tx.seconds:5.1f} s  "
                  f"{tx.peak_db:+5.1f} dB  -> {os.path.basename(clip)}" + ("  (cut short)" if cut else ""),
                  flush=True)
        self.log.close()

    def put(self, tx, cut):
        self.q.put((tx, cut))

    def finish(self):
        self.q.put(None)
        self.join()


class Scanner:
    """Calibrates each capture window, then hops between them, staying on one
    only while a channel in it is talking."""

    def __init__(self, radio, rate, windows, opts, saver):
        self.radio, self.rate, self.windows, self.opts, self.saver = radio, rate, windows, opts, saver
        self.clock = getattr(radio, "clock", time.time)
        self.chans = [[A.Channel(f, 0.0, opts.threshold) for f in w.channels] for w in windows]
        for ch in self.channels:
            ch.squelch = True            # ATIS too: here its squelch says whether it's received
            ch.ignored = False           # no longer holds the scan (ATIS recorded, or a steady carrier)
            ch.cal_floor_db = ch.cal_own_db = None
        self.banks = [A.FilterBank(rate, [f.mhz * 1e6 - w.center for f in w.channels]) for w in windows]
        self.adc = [None] * len(windows)          # (rms dBFS, fraction clipped) per window
        self.window = None
        self.never = threading.Event()            # reconnect until it works; Ctrl-C ends it
        self.stats = dict(passes=0, visits=0, stays=0, retune_s=0.0, reconnects=0)
        self.notes = []
        self.state = dict(mode="", started=self.clock())
        self.deadline = None             # clock time at which a visit ends early (--duration)

    @property
    def channels(self):
        return [c for chans in self.chans for c in chans]

    # ---------------------------------------------------------------- radio

    def read(self):
        while True:
            try:
                return self.radio.read()
            except OSError as e:
                print(f"{CLEAR}RTL-SDR: {e}; reconnecting...", flush=True)
                gone = self.radio.reconnect(self.never)
                self.stats["reconnects"] += 1
                if self.window is not None:
                    self.banks[self.window].reset()
                print(f"{CLEAR}RTL-SDR back after {gone:.1f} s.", flush=True)

    def tune(self, i):
        t = self.clock()
        self.radio.tune(self.windows[i].center)
        self.window = i
        for _ in range(SETTLE_READS):
            self.read()
        self.banks[i].reset()
        for ch in self.chans[i]:
            ch.pre_roll.clear()          # audio from the last visit isn't this one's pre-roll
            ch.loud = 0
        self.stats["retune_s"] += self.clock() - t

    def frames(self, raw=None):
        """One read of the current window: (time, [one frame per channel]) for
        each frame, timed so that the last one ends now."""
        raw = self.read() if raw is None else raw
        now = self.clock()
        blocks = list(self.banks[self.window].process(to_complex(raw)))
        for k, block in enumerate(blocks):
            yield now - (len(blocks) - 1 - k) * A.FRAME_S, block

    # ---------------------------------------------------------------- calibrate

    def calibrate(self, i, seconds=CAL_S):
        """Each channel's noise floor in window i: the 20th percentile of its
        level, capped at STEADY_DB above the window's median, so a channel that
        was talking (or an ATIS on the air) all through calibration still opens
        its squelch afterwards."""
        self.tune(i)
        chans = self.chans[i]
        levels = [[] for _ in chans]
        clipped = total = 0
        power = 0.0
        t_end = self.clock() + seconds
        while self.clock() < t_end:
            raw = self.read()
            clipped += int(np.count_nonzero(np.abs(raw) >= CLIP_LEVEL))
            total += len(raw)
            power += float(np.sum(raw.astype(np.float32) ** 2))
            for _, block in self.frames(raw):
                for lv, x in zip(levels, block):
                    lv.append(level_db(x))
        own = [float(np.percentile(lv, 20)) for lv in levels]
        ref = float(np.median(own))
        for ch, o in zip(chans, own):
            ch.cal_own_db, ch.cal_floor_db = o, min(o, ref + STEADY_DB)
            ch.history.clear()
            ch.history.extend([ch.cal_floor_db] * ch.calibrate)
            ch.floor_db, ch.floor_age = ch.cal_floor_db, A.FLOOR_EVERY
        rms = math.sqrt(power / max(total, 1)) / 2048
        self.adc[i] = (20 * math.log10(rms + 1e-12), clipped / max(total, 1))
        return ref

    # ---------------------------------------------------------------- scan

    def finish(self, tx, cut=False):
        """Count a transmission and hand it to the saver (unless it's a blip, or
        from an ignored channel)."""
        if tx is None or tx.channel.ignored:
            return
        ch = tx.channel
        if tx.seconds < A.MIN_CLIP_S:
            ch.blips += 1
            return
        ch.transmissions += 1
        ch.talk_s += tx.seconds
        ch.best_db = max(ch.best_db or tx.peak_db, tx.peak_db)
        self.saver.put(tx, cut)
        if ch.kind == "atis" and ch.talk_s >= self.opts.atis_seconds:
            self.ignore(ch, f"{ch.name} {ch.mhz:.3f}: ATIS received and recorded; no longer holds the scan")

    def ignore(self, ch, why):
        ch.ignored = True
        self.notes.append(why)
        print(f"{CLEAR}{why}", flush=True)

    def visit(self, i):
        """Look at window i; stay while any of its channels is talking. Returns
        (seconds, names of the channels heard)."""
        o = self.opts
        self.tune(i)
        chans = self.chans[i]
        start = last_busy = self.clock()
        stayed, heard = False, []
        while True:
            for t, block in self.frames():
                for ch, x in zip(chans, block):
                    _, tx = ch.process(x, t, 0.0)
                    self.finish(tx)
                    if ch.open and not ch.ignored:
                        if ch.name not in heard:
                            heard.append(ch.name)
                        if ch.kind == "atis" and t - ch.opened_at >= o.atis_seconds:
                            self.finish(ch.end(), cut=True)
            now = self.clock()
            if any(c.open and not c.ignored for c in chans):
                last_busy, stayed = now, True
            self.state.update(mode="listening" if stayed else "looking", window=i)
            self.status()
            if not stayed and now - start >= o.look:
                break
            if stayed and now - last_busy >= o.linger:
                break
            if self.deadline and now >= self.deadline:
                break
            if now - start >= o.max_visit:
                for ch in chans:
                    if ch.open and not ch.ignored and ch.opened_at - start < 1.0:
                        self.finish(ch.end(), cut=True)
                        self.ignore(ch, f"{ch.name} {ch.mhz:.3f}: open for all of a {o.max_visit:g} s "
                                        "visit, a steady carrier or interference; ignored from now on")
                break
        self.close_window(i, cut=True)
        self.stats["visits"] += 1
        self.stats["stays"] += stayed
        return self.clock() - start, heard

    def close_window(self, i, cut):
        for ch in self.chans[i]:
            self.finish(ch.end(), cut=cut)

    def status(self):
        if not TTY:
            return
        s, i = self.state, self.state.get("window", 0)
        lo, hi = self.windows[i].span()
        talking = [f"● {c.name} {c.level_db - c.floor_db:+.0f}" for c in self.chans[i]
                   if c.open and c.floor_db is not None]
        elapsed = self.clock() - s["started"]
        clips = sum(c.transmissions for c in self.channels)
        parts = [f"pass {self.stats['passes'] + 1}", f"window {i + 1}/{len(self.windows)} {lo:.1f}-{hi:.1f} MHz",
                 s["mode"], "  ".join(talking) or "quiet", f"{clips} clips",
                 f"{elapsed:.0f}" + (f"/{self.opts.duration:.0f} s" if self.opts.duration else " s")]
        print(CLEAR + "  |  ".join(parts)[: shutil.get_terminal_size((120, 20)).columns - 1], end="", flush=True)


# -------------------------------------------------------------------- report

def calibration_report(scanner, path):
    lines = []
    with open(path, "w") as f:
        f.write("window,center_mhz,adc_rms_dbfs,adc_clipped_pct,mhz,name,kind,floor_db,own_floor_db,steady\n")
        for i, (w, chans) in enumerate(zip(scanner.windows, scanner.chans), 1):
            rms, clip = scanner.adc[i - 1]
            lo, hi = w.span()
            lines.append(f"  window {i} {lo:.3f}-{hi:.3f} MHz: ADC {rms:+.0f} dBFS, {100 * clip:.2f}% clipped"
                         + ("  <- too much gain: try a lower --gain" if clip > CLIPPING_WARN else ""))
            for ch in chans:
                steady = ch.cal_own_db > ch.cal_floor_db
                f.write(f"{i},{w.center / 1e6:.4f},{rms:.1f},{100 * clip:.3f},{ch.mhz:.3f},{ch.name},{ch.kind},"
                        f"{ch.cal_floor_db:.1f},{ch.cal_own_db:.1f},{int(steady)}\n")
                if steady:
                    what = "ATIS on the air" if ch.kind == "atis" else "talking, or a steady signal"
                    lines.append(f"      {ch.mhz:8.3f} {ch.name:<11} "
                                 f"{ch.cal_own_db - ch.cal_floor_db + STEADY_DB:+.0f} dB above the window: {what}")
    return lines


def scan_summary(scanner, elapsed, started):
    s, chans = scanner.stats, scanner.channels
    lines = [A.summary(chans, elapsed, started), "",
             f"Scan: {s['passes']} full passes over {len(scanner.windows)} windows, {s['visits']} stops, "
             f"{s['stays']} of them stayed for traffic; {100 * s['retune_s'] / max(elapsed, 1e-9):.0f}% of the "
             "time spent retuning" + (f"; {s['reconnects']} reconnect(s)" if s["reconnects"] else "") + "."]
    adc = [a for a in scanner.adc if a]
    if adc:
        worst = max(a[1] for a in adc)
        lines.append(f"ADC: {min(a[0] for a in adc):+.0f} to {max(a[0] for a in adc):+.0f} dBFS, at most "
                     f"{100 * worst:.2f}% of samples clipped"
                     + (" -- too much gain, try a lower --gain." if worst > CLIPPING_WARN else "."))
    atis = [c for c in chans if c.kind == "atis"]
    lines.append(f"ATIS received: {sum(1 for c in atis if c.transmissions)} of {len(atis)}"
                 + "".join(f"\n  {c.mhz:8.3f} {c.name}" for c in atis if c.transmissions))
    silent = [c for c in chans if not c.transmissions]
    lines.append(f"Never heard ({len(silent)}): " + (", ".join(f"{c.name} {c.mhz:g}" for c in silent) or "none")
                 + ("\n  (quiet, out of range, or closed: SJC, PAO, SQL, RHV and HWD towers close at night)"
                    if silent else ""))
    if scanner.notes:
        lines.append("Notes:\n" + "\n".join(f"  {n}" for n in scanner.notes))
    return "\n".join(lines)


# -------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["rtl", "synthetic"], default="rtl", help="default rtl")
    p.add_argument("--rtl", default="rtl", help="which dongle: rtl, or rtl:N (default rtl)")
    p.add_argument("--gain", default="40", help="RTL-SDR gain, dB (default 40)")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("20m"),
                   help="how long: 90s, 20m, 2h (default 20m); 0 = until Ctrl-C")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above a channel's noise floor that opens its squelch (default 8)")
    p.add_argument("--look", type=float, default=0.3, help="seconds per window when it's quiet (default 0.3)")
    p.add_argument("--linger", type=float, default=4.0,
                   help="stay this long after the last transmission, for the reply (default 4)")
    p.add_argument("--max-visit", type=float, default=60.0, help="longest stay on one window, s (default 60)")
    p.add_argument("--atis-seconds", type=float, default=20.0,
                   help="seconds of each ATIS to record before it stops holding the scan (default 20)")
    p.add_argument("--strength", type=float, default=0.9, help="noise reduction for cleaned clips, 0-1 (default 0.9)")
    p.add_argument("--no-clean", action="store_true", help="don't save cleaned copies of the clips")
    p.add_argument("--no-mp3", action="store_true", help="clips as WAV only")
    p.add_argument("--out", help="output folder (default recordings/atc_reception_<time>)")
    args = p.parse_args()
    signal.signal(signal.SIGTERM, _terminate)

    freqs = fq.area_channels("bay", atis=True)
    rate, windows = A.plan(freqs, RTL_MAX_RATE)
    out = Path(args.out or ROOT / f"recordings/atc_reception_{datetime.now():%Y%m%d-%H%M}")
    out.mkdir(parents=True, exist_ok=True)
    opts = argparse.Namespace(**vars(args), clips=str(out / "clips"),
                              clean=None if args.no_clean else str(out / "clips_clean"),
                              log=str(out / "transmissions.csv"))
    print(f"ATC reception test: {len(freqs)} channels in {len(windows)} windows of "
          f"{rate / 1e6:g} MSPS, gain {args.gain}, "
          + (f"for {args.duration / 60:g} min." if args.duration else "until Ctrl-C.")
          + f"\nSaving to {out}/\n", flush=True)

    if args.source == "rtl":
        from atcsdr.rtl import RtlSdr
        try:
            radio = RtlSdr(args.rtl, windows[0].center, rate, args.gain)
        except OSError as e:
            sys.exit(f"Could not open the RTL-SDR ({args.rtl}): {e}\n"
                     "Is it plugged in, and closed in every other program (SDR++, an ADS-B test)?")
    else:
        radio = SyntheticRadio(windows[0].center, rate)

    saver = Saver(opts)
    saver.start()
    scanner = Scanner(radio, rate, windows, opts, saver)
    started, t0 = datetime.now(), scanner.clock()
    scanner.state["started"] = t0
    scanner.deadline = t0 + args.duration if args.duration else None
    visits = open(out / "visits.csv", "w", buffering=1)
    visits.write("time,window,low_mhz,high_mhz,seconds,heard\n")
    try:
        print(f"Calibrating: {CAL_S:g} s per window...", flush=True)
        for i in range(len(windows)):
            scanner.calibrate(i)
        print("\n".join(calibration_report(scanner, out / "calibration.csv")) + "\n\nScanning. Ctrl-C to stop.\n",
              flush=True)
        while not (args.duration and scanner.clock() - t0 >= args.duration):
            for i in range(len(windows)):
                when = datetime.now()
                seconds, heard = scanner.visit(i)
                lo, hi = windows[i].span()
                visits.write(f"{when:%Y-%m-%d %H:%M:%S},{i + 1},{lo:.3f},{hi:.3f},{seconds:.1f},"
                             f"{';'.join(heard)}\n")
                if args.duration and scanner.clock() - t0 >= args.duration:
                    break
            else:
                scanner.stats["passes"] += 1
    except KeyboardInterrupt:
        pass
    finally:
        if scanner.window is not None:
            scanner.close_window(scanner.window, cut=True)
        radio.close()
        visits.close()
        saver.finish()
        if all(scanner.adc) and not (out / "calibration.csv").exists():
            calibration_report(scanner, out / "calibration.csv")
        elapsed = scanner.clock() - t0
        report = scan_summary(scanner, elapsed, started)
        print("\n\n" + report)
        (out / "summary.txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
