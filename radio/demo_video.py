#!/usr/bin/env python3
"""
Demonstration video: replay a station test's ADS-B tracks on a street map, with
its ATC transmissions on the soundtrack at the moments they were heard.

    python3 radio/demo_video.py recordings/<run>/station --ref 37.43 -122.17
    python3 radio/demo_video.py <station dir> --preview 60          # the first minute only
    python3 radio/demo_video.py <station dir> --size 1280x720 --ff 20

Reads <dir>/atc/transmissions.csv and the clips in atc/clips_clean/ (atc/clips
with --plain), and <dir>/adsb/positions.csv. Playback runs in real time while
anyone is talking (from --pre before a transmission to --post after it) and
fast-forwards --ff times in between, so speech is never sped up.

On screen: aircraft (arrows coloured by altitude, fading trails, callsign and
altitude), the airports, pulsing while their tower / approach / ground channel
talks, the UTC clock (with a fast-forward marker), the channel talking now,
recent transmissions and the aircraft in view. The map is OpenStreetMap's,
fetched once and cached in recordings/tile_cache/.
Needs ffmpeg (on PATH, or imageio-ffmpeg). Writes <dir>/demo.mp4 (or --out).
"""

import argparse
import csv
import math
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.markers import MarkerStyle
from matplotlib.transforms import Affine2D

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adsb"))

import animate_tracks as AT                          # noqa: E402  (track loading, interpolation, arrows)
from atcsdr.adsb_map import altitude_colors          # noqa: E402

AUDIO_RATE = 16_000
# OpenStreetMap's standard tiles: light personal use is fine under its tile policy
# when tiles are cached (they are) and credited (on screen). --tile-url for others.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_PX = 256
ATTRIBUTION = "© OpenStreetMap contributors"
ACCENT = "#e8590c"
INK, MUTED, PANEL = "#1f2933", "#5f6b7a", "#f7f7f5"

# Approximate airport reference points, for the map and the talking highlight.
AIRPORTS = {
    "SFO": (37.6189, -122.3750), "OAK": (37.7214, -122.2208), "SJC": (37.3639, -121.9289),
    "PAO": (37.4611, -122.1150), "SQL": (37.5119, -122.2494), "NUQ": (37.4161, -122.0490),
    "RHV": (37.3329, -121.8197), "HWD": (37.6592, -122.1217),
}
KINDS = {"tower": "Tower", "approach": "Approach", "departure": "Departure", "ground": "Ground",
         "clearance": "Clearance", "guard": "Guard", "atis": "ATIS"}


# ------------------------------------------------------------------ map tiles (Web Mercator)

def merc(lat, lon):
    """lat / lon (degrees) -> Web Mercator metres."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    return (6378137 * np.radians(lon),
            6378137 * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)))


def tile_xy(lat, lon, z):
    n = 2 ** z
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def tile_bounds(x, y, z):
    """Mercator (west, south, east, north) of tile x, y at zoom z."""
    n, r = 2 ** z, 6378137 * math.pi
    return (x / n * 2 * r - r, r - (y + 1) / n * 2 * r, (x + 1) / n * 2 * r - r, r - y / n * 2 * r)


def basemap(south, west, north, east, width_px, url, cache):
    """A stitched tile image covering the box, and its Mercator extent."""
    from PIL import Image
    span = max(east - west, 1e-6)
    z = int(max(1, min(16, round(math.log2(width_px * 360 / (TILE_PX * span))))))
    x0, y0 = map(int, tile_xy(north, west, z))
    x1, y1 = map(int, tile_xy(south, east, z))
    img = Image.new("RGB", ((x1 - x0 + 1) * TILE_PX, (y1 - y0 + 1) * TILE_PX), "white")
    style = url.split("//")[-1].split("/")[0]
    fetched = 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            path = cache / style.replace("/", "_") / str(z) / str(x) / f"{y}.png"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                req = urllib.request.Request(url.format(z=z, x=x, y=y),
                                             headers={"User-Agent": "ATC_SDR-demo_video/1.0 (personal project)"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    path.write_bytes(r.read())
                fetched += 1
                time.sleep(0.05)
            tile = Image.open(path).convert("RGB")
            if tile.size != (TILE_PX, TILE_PX):
                tile = tile.resize((TILE_PX, TILE_PX))
            img.paste(tile, ((x - x0) * TILE_PX, (y - y0) * TILE_PX))
    w, _, _, n = tile_bounds(x0, y0, z)
    _, s, e, _ = tile_bounds(x1, y1, z)
    print(f"map: zoom {z}, {(x1 - x0 + 1) * (y1 - y0 + 1)} tiles ({fetched} fetched, the rest cached)")
    return np.asarray(img), (w, e, s, n)


# ------------------------------------------------------------------ audio

def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found: install it, or pip install imageio-ffmpeg")


def decode(ff, path):
    """Any audio file -> float32 mono at AUDIO_RATE."""
    out = subprocess.run([ff, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(AUDIO_RATE), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768


# ------------------------------------------------------------------ the replay

def utc_time(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def airport_of(name):
    code = name.split()[0]
    return code if code in AIRPORTS else None


def schedule(t0, t1, talks, fps, ff):
    """Real time of each video frame: 1x inside the talk windows, `ff`x outside,
    never stepping past the start of a window."""
    times, flags, t, k = [], [], t0, 0
    while t < t1:
        while k < len(talks) and talks[k][1] <= t:
            k += 1
        talking = k < len(talks) and talks[k][0] <= t
        times.append(t)
        flags.append(not talking)
        if talking:
            t += 1 / fps
        else:
            nxt = talks[k][0] if k < len(talks) else t1
            t = min(t + ff / fps, nxt) if nxt > t else t + ff / fps
    return np.array(times), np.array(flags)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dir", help="a station test folder (with atc/ and adsb/)")
    p.add_argument("--ref", type=float, nargs=2, metavar=("LAT", "LON"), help="the receiver's position")
    p.add_argument("--out", help="output .mp4 (default <dir>/demo.mp4)")
    p.add_argument("--plain", action="store_true", help="the plain clips instead of the cleaned ones")
    p.add_argument("--ff", type=float, default=10, help="fast-forward between transmissions (default 10x)")
    p.add_argument("--pre", type=float, default=1.5, help="real time before each transmission (default 1.5 s)")
    p.add_argument("--post", type=float, default=1.0, help="... and after it (default 1 s)")
    p.add_argument("--fps", type=int, default=20)
    p.add_argument("--size", default="1920x1080", help="video size (default 1920x1080)")
    p.add_argument("--tile-url", default=TILE_URL, help="map tiles, {z}/{x}/{y} (default OpenStreetMap)")
    p.add_argument("--gap", type=float, default=45, help="bridge position gaps up to this long (default 45 s)")
    p.add_argument("--trail", type=float, default=180, help="aircraft trail, seconds (default 180)")
    p.add_argument("--preview", type=float, help="render only this many seconds of video")
    args = p.parse_args()

    run = Path(args.dir)
    out = Path(args.out or run / "demo.mp4")
    W, H = map(int, args.size.lower().split("x"))
    ff = ffmpeg_exe()
    AT.GAP_SECONDS = args.gap                            # bridge the sparse fixes a weak site gets

    # --- data
    clip_dir = run / "atc" / ("clips" if args.plain else "clips_clean")
    txs = []
    for r in csv.DictReader(open(run / "atc" / "transmissions.csv", newline="")):
        f = clip_dir / r["file"]
        if not f.exists():
            f = next(iter(clip_dir.glob(Path(r["file"]).stem + ".*")), f)   # WAV fallback
        txs.append(dict(start=utc_time(r["time_utc"]), seconds=float(r["seconds"]), mhz=r["mhz"],
                        name=r["name"], kind=r["kind"], file=f, airport=airport_of(r["name"])))
    txs.sort(key=lambda t: t["start"])
    pos_csv = run / "adsb" / "positions.csv"
    planes = AT.load(pos_csv) if pos_csv.exists() and pos_csv.stat().st_size > 100 else {}
    ac_csv = run / "adsb" / "aircraft.csv"                # callsigns that arrived apart from positions
    if ac_csv.exists():
        for r in csv.DictReader(open(ac_csv, newline="")):
            if r.get("callsign") and r["icao"] in planes and planes[r["icao"]]["callsign"] == r["icao"]:
                planes[r["icao"]]["callsign"] = r["callsign"]
    if not txs and not planes:
        sys.exit(f"nothing in {run}: no transmissions and no positions")
    starts = [t["start"] for t in txs] + [pl["time"][0] for pl in planes.values()]
    ends = [t["start"] + t["seconds"] for t in txs] + [pl["time"][-1] + AT.HOLD_S for pl in planes.values()]
    t0, t1 = min(starts) - 3, max(ends) + 3

    talks = []                                           # merged real-time windows
    for t in txs:
        a, b = t["start"] - args.pre, t["start"] + t["seconds"] + args.post
        if talks and a <= talks[-1][1]:
            talks[-1][1] = max(talks[-1][1], b)
        else:
            talks.append([a, b])
    real, fast = schedule(t0, t1, talks, args.fps, args.ff)
    if args.preview:
        real, fast = real[: int(args.preview * args.fps)], fast[: int(args.preview * args.fps)]
    video_t = np.arange(len(real)) / args.fps
    print(f"{len(txs)} transmissions, {len(planes)} aircraft, {(t1 - t0) / 60:.1f} min of recording -> "
          f"{len(real) / args.fps / 60:.1f} min of video ({len(real)} frames)")

    # --- soundtrack: each clip at the video time of its real start
    audio = np.zeros(int((len(real) / args.fps + 1) * AUDIO_RATE), np.float32)
    for t in txs:
        if not (real[0] <= t["start"] <= real[-1]):
            continue
        try:
            x = decode(ff, t["file"])
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f"  couldn't read {t['file'].name}, skipped")
            continue
        at = int(np.interp(t["start"], real, video_t) * AUDIO_RATE)
        n = min(len(x), len(audio) - at)
        audio[at: at + n] += 0.8 * x[:n]
    peak = np.max(np.abs(audio)) or 1
    audio = np.clip(audio / max(peak, 1.0), -1, 1)
    wav = Path(tempfile.mkdtemp()) / "soundtrack.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())

    # --- the map box: aircraft, receiver, airports heard, plus a margin, at the map's aspect
    lats = [la for pl in planes.values() for la in pl["lat"]]
    lons = [lo for pl in planes.values() for lo in pl["lon"]]
    for code in {t["airport"] for t in txs if t["airport"]}:
        lats.append(AIRPORTS[code][0])
        lons.append(AIRPORTS[code][1])
    if args.ref:
        lats.append(args.ref[0])
        lons.append(args.ref[1])
    mx, my = merc(lats, lons)
    map_frac = 0.70                                      # of the width; the rest is the side panel
    cx, cy = (mx.min() + mx.max()) / 2, (my.min() + my.max()) / 2
    half_w = max(np.ptp(mx), np.ptp(my) * map_frac * W / H, 20_000) / 2 * 1.25
    half_h = half_w * H / (map_frac * W)
    x_lim, y_lim = (cx - half_w, cx + half_w), (cy - half_h, cy + half_h)
    lat_of = lambda y: math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)
    lon_of = lambda x: math.degrees(x / 6378137)
    tiles, extent = basemap(lat_of(y_lim[0]), lon_of(x_lim[0]), lat_of(y_lim[1]), lon_of(x_lim[1]),
                            map_frac * W, args.tile_url, ROOT / "recordings" / "tile_cache")

    # --- figure: static parts drawn once, the moving parts each frame (blitting)
    dpi = 100
    fig = plt.figure(figsize=(W / dpi, H / dpi), dpi=dpi, facecolor=PANEL)
    ax = fig.add_axes([0, 0, map_frac, 1])
    ax.imshow(tiles, extent=extent, interpolation="bilinear", zorder=0)
    ax.set_xlim(*x_lim)
    ax.set_ylim(*y_lim)
    ax.set_axis_off()
    ax.text(0.995, 0.004, ATTRIBUTION, transform=ax.transAxes, ha="right", va="bottom", fontsize=8,
            color=MUTED, bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", pad=2))
    for code, (la, lo) in AIRPORTS.items():
        x, y = merc(la, lo)
        if x_lim[0] < x < x_lim[1] and y_lim[0] < y < y_lim[1]:
            ax.plot(x, y, marker="s", markersize=7, color=INK, zorder=3)
            ax.annotate(code, (x, y), xytext=(7, -4), textcoords="offset points", fontsize=11,
                        color=INK, weight="bold", zorder=3)
    if args.ref:
        x, y = merc(*args.ref)
        ax.plot(x, y, marker="^", markersize=11, color=ACCENT, markeredgecolor="white", zorder=3)
        ax.annotate("Receiver", (x, y), xytext=(-12, -18), textcoords="offset points", fontsize=10,
                    color=ACCENT, ha="right")              # below-left: aircraft labels go above-right
    side = fig.add_axes([map_frac, 0, 1 - map_frac, 1])
    side.set_axis_off()
    side.text(0.06, 0.965, "Bay Area ATC + ADS-B", fontsize=20, weight="bold", color=INK, va="top")
    side.text(0.06, 0.925, f"replay of {datetime.fromtimestamp(t0, timezone.utc):%Y-%m-%d}\n"
                           f"{len(txs)} transmissions, {len(planes)} aircraft", fontsize=11, color=MUTED, va="top")
    side.text(0.06, 0.55, "Recent transmissions", fontsize=12, weight="bold", color=INK, va="top")
    side.text(0.06, 0.25, "Aircraft in view", fontsize=12, weight="bold", color=INK, va="top")

    dyn = []

    def D(artist):
        artist.set_animated(True)
        dyn.append(artist)
        return artist

    clock = D(side.text(0.06, 0.86, "", fontsize=30, family="monospace", color=INK, va="top"))
    speed = D(side.text(0.06, 0.79, "", fontsize=13, color=MUTED, va="top"))
    now_box = D(side.text(0.06, 0.74, "", fontsize=16, color="white", va="top", weight="bold",
                          bbox=dict(boxstyle="round,pad=0.5", facecolor=ACCENT, edgecolor="none")))
    recent = D(side.text(0.06, 0.51, "", fontsize=11, family="monospace", color=INK, va="top"))
    inview = D(side.text(0.06, 0.21, "", fontsize=11, family="monospace", color=INK, va="top"))
    pulse = D(ax.scatter([], [], s=[], facecolors="none", edgecolors=ACCENT, linewidths=3, zorder=4))
    cmap, norm = altitude_colors()
    art = {}
    for icao in planes:
        art[icao] = dict(
            trail=D(ax.add_collection(LineCollection([], linewidths=2.5, capstyle="round", zorder=5))),
            marker=D(ax.plot([], [], linestyle="none", markersize=16, markeredgecolor="white",
                             markeredgewidth=1, zorder=6)[0]),
            label=D(ax.annotate("", (0, 0), xytext=(11, 7), textcoords="offset points", fontsize=10,
                                color=INK, zorder=7,
                                bbox=dict(facecolor="white", alpha=0.75, edgecolor="none", pad=1.5))))

    fig.canvas.draw()
    background = fig.canvas.copy_from_bbox(fig.bbox)

    enc = subprocess.Popen([ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
                            "-r", str(args.fps), "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset",
                            "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                            "-ar", "48000", "-shortest", str(out)], stdin=subprocess.PIPE)
    started = time.time()
    for i, t in enumerate(real):
        fig.canvas.restore_region(background)
        clock.set_text(f"{datetime.fromtimestamp(t, timezone.utc):%H:%M:%S} UTC")
        speed.set_text(f"▶▶ fast-forward {args.ff:g}x" if fast[i] else "▶ real time")
        talking = [x for x in txs if x["start"] <= t <= x["start"] + x["seconds"]]
        if talking:
            now_box.set_text("\n".join(f"{x['mhz']} MHz\n{x['name']} · {KINDS.get(x['kind'], x['kind'])}"
                                       for x in talking[:2]))
            now_box.set_visible(True)
        else:
            now_box.set_visible(False)
        done = [x for x in txs if x["start"] <= t][-8:]
        recent.set_text("\n".join(f"{datetime.fromtimestamp(x['start'], timezone.utc):%H:%M:%S}  "
                                  f"{x['name']:<11} {x['seconds']:4.1f} s" for x in reversed(done)))
        rings = [merc(*AIRPORTS[x["airport"]]) for x in talking if x["airport"]]
        if rings:                                    # (an empty collection still draws at the origin)
            phase = 0.5 + 0.5 * math.sin(2 * math.pi * 1.5 * (i / args.fps))
            pulse.set_offsets(np.array(rings, float))
            pulse.set_sizes([400 + 900 * phase] * len(rings))
            pulse.set_alpha(0.9 - 0.5 * phase)
        pulse.set_visible(bool(rings))
        rows = []
        for icao, pl in planes.items():
            a = art[icao]
            st = AT.state_at(pl, t)
            sel = (pl["time"] <= t) & (pl["time"] >= t - args.trail)
            lat_l, lon_l = list(pl["lat"][sel]), list(pl["lon"][sel])
            if st is not None:
                lat_l.append(st[0])
                lon_l.append(st[1])
            if len(lat_l) > 1:
                xs, ys = merc(lat_l, lon_l)
                pts = np.column_stack([xs, ys])
                seg_t = np.append(pl["time"][sel], t)[1: len(pts)]
                alt = np.append(pl["altitude_ft"][sel], st[2] if st else math.nan)[1: len(pts)]
                colors = cmap(norm(np.nan_to_num(alt)))
                colors[:, 3] = 0.85 * (1 - np.clip((t - seg_t) / args.trail, 0, 1))
                a["trail"].set_segments(np.stack([pts[:-1], pts[1:]], axis=1))
                a["trail"].set_color(colors)
            else:
                a["trail"].set_segments([])
            if st is None:
                a["marker"].set_data([], [])
                a["label"].set_text("")
            else:
                la, lo, alt, gs, trk, vr, alpha = st
                x, y = merc(la, lo)
                color = cmap(norm(alt if not math.isnan(alt) else 0))
                a["marker"].set_data([x], [y])
                a["marker"].set_marker(MarkerStyle(AT.ARROW, transform=Affine2D().rotate_deg(
                    -(trk if not math.isnan(trk) else 0))))
                a["marker"].set_color(color)
                a["marker"].set_markeredgecolor("white")
                a["marker"].set_alpha(max(alpha, 0.05))
                a["label"].xy = (x, y)
                a["label"].set_text(f"{pl['callsign']}\n{round(alt, -2):,.0f} ft")
                a["label"].set_alpha(max(alpha, 0.05))
                rows.append(f"{pl['callsign']:<9.9}{round(alt, -2):>7,.0f} ft"
                            + (f"{gs:>5.0f} kt" if not math.isnan(gs) else ""))
        inview.set_text("\n".join(rows) or "none")
        for artist in dyn:
            artist.axes.draw_artist(artist)
        enc.stdin.write(np.asarray(fig.canvas.buffer_rgba()).tobytes())
        if i % (10 * args.fps) == 0:
            done_s = time.time() - started
            print(f"\r  frame {i}/{len(real)}  ({done_s:.0f} s, about "
                  f"{done_s / max(i, 1) * (len(real) - i) / 60:.1f} min to go)", end="", flush=True)
    enc.stdin.close()
    enc.wait()
    print(f"\nSaved {out}" if enc.returncode == 0 else f"\nffmpeg failed ({enc.returncode})")


if __name__ == "__main__":
    main()
