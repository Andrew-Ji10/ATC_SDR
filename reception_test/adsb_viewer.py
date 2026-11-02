#!/usr/bin/env python3
"""
ADS-B viewer: a run's aircraft tracks on a real map, as one HTML file that
opens in any browser.

    python3 reception_test/adsb_viewer.py recordings/adsb_reception_<time>/      # -> map.html there
    python3 reception_test/adsb_viewer.py positions.csv --ref 37.43 -122.17 --out map.html
    python3 reception_test/adsb_viewer.py samples/adsb/tracks.csv --ref 37.43 -122.17 --open
    python3 reception_test/adsb_viewer.py positions.csv --ref cory      # Cory Hall, UC Berkeley

It reads the positions CSV that adsb_reception.py, adsb_listen.py and
adsb_decode.py write, and draws:

  - each aircraft's track, colored by altitude (light = low, dark = high), dotted
    where no fixes came in for over 30 s; hover for the callsign, altitude, speed
    and distance, and the last fix is marked with a dot
  - with --ref (your latitude and longitude): your receiver, range rings, and the
    coverage, the farthest aircraft heard in each 10-degree direction, shaded
  - every single fix, if switched on in the layer menu (top right)

Given a run folder, --ref is read from its reception.txt. The map tiles load
from the internet when the page opens; the data is all inside the file.
It also saves the same map as map.png, for a quick look: the tiles are
fetched in Python and cached in recordings/tile_cache/, as for
radio/demo_video.py (--no-png skips it).

The maps: map.html uses Esri's light grey map, which keeps the tracks easy to
see and loads in a page opened from a file. map.png uses OpenStreetMap's
standard map, the same as radio/demo_video.py. --tile-url esri, osm, or any
{z}/{x}/{y} tile URL sets both. OpenStreetMap blocks pages opened from a file,
though: in map.html its tiles show 'Access blocked'.
"""

import argparse
import csv
import html
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from atcsdr.adsb_map import ALT_MAX_FT, ALT_RAMP, GAP_SECONDS, INK, MUTED, SURFACE
except ModuleNotFoundError as e:
    sys.exit(f"{e.name} not found: this Python ({sys.executable}) isn't the sdr environment.\n"
             "Run 'conda activate sdr' first (set it up with: bash reception_test/setup.sh).")

RINGS_KM = [5, 10, 25, 50, 100, 150, 200, 300]
COVER_BINS = 36                  # 10 degrees each
OSM_CREDIT = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
# Neither needs a key. (CARTO's light map, used at first, now does.)
TILES = {
    "esri": dict(url="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/"
                     "MapServer/tile/{z}/{y}/{x}",
                 credit="Tiles &copy; Esri &mdash; Esri, HERE, Garmin, " + OSM_CREDIT, max_zoom=16),
    "osm": dict(url="https://tile.openstreetmap.org/{z}/{x}/{y}.png", credit=OSM_CREDIT, max_zoom=19),
}
# Receiver sites that --ref can name instead of giving LAT LON.
SITES = {
    "cory": (37.8751, -122.2574),            # Cory Hall, UC Berkeley
}
SITES["cory-hall"] = SITES["cory"]
RECEIVER_LINE = re.compile(r"^receiver:\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)", re.M)


def km_and_bearing(a, b):
    """Great-circle distance (km) and initial bearing (degrees) from a to b."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    km = 2 * 6371 * math.asin(math.sqrt(h))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return km, math.degrees(math.atan2(y, x)) % 360


def destination(a, km, bearing):
    """The point `km` from a along `bearing` (degrees)."""
    la1, lo1, b, d = math.radians(a[0]), math.radians(a[1]), math.radians(bearing), km / 6371
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180


def num(v):
    return float(v) if v not in ("", "None", None) else None


def alt_color(alt):
    if alt is None:
        return MUTED
    return ALT_RAMP[min(len(ALT_RAMP) - 1, max(0, int(alt / (ALT_MAX_FT / len(ALT_RAMP)))))]


def hms(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


def load(csv_path):
    """{icao: [(t, lat, lon, alt, speed, callsign)]}, each in time order."""
    tracks = {}
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            tracks.setdefault(r["icao"], []).append(
                (float(r["time"]), float(r["lat"]), float(r["lon"]), num(r["altitude_ft"]),
                 num(r.get("groundspeed_kt")), r["callsign"]))
    for fixes in tracks.values():
        fixes.sort()
    return tracks


def map_data(tracks, ref):
    """Everything the page draws, as plain lists for JSON."""
    aircraft, cover = [], [0.0] * COVER_BINS
    for icao, fixes in tracks.items():
        callsign = next((c for *_, c in reversed(fixes) if c), "")
        # Runs of fixes in one altitude band; a gap of over GAP_SECONDS gets its own dotted piece.
        segments, prev = [], None
        for t, lat, lon, alt, _speed, _cs in fixes:
            color = alt_color(alt)
            if prev is None:
                segments.append(dict(c=color, g=0, p=[[lat, lon]]))
            elif t - prev[0] > GAP_SECONDS:
                segments.append(dict(c=MUTED, g=1, p=[[prev[1], prev[2]], [lat, lon]]))
                segments.append(dict(c=color, g=0, p=[[lat, lon]]))
            elif color != segments[-1]["c"]:
                segments.append(dict(c=color, g=0, p=[[prev[1], prev[2]], [lat, lon]]))
            else:
                segments[-1]["p"].append([lat, lon])
            prev = (t, lat, lon)
        far = None
        if ref:
            for _t, lat, lon, *_ in fixes:
                km, brg = km_and_bearing(ref, (lat, lon))
                k = int(brg // (360 / COVER_BINS)) % COVER_BINS
                cover[k] = max(cover[k], km)
                far = km if far is None else max(far, km)
        last = fixes[-1]
        alts = [f[3] for f in fixes if f[3] is not None]
        aircraft.append(dict(
            icao=icao, cs=callsign, segs=segments, last=[last[1], last[2]], color=alt_color(last[3]),
            fixes=[[round(f[1], 5), round(f[2], 5)] for f in fixes],
            info=dict(alt=last[3], top=max(alts) if alts else None, kt=last[4], n=len(fixes),
                      first=hms(fixes[0][0]), lastt=hms(last[0]), far=None if far is None else round(far, 1))))
    data = dict(aircraft=aircraft, ref=list(ref) if ref else None,
                legend=[[f"{int(i * ALT_MAX_FT / len(ALT_RAMP)):,}", c] for i, c in enumerate(ALT_RAMP)],
                muted=MUTED)
    if ref and any(cover):
        step = 360 / COVER_BINS
        # Each bin is drawn as an arc at its farthest range, so the shape follows the data.
        poly = []
        for k, km in enumerate(cover):
            for b in (k * step, (k + 1) * step):
                poly.append(destination(ref, km, b) if km else list(ref))
        data["cover"] = [list(p) for p in poly]
        far_k = max(range(COVER_BINS), key=lambda k: cover[k])
        data["farthest"] = dict(km=round(cover[far_k], 1), dir=round((far_k + 0.5) * step))
        top = max(cover)
        data["rings"] = [r for r in RINGS_KM if r <= top * 1.3] or RINGS_KM[:1]
    return data


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADS-B Coverage Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { --ink: #1f1f1e; --muted: #6b6a66; --surface: #fcfcfb; --line: #e6e5e1; --cover: #2a78d6; }
  html, body { margin: 0; height: 100%; background: var(--surface); color: var(--ink);
               font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  #map { position: absolute; inset: 0; }
  .panel { position: absolute; z-index: 1000; left: 12px; top: 12px; max-width: min(300px, calc(100vw - 24px));
           background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
           padding: 12px 14px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .panel h1 { font-size: 16px; margin: 0 0 2px; }
  .panel .run { color: var(--muted); font-size: 12px; margin-bottom: 8px; word-break: break-all; }
  .stats { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; margin: 0 0 10px; }
  .stats dt { color: var(--muted); }
  .stats dd { margin: 0; font-variant-numeric: tabular-nums; }
  .legend { display: flex; gap: 0; margin-top: 4px; }
  .legend span { flex: 1; height: 8px; }
  .legend-labels { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; }
  .key { color: var(--muted); font-size: 12px; margin-top: 8px; }
  .ring-label { color: var(--muted); font-size: 11px; white-space: nowrap; text-shadow: 0 0 3px #fff, 0 0 3px #fff; }
  .leaflet-tooltip { font: 12px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  @media (max-width: 520px) { .panel { font-size: 13px; padding: 10px 12px; } }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>ADS-B coverage</h1>
  <div class="run">__RUN__</div>
  <dl class="stats" id="stats"></dl>
  <div>Altitude, ft</div>
  <div class="legend" id="legend"></div>
  <div class="legend-labels"><span>0</span><span>__ALT_MAX__+</span></div>
  <div class="key" id="key"></div>
</div>
<script>
const D = __DATA__;
const map = L.map("map", { preferCanvas: true, zoomControl: false });
L.control.zoom({ position: "bottomright" }).addTo(map);
// The view comes first: the canvas renderer can't place lines on a map without one.
const bounds = D.aircraft.flatMap(a => a.fixes).concat(D.ref ? [D.ref] : []);
if (bounds.length) map.fitBounds(bounds, { padding: [30, 30] }); else map.setView([37.6, -122.2], 9);
const T = __TILES__;
L.tileLayer(T.url, {
  subdomains: "abc", maxZoom: T.max_zoom, attribution: T.credit
}).addTo(map);

const tracks = L.layerGroup().addTo(map), fixes = L.layerGroup(), cover = L.layerGroup(), rings = L.layerGroup();
const fmt = (v, unit) => v == null ? "–" : Math.round(v).toLocaleString() + unit;

for (const a of D.aircraft) {
  const name = a.cs || a.icao;
  const tip = `<b>${name}</b>${a.cs ? " · " + a.icao : ""}<br>` +
    `${fmt(a.info.alt, " ft")} · ${fmt(a.info.kt, " kt")}<br>` +
    `${a.info.n} fixes, ${a.info.first}–${a.info.lastt} UTC` +
    (a.info.far != null ? `<br>farthest ${a.info.far} km` : "");
  for (const s of a.segs) {
    if (s.p.length < 2) continue;
    L.polyline(s.p, { color: s.c, weight: s.g ? 1.5 : 2.5, opacity: s.g ? 0.7 : 0.95,
                      dashArray: s.g ? "2 6" : null }).bindTooltip(tip, { sticky: true }).addTo(tracks);
  }
  L.circleMarker(a.last, { radius: 4.5, color: "#fff", weight: 1.5, fillColor: a.color, fillOpacity: 1 })
    .bindTooltip(tip).addTo(tracks);
  for (const p of a.fixes) {
    L.circleMarker(p, { radius: 1.5, stroke: false, fillColor: a.color, fillOpacity: 0.8 }).addTo(fixes);
  }
}

const overlays = { "Tracks": tracks, "Every fix": fixes };
if (D.ref) {
  if (D.cover) {
    L.polygon(D.cover, { color: "#2a78d6", weight: 1, dashArray: "4 4", fillColor: "#2a78d6",
                         fillOpacity: 0.08 }).bindTooltip("Coverage: the farthest aircraft heard in each direction",
                                                          { sticky: true }).addTo(cover);
    cover.addTo(map);
    overlays["Coverage"] = cover;
  }
  for (const km of D.rings || []) {
    L.circle(D.ref, { radius: km * 1000, color: "#6b6a66", weight: 0.8, opacity: 0.6, fill: false,
                      interactive: false }).addTo(rings);
    // Labelled on the side the aircraft were heard, which is the part of the ring in view.
    const b = (D.farthest ? D.farthest.dir : 0) * Math.PI / 180;
    const at = [D.ref[0] + km / 111.2 * Math.cos(b),
                D.ref[1] + km / (111.2 * Math.cos(D.ref[0] * Math.PI / 180)) * Math.sin(b)];
    L.marker(at, { interactive: false, icon: L.divIcon({ className: "ring-label", html: km + " km",
                                                          iconSize: [44, 14], iconAnchor: [22, 7] }) }).addTo(rings);
  }
  rings.addTo(map);
  overlays["Range rings"] = rings;
  L.circleMarker(D.ref, { radius: 6, color: "#1f1f1e", weight: 2, fillColor: "#fff", fillOpacity: 1 })
    .bindTooltip("Receiver").addTo(map);
}
L.control.layers(null, overlays, { collapsed: false }).addTo(map);

const nfix = D.aircraft.reduce((s, a) => s + a.info.n, 0);
const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const stats = [["Aircraft", D.aircraft.length.toLocaleString()], ["Positions", nfix.toLocaleString()]];
if (D.farthest) stats.push(["Farthest", `${D.farthest.km} km (${Math.round(D.farthest.km / 1.852)} NM) ` +
                                         dirs[Math.round(D.farthest.dir / 45) % 8]]);
document.getElementById("stats").innerHTML = stats.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
document.getElementById("legend").innerHTML = D.legend.map(([, c]) => `<span style="background:${c}"></span>`).join("");
document.getElementById("key").textContent = D.ref
  ? "Dotted: no fixes for over 30 s. Shaded: how far out aircraft were heard in each direction. Hover a track for details."
  : "Dotted: no fixes for over 30 s. Give --ref to see your receiver, range rings and coverage.";
</script>
</body>
</html>
"""


def tile_source(url):
    """{url, credit, max_zoom} for --tile-url: a name in TILES, or a {z}/{x}/{y} URL."""
    if url in TILES:
        return TILES[url]
    host = re.sub(r"^https?://(\{s\}\.)?([^/]+).*", r"\2", url)
    return dict(url=url, credit=f"Map tiles: {html.escape(host)}", max_zoom=19)


def add_ref_arg(p):
    """--ref LAT LON, or a site name from SITES (e.g. --ref cory); resolve it with ref_arg()."""
    p.add_argument("--ref", nargs="+", metavar="LAT LON | SITE",
                   help="your latitude and longitude, or a site: " + ", ".join(SITES))


def ref_arg(p, values):
    """--ref's words -> (lat, lon), or None if not given; exits through `p` if it can't be read."""
    if not values:
        return None
    if len(values) == 1 and values[0].lower() in SITES:
        return SITES[values[0].lower()]
    try:
        lat, lon = map(float, values)
    except ValueError:
        p.error(f"--ref takes LAT LON or a site name ({', '.join(SITES)}), not {' '.join(values)!r}")
    return lat, lon


def write_map(csv_path, ref, out_path, title=None, tile_url="esri"):
    """Write the HTML map for `csv_path`. Returns the number of aircraft drawn."""
    data = map_data(load(csv_path), ref)
    if "openstreetmap.org" in tile_source(tile_url)["url"]:
        print("warning: openstreetmap.org refuses map pages opened from a file (it wants a web site's\n"
              "address with each request), so map.html will show 'Access blocked' tiles unless it's\n"
              "served from a web server. --tile-url esri has no such limit.", file=sys.stderr)
    page = (PAGE.replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
                .replace("__RUN__", html.escape(title or Path(csv_path).parent.name))
                .replace("__ALT_MAX__", f"{ALT_MAX_FT:,}")
                .replace("__TILES__", json.dumps(tile_source(tile_url))))
    Path(out_path).write_text(page)
    return len(data["aircraft"])


def write_png(csv_path, ref, out_path, tile_url="osm", title=None, size=(1600, 1200)):
    """The same map as a picture. The tiles come through radio/demo_video.py's
    basemap(), which fetches them once into recordings/tile_cache/; without
    internet the picture is drawn on a plain background."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.patheffects as pe
    import matplotlib.pyplot as plt
    import numpy as np
    sys.path.insert(0, str(ROOT / "radio"))
    import demo_video as DV

    data = map_data(load(csv_path), ref)
    lat_lon = [p for a in data["aircraft"] for p in a["fixes"]] + ([list(ref)] if ref else [])
    if not lat_lon:
        return 0
    xs, ys = DV.merc([p[0] for p in lat_lon], [p[1] for p in lat_lon])
    # The view: everything plus a margin, at least ~10 km across, shaped like the picture.
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    half_w = max((xs.max() - xs.min()) * 0.6, 6000)
    half_h = max((ys.max() - ys.min()) * 0.6, 6000)
    aspect = size[0] / size[1]
    half_w, half_h = max(half_w, half_h * aspect), max(half_h, half_w / aspect)
    x0, x1, y0, y1 = cx - half_w, cx + half_w, cy - half_h, cy + half_h

    def lat_of(y):
        return math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)

    def lon_of(x):
        return math.degrees(x / 6378137)

    url, credit = tile_source(tile_url)["url"], tile_source(tile_url)["credit"]
    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100, facecolor=SURFACE)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1); ax.set_axis_off()
    try:
        tiles, extent = DV.basemap(lat_of(y0), lon_of(x0), lat_of(y1), lon_of(x1), size[0],
                                   url.replace("{s}", "a").replace("{r}", ""), ROOT / "recordings" / "tile_cache")
        ax.imshow(tiles, extent=extent, interpolation="bilinear", zorder=0)
    except Exception as e:                 # offline, or the tile server said no: draw without a map
        print(f"warning: no map tiles ({e}); drawing on a plain background", file=sys.stderr)
        credit = ""

    def line(points, **kw):
        mx, my = DV.merc([p[0] for p in points], [p[1] for p in points])
        return ax.plot(mx, my, **kw)

    halo = [pe.withStroke(linewidth=3, foreground="white")]
    if data.get("cover"):
        mx, my = DV.merc([p[0] for p in data["cover"]], [p[1] for p in data["cover"]])
        ax.fill(mx, my, color="#2a78d6", alpha=0.10, zorder=1, lw=0)
        ax.plot(np.append(mx, mx[0]), np.append(my, my[0]), color="#2a78d6", lw=1, ls=(0, (4, 4)), zorder=1)
    if ref:
        lab = (data.get("farthest") or {}).get("dir", 0)
        for km in data.get("rings", []):
            line([destination(ref, km, b) for b in range(0, 361, 3)], color=MUTED, lw=0.8, alpha=0.7, zorder=2)
            tx, ty = DV.merc(*destination(ref, km, lab))
            ax.text(tx, ty, f"{km} km", color=MUTED, fontsize=9, ha="center", va="center", zorder=2,
                    path_effects=halo)
    for a in data["aircraft"]:
        for seg in a["segs"]:
            if len(seg["p"]) > 1:
                lw = 1.2 if seg["g"] else 2.6
                # A white edge keeps light (low) tracks visible on a busy street map.
                line(seg["p"], color=seg["c"], zorder=3, solid_capstyle="round", lw=lw,
                     path_effects=[pe.Stroke(linewidth=lw + 2.5, foreground="white"), pe.Normal()],
                     **(dict(ls=(0, (1, 4)), alpha=0.9) if seg["g"] else {}))
        mx, my = DV.merc(*a["last"])
        ax.scatter([mx], [my], s=36, color=a["color"], edgecolor="white", linewidth=1.2, zorder=4)
        if len(data["aircraft"]) <= 30:
            alt = a["info"]["alt"]
            ax.annotate((a["cs"] or a["icao"]) + (f"\n{alt:,.0f} ft" if alt is not None else ""), (mx, my),
                        xytext=(6, 4), textcoords="offset points", fontsize=8.5, color=INK, zorder=5,
                        path_effects=halo)
    if ref:
        mx, my = DV.merc(*ref)
        ax.scatter([mx], [my], s=80, color="white", edgecolor=INK, linewidth=2, zorder=6)
        ax.annotate("Receiver", (mx, my), xytext=(8, -12), textcoords="offset points", fontsize=9,
                    color=INK, zorder=6, path_effects=halo)

    nfix = sum(a["info"]["n"] for a in data["aircraft"])
    info = [title or Path(csv_path).parent.name, f"{len(data['aircraft'])} aircraft, {nfix:,} positions"]
    if data.get("farthest"):
        f = data["farthest"]
        info.append(f"farthest {f['km']:.0f} km ({f['km'] / 1.852:.0f} NM) "
                    f"{['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][round(f['dir'] / 45) % 8]}")
    ax.text(0.012, 0.985, "\n".join(info), transform=ax.transAxes, va="top", fontsize=11, color=INK, zorder=7,
            linespacing=1.5, bbox=dict(boxstyle="round,pad=0.6", fc=SURFACE, ec="#e6e5e1"))
    box = fig.add_axes([0.008, 0.008, 0.215, 0.078])
    box.set_xticks([]); box.set_yticks([]); box.set_facecolor(SURFACE)
    for side in box.spines.values():
        side.set_color("#e6e5e1")
    key = fig.add_axes([0.02, 0.046, 0.19, 0.012])
    key.imshow([list(range(len(ALT_RAMP)))], aspect="auto", extent=(0, ALT_MAX_FT, 0, 1),
               cmap=matplotlib.colors.ListedColormap(ALT_RAMP))
    key.set_yticks([]); key.set_xticks([0, ALT_MAX_FT], ["0", f"{ALT_MAX_FT:,}+ ft"], fontsize=8, color=MUTED)
    key.set_title("Altitude", fontsize=8.5, color=INK, loc="left", pad=3)
    for side in key.spines.values():
        side.set_visible(False)
    if credit:
        ax.text(0.995, 0.005, html.unescape(re.sub(r"<[^>]+>", "", credit)), transform=ax.transAxes,
                ha="right", va="bottom", fontsize=8, color=MUTED, zorder=7,
                bbox=dict(fc=SURFACE, ec="none", alpha=0.8, pad=2))
    fig.savefig(out_path, dpi=100, facecolor=SURFACE)
    plt.close(fig)
    return len(data["aircraft"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="a run folder (with positions.csv) or a positions CSV")
    add_ref_arg(p)
    p.add_argument("--out", help="output HTML (default: map.html beside the CSV)")
    p.add_argument("--tile-url", help="map tiles for both: esri, osm, or a URL with {z}/{x}/{y} "
                                      "(default: esri for map.html, osm for map.png)")
    p.add_argument("--no-png", action="store_true", help="only the HTML map, no map.png")
    p.add_argument("--open", action="store_true", help="open the HTML map in the browser")
    args = p.parse_args()

    src = Path(args.source)
    csv_path = src / "positions.csv" if src.is_dir() else src
    if not csv_path.exists():
        sys.exit(f"{csv_path} not found")
    ref = ref_arg(p, args.ref)
    rec = csv_path.parent / "reception.txt"
    if ref is None and rec.exists() and (m := RECEIVER_LINE.search(rec.read_text())):
        ref = (float(m[1]), float(m[2]))
    out = Path(args.out or csv_path.parent / "map.html")
    n = write_map(csv_path, ref, out, tile_url=args.tile_url or "esri")
    print(f"{n} aircraft -> {out}" + ("" if ref else "  (no --ref: no receiver, rings or coverage)"))
    if not args.no_png:
        write_png(csv_path, ref, out.with_suffix(".png"), tile_url=args.tile_url or "osm")
        print(f"picture -> {out.with_suffix('.png')}")
    if args.open:
        subprocess.run(["open", str(out)])


if __name__ == "__main__":
    main()
