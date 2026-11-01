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

FULL_BAND = (19_200_000, 20_000_000, 21_600_000)


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


# (MSPS, connections, samples per DMA block, socket send buffer): all 16-bit,
# straight from the DMA block, which did 81 MB/s (20.3 MSPS) at 21.6 on the
# second run. The rest are the cheap knobs for the last 6%.
BLOCK, BIG = 1 << 17, 1 << 19
PLAN = [
    (21_600_000, 1, BLOCK, 0),         # the 81 MB/s baseline
    (21_600_000, 2, BLOCK, 0),         # a second sender, on the other core
    (21_600_000, 1, BIG, 0),           # 4x bigger blocks: fewer hand-offs and system calls
    (21_600_000, 1, BIG, 4 << 20),     # ... plus a 4 MB socket send buffer
    (21_600_000, 2, BIG, 4 << 20),     # ... plus a second sender
    (20_000_000, 1, BIG, 0),           # the whole band with 88% of the band usable
    (19_200_000, 1, BLOCK, 0),         # ... with 92%
]


def jumbo_probe(rep, host, password):
    """Can the radio's Ethernet take 9000-byte frames? (Set back to 1500 at once.)"""
    rep.section("Jumbo frames (9000-byte MTU) on the radio")
    try:
        shell = IQ.RadioShell(host, password)
        out = shell.text("ip link set dev eth0 mtu 9000 2>&1; echo rc=$?; "
                         "ip link set dev eth0 mtu 1500 2>&1; cat /sys/class/net/eth0/mtu", timeout=20)
    except Exception as e:                           # the link may blink while the MTU changes
        rep(f"  couldn't tell ({e})")
        return None
    ok = "rc=0" in out
    rep("  supported: the Pi's eth0 would need MTU 9000 too (sudo ip link set dev eth0 mtu 9000), "
        "and both sides back to 1500 afterwards" if ok else
        f"  not supported ({out.split('rc=')[0].strip()[:80] or 'refused'}): stays at 1500")
    return ok


def real(rep, host, password, seconds, snap):
    rep.section("3. The radio's real samples (16-bit, straight from the DMA block)")
    rep(f"  {'MSPS':>5} {'conns':>5} {'block':>7} {'sndbuf':>6} {'delivered':>9} {'arrived':>7} "
        f"{'MB/s':>5}  radio cores")
    out = []
    for rate, conns, block, sndbuf in PLAN:
        try:
            r = IQ.PackedPluto(f"packed:{host}", 127e6, rate, conns=conns, buffer_size=block,
                               sndbuf=sndbuf, password=password)
        except OSError as e:
            rep(f"  {rate / 1e6:5.1f} {conns:5d} {block:7d} {sndbuf >> 20:5d}M  couldn't start: {e}")
            continue
        res = measure(r, seconds, snap=snap)
        log = radio_log(r, "done", "send buffer", "no DMA block", "failed")
        res.update(rate=rate, bits=16, conns=conns, block=block, sndbuf=sndbuf)
        out.append(res)
        rep(f"  {rate / 1e6:5.1f} {conns:5d} {block:7d} {(str(sndbuf >> 20) + 'M') if sndbuf else '-':>6} "
            f"{100 * res['sps'] / rate:8.0f}% {res['sps'] / 1e6:7.1f} {res['sps'] * 4 / 1e6:5.0f}  "
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
            rep(f"{rate / 1e6:g} MSPS: best {100 * r['sps'] / rate:.0f}% ({r['conns']} connection(s), "
                f"blocks of {r['block']}, send buffer {(str(r['sndbuf'] >> 20) + ' MB') if r['sndbuf'] else 'default'}, "
                f"{r['sps'] * 4 / 1e6:.0f} MB/s)" + ("  -> gets through in full" if full else ""))
    if test_runs:
        top = max(test_runs, key=lambda r: r["sps"])
        rep(f"test pattern (network only, no capture): {100 * top['sps'] / 21.6e6:.0f}% of 21.6 MSPS "
            f"with {top['conns']} connection(s)")
    win = lambda rate: (r := best.get(rate)) and r["sps"] >= 0.99 * rate and r["lost"] == 0
    if win(21_600_000):
        r = best[21_600_000]
        rep(f"-> 21.6 MSPS works with {r['conns']} connection(s), blocks of {r['block']}: one window, all "
            "43 channels, with the usual margins.")
    elif win(20_000_000):
        rep("-> 20.0 MSPS works: the whole band, with 88% of the captured band used "
            "(characterize.sh shows the outer channels' noise floor).")
    elif win(19_200_000):
        rep("-> 19.2 MSPS works: the whole band, with 92% of the captured band used "
            "(characterize.sh shows the outer channels' noise floor).")


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
        jumbo_probe(rep, host, args.password)
        shell.run("killall iqstream 2>/dev/null")
    verdict(rep, ok_loop, test_runs, real_runs)

    out = Path(args.out or ROOT / f"recordings/iqstream_{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text("\n".join(rep.lines) + "\n")
    print(f"\nSaved {out}/report.txt")


if __name__ == "__main__":
    main()
