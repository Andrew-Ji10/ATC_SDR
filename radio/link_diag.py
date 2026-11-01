#!/usr/bin/env python3
"""
Link diagnosis: why can't the Pluto stream 21.6 MSPS (86 MB/s) to this computer,
and what would fix it? Three tests, each isolating one part of the path, while
reading the radio's CPU load (per core, and iiod's share) over SSH:

  A. capture on the radio itself (iio_readdev to /dev/null, no network):
     the AD9361 -> FPGA -> DMA -> radio RAM path
  B. plain TCP from the radio to this computer (dd /dev/zero | nc), no iiod:
     the radio's CPU + TCP/IP stack + Ethernet
  C. the normal libiio stream (iiod), at several rates and buffer sizes:
     the whole path as the recorder uses it

    python3 radio/link_diag.py --device ip:192.168.3.2
    python3 radio/link_diag.py --device ip:192.168.3.2 --rates 10.8 16.8 21.6 --seconds 8

It logs into the radio as root (default password analog; or an SSH key) to run
the radio-side commands. Needs sshpass for password login (pi/setup.sh installs
it). About 2 minutes. Writes report.txt to recordings/linkdiag_<date>-<time>/.
"""

import argparse
import shutil
import socket
import subprocess
import sys
import threading
import time
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atcsdr import pluto

TARGET = 21_600_000          # one window over all 43 channels
BYTES_PER_SAMPLE = 4         # int16 I + int16 Q
# (samples per buffer, kernel buffers) tried at 21.6 MSPS. Smaller buffers did better
# on the first run (131072: 83%, 524288: 72%), so the sweep goes smaller too.
BUFFERS = [(1 << 15, 16), (1 << 16, 16), (1 << 17, 8), (1 << 17, 16), (1 << 19, 8), (1 << 21, 4)]
DEFAULT_BUFFER = (1 << 19, 8)                          # what the recorder uses
TCP_PORT = 5201


class Report:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def section(self, title):
        self("")
        self(f"== {title}")


class Radio:
    """Runs shell commands on the Pluto over SSH (key login, else sshpass)."""

    OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5"]

    def __init__(self, host, password):
        self.host = host
        self.pre = []
        key = subprocess.run(["ssh", *self.OPTS, "-o", "BatchMode=yes", f"root@{host}", "true"],
                             capture_output=True, text=True)
        if key.returncode != 0:
            if not shutil.which("sshpass"):
                sys.exit("Can't log into the radio with a key, and sshpass isn't installed "
                         "(sudo apt install sshpass, or bash pi/setup.sh).")
            self.pre = ["sshpass", "-p", password]
            if self.run("true").returncode != 0:
                sys.exit(f"Can't log into root@{host} (password {password!r}?).")

    def cmd(self, command):
        return [*self.pre, "ssh", *self.OPTS, f"root@{self.host}", command]

    def run(self, command, timeout=120):
        return subprocess.run(self.cmd(command), capture_output=True, text=True, timeout=timeout)

    def popen(self, command):
        return subprocess.Popen(self.cmd(command), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)

    def snapshot(self):
        """CPU ticks per core, and iiod's, right now."""
        out = self.run("cat /proc/stat; for p in $(pidof iiod); do echo IIOD $(cat /proc/$p/stat); done").stdout
        cores, iiod = {}, 0
        for line in out.splitlines():
            if line.startswith("cpu") and len(line) > 3 and line[3].isdigit():
                name, *v = line.split()
                v = list(map(int, v[:8]))
                cores[name] = (sum(v) - v[3] - v[4], sum(v))
            elif line.startswith("IIOD"):
                f = line.split(")")[-1].split()       # fields after the command name
                iiod += int(f[11]) + int(f[12])       # utime + stime
        return cores, iiod

    @staticmethod
    def load(s0, s1):
        """({core: % busy}, iiod as % of one core) between two snapshots."""
        busy = {k: 100 * (s1[0][k][0] - s0[0][k][0]) / max(1, s1[0][k][1] - s0[0][k][1])
                for k in s1[0] if k in s0[0]}
        per_core = sum(s1[0][k][1] - s0[0][k][1] for k in busy) / max(1, len(busy))
        return busy, 100 * (s1[1] - s0[1]) / max(1, per_core)


def fmt_load(busy, iiod=None):
    text = ", ".join(f"{k} {v:.0f}%" for k, v in sorted(busy.items()))
    return f"radio CPU: {text}" + ("" if iiod is None else f"; iiod {iiod:.0f}% of one core")


def configure(uri, rate):
    """Set the radio's sample rate and RF bandwidth through libiio, without
    opening a buffer (so iio_readdev on the radio can use it)."""
    iio = pluto._iio()
    ctx = iio.Context(uri)
    ch = ctx.find_device("ad9361-phy").find_channel("voltage0", False)
    ch.attrs["sampling_frequency"].value = str(int(rate))
    ch.attrs["rf_bandwidth"].value = str(int(rate))
    got = int(ch.attrs["sampling_frequency"].value)
    del ctx
    return got


# ------------------------------------------------------------------ A

def test_capture(rep, radio, uri, seconds):
    rep.section(f"A. Capture on the radio itself at {TARGET / 1e6:g} MSPS (no network)")
    if radio.run("which iio_readdev").returncode != 0:
        rep("  iio_readdev isn't on the radio: skipped")
        return None
    got = configure(uri, TARGET)
    n = int(TARGET * seconds)
    script = ("read a b < /proc/uptime; "
              f"iio_readdev -b 1048576 -s {n} cf-ad9361-lpc voltage0 voltage1 > /dev/null 2>/tmp/rd.err; "
              "rc=$?; read c d < /proc/uptime; echo $rc $a $c; cat /tmp/rd.err | tail -2")
    s0 = radio.snapshot()
    out = radio.run(script, timeout=seconds * 10 + 30).stdout.split("\n")
    s1 = radio.snapshot()
    try:
        rc, start, end = out[0].split()[:3]
        elapsed = float(end) - float(start)
    except ValueError:
        rep(f"  couldn't run iio_readdev: {' '.join(out)[:200]}")
        return None
    if rc != "0":
        rep(f"  iio_readdev failed ({' '.join(out[1:])[:200]})")
        return None
    rate = n / elapsed
    busy, _ = Radio.load(s0, s1)
    rep(f"  rate set {got / 1e6:g} MSPS; read {n / 1e6:.0f} M samples in {elapsed:.1f} s = "
        f"{rate / 1e6:.1f} MSPS ({100 * rate / TARGET:.0f}%)")
    rep(f"  {fmt_load(busy)}")
    return rate


# ------------------------------------------------------------------ B

def local_ip_toward(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return s.getsockname()[0]


def has_applet(radio, name):
    return radio.run(f"which {name} >/dev/null 2>&1 || "
                     f"busybox --list 2>/dev/null | grep -qx {name}").returncode == 0


def measure_receive(radio, recv, seconds):
    """Call recv() (bytes received; 0 = the stream ended) flat out, and measure from
    1 s in (past TCP slow start) for `seconds`. The radio's CPU snapshots run on
    threads, so the receive loop never pauses. Returns (MB/s, radio load, this
    computer's % of a core), or None if the stream ended early."""
    total, start, mark, mark_bytes, end, end_bytes = 0, None, None, 0, None, 0
    snaps, threads = {}, {}

    def snap(key):
        threads[key] = threading.Thread(target=lambda: snaps.__setitem__(key, radio.snapshot()),
                                        daemon=True)
        threads[key].start()

    t_pi = os.times()
    while True:
        n = recv()
        if not n:
            break
        now = time.time()
        start = start or now
        total += n
        if mark is None and now - start >= 1:
            snap(0)
            mark, mark_bytes = now, total
        if mark is not None and end is None and now - mark >= seconds:
            snap(1)
            end, end_bytes = now, total
        if end is not None and not threads[1].is_alive():
            break
    if end is None:
        return None
    threads[0].join()
    if 0 not in snaps or 1 not in snaps:
        return None
    t1 = os.times()
    pi_cpu = 100 * (t1.user + t1.system - t_pi.user - t_pi.system) / (end - start)
    return (end_bytes - mark_bytes) / (end - mark) / 1e6, Radio.load(snaps[0], snaps[1])[0], pi_cpu


def tcp_nc(radio, seconds):
    """The radio sends /dev/zero with nc to a socket on this computer."""
    me = local_ip_toward(radio.host)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((me, TCP_PORT))
    srv.listen(1)
    srv.settimeout(15)
    sender = radio.popen(f"dd if=/dev/zero bs=65536 count=200000 2>/dev/null | nc {me} {TCP_PORT}")
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        sender.kill()
        return f"the radio couldn't connect back to {me}:{TCP_PORT} (a firewall on this computer?)"
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    buf = bytearray(1 << 20)
    try:
        return measure_receive(radio, lambda: conn.recv_into(buf), seconds)
    finally:
        conn.close()
        srv.close()
        sender.kill()


def tcp_httpd(radio, seconds, port=8099):
    """busybox httpd on the radio serves a 64 MB file from RAM (tmpfs), fetched over and over."""
    pid = radio.run("mkdir -p /tmp/linkdiag && "
                    "dd if=/dev/zero of=/tmp/linkdiag/z bs=1048576 count=64 2>/dev/null && "
                    f"(httpd -f -p {port} -h /tmp/linkdiag </dev/null >/dev/null 2>&1 & echo $!)").stdout.strip()
    if not pid.isdigit():
        return "couldn't start httpd on the radio"
    time.sleep(0.5)
    buf = bytearray(1 << 20)
    state = {"sock": None}

    def recv():
        while True:
            if state["sock"] is None:
                s = socket.create_connection((radio.host, port), timeout=10)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
                s.sendall(b"GET /z HTTP/1.0\r\nHost: radio\r\n\r\n")
                state["sock"] = s
            n = state["sock"].recv_into(buf)
            if n:
                return n
            state["sock"].close()                  # that copy is done: fetch it again
            state["sock"] = None

    try:
        return measure_receive(radio, recv, seconds)
    except OSError as e:
        return f"couldn't download from the radio's httpd: {e}"
    finally:
        if state["sock"] is not None:
            state["sock"].close()
        radio.run(f"kill {pid}; rm -rf /tmp/linkdiag")


def test_tcp(rep, radio, seconds):
    if has_applet(radio, "nc"):
        rep.section("B. Plain TCP from the radio (dd /dev/zero | nc), no iiod")
        res = tcp_nc(radio, seconds)
    elif has_applet(radio, "httpd"):
        rep.section("B. Plain TCP from the radio (busybox httpd, a file in RAM), no iiod")
        res = tcp_httpd(radio, seconds)
    else:
        rep.section("B. Plain TCP from the radio, no iiod")
        rep("  the radio has neither nc nor httpd: skipped")
        return None
    if isinstance(res, str) or res is None:
        rep(f"  {res or 'the stream ended early'}")
        return None
    mbps, busy, pi_cpu = res
    rep(f"  {mbps:.0f} MB/s ({mbps * 8:.0f} Mb/s) = {mbps / BYTES_PER_SAMPLE:.1f} MSPS worth of "
        f"16-bit samples; {TARGET / 1e6:g} MSPS needs {TARGET * BYTES_PER_SAMPLE / 1e6:.0f} MB/s")
    rep(f"  {fmt_load(busy)}; this computer {pi_cpu:.0f}% of one core")
    return mbps


# ------------------------------------------------------------------ C

def stream(uri, rate, buffer, kbufs, seconds, radio):
    """Delivered fraction, radio load and this computer's CPU for one setting."""
    try:
        r = pluto.Pluto(uri, 127e6, rate, "40", buffer_size=buffer, kernel_buffers=kbufs)
    except OSError as e:
        return dict(error=str(e))
    count, stop, err = [0], threading.Event(), []

    def reader():
        try:
            r.flush()
            while not stop.is_set():
                count[0] += len(r.read()) // 2
        except OSError as e:
            err.append(str(e))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(1.5)                                  # past the queued buffers and startup
    s0, n0, t0, c0 = radio.snapshot(), count[0], time.time(), os.times()
    time.sleep(seconds)
    s1, n1, t1, c1 = radio.snapshot(), count[0], time.time(), os.times()
    stop.set()
    th.join(timeout=10)
    r.close()
    busy, iiod = Radio.load(s0, s1)
    return dict(got=(n1 - n0) / ((t1 - t0) * rate), busy=busy, iiod=iiod, error=err[0] if err else None,
                pi=100 * (c1.user + c1.system - c0.user - c0.system) / (t1 - t0))


def test_stream(rep, radio, uri, rates, seconds):
    rep.section("C. The libiio stream (iiod), as the recorder uses it")
    rep(f"  {'MSPS':>6} {'buffer':>9} {'kbufs':>5} {'delivered':>10} {'MB/s':>6} {'iiod':>6} "
        f"{'radio cores':>16} {'this pc':>8}")
    results = []
    plan = [(r, *DEFAULT_BUFFER) for r in rates] + [(TARGET, b, k) for b, k in BUFFERS if (b, k) != DEFAULT_BUFFER]
    for rate, buffer, kbufs in plan:
        res = stream(uri, rate, buffer, kbufs, seconds, radio)
        if res.get("error") and "got" not in res:
            rep(f"  {rate / 1e6:6.2f} {buffer:9d} {kbufs:5d}  could not open: {res['error'][:60]}")
            continue
        res.update(rate=rate, buffer=buffer, kbufs=kbufs)
        results.append(res)
        cores = " ".join(f"{v:3.0f}%" for _, v in sorted(res["busy"].items()))
        rep(f"  {rate / 1e6:6.2f} {buffer:9d} {kbufs:5d} {100 * res['got']:9.0f}% "
            f"{res['got'] * rate * BYTES_PER_SAMPLE / 1e6:6.0f} {res['iiod']:5.0f}% {cores:>16} "
            f"{res['pi']:7.0f}%" + (f"  ({res['error'][:40]})" if res["error"] else ""))
    rep("  delivered = % of the rate that arrived; iiod = % of one radio core; this pc = % of one core")
    return results


# ------------------------------------------------------------------ D

def test_steering(rep, radio, uri, buffer, kbufs, seconds):
    """The network stack's interrupt work and iiod both default to core 0: try them
    on separate cores. Settings are put back afterwards (and reset on reboot anyway)."""
    rep.section(f"D. Spreading the work over both radio cores ({TARGET / 1e6:g} MSPS, "
                f"buffer {buffer} x {kbufs})")
    irqs = [line.split(":")[0].strip() for line in
            radio.run("grep -iE 'eth0|ethernet' /proc/interrupts").stdout.splitlines() if ":" in line]
    pids = radio.run("pidof iiod").stdout.split()
    if not irqs or not pids:
        rep(f"  couldn't find the Ethernet interrupt ({irqs}) or iiod ({pids}): skipped")
        return []
    pid = pids[0]
    taskset = has_applet(radio, "taskset")
    rps = "/sys/class/net/eth0/queues/rx-0/rps_cpus"
    orig = {f"/proc/irq/{n}/smp_affinity": radio.run(f"cat /proc/irq/{n}/smp_affinity").stdout.strip()
            for n in irqs}
    orig_rps = radio.run(f"cat {rps} 2>/dev/null").stdout.strip()
    m = radio.run(f"taskset -p {pid} 2>/dev/null").stdout.strip().split(":")
    orig_task = m[-1].strip() if taskset and len(m) > 1 else None
    rep(f"  Ethernet interrupt(s) {', '.join(irqs)} on cores mask {', '.join(orig.values())}; "
        f"iiod pid {pid}" + (f", cores mask {orig_task}" if orig_task else
                             " (no taskset on the radio: only the interrupts can be moved)"))

    def place(net, iiod):
        errs = []
        for path in orig:
            if radio.run(f"echo {net} > {path}").returncode:
                errs.append(path)
        if orig_rps:
            radio.run(f"echo {net} > {rps}")
        if iiod and taskset:
            radio.run(f"taskset -p {iiod} {pid}")
        return errs

    results = []
    configs = [("as is", None, None),
               ("network on core 1, iiod on core 0", "2", "1"),
               ("network on core 0, iiod on core 1", "1", "2")]
    try:
        for name, net, iiod in configs:
            if net:
                errs = place(net, iiod)
                if errs:
                    rep(f"  {name}: couldn't set {', '.join(errs)}")
                    continue
            res = stream(uri, TARGET, buffer, kbufs, seconds, radio)
            if "got" not in res:
                rep(f"  {name}: couldn't open the stream ({res.get('error', '')[:60]})")
                continue
            res.update(name=name, rate=TARGET, buffer=buffer, kbufs=kbufs)
            results.append(res)
            cores = ", ".join(f"{k} {v:.0f}%" for k, v in sorted(res["busy"].items()))
            rep(f"  {name:<36} {100 * res['got']:4.0f}% delivered, "
                f"{res['got'] * TARGET * BYTES_PER_SAMPLE / 1e6:3.0f} MB/s; iiod {res['iiod']:3.0f}%; {cores}")
    finally:
        for path, mask in orig.items():
            radio.run(f"echo {mask} > {path}")
        if orig_rps:
            radio.run(f"echo {orig_rps} > {rps}")
        if orig_task:
            radio.run(f"taskset -p {orig_task} {pid}")
        rep("  (original placement restored)")
    return results


# ------------------------------------------------------------------ verdict

def verdict(rep, capture, tcp, results, steered=()):
    rep.section("Verdict")
    as_is = next((r for r in steered if r["name"] == "as is"), None)
    moved = max((r for r in steered if r["name"] != "as is"), key=lambda r: r["got"], default=None)
    if as_is and moved:
        gain = moved["got"] - as_is["got"]
        rep(f"spreading over both cores ({moved['name']}): {100 * as_is['got']:.0f}% -> "
            f"{100 * moved['got']:.0f}% delivered"
            + ("  (helps)" if gain > 0.03 else "  (no real change)"))
    plain = [r for r in results if r["rate"] == TARGET]      # test C only, for the buffer comparison
    results = list(results) + [r for r in steered if r["name"] != "as is"]
    near = [r for r in results if abs(r["rate"] - 19_200_000) < 1 and r["got"] >= 0.99]
    if near:
        rep("19.2 MSPS gets through in full: the whole band fits at 19.2 if the outer channels may sit "
            "nearer the band edges (to check with characterize.sh).")
    need = TARGET * BYTES_PER_SAMPLE / 1e6
    at_target = [r for r in results if r["rate"] == TARGET]
    best = max(at_target, key=lambda r: r["got"], default=None)
    if best:
        rep(f"best stream at {TARGET / 1e6:g} MSPS: {100 * best['got']:.0f}% delivered "
            f"(buffer {best['buffer']}, {best['kbufs']} kernel buffers), about "
            f"{best['got'] * TARGET / 1e6:.1f} MSPS")
    ceiling = max((r["got"] * r["rate"] for r in results if r["got"] >= 0.99), default=None)
    if ceiling:
        rep(f"highest rate delivered in full: {ceiling / 1e6:.1f} MSPS")

    if capture is not None and capture < 0.97 * TARGET:
        rep("-> The radio can't even capture 21.6 MSPS into its own memory: the limit is the "
            "radio's capture path (FPGA / DMA / RAM). Software on the Pi can't fix that.")
        return
    if best and best["got"] >= 0.99:
        rep("-> The link carries 21.6 MSPS with these settings: one window over the whole band works."
            + (f" Use buffer {best['buffer']} with {best['kbufs']} kernel buffers."
               if (best["buffer"], best["kbufs"]) != DEFAULT_BUFFER else ""))
        return
    if best and any(r["pi"] > 85 for r in at_target):
        rep("-> This computer's reading loop is near 100% of a core: the Pi side is (also) a limit.")
    if tcp is not None:
        if tcp < need:
            packed = TARGET * 3 / 1e6                 # 12-bit I + 12-bit Q = 3 bytes a sample
            rep(f"-> Plain TCP from the radio tops out at {tcp:.0f} MB/s, below the {need:.0f} MB/s needed, "
                "even without iiod: the radio's CPU / network stack is the limit, not iiod.")
            rep(f"   12-bit packing needs {packed:.0f} MB/s: "
                + ("that would fit (it needs a custom streaming program on the radio)."
                   if tcp > packed * 1.05 else "still too much; only processing on the radio's FPGA "
                   "(sending just the channels) gets the whole band through."))
        else:
            rep(f"-> Plain TCP from the radio reaches {tcp:.0f} MB/s, more than the {need:.0f} MB/s needed, "
                "but the libiio stream doesn't: iiod itself is the bottleneck"
                + (f" (it uses {best['iiod']:.0f}% of a radio core)" if best else "") + ".")
            rep("   Fixes, easiest first: a newer libiio/iiod on the radio (firmware update); "
                "a small custom streaming program on the radio in place of iiod.")
    elif best and best["iiod"] >= 85:
        # No plain-TCP number: judge from iiod's own load.
        cores = sorted(best["busy"].values(), reverse=True)
        rep(f"-> iiod uses {best['iiod']:.0f}% of one radio core at {TARGET / 1e6:g} MSPS, while the "
            f"radio's capture path {'keeps up' if capture else 'was not tested'} and this computer is "
            f"nearly idle ({best['pi']:.0f}% of a core): iiod is CPU-bound on the radio. It works on one "
            f"core at a time"
            + (f"; the other was {cores[1]:.0f}% busy." if len(cores) > 1 else "."))
        got_mb = best["got"] * TARGET * BYTES_PER_SAMPLE / 1e6
        packed = TARGET * 3 / 1e6
        rep(f"   Fixes, easiest first: the best buffer settings (below); a newer iiod (libiio 1.x, in "
            f"newer firmware); a custom streaming program on the radio, which could also pack samples "
            f"to 12 bits ({packed:.0f} MB/s instead of {need:.0f}; iiod managed {got_mb:.0f} MB/s at best); "
            f"or processing on the radio's FPGA, sending just the channels.")
    if best:
        busy = best["busy"]
        if busy and max(busy.values()) > 90:
            rep(f"   During the {TARGET / 1e6:g} MSPS stream a radio core was {max(busy.values()):.0f}% busy: "
                "the radio's CPU is saturated.")
        default = next((r for r in plain if (r["buffer"], r["kbufs"]) == DEFAULT_BUFFER), None)
        best_buf = max(plain, key=lambda r: r["got"], default=None)
        if default and best_buf and best_buf is not default and best_buf["got"] - default["got"] > 0.03:
            rep(f"   Buffer size matters: {100 * default['got']:.0f}% with the recorder's "
                f"{default['buffer']} samples x {default['kbufs']}, {100 * best_buf['got']:.0f}% with "
                f"{best_buf['buffer']} x {best_buf['kbufs']}: the recorder should use the latter.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="", help="Pluto URI, e.g. ip:192.168.3.2")
    p.add_argument("--password", default="analog", help="the radio's root password (default analog)")
    p.add_argument("--rates", type=float, nargs="+", default=[10.8, 14.4, 18.0, 19.2, 21.6],
                   help="MSPS to stream in test C (default 10.8 14.4 18.0 19.2 21.6)")
    p.add_argument("--no-steering", action="store_true", help="skip test D")
    p.add_argument("--seconds", type=float, default=6.0, help="seconds per measurement (default 6)")
    p.add_argument("--out", help="output folder (default recordings/linkdiag_<time>)")
    args = p.parse_args()

    uri = pluto.find_uri(args.device)
    if not uri.startswith("ip:"):
        sys.exit("This diagnoses the Ethernet link: pass --device ip:<address>.")
    host = uri[3:]
    rep = Report()
    rep(f"Link diagnosis, {datetime.now():%Y-%m-%d %H:%M}: {uri}, target {TARGET / 1e6:g} MSPS "
        f"({TARGET * BYTES_PER_SAMPLE / 1e6:.0f} MB/s)")
    radio = Radio(host, args.password)
    info = radio.run("uname -r; grep -m1 -i 'model name\\|Processor' /proc/cpuinfo; nproc 2>/dev/null || "
                     "grep -c ^processor /proc/cpuinfo; cat /sys/class/net/eth0/mtu; "
                     "cat /sys/class/net/eth0/speed 2>/dev/null; iio_info -V 2>/dev/null | head -1").stdout
    lines = [l.strip() for l in info.splitlines() if l.strip()]
    rep(f"radio: kernel {lines[0] if lines else '?'}; " + "; ".join(lines[1:]))
    iio = pluto._iio()
    rep(f"this computer: libiio {'.'.join(map(str, iio.version[:2])) if hasattr(iio, 'version') else '?'}")

    capture = test_capture(rep, radio, uri, args.seconds)
    tcp = test_tcp(rep, radio, args.seconds)
    rates = sorted({int(r * 1e6) for r in args.rates} | {TARGET})
    results = test_stream(rep, radio, uri, rates, args.seconds)
    steered = []
    at_target = [r for r in results if r["rate"] == TARGET]
    if at_target and not args.no_steering:
        b = max(at_target, key=lambda r: r["got"])
        steered = test_steering(rep, radio, uri, b["buffer"], b["kbufs"], args.seconds)
    verdict(rep, capture, tcp, results, steered)

    out = Path(args.out or ROOT / f"recordings/linkdiag_{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text("\n".join(rep.lines) + "\n")
    print(f"\nSaved {out}/report.txt")


if __name__ == "__main__":
    main()
