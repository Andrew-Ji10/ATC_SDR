#!/usr/bin/env python3
"""
Animate a recorded ADS-B session: aircraft move across the map in time.

    python3 adsb/animate_tracks.py samples/adsb/tracks.csv --ref 37.43 -122.17   # -> .mp4
    python3 adsb/animate_tracks.py tracks.csv --speed 20 --out fast.mp4
    python3 adsb/animate_tracks.py tracks.csv --out tracks.gif         # GIF instead

Reads the position CSV that adsb_plot.py --csv and adsb_decode.py --out write. Each aircraft is drawn as an
arrow pointing along its track, colored by altitude, with:
  - a trail of where it has been (the last --trail seconds, fading out),
  - a dashed velocity vector: where it will be in --lead seconds at its current
    ground speed and track,
  - a label: callsign, altitude, ground speed and climb / descent rate.
Positions between fixes are interpolated. Where no fixes were received for more
than 30 s the aircraft is hidden until it is heard again, and it fades out 10 s
after its last fix. Playback runs --speed times faster than real time, and
stretches with no aircraft in view are skipped (the clock shows the real time).

MP4 output uses the ffmpeg bundled with imageio-ffmpeg (pip install imageio-ffmpeg);
GIF output needs only Pillow.
"""

import argparse
import csv
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FFMpegWriter, FuncAnimation, PillowWriter
from matplotlib.collections import LineCollection
from matplotlib.markers import MarkerStyle
from matplotlib.path import Path as MPath
from matplotlib.transforms import Affine2D

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr.adsb_map import GAP_SECONDS, GRID, INK, MUTED, SURFACE, altitude_colors  # noqa: E402

HOLD_S = 10.0                # keep an aircraft on screen this long after its last fix
NOTE_S = 1.5                 # how long (video seconds) the "skipped" note stays up

# An arrowhead pointing north (+y), rotated to each aircraft's track.
ARROW = MPath([(0, 1), (0.62, -0.8), (0, -0.35), (-0.62, -0.8), (0, 1)],
              [MPath.MOVETO, MPath.LINETO, MPath.LINETO, MPath.LINETO, MPath.CLOSEPOLY])


def num(v):
    return float(v) if v not in ("", "None", None) else math.nan


def load(path):
    """{icao: dict of numpy arrays} sorted by time, with speed / track / climb
    rate carried forward over fixes that didn't have them."""
    rows = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows[r["icao"]].append(r)
    planes = {}
    for icao, rs in rows.items():
        rs.sort(key=lambda r: float(r["time"]))
        cols = {k: np.array([num(r[k]) for r in rs]) for k in
                ("time", "lat", "lon", "altitude_ft", "groundspeed_kt", "track_deg",
                 "vertical_rate_fpm")}
        for k in ("altitude_ft", "groundspeed_kt", "track_deg", "vertical_rate_fpm"):
            col = cols[k]
            for i in range(1, len(col)):          # carry forward, then back-fill the start
                if math.isnan(col[i]):
                    col[i] = col[i - 1]
            for i in range(len(col) - 2, -1, -1):
                if math.isnan(col[i]):
                    col[i] = col[i + 1]
        cols["callsign"] = next((r["callsign"] for r in reversed(rs) if r["callsign"]), icao)
        planes[icao] = cols
    return planes


def state_at(p, t):
    """Interpolated (lat, lon, alt, gs, track, vrate, alpha) at time t, or None
    when the aircraft isn't being heard."""
    ts = p["time"]
    if t < ts[0] or t > ts[-1] + HOLD_S:
        return None
    i = np.searchsorted(ts, t)
    if i == 0:
        i = 1
    if i >= len(ts):                              # after the last fix: hold, fading
        k = len(ts) - 1
        alpha = 1 - (t - ts[-1]) / HOLD_S
        return (p["lat"][k], p["lon"][k], p["altitude_ft"][k], p["groundspeed_kt"][k],
                p["track_deg"][k], p["vertical_rate_fpm"][k], alpha)
    t0, t1 = ts[i - 1], ts[i]
    if t1 - t0 > GAP_SECONDS:                     # not heard for a while: hide
        return None if t - t0 > HOLD_S else (
            p["lat"][i - 1], p["lon"][i - 1], p["altitude_ft"][i - 1],
            p["groundspeed_kt"][i - 1], p["track_deg"][i - 1],
            p["vertical_rate_fpm"][i - 1], 1 - (t - t0) / HOLD_S)
    w = (t - t0) / (t1 - t0) if t1 > t0 else 0
    lerp = lambda k: p[k][i - 1] + w * (p[k][i] - p[k][i - 1])
    # Track is an angle: interpolate the short way round.
    a0, a1 = p["track_deg"][i - 1], p["track_deg"][i]
    trk = (a0 + w * (((a1 - a0 + 180) % 360) - 180)) % 360
    return (lerp("lat"), lerp("lon"), lerp("altitude_ft"), lerp("groundspeed_kt"), trk,
            p["vertical_rate_fpm"][i - 1], 1.0)


def project(lat, lon, gs_kt, trk_deg, seconds):
    """Position after flying `seconds` at gs_kt on trk_deg (flat-earth, fine for minutes)."""
    d_deg = gs_kt * seconds / 3600 / 60          # nautical miles -> degrees of latitude
    return (lat + d_deg * math.cos(math.radians(trk_deg)),
            lon + d_deg * math.sin(math.radians(trk_deg)) / math.cos(math.radians(lat)))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="positions logged by adsb_plot.py --csv")
    p.add_argument("--out", help="output .mp4 or .gif (default: <csv name>.mp4)")
    p.add_argument("--ref", nargs=2, type=float, metavar=("LAT", "LON"), help="receiver position")
    p.add_argument("--speed", type=float, default=10, help="playback speed-up (default 10x)")
    p.add_argument("--fps", type=int, default=20, help="frames per second (default 20)")
    p.add_argument("--trail", type=float, default=120, help="trail length in seconds (default 120)")
    p.add_argument("--lead", type=float, default=60,
                   help="velocity vector: seconds of flight ahead (default 60)")
    args = p.parse_args()
    out = args.out or args.csv.rsplit(".", 1)[0] + ".mp4"

    planes = load(args.csv)
    t_start = min(pl["time"][0] for pl in planes.values())
    t_end = max(pl["time"][-1] for pl in planes.values()) + HOLD_S
    # Skip stretches with nobody in view, keeping a moment either side of each.
    grid = np.arange(t_start, t_end, args.speed / args.fps)
    seen = np.array([any(state_at(pl, t) is not None for pl in planes.values()) for t in grid])
    pad = args.fps              # frames
    near = np.convolve(seen, np.ones(2 * pad + 1), "full")[pad: pad + len(seen)] > 0
    keep = seen | near
    frame_times = grid[keep]
    jumps = np.diff(frame_times) > 1.5 * args.speed / args.fps
    skipped = {}                      # frame index -> seconds skipped just before it
    for k in np.flatnonzero(jumps) + 1:
        for j in range(k, min(k + int(NOTE_S * args.fps), len(frame_times))):
            skipped[j] = frame_times[k] - frame_times[k - 1]

    cmap, norm = altitude_colors()

    fig = plt.figure(figsize=(12, 7.5), facecolor=SURFACE)
    ax = fig.add_axes([0.06, 0.08, 0.62, 0.84], facecolor=SURFACE)
    side = fig.add_axes([0.71, 0.08, 0.27, 0.84], facecolor=SURFACE)
    side.axis("off")
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), ax=ax,
                      orientation="horizontal", fraction=0.035, pad=0.08)
    cb.set_label("Altitude (ft)", color=MUTED)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, labelsize=8)

    # Fixed map extent: every fix (and the receiver), plus a margin.
    lats = np.concatenate([pl["lat"] for pl in planes.values()])
    lons = np.concatenate([pl["lon"] for pl in planes.values()])
    if args.ref:
        lats, lons = np.append(lats, args.ref[0]), np.append(lons, args.ref[1])
    pad = max(0.15 * max(np.ptp(lats), np.ptp(lons)), 0.01)
    ax.set_xlim(lons.min() - pad, lons.max() + pad)
    ax.set_ylim(lats.min() - pad, lats.max() + pad)
    ax.set_aspect(1 / math.cos(math.radians(lats.mean())), adjustable="box")
    ax.grid(True, color=GRID, linewidth=0.8)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.ticklabel_format(useOffset=False)
    ax.set_xlabel("Longitude", color=MUTED)
    ax.set_ylabel("Latitude", color=MUTED)
    if args.ref:
        ax.plot(args.ref[1], args.ref[0], marker="^", color=INK, markersize=9, zorder=5)
        ax.annotate("Receiver", (args.ref[1], args.ref[0]), xytext=(6, -12),
                    textcoords="offset points", color=INK, fontsize=8)

    # Per-aircraft artists, updated every frame.
    art = {}
    for icao in planes:
        trail = LineCollection([], linewidths=2, capstyle="round", zorder=2)
        ax.add_collection(trail)
        art[icao] = dict(
            trail=trail,
            vector=ax.plot([], [], linestyle=(0, (3, 2)), linewidth=1.2, zorder=3)[0],
            marker=ax.plot([], [], linestyle="none", markersize=13, markeredgecolor=SURFACE,
                           markeredgewidth=1, zorder=4)[0],
            label=ax.annotate("", (0, 0), xytext=(9, 6), textcoords="offset points",
                              fontsize=8, color=INK, zorder=6, annotation_clip=True),
        )
    clock = side.text(0, 1, "", fontsize=16, color=INK, va="top", transform=side.transAxes)
    status = side.text(0, 0.94, "", fontsize=9, color=MUTED, va="top", transform=side.transAxes)
    rows = side.text(0, 0.86, "", family="monospace", fontsize=9, color=INK, va="top",
                     transform=side.transAxes)
    side.text(0, 0.0, f"Dashed line: position {args.lead:g} s ahead at the current\n"
                      "ground speed and track. Trails fade over "
                      f"{args.trail:g} s.", fontsize=8, color=MUTED, va="bottom",
              transform=side.transAxes)

    def draw(frame):
        t = frame_times[frame]
        clock.set_text(time.strftime("%H:%M:%S", time.localtime(t)))
        table = [f"{'Flight':<9}{'Alt ft':>7}{'Kt':>5}{'fpm':>7}"]
        for icao, pl in planes.items():
            a = art[icao]
            st = state_at(pl, t)
            # Trail: fixes in the last `trail` seconds, fading towards the oldest.
            sel = (pl["time"] <= t) & (pl["time"] >= t - args.trail)
            pts = np.column_stack([pl["lon"][sel], pl["lat"][sel]])
            if st is not None:
                pts = np.vstack([pts, [st[1], st[0]]])
            if len(pts) > 1:
                seg_t = np.append(pl["time"][sel], t)[1:]
                age = np.clip((t - seg_t) / args.trail, 0, 1)
                alt = np.append(pl["altitude_ft"][sel], st[2] if st else math.nan)[1:]
                colors = cmap(norm(np.nan_to_num(alt)))
                colors[:, 3] = 0.85 * (1 - age)
                gaps = np.diff(np.append(pl["time"][sel], t)) > GAP_SECONDS
                colors[gaps, 3] *= 0.3                # thin out joins across gaps
                a["trail"].set_segments(np.stack([pts[:-1], pts[1:]], axis=1))
                a["trail"].set_color(colors)
            else:
                a["trail"].set_segments([])
            if st is None:
                a["marker"].set_data([], [])
                a["vector"].set_data([], [])
                a["label"].set_text("")
                continue
            lat, lon, alt, gs, trk, vr, alpha = st
            color = cmap(norm(alt if not math.isnan(alt) else 0))
            a["marker"].set_data([lon], [lat])
            a["marker"].set_marker(MarkerStyle(ARROW, transform=Affine2D().rotate_deg(-trk)))
            a["marker"].set_color(color)
            a["marker"].set_markeredgecolor(SURFACE)
            a["marker"].set_alpha(alpha)
            if not math.isnan(gs) and not math.isnan(trk):
                lat2, lon2 = project(lat, lon, gs, trk, args.lead)
                a["vector"].set_data([lon, lon2], [lat, lat2])
                a["vector"].set_color(color)
                a["vector"].set_alpha(0.9 * alpha)
            climb = "" if math.isnan(vr) or abs(vr) < 200 else ("↑" if vr > 0 else "↓")
            a["label"].xy = (lon, lat)
            alt = round(alt, -2)          # interpolated: don't show false precision
            a["label"].set_text(f"{pl['callsign']}\n{alt:,.0f} ft  {gs:.0f} kt {climb}")
            a["label"].set_alpha(alpha)
            table.append(f"{pl['callsign']:<9.9}{alt:>7,.0f}{gs:>5.0f}{vr:>+7.0f}"
                         if not math.isnan(vr) else f"{pl['callsign']:<9.9}{alt:>7,.0f}{gs:>5.0f}")
        rows.set_text("\n".join(table))
        note = f"{len(table) - 1} aircraft in view  ·  playback {args.speed:g}x"
        if frame in skipped:
            gap = skipped[frame]
            note += f"\nskipped {int(gap // 60)}:{int(gap % 60):02d} with no aircraft"
        status.set_text(note)
        return []

    anim = FuncAnimation(fig, draw, frames=len(frame_times), interval=1000 / args.fps)
    if out.lower().endswith(".gif"):
        writer = PillowWriter(fps=args.fps)
    else:
        import imageio_ffmpeg
        matplotlib.rcParams["animation.ffmpeg_path"] = imageio_ffmpeg.get_ffmpeg_exe()
        writer = FFMpegWriter(fps=args.fps, bitrate=4000,
                              extra_args=["-pix_fmt", "yuv420p"])   # plays everywhere
    span = t_end - t_start
    print(f"{len(planes)} aircraft, {span / 60:.1f} min of data, "
          f"{max(0, span - len(frame_times) * args.speed / args.fps) / 60:.1f} min of it empty -> "
          f"{len(frame_times) / args.fps:.0f} s of video ({len(frame_times)} frames) ...")
    anim.save(out, writer=writer, dpi=120, savefig_kwargs={"facecolor": SURFACE})
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
