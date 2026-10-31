import queue
import threading
import types

import numpy as np
import pytest

import synth
from atcsdr import adsb


@pytest.mark.parametrize("rate, min_fraction", [(4e6, 0.9), (2e6, 0.85)])
def test_demodulator_finds_frames(rate, min_fraction):
    count = 200
    iq = synth.mode_s_iq(rate, synth.KNOWN, count, seconds=0.5)
    demod = adsb.Demodulator(rate)
    found = []
    for k in range(0, len(iq), 1 << 18):                   # chunked, like a live stream
        found += [m for _, m in demod.process(iq[k:k + (1 << 18)])]
    assert len(found) >= min_fraction * count
    assert set(found) <= set(synth.KNOWN)                   # nothing but real frames
    assert len(found) <= count                               # no frame decoded twice


def test_tracker_decodes_known_messages_with_a_reference():
    tr = adsb.Tracker(ref=(52.2, 3.9))
    for t, msg in zip(range(4), synth.KNOWN):
        tr.add(1000.0 + t, msg)
    assert tr.aircraft["406B90"].callsign == "EZY85MH"
    ac = tr.aircraft["40621D"]
    assert ac.altitude == 38000
    # With a reference each position message decodes on its own: the even one,
    # then the odd one (pyModeS's documented answers for these two messages).
    (_, lat0, lon0, _), (_, lat1, lon1, _) = ac.path
    assert (lat0, lon0) == pytest.approx((52.2572, 3.9194), abs=1e-3)
    assert (lat1, lon1) == pytest.approx((52.2658, 3.9389), abs=1e-3)
    assert tr.aircraft["485020"].speed == 159


@pytest.mark.parametrize("ref", [None, (37.43, -122.17)])
def test_tracker_follows_a_simulated_flight(ref, tmp_path):
    msgs = synth.flight(0xA1B2C3, "UAL123", 37.6, -122.4, 5000, 45, 250, 1500)
    csv_path = tmp_path / "positions.csv"
    tr = adsb.Tracker(ref=ref, csv_path=str(csv_path))
    for t, m in msgs:
        tr.add(t, m)
    tr.close()
    ac = tr.aircraft["A1B2C3"]
    assert ac.callsign == "UAL123"
    assert ac.speed == pytest.approx(250, abs=2) and ac.track == pytest.approx(45, abs=1)
    positions = sum(1 for _ in open(csv_path)) - 1
    assert positions >= 230 if ref is None else positions == 240
    # Re-loading the CSV gives back the same track.
    again = adsb.Tracker()
    again.load_csv(str(csv_path))
    assert len(again.aircraft["A1B2C3"].path) == positions


@pytest.mark.parametrize("line, expected", [
    ("8D406B902015A678D4D220AA4BDA", (None, "8D406B902015A678D4D220AA4BDA")),
    ("*8d406b902015a678d4d220aa4bda;", (None, "8D406B902015A678D4D220AA4BDA")),
    ("1700000000.5 8D406B902015A678D4D220AA4BDA", (1700000000.5, "8D406B902015A678D4D220AA4BDA")),
    ("hello", None),
])
def test_parse_hex_line(line, expected):
    assert adsb.parse_hex_line(line) == expected


def test_hex_file_source_round_trip(tmp_path):
    path = tmp_path / "messages.txt"
    path.write_text("".join(f"{1000 + i} {m}\n" for i, m in enumerate(synth.KNOWN)))
    args = types.SimpleNamespace(network=None, hex=str(path), iq=None)
    out, stop = queue.Queue(), threading.Event()
    adsb.start_source(args, out, stop).join(5)
    items = []
    while (item := out.get()) is not None:
        items.append(item)
    assert [m for _, m in items] == synth.KNOWN
    assert [t for t, _ in items] == [1000, 1001, 1002, 1003]
