import wave

import numpy as np
import pytest

import synth
from atcsdr import airband as A
from atcsdr import frequencies as fq
from atcsdr.pluto import NET_MAX_RATE, USB_MAX_RATE
from atcsdr.rtl import RTL_MAX_RATE

WIDEBAND = 30_720_000     # a rate that holds all of NorCal: tests the DSP, not a link


def tone_and_thd(audio, rate=A.AUDIO_RATE):
    """Strongest frequency, modulation depth and harmonic distortion (dB)."""
    a = (audio - audio.mean()) / audio.mean()
    w = np.hanning(len(a))
    spec = np.abs(np.fft.rfft(a * w))
    f = np.fft.rfftfreq(len(a), 1 / rate)
    k = np.argmax(spec)
    harm = np.sqrt(sum(spec[np.argmin(abs(f - m * f[k]))] ** 2 for m in (2, 3, 4)))
    return f[k], 2 * spec[k] / w.sum(), 20 * np.log10(harm / spec[k])


@pytest.mark.parametrize("preset, limit", [("sfo-airport", USB_MAX_RATE),
                                           ("norcal", WIDEBAND)])
def test_filter_bank_recovers_each_channel(preset, limit):
    freqs = fq.preset_channels(preset)
    rate, windows = A.plan(freqs, limit)
    assert len(windows) == 1
    center = windows[0].center
    tones = [(f.mhz, 400 + 200 * i) for i, f in enumerate(freqs)]
    iq = synth.am_channels(rate, center, tones, seconds=1.0)
    bank = A.FilterBank(rate, [f.mhz * 1e6 - center for f in freqs])
    out = np.concatenate(list(bank.process(iq)), axis=1)
    for (mhz, tone), y in zip(tones, out):
        got, depth, thd = tone_and_thd(np.abs(y)[A.AUDIO_RATE // 4:])
        assert got == pytest.approx(tone, abs=5), mhz        # its own tone, not a neighbour's
        assert depth == pytest.approx(0.5, abs=0.08), mhz
        assert thd < -60, mhz                                # no clicks at block joins


@pytest.mark.parametrize("area", list(fq.AREAS))
def test_plan_covers_every_channel_once(area):
    freqs = fq.area_channels(area)
    # Ethernet is measured at 12 MSPS: 2 windows per area. An RTL-SDR (2.4 MSPS) needs more.
    for limit, most in ((USB_MAX_RATE, 3), (NET_MAX_RATE, 2), (WIDEBAND, 1), (RTL_MAX_RATE, 12)):
        rate, windows = A.plan(freqs, limit)
        assert rate <= limit and A.smooth(rate // A.AUDIO_RATE)
        assert len(windows) <= most
        planned = [c for w in windows for c in w.channels]
        assert sorted(planned) == sorted(freqs)
        for w in windows:
            reach = rate / 2 * A.USABLE - A.STOP_HZ
            for c in w.channels:
                assert A.DC_GUARD <= abs(c.mhz * 1e6 - w.center) <= reach


def test_squelch_opens_for_a_transmission_and_closes_after():
    ch = A.Channel(fq.Freq(120.5, "Test", "tower"), threshold_db=8)
    rng = np.random.default_rng(0)
    frame = A.KEEP
    t, done = 0.0, []

    def run(seconds, carrier):
        nonlocal t
        tt = np.arange(frame) / A.AUDIO_RATE
        for _ in range(int(seconds / A.FRAME_S)):
            x = (rng.normal(size=frame) + 1j * rng.normal(size=frame)) * 0.01
            if carrier:
                x = x + 0.2 * (1 + 0.5 * np.sin(2 * np.pi * 700 * (tt + t)))
            t += A.FRAME_S
            _, tx = ch.process(x.astype(np.complex64), t, 0.0)
            if tx:
                done.append(tx)

    run(4, False)          # calibrate on noise
    assert ch.floor_db is not None and not ch.open
    run(3, True)           # a 3 s transmission
    assert ch.open
    run(2, False)          # hang time passes
    assert not ch.open and len(done) == 1
    assert done[0].seconds == pytest.approx(3.4, abs=0.4)    # + pre-roll and tail
    assert done[0].peak_db > 20


def test_raw_files_stay_aligned_across_unmonitored_time(tmp_path):
    ch = A.Channel(fq.Freq(120.5, "Test", "tower"))
    path = tmp_path / "raw.wav"
    ch.raw = wave.open(str(path), "wb")
    ch.raw.setnchannels(1); ch.raw.setsampwidth(2); ch.raw.setframerate(A.AUDIO_RATE)
    block = np.ones(A.KEEP, np.float32) * 0.1
    ch.write_raw(block, 0.0)
    ch.write_raw(block, 5.0)                 # nothing heard from 0.01 s to 5 s
    ch.write_raw(np.zeros(0, np.float32), 8.0)
    ch.raw.close()
    with wave.open(str(path)) as w:
        assert w.getnframes() == 8 * A.AUDIO_RATE


def test_status_line_fits_the_terminal():
    freqs = fq.area_channels("bay")
    rate, windows = A.plan(freqs, USB_MAX_RATE)
    chans = [[A.Channel(f) for f in w.channels] for w in windows]
    line = A._status(windows, chans, dict(window=0, next_switch=None), 12.0, 600)
    assert len(line) < 200
