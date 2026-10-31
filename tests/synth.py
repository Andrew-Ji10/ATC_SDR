"""Synthetic test signals: AM channels, Mode S IQ, and ADS-B messages for a
simulated flight. No hardware needed."""

import math

import numpy as np
from pyModeS.util import crc

# Real messages from the pyModeS test set: identification, an even/odd airborne
# position pair (decodes to 52.2572, 3.9194), and an airborne velocity.
KNOWN = ["8D406B902015A678D4D220AA4BDA", "8D40621D58C382D690C8AC2863A7",
         "8D40621D58C386435CC412692AD6", "8D485020994409940838175B284F"]


def am_channels(rate, center, channels, seconds=1.0, depth=0.5, neighbour_db=12, seed=0):
    """IQ at `rate` around `center` with an AM tone on each (mhz, tone_hz), plus an
    unmodulated carrier `neighbour_db` stronger 25 kHz above each, and noise."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    x = np.zeros(n, np.complex64)
    for mhz, tone in channels:
        off = mhz * 1e6 - center
        x += (0.05 * (1 + depth * np.cos(2 * np.pi * tone * t))
              * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        x += (0.05 * 10 ** (neighbour_db / 20)
              * np.exp(2j * np.pi * (off + 25e3) * t)).astype(np.complex64)
    rng = np.random.default_rng(seed)
    x += ((rng.normal(size=n) + 1j * rng.normal(size=n)) * 0.002).astype(np.complex64)
    return x


def mode_s_iq(rate, messages, count, seconds, seed=1):
    """IQ at `rate` with `count` Mode S frames (cycling through `messages`) at
    random amplitude, phase and sub-sample timing, plus noise."""
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    sig = np.zeros(n, complex)
    spacing = (n - 4000) // count
    for k in range(count):
        bits = np.unpackbits(np.frombuffer(bytes.fromhex(messages[k % len(messages)]), np.uint8))
        t0 = (2000 + k * spacing) / rate + rng.uniform(0, 1 / rate)
        pulses = [0, 1, 3.5, 4.5] + [8 + i + (0 if b else 0.5) for i, b in enumerate(bits)]
        amp, ph = rng.uniform(0.05, 0.5), rng.uniform(0, 2 * np.pi)
        for p in pulses:
            a = t0 + p * 1e-6
            idx = np.arange(int(a * rate), int(a * rate) + int(rate * 1e-6) + 2)
            on = (idx / rate >= a) & (idx / rate < a + 0.5e-6)
            sig[idx[on]] += amp * np.exp(1j * ph)
    sig += (rng.normal(size=n) + 1j * rng.normal(size=n)) * 0.02
    return sig.astype(np.complex64)


# --- ADS-B message encoding, for simulated flights

_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def _nl(lat):
    if abs(lat) >= 87:
        return 1
    return math.floor(2 * math.pi / math.acos(
        1 - (1 - math.cos(math.pi / 30)) / math.cos(math.radians(lat)) ** 2))


def _frame(icao, me_bits):
    bits = f"{17:05b}101{icao:024b}" + me_bits
    h = f"{int(bits, 2):022X}" + "000000"
    return h[:22] + f"{crc(h):06X}"


def position(icao, lat, lon, alt, odd):
    i = 1 if odd else 0
    dlat = 360 / (60 - i)
    yz = math.floor(2 ** 17 * ((lat % dlat) / dlat) + 0.5)
    rlat = dlat * (yz / 2 ** 17 + math.floor(lat / dlat))
    dlon = 360 / max(_nl(rlat) - i, 1)
    xz = math.floor(2 ** 17 * ((lon % dlon) / dlon) + 0.5)
    n = round((alt + 1000) / 25)
    ac = ((n >> 4) << 5) | (1 << 4) | (n & 0xF)
    return _frame(icao, f"{11:05b}000{ac:012b}0{i}{yz % 2 ** 17:017b}{xz % 2 ** 17:017b}")


def identification(icao, callsign):
    cs = (callsign + "        ")[:8]
    return _frame(icao, f"{4:05b}000" + "".join(f"{_CHARS.index(c):06b}" for c in cs))


def velocity(icao, gs, trk, vr):
    vx, vy = gs * math.sin(math.radians(trk)), gs * math.cos(math.radians(trk))
    return _frame(icao, f"{19:05b}00100000"
                  + f"{int(vx < 0)}{round(abs(vx)) + 1:010b}{int(vy < 0)}{round(abs(vy)) + 1:010b}"
                  + f"1{int(vr < 0)}{round(abs(vr) / 64) + 1:09b}00" + "0" + "0000000")


def flight(icao, callsign, lat, lon, alt, trk, gs, vr, t0=1.7e9, seconds=120):
    """(time, hex) messages for an aircraft flying straight: two positions a
    second (alternating even / odd), plus identification and velocity."""
    out = []
    for k in range(int(seconds * 2)):
        t = t0 + k * 0.5
        d = gs * 0.5 / 3600 / 60
        lat += d * math.cos(math.radians(trk))
        lon += d * math.sin(math.radians(trk)) / math.cos(math.radians(lat))
        alt += vr * 0.5 / 60
        out.append((t, position(icao, lat, lon, alt, k % 2)))
        if k % 10 == 0:
            out.append((t + 0.05, identification(icao, callsign)))
            out.append((t + 0.07, velocity(icao, gs, trk, vr)))
    return out
