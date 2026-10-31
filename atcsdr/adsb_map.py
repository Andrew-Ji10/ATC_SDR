"""Draw ADS-B tracks on a map with matplotlib (live window or saved PNG).

Each track is drawn as a line colored by altitude, with a dot at every fix and
the fixes' times as labels. By default about ten labels per track are shown and
labels that would overlap are skipped; with label_every=0 every fix is labeled,
stacked in columns beside its track with a leader line, and save_map() enlarges
the image until none overlap.
"""

import math
import time

import numpy as np

STALE_SECONDS = 60           # live: aircraft not heard for this long are greyed out
GAP_SECONDS = 30             # fixes further apart than this are joined by a dotted line
ALT_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]
ALT_MAX_FT = 40000           # sequential blue ramp: light = low, dark = high
INK, MUTED, GRID, SURFACE, STALE = "#1f1f1e", "#6b6a66", "#e6e5e1", "#fcfcfb", "#b4b2ab"
SAVE_DPI = 150
MAX_SAVE_PX = 12000


def altitude_colors():
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    return LinearSegmentedColormap.from_list("alt", ALT_RAMP), Normalize(0, ALT_MAX_FT)


def build_map(tracker, ref=None, replay=False, label_every=None):
    """Create the figure; return (fig, draw) where draw() redraws from `tracker`
    and returns how many labels overlap. replay: a finished session (whole tracks,
    nothing greyed out, clock times instead of ages)."""
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    cmap, norm = altitude_colors()
    label_all = label_every == 0
    fig = plt.figure(figsize=(12, 7.5), facecolor=SURFACE)
    ax = fig.add_axes([0.06, 0.08, 0.62, 0.84], facecolor=SURFACE)
    side = fig.add_axes([0.71, 0.08, 0.27, 0.84], facecolor=SURFACE)
    side.axis("off")
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), ax=ax,
                      orientation="horizontal", fraction=0.035, pad=0.08)
    cb.set_label("Altitude (ft)", color=MUTED)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, labelsize=8)
    fig.canvas.manager.set_window_title("ADS-B tracks")

    def draw(_=None):
        now, total, planes = tracker.snapshot(keep_all=replay)
        ax.clear()
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.8)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.ticklabel_format(useOffset=False)
        ax.set_xlabel("Longitude", color=MUTED)
        ax.set_ylabel("Latitude", color=MUTED)

        lats, lons = [], []
        if ref:
            ax.plot(ref[1], ref[0], marker="^", color=INK, markersize=9, zorder=5)
            lats.append(ref[0]); lons.append(ref[1])
        rows = []
        # Text is placed after the limits are known: (lon, lat, text, style) for
        # callsign tags (always shown) and fix timestamps (shown where they fit).
        tags, stamps = [], []
        for icao, cs, alt, spd, vr, seen, path in sorted(planes, key=lambda p: -p[5]):
            age = now - seen
            if age > 600 and not replay:
                continue
            rows.append((cs or icao, alt, spd, age, seen))
            if not path:
                continue
            t, la, lo, al = zip(*path)
            lats += la; lons += lo
            stale = age > STALE_SECONDS and not replay
            color = STALE if stale else cmap(norm(alt or 0))
            if len(path) > 1:
                # One segment per fix, colored by the altitude at that fix. Where
                # no fixes were heard for a while, the join is thin and dotted.
                pts = np.column_stack([lo, la])
                segs = np.stack([pts[:-1], pts[1:]], axis=1)
                gap = np.diff(t) > GAP_SECONDS
                seg_alt = np.array([a or 0 for a in al[1:]], dtype=float)
                for mask, style in ((~gap, dict(linewidth=2, capstyle="round")),
                                    (gap, dict(linewidth=1, linestyle=(0, (1, 3)), alpha=0.8))):
                    if not mask.any():
                        continue
                    seg = LineCollection(segs[mask], **style)
                    if stale:
                        seg.set_color(color)
                    else:
                        seg.set_array(seg_alt[mask])
                        seg.set_cmap(cmap); seg.set_norm(norm)
                    ax.add_collection(seg)
            # A small dot at every fix, and its time on a thinned subset.
            dots = color if stale else cmap(norm(np.array([a or 0 for a in al], dtype=float)))
            # No outline: on dense tracks each outline would paint over the dot before it.
            ax.scatter(lo, la, s=14, c=dots if stale else list(dots), linewidths=0, zorder=3)
            # Newest first, so when stamps collide the recent ones win. When every
            # fix is labeled, alternate sides so neighbouring labels don't stack.
            for k in reversed(label_fixes(t, label_every)):
                side_style = "stamp_r" if label_all and k % 2 else "stamp"
                stamps.append((lo[k], la[k], clock(t[k], tenths=label_all), side_style, icao))
            ax.plot(lo[-1], la[-1], "o", color=color, markersize=8,
                    markeredgecolor=SURFACE, markeredgewidth=2, zorder=4)
            label = cs or icao
            if alt is not None:
                label += f"\nFL{int(alt) // 100:03d}"
            tags.append((lo[-1], la[-1], label, "stale" if stale else "tag"))

        if lats:
            # Fit the tracks, with a margin of 15% (at least ~1 km).
            pad = max(0.15 * max(max(lats) - min(lats), max(lons) - min(lons)), 0.01)
            lat0 = np.mean([min(lats), max(lats)])
            ax.set_xlim(min(lons) - pad, max(lons) + pad)
            ax.set_ylim(min(lats) - pad, max(lats) + pad)
            ax.set_aspect(1 / max(math.cos(math.radians(lat0)), 0.1), adjustable="box")
        if ref:   # the receiver label yields to aircraft tags, like a timestamp
            stamps.insert(0, (ref[1], ref[0], "Receiver", "receiver", None))
        overlaps = place_labels(ax, tags, stamps, force=label_all)
        n_pos = sum(1 for p in planes if p[6])
        ax.set_title(f"{len(rows)} aircraft heard, {n_pos} with position  ·  "
                     f"{total} messages", color=INK, loc="left", fontsize=11)

        side.clear(); side.axis("off")
        last = "Last seen" if replay else "Age"
        side.annotate(f"{'Flight':<9}{'Alt ft':>7}{'Kt':>5}{last:>10}", (0, 1),
                      xycoords="axes fraction", family="monospace", fontsize=9, color=MUTED,
                      va="top")
        for k, (name, alt, spd, age, seen) in enumerate(rows[:40]):
            when = time.strftime("%H:%M:%S", time.localtime(seen)) if replay else f"{int(age)}s"
            line = (f"{name:<9.9}{'' if alt is None else int(alt):>7}"
                    f"{'' if spd is None else int(spd):>5}{when:>10}")
            side.annotate(line, (0, 1), xycoords="axes fraction", xytext=(0, -14 * (k + 1)),
                          textcoords="offset points", family="monospace", fontsize=9,
                          color=MUTED if age > STALE_SECONDS and not replay else INK, va="top")
        return overlaps

    return fig, draw


# Text style per label kind; boxes for overlap checks are estimated from these.
LABEL_STYLE = {
    "tag":      dict(xytext=(7, 4), fontsize=8, ha="left", va="bottom"),
    "stale":    dict(xytext=(7, 4), fontsize=8, ha="left", va="bottom"),
    "receiver": dict(xytext=(6, -12), fontsize=8, ha="left", va="bottom"),
    "stamp":    dict(xytext=(-6, 0), fontsize=7, ha="right", va="center"),
    "stamp_r":  dict(xytext=(6, 0), fontsize=7, ha="left", va="center"),
}
# Approximate glyph widths in em for the default font.
NARROW_GLYPHS = set(":.,;!|'iIl1 ")


def clock(t, tenths=False):
    """Local time of day; with tenths, since fixes often come twice a second."""
    text = time.strftime("%H:%M:%S", time.localtime(t))
    return f"{text}.{int(t * 10) % 10}" if tenths else text


def text_width_em(line):
    return sum(0.33 if c in NARROW_GLYPHS else 0.64 for c in line)


def dodge(ys, h):
    """Move positions `ys` apart until neighbours are at least `h` apart, each
    run of crowded labels staying centered on where its labels want to be."""
    clusters = []                          # [sum of wanted ys, count, member indices]
    for i in np.argsort(ys):
        clusters.append([ys[i], 1, [i]])
        while len(clusters) > 1:
            (sa, na, _), (sb, nb, _) = clusters[-2], clusters[-1]
            if (sb / nb - (nb - 1) * h / 2) - (sa / na + (na - 1) * h / 2) >= h:
                break
            b = clusters.pop()
            clusters[-1] = [sa + sb, na + nb, clusters[-1][2] + b[2]]
    out = np.empty(len(ys))
    for total, n, members in clusters:
        start = total / n - (n - 1) * h / 2
        for k, i in enumerate(members):
            out[i] = start + k * h
    return out


def place_labels(ax, tags, stamps, force=False):
    """Draw the callsign tags and the timestamps.

    Normally tags go first and a timestamp is drawn only where it doesn't overlap
    text already drawn. With force, every timestamp is drawn: they go first, in
    stacks beside their track with a leader line to their fix, and the tags then
    take a free corner. Returns how many labels overlap or leave the map."""
    ax.apply_aspect()                      # so data -> pixel mapping is final
    fig = ax.figure
    px = fig.dpi / 72
    W = fig.bbox.width
    bottom, top = ax.bbox.y0, ax.bbox.y1    # keep labels off the title and the colorbar
    placed = []

    def box(x, y, text, style, st=None):
        st = st or LABEL_STYLE[style]
        lines = text.split("\n")
        w = max(map(text_width_em, lines)) * st["fontsize"] * px
        h = len(lines) * st["fontsize"] * 1.2 * px
        cx, cy = ax.transData.transform((x, y)) + np.array(st["xytext"]) * px
        x0 = cx - w if st["ha"] == "right" else cx
        y0 = {"center": cy - h / 2, "top": cy - h}.get(st["va"], cy)
        return x0, y0, x0 + w, y0 + h

    def bad(a):
        outside = a[0] < 0 or a[2] > W or a[1] < bottom or a[3] > top
        return outside or any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                              for b in placed)

    def draw(x, y, text, style, st=None, leader=False):
        st = st or LABEL_STYLE[style]
        color = INK if style in ("tag", "receiver") else MUTED
        arrow = dict(arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.4, alpha=0.5,
                                     shrinkA=1, shrinkB=2)) if leader else {}
        ax.annotate(text, (x, y), textcoords="offset points", color=color,
                    annotation_clip=False, **arrow, **st)
        placed.append(box(x, y, text, style, st))

    clashes = 0
    if force:
        clashes += _stack_stamps([s for s in stamps if s[3] != "receiver"], box, bad, draw, px)
    # Tags are always drawn; each takes the first corner around its dot that is free.
    for x, y, text, style in tags:
        base = LABEL_STYLE[style]
        corners = [base, dict(base, xytext=(7, -4), va="top"),
                   dict(base, xytext=(-7, 4), ha="right"),
                   dict(base, xytext=(-7, -4), ha="right", va="top")]
        st = next((c for c in corners if not bad(box(x, y, text, style, c))), None)
        clashes += st is None
        draw(x, y, text, style, st or base)
    for x, y, text, style, _ in stamps:
        if force and style != "receiver":
            continue
        if not bad(box(x, y, text, style)):
            draw(x, y, text, style)
        elif style != "receiver":
            clashes += 1
    return clashes


def _stack_stamps(stamps, box, bad, draw, px):
    """Draw every timestamp in a column beside its track (one column per side),
    with a leader line to its fix. The column sits just outside the track, so
    leader lines never cross a label. Labels slide up or down just enough that
    none overlap; columns whose x ranges overlap are spaced as one stack."""
    reach = 16                             # points from the outermost fix to the column
    columns = {}
    for x, y, text, style, group in stamps:
        columns.setdefault((group, style), []).append((x, y, text, style))
    stacks = []                            # [x0, x1, items]
    for (_, style), fixes in columns.items():
        right = style == "stamp_r"
        boxes = [box(x, y, text, style) for x, y, text, style in fixes]
        dots_x = [b[0] - LABEL_STYLE[style]["xytext"][0] * px if right else
                  b[2] - LABEL_STYLE[style]["xytext"][0] * px for b in boxes]
        col_x = max(dots_x) + reach * px if right else min(dots_x) - reach * px
        width = max(b[2] - b[0] for b in boxes)
        items = []
        for (x, y, text, style), b, dx in zip(fixes, boxes, dots_x):
            st = dict(LABEL_STYLE[style], xytext=((col_x - dx) / px, 0))
            items.append(((b[1] + b[3]) / 2, b[3] - b[1], x, y, text, style, st))
        stacks.append([col_x if right else col_x - width,
                       col_x + width if right else col_x, items])
    # Merge stacks that share horizontal space.
    stacks.sort(key=lambda st: st[0])
    merged = []
    for st in stacks:
        if merged and st[0] < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], st[1])
            merged[-1][2] += st[2]
        else:
            merged.append(st)
    clashes = 0
    for _, _, items in merged:
        h = max(it[1] for it in items) * 1.1    # a little air between rows
        ys = dodge(np.array([it[0] for it in items]), h)
        for (cy, _, x, y, text, style, st), ly in zip(items, ys):
            st = dict(st, xytext=(st["xytext"][0], (ly - cy) / px))
            clashes += bad(box(x, y, text, style, st))
            draw(x, y, text, style, st, leader=True)
    return clashes


def label_fixes(times, every):
    """Indices of fixes to timestamp: one per `every` seconds (0 = all of them),
    or about ten per track when `every` is None. The newest fix is always included."""
    if every is None:
        every = (times[-1] - times[0]) / 9
    picked, last = [], -math.inf
    for k, t in enumerate(times):
        if t - last >= every:
            picked.append(k)
            last = t
    if picked[-1] != len(times) - 1:
        picked.append(len(times) - 1)
    return picked


def save_map(fig, draw, path, fit_labels=False):
    """Save the map. fit_labels: enlarge the image (text keeps its size) until
    no timestamp overlaps another label, up to MAX_SAVE_PX on the long side."""
    clashes = draw()
    if fit_labels:
        w, h = fig.get_size_inches()
        while clashes and max(w, h) * 1.25 * SAVE_DPI <= MAX_SAVE_PX:
            w, h = w * 1.25, h * 1.25
            fig.set_size_inches(w, h)
            clashes = draw()
        if clashes:
            print(f"{clashes} timestamps still overlap at {w * SAVE_DPI:.0f} x "
                  f"{h * SAVE_DPI:.0f} px; zoom in on the image to read them.")
    fig.savefig(path, dpi=SAVE_DPI, facecolor=fig.get_facecolor())
    print(f"Saved {path} ({fig.get_size_inches()[0] * SAVE_DPI:.0f} x "
          f"{fig.get_size_inches()[1] * SAVE_DPI:.0f} px)")


def show_live(tracker, ref=None, replay=False, label_every=None, save=None):
    """Open the map window, redrawn once a second; on close, optionally save it."""
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    fig, draw = build_map(tracker, ref, replay, label_every)
    anim = FuncAnimation(fig, draw, interval=1000, cache_frame_data=False)
    draw()
    plt.show()
    if save:
        save_map(fig, draw, save, label_every == 0)
    del anim
