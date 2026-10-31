import numpy as np
import pytest

from atcsdr import audio
from atcsdr import frequencies as fq


def speech_like(rate=48000, seconds=4.0, noise=0.05, seed=0):
    """Bursts of voiced, harmonic sound (syllables) with gaps, in static."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    voice = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 15))
    syllables = (np.sin(2 * np.pi * 3 * t) > 0.2).astype(float)
    return (0.3 * voice * syllables + noise * rng.normal(size=len(t))).astype(np.float32)


def test_clean_raises_speech_over_static_and_keeps_the_speech():
    x = speech_like()
    before = audio.speech_to_gap_db(x, 48000)
    y = audio.clean(x, 48000)
    assert audio.speech_to_gap_db(y, 48000) > before + 6
    assert np.max(np.abs(y)) <= audio.PEAK + 1e-6
    # The loud (speech) frames should survive: compare in the voice band.
    n = 2400
    e = lambda s: (audio.band_pass(s, 48000)[: len(s) // n * n].reshape(-1, n) ** 2).mean(1)
    ex, ey = e(x), e(y)
    loud = ex >= np.percentile(ex, 75)
    kept = (ey[loud] / ey.mean()).mean() / (ex[loud] / ex.mean()).mean()
    assert kept > 0.8


def test_save_and_load_clip(tmp_path):
    base = audio.clip_base(str(tmp_path), 120.5, "SFO Twr", "20261016-120000")
    assert base.endswith("120.500MHz_SFO_Twr_20261016-120000")
    x = speech_like(seconds=1.0)
    (wav,) = audio.save_clip(x, base, 48000, None, "test")
    y, rate = audio.load_wav(wav)
    assert rate == 48000 and np.allclose(np.clip(x, -1, 1), y, atol=1e-4)
    assert audio.CLIP_NAME.match(wav.rsplit("/", 1)[1])["name"] == "SFO_Twr"


@pytest.mark.parametrize("area", list(fq.AREAS))
def test_area_channels_are_airband_and_unique(area):
    chans = fq.area_channels(area)
    mhz = [c.mhz for c in chans]
    assert len(mhz) == len(set(mhz)) and mhz == sorted(mhz)
    assert all(118.0 <= m <= 137.0 for m in mhz)
    assert not any(c.kind == "atis" for c in chans)
    assert any(c.kind == "atis" for c in fq.area_channels(area, atis=True))
    kinds = {c.kind for c in chans}
    assert {"tower", "ground", "approach"} <= kinds


def test_bay_includes_every_area():
    bay = {c.mhz for c in fq.area_channels("bay")}
    for area in ("south_bay", "sfo", "oak"):
        assert {c.mhz for c in fq.area_channels(area)} <= bay


def test_parse_channels():
    chans = fq.parse_channels("121.8 Ground, 120.5 Tower,118.2")
    assert [(c.mhz, c.name) for c in chans] == [(118.2, "118.2"), (120.5, "Tower"),
                                                 (121.8, "Ground")]
