import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "radio"))

import gain_sweep as G
from atcsdr import airband as A
from atcsdr.frequencies import Freq

FREQS = [Freq(118.850, "SFO ATIS", "atis"), Freq(119.000, "SQL Twr", "tower"), Freq(120.500, "SFO Twr", "tower"),
         Freq(120.900, "Dep NW", "departure"), Freq(121.800, "SFO Gnd", "ground")]


def model_snr(g):
    """The synthetic radio's SNR loss to its own noise at gain g, dB."""
    return -10 * np.log10(1 + 10 ** ((G.SyntheticRadio.KNEE_GAIN - g) / 10))


def test_meter_reads_a_tone_at_its_power_and_only_in_its_channel():
    rate, (w,) = A.plan(FREQS, 4_800_000)
    m = G.Channelizer(rate, w)
    t = np.arange(m.n) / rate
    x = 0.1 * np.exp(2j * np.pi * (120.5e6 - w.center) * t)
    raw = np.empty(2 * m.n, np.int16)
    raw[0::2], raw[1::2] = x.real * 2048, x.imag * 2048
    lv, audio = m.process(raw)
    i = [c.name for c in w.channels].index("SFO Twr")
    assert lv[i] == pytest.approx(-20.0, abs=0.3)
    assert np.delete(lv, i).max() < lv[i] - 60
    assert audio.shape == (len(w.channels), A.KEEP)


def test_fit_recovers_gain_effects_from_paired_cells():
    rng = np.random.default_rng(1)
    effect = np.array([-6.0, 0.0, 3.0, 3.5])
    cells = []
    for e in range(40):
        strength = rng.uniform(5, 30)
        for g in rng.choice(4, size=2, replace=False):
            cells.append(dict(event=e, gain=int(g), snr_db=strength + effect[g] + rng.normal(0, 0.3)))
    got, se, n, strength, over = G.fit(cells, 4, ref=1)
    assert got == pytest.approx(effect, abs=0.3)
    assert (se[[0, 2, 3]] > 0).all() and n.sum() == len(cells) and not over.any()


def test_synthetic_sweep_finds_the_knee_and_the_clipping(tmp_path):
    gains = [30, 40, 50, 60, 70]
    rate, windows = A.plan(FREQS, 4_800_000)
    radio = G.SyntheticRadio(windows[0].center, rate, gains[0], realtime=False)
    sweep = G.Sweep(radio, rate, windows, gains, slot_s=2.0, stride=1)
    sweep.run(deadline=radio.clock() + 50)
    assert sweep.rounds >= 4
    rows, cells, (gain, why) = G.analyze(sweep.data(), sweep.slots, gains, 8.0, 40)
    by = {r["gain"]: r for r in rows}
    # SNR tracks the model: big gains below the knee (45 dB), little above it.
    for g in (30, 50):
        assert by[g]["snr_vs_ref"] == pytest.approx(model_snr(g) - model_snr(40), abs=1.5)
    assert by[70]["clipped"] > G.CLIP_OK            # the blocker overloads the ADC at the top
    assert by[70]["snr_vs_ref"] < by[60]["snr_vs_ref"]
    assert gain == 50
    assert {c["name"] for c in cells} >= {"SFO ATIS", "SFO Twr", "SFO Gnd"}

    G.save_levels(tmp_path / "levels.npz", sweep.data(), sweep.slots, gains, dict(header=["x"]))
    data, meta = G.load_levels(tmp_path / "levels.npz")
    rows2, _, (gain2, _) = G.analyze(data, meta["slots"], meta["gains"], 8.0, 40)
    assert gain2 == gain and rows2[0]["snr_vs_ref"] == pytest.approx(rows[0]["snr_vs_ref"])
    text = G.report(rows, cells, (gain, why), gains, 40, ["header"])
    assert "--gain 50" in text


class _Saver:
    def __init__(self):
        self.rows = []

    def put(self, row, audio):
        self.rows.append((row, audio))


def test_clips_cut_one_transmission_into_one_clip_per_gain():
    gains = [40, 50, 60]
    rate, windows = A.plan(FREQS, 4_800_000)
    radio = G.SyntheticRadio(windows[0].center, rate, gains[0], realtime=False)
    saver = _Saver()
    clips = G.Clips(windows, gains, 8.0, saver)
    sweep = G.Sweep(radio, rate, windows, gains, slot_s=2.0, stride=1, clips=clips)
    sweep.run(deadline=radio.clock() + 40)
    rows = [r for r, _ in saver.rows]
    assert {r["name"] for r in rows} >= {"SFO Twr", "SFO Gnd", "SFO ATIS"}
    for r, audio in saver.rows:
        assert len(audio) == pytest.approx(r["seconds"] * A.AUDIO_RATE, rel=0.01)
        assert r["seconds"] <= 2.0 + 0.1                   # never past a gain change
    by_tx = {}
    for r in rows:
        by_tx.setdefault(r["tx"], set()).add(r["gain"])
    assert any(len(g) >= 2 for tx, g in by_tx.items())    # one voice at several gains
    atis = [r for r in rows if r["kind"] == "atis"]
    assert len({r["tx"] for r in atis}) == 1 and len(atis) <= len(gains)   # one per gain


def test_fast_channelizer_matches_the_numpy_one_on_a_tone():
    pytest.importorskip("numba")
    rate, (w,), kind = G.plan_sweep(FREQS, 4_800_000)
    assert kind is G.FastChannelizer
    chz = G.FastChannelizer(rate, w, threads=1)
    t = np.arange(1 << 18) / rate
    x = 0.1 * np.exp(2j * np.pi * (120.5e6 - w.center) * t)
    raw = np.empty(2 * len(t), np.int16)
    raw[0::2], raw[1::2] = x.real * 2048, x.imag * 2048
    frames = chz.feed(raw)
    assert [k for k, _, _ in frames] == list(range(len(frames)))
    lv = frames[-1][1]
    i = [c.name for c in w.channels].index("SFO Twr")
    assert lv[i] == pytest.approx(-20.0, abs=0.5)
    assert np.delete(lv, i).max() < lv[i] - 50
    assert frames[-1][2].shape == (len(w.channels), 400)


def test_whole_band_plan_is_the_listening_stations_20_msps():
    pytest.importorskip("numba")
    from atcsdr import frequencies as fq
    from atcsdr import pluto
    for atis, n in ((False, 43), (True, 51)):
        rate, windows, kind = G.plan_sweep(fq.area_channels("bay", atis=atis), pluto.PACKED_MAX_RATE)
        assert (rate, len(windows), kind) == (20_000_000, 1, G.FastChannelizer)
        assert len(windows[0].channels) == n


def test_synthetic_sweep_with_the_fast_receiver(tmp_path):
    pytest.importorskip("numba")
    gains = [30, 40, 50, 60, 70]
    rate, windows, kind = G.plan_sweep(FREQS, 4_800_000)
    radio = G.SyntheticRadio(windows[0].center, rate, gains[0], realtime=False)
    saver = _Saver()
    clips = G.Clips(windows, gains, 8.0, saver, audio_rate=kind.audio_rate)
    sweep = G.Sweep(radio, rate, windows, gains, slot_s=2.0, clips=clips, channelizer=kind, threads=1)
    assert sweep.stride == 1 and sweep.clips is clips
    sweep.run(deadline=radio.clock() + 50)
    rows, cells, (gain, _) = G.analyze(sweep.data(), sweep.slots, gains, 8.0, 40)
    by = {r["gain"]: r for r in rows}
    assert by[50]["snr_vs_ref"] == pytest.approx(model_snr(50) - model_snr(40), abs=1.5)
    assert gain == 50
    for r, audio in saver.rows:
        assert len(audio) == pytest.approx(r["seconds"] * kind.audio_rate, rel=0.01)
