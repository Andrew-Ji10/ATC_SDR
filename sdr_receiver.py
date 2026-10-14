#!/usr/bin/env python3
"""
GNU Radio receiver for broadcast FM and airband (ATC) AM.

Supports ADALM-PLUTO (via gr-iio, the default) and RTL-SDR / HackRF / Airspy
(via gr-osmosdr, --sdr osmosdr). A stock Pluto only tunes 325 MHz - 3.8 GHz;
FM and airband need the AD9364 firmware setting (70 MHz - 6 GHz), see README.md.
For a graphical interface with a spectrum display, run sdr_gui.py.

Examples:
    python3 sdr_receiver.py 88.5              # broadcast FM (mode auto-detected)
    python3 sdr_receiver.py 118.6 --squelch -45   # ATC tower, AM with squelch
    python3 sdr_receiver.py 162.55 --mode nbfm    # NOAA weather radio
    python3 sdr_receiver.py 120.5 --record sfo_tower.wav

While running, type commands at the prompt:
    <MHz>        retune (e.g. 121.8)
    v <0-5>      volume
    g <dB>|auto  RF gain
    sq <dB>      squelch threshold (AM / NBFM only), e.g. sq -45
    s            print current channel power (use it to pick a squelch level)
    q            quit

Signal chain:
    SDR parked on a band center ("LO"), sampling `span` Hz of spectrum
    -> frequency-xlating FIR: shift the tuned channel to 0 Hz, decimate to 240 ksps
    -> WFM, AM and NBFM demodulators all run in parallel; the selected one is
       summed into the 48 kHz audio output (others are multiplied by 0)
    -> volume -> speakers (and optional WAV)
    The raw SDR stream also feeds an averaged FFT for the GUI spectrum display.

Tuning anywhere inside the band only moves the xlating filter, so it is instant.
The LO is moved only when the target falls outside the band or too close to the
LO itself, where DC offset / LO leakage (and the Pluto's DC tracking) live.
"""

import argparse
import math
import sys

import numpy as np
from gnuradio import analog, audio, blocks, fft, filter, gr
from gnuradio.fft import window
from gnuradio.filter import firdes

SDR_RATE = 2.4e6
CHAN_RATE = 240e3
AUDIO_RATE = 48000
TUNE_OFFSET = 250e3
# Spans must be integer multiples of CHAN_RATE.
SPANS = (1_200_000, 2_400_000, 3_840_000)

# Keep the tuned channel at least this far from the LO (DC) ...
DC_GUARD = 30e3
# ... and this far inside the band edges (half a WFM channel plus filter skirt).
EDGE_GUARD = 120e3

FFT_SIZE = 1024
FFT_FPS = 30

# Airband (108-137 MHz) is AM; broadcast FM band is wideband FM.
FM_BROADCAST = (87.5e6, 108e6)
AIRBAND = (108e6, 137e6)
MODES = ("wfm", "am", "nbfm")

# AD9363 (stock Pluto) lower tuning limit; AD9364 mode goes down to 70 MHz.
PLUTO_STOCK_MIN = 325e6
PLUTO_GAIN_MODES = {"auto": "slow_attack", "fast": "fast_attack"}

SQUELCH_OFF = -200.0


def auto_mode(freq):
    if FM_BROADCAST[0] <= freq < FM_BROADCAST[1]:
        return "wfm"
    if AIRBAND[0] <= freq <= AIRBAND[1]:
        return "am"
    return "nbfm"


class Receiver(gr.top_block):
    def __init__(self, freq, mode, sdr, device, gain, ppm=0.0, volume=1.0,
                 squelch_db=SQUELCH_OFF, audio_device="", record_path=None,
                 deemph_tau=75e-6, span=SDR_RATE, spectrum=False):
        gr.top_block.__init__(self, "SDR Receiver")
        span = int(round(span))
        if span not in SPANS:
            raise ValueError(f"span must be one of {SPANS}")
        self.sdr = sdr
        self.span = span
        self.lo = None
        self.freq = None
        self.vol = volume
        self.muted = False

        # --- SDR source -------------------------------------------------
        if sdr == "pluto":
            self.src = make_pluto_source(device, span)
        else:
            self.src = make_osmosdr_source(device, ppm, span)
        self.set_gain(gain)

        # --- Channel select: tuned channel -> 0 Hz, span -> 240k --------
        self.xlate = filter.freq_xlating_fir_filter_ccf(
            int(span / CHAN_RATE),
            firdes.low_pass(1.0, span, 100e3, 20e3),
            0,
            span,
        )
        self.connect(self.src, self.xlate)

        # Per-mode output gain: 1 for the selected mode, 0 for the rest.
        self.mode_gate = {m: blocks.multiply_const_ff(0.0) for m in MODES}
        # Exposed (with self.chans) so tools like capture_clips.py can tap the audio.
        self.mix = mix = blocks.add_ff()
        for i, m in enumerate(MODES):
            self.connect(self.mode_gate[m], (mix, i))

        # WFM: straight off the 240k channel.
        self.probes = {"wfm": analog.probe_avg_mag_sqrd_c(0, 1e-3)}
        self.connect(self.xlate, self.probes["wfm"])
        wfm = analog.wfm_rcv(quad_rate=CHAN_RATE,
                             audio_decimation=int(CHAN_RATE / AUDIO_RATE))
        self.connect(self.xlate, wfm, self.mode_gate["wfm"])

        # Narrow modes: second stage 240k -> 48k with a tight channel filter.
        # AM airband channels are 25 / 8.33 kHz wide; NBFM ~12.5 kHz.
        self.squelches = {}
        self.chans = {}
        for m, cutoff in (("am", 5e3), ("nbfm", 8e3)):
            chan = filter.fir_filter_ccf(
                int(CHAN_RATE / AUDIO_RATE),
                firdes.low_pass(1.0, CHAN_RATE, cutoff, 2e3),
            )
            self.chans[m] = chan
            self.probes[m] = analog.probe_avg_mag_sqrd_c(0, 1e-3)
            # gate=False outputs zeros while closed, so the audio sink keeps flowing.
            self.squelches[m] = analog.pwr_squelch_cc(squelch_db, 1e-3, 0, False)
            self.connect(self.xlate, chan, self.squelches[m])
            self.connect(chan, self.probes[m])

        # AGC normalizes the carrier to ~1 so volume is consistent
        # between strong tower and weak aircraft transmissions.
        agc = analog.agc2_cc(1e-3, 1e-4, 1.0, 1.0)
        agc.set_max_gain(1e4)
        envelope = blocks.complex_to_mag()
        dc_block = filter.dc_blocker_ff(128, True)
        audio_lpf = filter.fir_filter_fff(
            1, firdes.low_pass(1.0, AUDIO_RATE, 3.5e3, 1e3))
        self.connect(self.squelches["am"], agc, envelope, dc_block, audio_lpf,
                     self.mode_gate["am"])

        nbfm = analog.nbfm_rx(audio_rate=AUDIO_RATE, quad_rate=AUDIO_RATE,
                              tau=deemph_tau, max_dev=5e3)
        self.connect(self.squelches["nbfm"], nbfm, self.mode_gate["nbfm"])

        # --- Outputs ----------------------------------------------------
        self.volume = blocks.multiply_const_ff(volume)
        self.connect(mix, self.volume, audio.sink(AUDIO_RATE, audio_device, True))
        if record_path:
            # Tap before volume so muting / volume changes don't affect the recording.
            self.connect(mix, make_wav_sink(record_path))

        # --- Spectrum (GUI) ---------------------------------------------
        self.spectrum_probe = None
        if spectrum:
            keep = max(1, int(span / FFT_SIZE / FFT_FPS))
            self.spectrum_probe = blocks.probe_signal_vf(FFT_SIZE)
            self.connect(
                self.src,
                blocks.stream_to_vector(gr.sizeof_gr_complex, FFT_SIZE),
                blocks.keep_one_in_n(gr.sizeof_gr_complex * FFT_SIZE, keep),
                fft.fft_vcc(FFT_SIZE, True, window.blackmanharris(FFT_SIZE), True, 1),
                blocks.complex_to_mag_squared(FFT_SIZE),
                filter.single_pole_iir_filter_ff(0.3, FFT_SIZE),
                self.spectrum_probe,
            )

        self.set_mode(mode)
        self.set_freq(freq)

    # --- Tuning ---------------------------------------------------------
    def _set_lo(self, lo):
        self.lo = lo
        if self.sdr == "pluto":
            self.src.set_frequency(int(lo))
        else:
            self.src.set_center_freq(lo, 0)

    def _in_band(self, freq):
        off = abs(freq - self.lo)
        return DC_GUARD <= off <= self.span / 2 - EDGE_GUARD

    def set_freq(self, freq):
        """Tune to `freq`, moving the LO only if the channel is outside the band."""
        if self.lo is None or not self._in_band(freq):
            self._set_lo(freq - TUNE_OFFSET)
        self.freq = freq
        self.xlate.set_center_freq(freq - self.lo)

    def set_band(self, center):
        """Park the LO at `center`. Returns the (possibly moved) tuned frequency."""
        self._set_lo(center)
        freq = self.freq if self._in_band(self.freq) else center + TUNE_OFFSET
        self.set_freq(freq)
        return self.freq

    def band(self):
        return self.lo - self.span / 2, self.lo + self.span / 2

    def set_mode(self, mode):
        self.mode = mode
        for m, gate in self.mode_gate.items():
            gate.set_k(1.0 if m == mode else 0.0)

    # --- RF / audio -----------------------------------------------------
    def set_gain(self, gain):
        if self.sdr == "pluto":
            if gain in PLUTO_GAIN_MODES:
                self.src.set_gain_mode(0, PLUTO_GAIN_MODES[gain])
            else:
                self.src.set_gain_mode(0, "manual")
                self.src.set_gain(0, float(gain))
        elif gain == "auto":
            self.src.set_gain_mode(True, 0)
        else:
            self.src.set_gain_mode(False, 0)
            self.src.set_gain(float(gain), 0)

    def set_volume(self, vol):
        self.vol = vol
        self.volume.set_k(0.0 if self.muted else vol)

    def set_muted(self, muted):
        self.muted = muted
        self.set_volume(self.vol)

    def set_squelch(self, db):
        """Set the AM / NBFM squelch threshold (WFM has no squelch)."""
        for sq in self.squelches.values():
            sq.set_threshold(db)

    # --- Measurements ---------------------------------------------------
    def power_db(self):
        """Average power in the tuned channel, dB relative to full scale."""
        return 10 * math.log10(self.probes[self.mode].level() + 1e-20)

    def squelch_open(self):
        if self.mode == "wfm":
            return True
        return self.squelches[self.mode].unmuted()

    def spectrum_db(self):
        """(freqs_hz, power_db) across the band, or None if no FFT frame is available."""
        if self.spectrum_probe is None:
            return None
        power = np.asarray(self.spectrum_probe.level(), dtype=np.float64)
        if power.size != FFT_SIZE:
            return None
        db = 10 * np.log10(power + 1e-20) - 20 * math.log10(FFT_SIZE)
        freqs = self.lo + (np.arange(FFT_SIZE) - FFT_SIZE / 2) * (self.span / FFT_SIZE)
        return freqs, db


def make_pluto_source(uri, span):
    try:
        from gnuradio import iio
    except ImportError:
        sys.exit("gr-iio not found. Install it (see README.md) to talk to the Pluto.")
    # Empty URI: let libiio find the Pluto (USB first, then ip:pluto.local).
    src = iio.fmcomms2_source_fc32(uri or iio.get_pluto_uri(), [True, True], 32768)
    src.set_len_tag_key("packet_len")
    src.set_samplerate(int(span))
    src.set_quadrature(True)
    src.set_rfdc(True)
    src.set_bbdc(True)
    src.set_filter_params("Auto", "", 0, 0)
    return src


def make_osmosdr_source(device, ppm, span):
    try:
        import osmosdr
    except ImportError:
        sys.exit("gr-osmosdr not found. Install it (see README.md) to talk to the SDR.")
    src = osmosdr.source(args="numchan=1 " + (device or "rtl=0"))
    src.set_sample_rate(span)
    src.set_freq_corr(ppm, 0)
    src.set_dc_offset_mode(0, 0)
    src.set_iq_balance_mode(0, 0)
    return src


def make_wav_sink(path):
    # wavfile_sink's signature changed in GNU Radio 3.9.
    try:
        return blocks.wavfile_sink(path, 1, AUDIO_RATE, blocks.FORMAT_WAV,
                                   blocks.FORMAT_PCM_16, False)
    except (AttributeError, TypeError):
        return blocks.wavfile_sink(path, 1, AUDIO_RATE, 16)


def control_loop(rx):
    print("Commands: <MHz> | v <vol> | g <dB|auto|fast> | sq <dB> | s | q")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        cmd, *rest = line.split()
        arg = rest[0] if rest else None
        try:
            if cmd in ("q", "quit", "exit"):
                return
            elif cmd == "s":
                print(f"channel power: {rx.power_db():.1f} dB")
            elif cmd == "v" and arg:
                rx.set_volume(float(arg))
            elif cmd == "g" and arg:
                rx.set_gain(arg)
            elif cmd == "sq" and arg:
                rx.set_squelch(float(arg))
            else:
                rx.set_freq(float(cmd) * 1e6)
                print(f"tuned to {rx.freq / 1e6:.4f} MHz")
        except ValueError as e:
            print(f"error: {e}")


def add_sdr_args(p):
    """Arguments shared by the CLI and the GUI."""
    p.add_argument("--mode", choices=["auto", *MODES], default="auto",
                   help="demodulator (auto: 87.5-108 wfm, 108-137 am, else nbfm)")
    p.add_argument("--sdr", choices=["pluto", "osmosdr"], default="pluto",
                   help="pluto (gr-iio) or osmosdr (RTL-SDR, HackRF, Airspy...)")
    p.add_argument("--device", default="",
                   help="pluto: libiio URI, e.g. ip:192.168.2.1 or usb: (default auto-detect); "
                        "osmosdr: device string, e.g. rtl=0, hackrf=0 (default rtl=0)")
    p.add_argument("--gain", default="30",
                   help="RF gain in dB, or 'auto' (Pluto also: 'fast'). Pluto range 0-73. "
                        "Default 30")
    p.add_argument("--ppm", type=float, default=0.0,
                   help="frequency correction in ppm (osmosdr only)")
    p.add_argument("--span", type=float, default=SDR_RATE / 1e6,
                   choices=[s / 1e6 for s in SPANS],
                   help="band width / sample rate in MHz (default 2.4)")
    p.add_argument("--volume", type=float, default=1.0)
    p.add_argument("--squelch", type=float, default=SQUELCH_OFF,
                   help="squelch threshold in dB for am/nbfm (default open); try -45")
    p.add_argument("--audio-device", default="",
                   help="audio output device name (default system output)")
    p.add_argument("--record", metavar="WAV", help="also write audio to a WAV file")
    p.add_argument("--deemph", type=float, default=75e-6,
                   help="NBFM de-emphasis time constant (75e-6 US, 50e-6 EU)")


def open_receiver(args, freq, mode, spectrum=False):
    """Build a Receiver from parsed args. Raises RuntimeError with a hint on failure."""
    try:
        return Receiver(freq, mode, args.sdr, args.device, args.gain, args.ppm,
                        args.volume, args.squelch, args.audio_device, args.record,
                        args.deemph, span=round(args.span * 1e6), spectrum=spectrum)
    except RuntimeError as e:
        hint = ("Is the Pluto connected? Try `iio_info -s` to list devices."
                if args.sdr == "pluto" else
                "Is the dongle plugged in? Try `rtl_test` to check.")
        raise RuntimeError(f"Could not open SDR: {e}\n{hint}") from e


def warn_pluto_range(args, freq):
    if args.sdr == "pluto" and freq < PLUTO_STOCK_MIN:
        print(f"note: {freq / 1e6:g} MHz needs a Pluto with an AD9361/AD9364 (or the AD9364 "
              "firmware setting); a stock AD9363 stops at 325 MHz. See README.md.")


def main():
    p = argparse.ArgumentParser(
        description="Listen to broadcast FM or airband ATC with an SDR.")
    p.add_argument("freq", type=float, help="frequency in MHz, e.g. 88.5 or 118.6")
    add_sdr_args(p)
    args = p.parse_args()

    freq = args.freq * 1e6
    mode = auto_mode(freq) if args.mode == "auto" else args.mode
    warn_pluto_range(args, freq)

    try:
        rx = open_receiver(args, freq, mode)
    except RuntimeError as e:
        sys.exit(str(e))

    print(f"Listening on {args.freq:.4f} MHz [{mode.upper()}], gain {args.gain}")
    rx.start()
    try:
        if sys.stdin.isatty():
            control_loop(rx)
        else:
            rx.wait()
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        rx.wait()


if __name__ == "__main__":
    main()
