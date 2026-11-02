import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reception_test"))

import atc_reception as R
from atcsdr import airband as A
from atcsdr import frequencies as fq
from atcsdr.rtl import RTL_MAX_RATE


def scanner(tmp_path, **kw):
    rate, windows = A.plan(fq.area_channels("bay", atis=True), RTL_MAX_RATE)
    opts = argparse.Namespace(threshold=8.0, look=0.3, linger=4.0, max_visit=60.0, atis_seconds=20.0,
                              duration=None, strength=0.9, no_mp3=True, clips=str(tmp_path / "clips"),
                              clean=None, log=str(tmp_path / "transmissions.csv"), **kw)
    saver = R.Saver(opts)
    saver.start()
    radio = R.SyntheticRadio(windows[0].center, rate, realtime=False)
    return R.Scanner(radio, rate, windows, opts, saver), saver


def by_name(s, name):
    return next(c for c in s.channels if c.name == name)


def test_calibration_caps_a_steady_signal_so_it_still_opens(tmp_path):
    s, saver = scanner(tmp_path)
    s.calibrate(0)                      # window 1 holds SFO ATIS, which never stops
    saver.finish()
    atis, quiet = by_name(s, "SFO ATIS"), by_name(s, "SJC Clr")
    assert atis.cal_own_db > atis.cal_floor_db + 20                         # its own floor: the carrier
    assert atis.cal_floor_db == pytest.approx(quiet.cal_floor_db + R.STEADY_DB, abs=0.5)   # capped
    assert s.adc[0][1] < R.CLIPPING_WARN


def test_scan_stays_for_traffic_and_records_atis_once(tmp_path):
    s, saver = scanner(tmp_path)
    for i in range(len(s.windows)):
        s.calibrate(i)
    t_end = s.clock() + 60
    seconds = {}
    while s.clock() < t_end:
        for i in range(len(s.windows)):
            took, heard = s.visit(i)
            seconds.setdefault(i, []).append(took)
    saver.finish()
    atis = by_name(s, "SFO ATIS")
    assert atis.ignored and atis.transmissions == 1 and atis.talk_s >= 20
    assert by_name(s, "SFO Twr").transmissions >= 1
    assert by_name(s, "App South").transmissions >= 1
    heard = {c.name for c in s.channels if c.transmissions}
    assert heard <= {"SFO ATIS", "SFO Twr", "App South"}
    # Quiet windows are only looked at; window 1 no longer waits on its ATIS.
    assert max(seconds[3]) < 1.0 and min(seconds[0]) < 1.0
    assert len(list((tmp_path / "clips").glob("*.wav"))) == sum(c.transmissions for c in s.channels)


def test_viewer_writes_a_map_of_every_aircraft(tmp_path):
    import json
    import adsb_viewer as V
    sample = Path(__file__).resolve().parents[1] / "samples/adsb/tracks.csv"

    def data(page):
        return json.loads(page.split("const D = ", 1)[1].split(";\n", 1)[0])

    assert V.write_map(sample, (37.43, -122.17), tmp_path / "map.html") == 4
    d = data((tmp_path / "map.html").read_text())
    assert {a["cs"] for a in d["aircraft"]} == {"ASA1331", "EJA756", "JZA846", "SWA2285"}
    assert d["ref"] == [37.43, -122.17] and len(d["cover"]) == 2 * V.COVER_BINS
    assert 0 < d["farthest"]["km"] < 60
    V.write_map(sample, None, tmp_path / "noref.html")         # no receiver: tracks only
    assert "cover" not in data((tmp_path / "noref.html").read_text())


def test_viewer_tile_sources_carry_their_credit():
    import adsb_viewer as V
    esri = V.tile_source("esri")
    assert "arcgisonline.com" in esri["url"] and "Esri" in esri["credit"] and esri["max_zoom"] == 16
    osm = V.tile_source("osm")
    assert osm["url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png" and "OpenStreetMap" in osm["credit"]
    assert V.tile_source("https://{s}.tiles.example.com/{z}/{x}/{y}.png")["credit"] == "Map tiles: tiles.example.com"


def test_ref_takes_a_site_name_or_lat_lon():
    import adsb_viewer as V
    p = argparse.ArgumentParser()
    V.add_ref_arg(p)
    assert V.ref_arg(p, p.parse_args(["--ref", "cory"]).ref) == V.SITES["cory"]
    assert V.ref_arg(p, p.parse_args(["--ref", "Cory-Hall"]).ref) == V.SITES["cory"]
    assert V.ref_arg(p, p.parse_args(["--ref", "37.43", "-122.17"]).ref) == (37.43, -122.17)
    assert V.ref_arg(p, p.parse_args([]).ref) is None
    with pytest.raises(SystemExit):
        V.ref_arg(p, ["berkeley"])
