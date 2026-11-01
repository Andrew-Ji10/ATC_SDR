"""The fast receiver (atcsdr.fastrx), at a small rate so it runs quickly."""

import numpy as np
import pytest

from atcsdr.frequencies import Freq

fastrx = pytest.importorskip("atcsdr.fastrx")
if fastrx.numba is None:
    pytest.skip("numba not installed", allow_module_level=True)

RATE = 4_000_000                       # 160 channels of 25 kHz
FREQS = [Freq(119.500, "A", "tower"), Freq(120.025, "B", "approach"), Freq(120.600, "C", "ground")]


def to_int16(x):
    raw = np.empty(2 * len(x), np.int16)
    raw[0::2], raw[1::2] = np.round(x.real), np.round(x.imag)
    return raw


def test_tone_comes_out_of_its_own_channel_only():
    center = 120_000_000
    ch = fastrx.Channelizer(RATE, center, FREQS, threads=2)
    n = np.arange(400 * ch.M)
    out = []
    for target in FREQS:
        tone = 600 * np.exp(2j * np.pi * (target.mhz * 1e6 + 1_000 - center) * n / RATE)
        ch.reset()
        p = np.mean(np.abs(ch.process(to_int16(tone))[:, fastrx.TAPS:]) ** 2, axis=1)
        out.append(p)
        own = FREQS.index(target)
        others = np.delete(p, own)
        assert 10 * np.log10(others.max() / p[own]) < -50, (target, p)
    assert all(np.argmax(p) == i for i, p in enumerate(out))


def test_receiver_catches_one_transmission():
    rx = fastrx.Receiver(RATE, FREQS, threshold_db=8.0, threads=2)
    rng = np.random.default_rng(0)
    on, off, total, chunk = 3.0, 5.0, 7.0, 1 << 16
    off_hz = FREQS[1].mhz * 1e6 - rx.center
    t0, done, k = 1_700_000_000.0, [], 0
    while k < total * RATE:
        t = (k + np.arange(chunk)) / RATE
        x = (rng.standard_normal(chunk) + 1j * rng.standard_normal(chunk)) * 20
        talking = (t >= on) & (t < off)
        x += talking * 300 * (1 + 0.5 * np.cos(2 * np.pi * 1_000 * t)) * np.exp(2j * np.pi * off_hz * t)
        k += chunk
        done += rx.process(to_int16(x), t0 + k / RATE)
    done += rx.finish()
    real = [tx for tx in done if tx.seconds >= 1.0]
    assert [tx.channel.name for tx in real] == ["B"]
    tx = real[0]
    assert on - 0.5 < tx.start - t0 < on + 0.1          # 0.3 s of pre-roll before it opened
    assert 1.8 < tx.seconds < 3.0
    assert len(tx.audio) == pytest.approx(tx.seconds * fastrx.AUDIO_RATE)


def test_plan_center_fits_the_bay_at_20_msps():
    from atcsdr import frequencies as fq
    center, edge = fastrx.plan_center(fq.area_channels("bay"), 20_000_000)
    assert center % fastrx.SPACING == 0
    assert edge < 0.9
    with pytest.raises(ValueError):
        fastrx.plan_center(fq.area_channels("bay"), 14_400_000)
