#!/usr/bin/env python3
"""
iqstream check: does the 12-bit streamer work, and does it get the whole band
(19.2 / 21.6 MSPS) through? Three stages:

  1. loopback on this computer (build/iqstream-native, test pattern, flat out):
     every sample checked exactly, plus how fast this computer unpacks
  2. the radio sending its test pattern flat out (no capture): how much the
     radio can pack and send, with 1 and 2 connections
  3. the radio's real samples at 19.2 and 21.6 MSPS, 12-bit with 1 and 2
     connections, and 16-bit for comparison

    python3 radio/iqstream_check.py --device ip:192.168.3.2
    python3 radio/iqstream_check.py --loopback-only            # no radio needed

Build first: bash pi/build_iqstream.sh. Stages 2-3 log into the radio as root
(default password analog). Writes report.txt to recordings/iqstream_<time>/.
"""

import argparse
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "radio"))

from atcsdr import iqstream as IQ
from atcsdr import pluto

FULL_BAND = (19_200_000, 21_600_000)


class Report:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def section(self, title):
        self("")
        self(f"== {title}")


def measure(radio, seconds, verify=False, snap=None):
    """Read flat out for `seconds` (after 1.5 s to settle). Returns samples/s,
    chunks checked / wrong, lost chunks, radio CPU load and error, if any."""
    count, checked, wrong, err, stop = [0], [0], [0], [], threading.Event()

    def reader():
        try:
            while not stop.is_set():
                x = radio.read()
                count[0] += len(x) // 2
                if verify and radio.stats["chunks"] % 25 == 0:
                    k0 = (radio.last_seq + radio.stats["lost"]) * (len(x) // 2)
                    checked[0] += 1
                    wrong[0] += not np.array_equal(x, IQ.test_pattern(k0, len(x) // 2))
        except OSError as e:
            if not stop.is_set():                    # not just the close() at the end
                err.append(str(e))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(1.5)
    s0 = snap() if snap else None
    n0, lost0, t0 = count[0], radio.stats["lost"], time.time()
    time.sleep(seconds)
    s1 = snap() if snap else None
    n1, lost1, t1 = count[0], radio.stats["lost"], time.time()
    stop.set()
    radio.close()
    th.join(timeout=10)
    load = None
    if s0 and s1:
        from link_diag import Radio
        load = Radio.load(s0, s1)[0]
    return dict(sps=(n1 - n0) / (t1 - t0), checked=checked[0], wrong=wrong[0],
                lost=lost1 - lost0, load=load, error=err[0] if err else None)


def fmt_load(load):
    return ", ".join(f"{k} {v:.0f}%" for k, v in sorted(load.items())) if load else ""


def loopback(rep, seconds):
    rep.section("1. Loopback on this computer (test pattern, flat out, 2 connections)")
    if not IQ.NATIVE.exists():
        rep(f"  {IQ.NATIVE} isn't built: run bash pi/build_iqstream.sh")
        return None
    n = 1 << 17
    x = IQ.test_pattern(0, n)
    packed = IQ.pack12(x)
    exact = np.array_equal(IQ.unpack12(packed), x)          # (also compiles numba, if used)
    t = time.perf_counter()
    for _ in range(20):
        IQ.unpack12(packed)
    rep(f"  pack + unpack round trip exact: {exact}; this computer unpacks "
        f"{20 * n / (time.perf_counter() - t) / 1e6:.0f} MSPS on one core "
        f"({'numba' if IQ.numba else 'numpy'}; 21.6 needed)")
    r = IQ.PackedPluto("packed:127.0.0.1", 0, 21.6e6, bits=12, conns=2, test_rate=0, local=True, port=5310)
    res = measure(r, seconds, verify=True)
    ok = res["error"] is None and res["checked"] > 0 and res["wrong"] == 0
    rep(f"  {res['sps'] / 1e6:.0f} MSPS ({res['sps'] * 3 / 1e6:.0f} MB/s packed); {res['checked']} chunks "
        f"checked, {res['wrong']} wrong; {res['lost']} lost"
        + (f"; error: {res['error']}" if res["error"] else "") + ("  ok" if ok else "  FAIL"))
    return ok


def radio_log(r, *keys):
    """iqstream's log lines containing any of `keys`, once it has stopped (it
    logs its totals and each thread's CPU last: wait up to 3 s for that)."""
    lines = []
    for _ in range(6):
        time.sleep(0.5)
        try:
            lines = r.shell.text("cat /tmp/iqstream.log").splitlines()
        except Exception:
            return []
        if any("done" in l for l in lines):
            break
    return [l.replace("iqstream: ", "") for l in lines if any(k in l for k in keys)]


def radio_test(rep, host, password, seconds, snap):
    rep.section("2. The radio sending a test pattern at 21.6 MSPS, 16-bit (network only, no capture)")
    out = []
    for conns in (1, 2):
        try:
            r = IQ.PackedPluto(f"packed:{host}", 0, 21.6e6, conns=conns, test_rate=21.6, password=password)
        except OSError as e:
            rep(f"  {conns} connection(s): couldn't start: {e}")
            continue
        res = measure(r, seconds, verify=True, snap=snap)
        res.update(conns=conns)
        out.append(res)
        rep(f"  {conns} connection(s): {100 * res['sps'] / 21.6e6:4.0f}% of 21.6 MSPS = "
            f"{res['sps'] * 4 / 1e6:3.0f} MB/s; {res['checked']} chunks checked, {res['wrong']} wrong; "
            f"radio {fmt_load(res['load'])}" + (f"; error: {res['error']}" if res["error"] else ""))
        for line in radio_log(r, "done")[-1:]:
            rep(f"         radio: {line}")
    return out


# (MSPS, bits, connections, capture, copy first): what each run isolates. On the
# first run, 16-bit straight from the DMA block (1 connection) did 81 MB/s at
# 21.6 MSPS, and packing to 12 bits cost the radio more than it saved.
PLAN = [
    (19_200_000, 16, 1, "mmap", False),   # the whole band, outer channels nearer the band edges
    (19_200_000, 16, 2, "mmap", False),
    (21_600_000, 16, 1, "mmap", False),   # the whole band, with the usual margins
    (21_600_000, 16, 2, "mmap", False),   # a second sender on the other core
    (21_600_000, 12, 2, "mmap", True),    # 12-bit for comparison: copy first, two packers
]


def real(rep, host, password, seconds, snap):
    rep.section("3. The radio's real samples")
    rep(f"  {'MSPS':>5} {'bits':>4} {'conns':>5} {'capture':>12} {'delivered':>9} {'arrived':>7} "
        f"{'MB/s':>5}  radio cores")
    out = []
    for rate, bits, conns, capture, copy in PLAN:
        name = capture + (", copy" if copy else "")
        try:
            r = IQ.PackedPluto(f"packed:{host}", 127e6, rate, conns=conns, bits=bits, password=password,
                               capture=capture, copy_first=copy)
        except OSError as e:
            rep(f"  {rate / 1e6:5.1f} {bits:4d} {conns:5d} {name:>12}  couldn't start: {e}")
            continue
        res = measure(r, seconds, snap=snap)
        log = radio_log(r, "capturing with", "done", "no DMA block", "failed")
        res.update(rate=rate, bits=bits, conns=conns, capture=name)
        out.append(res)
        rep(f"  {rate / 1e6:5.1f} {bits:4d} {conns:5d} {name:>12} {100 * res['sps'] / rate:8.0f}% "
            f"{res['sps'] / 1e6:7.1f} {res['sps'] * (3 if bits == 12 else 4) / 1e6:5.0f}  "
            f"{fmt_load(res['load'])}" + (f"  error: {res['error']}" if res["error"] else ""))
        for line in log:
            rep(f"         radio: {line}")
    return out


def verdict(rep, ok_loop, test_runs, real_runs):
    rep.section("Verdict")
    if ok_loop is False:
        rep("-> The loopback check failed: fix that before trusting anything else.")
    best = {}
    for r in real_runs:
        if not r["error"]:
            key = r["rate"]
            if key not in best or r["sps"] > best[key]["sps"]:
                best[key] = r
    for rate in FULL_BAND:
        r = best.get(rate)
        if r:
            full = r["sps"] >= 0.99 * rate and r["lost"] == 0
            rep(f"{rate / 1e6:g} MSPS: best {100 * r['sps'] / rate:.0f}% ({r['bits']}-bit, {r['conns']} "
                f"connection(s), {r['sps'] * (3 if r['bits'] == 12 else 4) / 1e6:.0f} MB/s)"
                + ("  -> the whole band gets through" if full else ""))
    if test_runs:
        top = max(test_runs, key=lambda r: r["sps"])
        rep(f"test pattern (network only, no capture): {100 * top['sps'] / 21.6e6:.0f}% of 21.6 MSPS "
            f"with {top['conns']} connection(s)")
    win = lambda rate: (r := best.get(rate)) and r["sps"] >= 0.99 * rate and r["lost"] == 0
    if win(FULL_BAND[1]):
        r = best[FULL_BAND[1]]
        rep(f"-> Use --device packed:<address> at 21.6 MSPS ({r['bits']}-bit, {r['conns']} connection(s)): "
            "one window, all 43 channels.")
    elif win(FULL_BAND[0]):
        r = best[FULL_BAND[0]]
        rep(f"-> 19.2 MSPS works ({r['bits']}-bit, {r['conns']} connection(s)): the whole band, if the outer "
            "channels may sit nearer the band edges (characterize.sh shows their noise floor).")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="", help="the radio, e.g. ip:192.168.3.2")
    p.add_argument("--password", default="analog", help="the radio's root password (default analog)")
    p.add_argument("--seconds", type=float, default=6.0, help="seconds per measurement (default 6)")
    p.add_argument("--loopback-only", action="store_true", help="only stage 1 (no radio)")
    p.add_argument("--out", help="output folder (default recordings/iqstream_<time>)")
    args = p.parse_args()

    rep = Report()
    rep(f"iqstream check, {datetime.now():%Y-%m-%d %H:%M}")
    ok_loop = loopback(rep, args.seconds)
    test_runs, real_runs = [], []
    if not args.loopback_only:
        uri = pluto.find_uri(args.device)
        host = uri.split(":", 1)[1] if uri.startswith(("ip:", "packed:")) else None
        if not host:
            sys.exit("Pass the radio's address: --device ip:<address>.")
        from link_diag import Radio
        shell = Radio(host, args.password)
        if not IQ.BINARY.exists():
            sys.exit(f"{IQ.BINARY} isn't built: run bash pi/build_iqstream.sh")
        test_runs = radio_test(rep, host, args.password, args.seconds, shell.snapshot)
        real_runs = real(rep, host, args.password, args.seconds, shell.snapshot)
        shell.run("killall iqstream 2>/dev/null")
    verdict(rep, ok_loop, test_runs, real_runs)

    out = Path(args.out or ROOT / f"recordings/iqstream_{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text("\n".join(rep.lines) + "\n")
    print(f"\nSaved {out}/report.txt")


if __name__ == "__main__":
    main()
