#!/usr/bin/env python3
"""
ADS-B reception test for an RTL-SDR: how many aircraft, how far away, in
which directions. Run it after atc_reception.py, with the dongle's antenna
swapped for the short ADS-B one (about 6.9 cm).

    python3 reception_test/adsb_reception.py --ref 37.43 -122.17              # 10 minutes
    python3 reception_test/adsb_reception.py --ref cory                       # at Cory Hall, UC Berkeley
    python3 reception_test/adsb_reception.py --ref 37.43 -122.17 --duration 30m --gain 49.6
    python3 reception_test/adsb_reception.py --source synthetic --duration 1m  # no dongle

It runs radio/adsb_listen.py (1090 MHz, 2 MSPS: messages, tracks, health and
summary, printed live), then adds reception.txt: messages and aircraft heard,
the farthest aircraft overall and in each of 8 compass directions from --ref,
and how far out most positions were heard, and map.html: the tracks and the
coverage on a map (reception_test/adsb_viewer.py). --ref is your latitude and
longitude (from a map); without it there are no distances.

Everything goes in recordings/adsb_reception_<time>/.
"""

import argparse
import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import numpy, pyModeS  # noqa: F401  (the listener needs both)
except ModuleNotFoundError as e:
    sys.exit(f"{e.name} not found: this Python ({sys.executable}) isn't the sdr environment.\n"
             "Run 'conda activate sdr' first (set it up with: bash reception_test/setup.sh).")

from atcsdr.airband import parse_duration
from adsb_viewer import add_ref_arg, km_and_bearing, ref_arg, write_map, write_png

SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
KM_PER_NM = 1.852


def reception(out, ref):
    """reception.txt's lines, from adsb_listen.py's files in `out`."""
    lines = [f"ADS-B reception test, {out.name}"]
    summary = out / "summary.txt"
    if summary.exists():
        lines += [l for l in summary.read_text().splitlines()[1:] if l.startswith(("uptime", "stream", "traffic"))]
    positions = out / "positions.csv"
    rows = list(csv.DictReader(open(positions))) if positions.exists() else []
    if not rows:
        lines += ["No positions decoded. Check: the 1090 MHz antenna is on the dongle, it's vertical with a clear",
                  "view of the sky (a window helps a lot), the gain (try --gain 49.6), and that the 'traffic' line",
                  "above shows messages at all (none means no aircraft heard: antenna, cable or dongle)."]
        return lines
    if not ref:
        lines.append(f"{len(rows)} positions from {len({r['icao'] for r in rows})} aircraft. "
                     "Give --ref LAT LON for distances.")
        return lines

    # Farthest fix per aircraft, then the farthest aircraft per direction.
    best = {}
    for r in rows:
        km, brg = km_and_bearing(ref, (float(r["lat"]), float(r["lon"])))
        if km > best.get(r["icao"], (0,))[0]:
            best[r["icao"]] = (km, brg, r["callsign"], r["altitude_ft"])
    dists = sorted(km for km, *_ in best.values())
    far_km, far_brg, far_cs, far_alt = max(best.values())
    lines += [f"receiver:  {ref[0]:.5f}, {ref[1]:.5f}",
              f"aircraft with positions: {len(best)}",
              f"farthest:  {far_km:.0f} km ({far_km / KM_PER_NM:.0f} NM) {SECTORS[int((far_brg + 22.5) // 45) % 8]}, "
              f"{far_cs or 'no callsign'}" + (f" at {float(far_alt):.0f} ft" if far_alt not in ("", "None") else ""),
              f"typical:   half the aircraft were heard out to {dists[len(dists) // 2]:.0f} km, "
              f"90% to {dists[int(0.9 * (len(dists) - 1))]:.0f} km",
              "by direction (farthest aircraft, and how many were heard that way):"]
    for k, name in enumerate(SECTORS):
        here = [km for km, brg, *_ in best.values() if int((brg + 22.5) // 45) % 8 == k]
        lines.append(f"  {name:>2}  {max(here):5.0f} km  {len(here):3d} aircraft" if here else f"  {name:>2}      -")
    lines.append("A direction much shorter than the rest usually means something blocks the antenna that way "
                 "(a building, a hill, the house itself).")
    return lines


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_ref_arg(p)
    p.add_argument("--duration", default="10m", help="how long: 90s, 10m, 1h (default 10m)")
    p.add_argument("--gain", default="40", help="RTL-SDR gain, dB (default 40)")
    p.add_argument("--rtl", default="rtl", help="which dongle: rtl, or rtl:N (default rtl)")
    p.add_argument("--source", choices=["rtl", "synthetic"], default="rtl", help="default rtl")
    p.add_argument("--out", help="output folder (default recordings/adsb_reception_<time>)")
    p.add_argument("--tile-url", help="map tiles for both maps: esri, osm, or a URL with {z}/{x}/{y} "
                                      "(default: esri for map.html, osm for map.png)")
    args = p.parse_args()
    args.ref = ref_arg(p, args.ref)
    parse_duration(args.duration)                      # fail here, not in the child
    if not args.ref:
        print("No --ref given: messages and aircraft only, no distances.\n")

    out = Path(args.out or ROOT / f"recordings/adsb_reception_{datetime.now():%Y%m%d-%H%M}")
    cmd = [sys.executable, str(ROOT / "radio/adsb_listen.py"), "--source", args.source, "--rtl", args.rtl,
           "--gain", args.gain, "--duration", args.duration, "--out", str(out)]
    if args.ref:
        cmd += ["--ref", str(args.ref[0]), str(args.ref[1])]
    proc = subprocess.Popen(cmd)
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            pass         # Ctrl-C reaches the listener too: it saves its files and exits, then this reports
    if not out.exists():
        sys.exit(code or 1)
    report = "\n".join(reception(out, args.ref))
    print("\n" + report)
    (out / "reception.txt").write_text(report + "\n")
    if (out / "positions.csv").exists():
        write_map(out / "positions.csv", args.ref, out / "map.html", tile_url=args.tile_url or "esri")
        write_png(out / "positions.csv", args.ref, out / "map.png", tile_url=args.tile_url or "osm")
        print(f"\nMap: {out / 'map.html'}  (open it in a browser)\n     {out / 'map.png'}  (a picture of it)")
    sys.exit(code)


if __name__ == "__main__":
    main()
