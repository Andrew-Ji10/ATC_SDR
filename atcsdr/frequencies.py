"""Bay Area airband frequencies (VHF AM), from AirNav, October 2026.

Check before relying on them: frequencies, hours and sectors change. Towers at
the smaller fields close at night (PAO and SQL 0700-2100, RHV 0700-2200, HWD
0700-2100, SJC 0600-0000 local); NorCal Approach runs around the clock.

Names are short because they appear in the status line and in file names.
"""

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Freq:
    mhz: float
    name: str
    kind: str = "other"     # clearance, ground, tower, approach, departure, atis, guard


def _airport(code, *entries):
    return [Freq(mhz, f"{code} {label}", kind) for mhz, label, kind in entries]


AIRPORTS = {
    "SJC": _airport("SJC", (118.000, "Clr", "clearance"), (121.700, "Gnd", "ground"),
                    (124.000, "Twr", "tower"), (126.950, "ATIS", "atis")),
    "PAO": _airport("PAO", (118.600, "Twr", "tower"), (125.000, "Gnd", "ground"),
                    (135.275, "ATIS", "atis")),
    "RHV": _airport("RHV", (119.800, "Twr", "tower"), (126.100, "Twr 2", "tower"),
                    (121.650, "Gnd", "ground"), (125.200, "ATIS", "atis")),
    "NUQ": _airport("NUQ", (119.550, "Twr", "tower"), (121.850, "Gnd", "ground"),
                    (124.175, "ATIS", "atis")),
    "SFO": _airport("SFO", (118.200, "Clr", "clearance"), (120.500, "Twr", "tower"),
                    (121.800, "Gnd", "ground"), (124.250, "Gnd 2", "ground"),
                    (118.850, "ATIS", "atis")),
    "SQL": _airport("SQL", (119.000, "Twr", "tower"), (118.025, "Gnd", "ground"),
                    (125.900, "ATIS", "atis")),
    "OAK": _airport("OAK", (121.100, "Clr", "clearance"), (118.300, "Twr", "tower"),
                    (127.200, "Twr 2", "tower"), (124.900, "Twr 3", "tower"),
                    (121.900, "Gnd", "ground"), (121.750, "Gnd 2", "ground"),
                    (133.775, "ATIS", "atis")),
    "HWD": _airport("HWD", (128.050, "Clr", "clearance"), (120.200, "Twr", "tower"),
                    (118.900, "Twr 2", "tower"), (121.400, "Gnd", "ground"),
                    (126.700, "ATIS", "atis")),
}

# NorCal TRACON (approach / departure) sectors that serve the airports above.
NORCAL = {
    120.100: Freq(120.100, "SJC App", "approach"),
    120.900: Freq(120.900, "Dep NW", "departure"),
    121.300: Freq(121.300, "SJC Dep", "departure"),
    124.400: Freq(124.400, "HWD App", "approach"),
    124.525: Freq(124.525, "OAK Arr S", "approach"),
    124.800: Freq(124.800, "OAK Arr", "approach"),
    125.350: Freq(125.350, "App East", "approach"),
    126.475: Freq(126.475, "SJC App 2", "approach"),
    127.000: Freq(127.000, "App North", "approach"),
    128.325: Freq(128.325, "SFO Arr E", "approach"),
    128.575: Freq(128.575, "SFO Arr S", "approach"),
    133.950: Freq(133.950, "App South", "approach"),
    134.500: Freq(134.500, "App East 2", "approach"),
    135.100: Freq(135.100, "Dep West", "departure"),
    135.200: Freq(135.200, "SJC App W", "approach"),
    135.650: Freq(135.650, "SQL App", "approach"),
}

GUARD = Freq(121.500, "Guard", "guard")

AREAS = {
    "south_bay": dict(
        title="South Bay: SJC, PAO, RHV, NUQ and NorCal South",
        airports=["SJC", "PAO", "RHV", "NUQ"],
        norcal=[120.100, 121.300, 125.350, 126.475, 133.950, 134.500, 135.200]),
    "sfo": dict(
        title="SFO area: SFO, SQL and NorCal West",
        airports=["SFO", "SQL"],
        norcal=[120.900, 125.350, 127.000, 128.325, 128.575, 133.950, 134.500, 135.100,
                135.650]),
    "oak": dict(
        title="OAK area: OAK, HWD and NorCal East",
        airports=["OAK", "HWD"],
        norcal=[120.900, 124.400, 124.525, 124.800, 125.350, 127.000, 128.325, 133.950,
                134.500, 135.100]),
}
AREAS["bay"] = dict(
    title="All of the Bay: every airport and NorCal sector above, plus guard",
    airports=list(AIRPORTS), norcal=sorted(NORCAL), extra=[GUARD])


def dedupe(freqs):
    """Sorted by frequency, one entry per frequency (the first name wins)."""
    seen = {}
    for f in freqs:
        seen.setdefault(round(f.mhz, 4), f)
    return sorted(seen.values())


def area_channels(area, atis=False):
    a = AREAS[area]
    freqs = [f for code in a["airports"] for f in AIRPORTS[code]]
    freqs += [NORCAL[m] for m in a["norcal"]] + a.get("extra", [])
    return dedupe(f for f in freqs if atis or f.kind != "atis")


# Fixed channel sets for multi_atc.py, besides the areas above.
PRESETS = {
    # SFO's own channels plus departure and guard: fits one USB capture.
    "sfo-airport": [f for f in AIRPORTS["SFO"] if f.kind != "atis"]
                   + [NORCAL[120.900], GUARD],
    # Every NorCal sector above. Needs ~21 MSPS: Ethernet only.
    "norcal": sorted(NORCAL.values()),
    # The NorCal sectors that fit one USB capture (120.1-126.475 MHz).
    "norcal-usb": [f for f in sorted(NORCAL.values()) if f.mhz < 127],
}


def preset_channels(name, atis=False):
    if name in AREAS:
        return area_channels(name, atis)
    freqs = list(PRESETS[name])
    if atis and name == "sfo-airport":
        freqs += [f for f in AIRPORTS["SFO"] if f.kind == "atis"]
    return dedupe(freqs)


def parse_channels(text):
    """'120.5 Tower, 121.8 Ground' -> [Freq(120.5, 'Tower'), Freq(121.8, 'Ground')]"""
    out = []
    for item in text.split(","):
        parts = item.split(None, 1)
        if parts:
            mhz = float(parts[0])
            out.append(Freq(mhz, parts[1].strip() if len(parts) > 1 else f"{mhz:g}"))
    return dedupe(out)
