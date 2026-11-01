#!/usr/bin/env python3
"""
Station test: ATC audio and ADS-B together, on the planned Raspberry Pi 5 core
layout, for a set time. Reports each system's uptime, the load on every core,
temperature and throttling, and the storage used per day / week / month.

  cores 0-1  ATC receiver (radio/atc_listen.py: iqstream, all 43 channels, 20.0 MSPS)
  core  3    ATC clips: plain and cleaned MP3s
  core  2    ADS-B (radio/adsb_listen.py: the RTL-SDR at 1090 MHz)

    python3 radio/station_test.py --device packed:192.168.3.2 --duration 1h --ref 37.43 -122.17
    python3 radio/station_test.py ... --adsb-source synthetic     # without the dongle
    python3 radio/station_test.py --atc-source synthetic --adsb-source synthetic   # no radios at all

While it runs, a line appears for each ATC transmission saved and each new
aircraft (with its position), and one status line shows both systems (stream %,
load, clips and aircraft so far) and each core's load. Output in recordings/station_<UTC>/:
atc/ and adsb/ (each test's own files and summary), atc.log, adsb.log, cores.csv
(each core's load every 5 s) and summary.txt (both together). Ctrl-C stops both
cleanly.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atcsdr.airband import parse_duration

TTY = sys.stdout.isatty()


def utc(t, files=False):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ" if files else "%Y-%m-%dT%H:%M:%SZ")


def core_ticks():
    out = {}
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("cpu") and line[3].isdigit():
                name, *v = line.split()
                v = list(map(int, v[:8]))
                out[int(name[3:])] = (sum(v) - v[3] - v[4], sum(v))
    except OSError:
        pass
    return out


def temp_c():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        return None


def throttled():
    if not shutil.which("vcgencmd"):
        return None
    out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout
    return int(out.split("=")[-1], 16) if "=" in out else None


def last_row(path):
    """The last line of a CSV as a dict (header from the first line), or {}."""
    try:
        with open(path, "rb") as f:
            header = f.readline().decode().strip().split(",")
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400))
            lines = f.read().decode(errors="replace").strip().splitlines()
    except OSError:
        return {}
    if len(lines) < 1 or lines[-1].startswith(header[0]):
        return {}
    return dict(zip(header, lines[-1].split(",")))


class Follow:
    """New complete lines of a CSV that another process is writing, as dicts."""

    def __init__(self, path):
        self.path, self.pos, self.header, self.count = path, 0, None, 0

    def new(self):
        try:
            with open(self.path, "rb") as f:              # bytes: positions stay exact
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        done = data[: data.rfind(b"\n") + 1]             # only whole lines
        self.pos += len(done)
        rows = []
        for line in done.decode(errors="replace").splitlines():
            if self.header is None:
                self.header = line.split(",")
                continue
            rows.append(dict(zip(self.header, line.split(","))))
        self.count += len(rows)
        return rows


def clock(t):
    """'2026-11-01T04:25:13.250Z' -> '04:25:13Z'."""
    return t[11:19] + "Z" if len(t) >= 19 else t


def tail(path, n=8):
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="", help="the Pluto, e.g. packed:192.168.3.2")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("30m"),
                   help="how long: 30m, 8h, 1d (default 30m); 0 = until Ctrl-C")
    p.add_argument("--rate", type=float, default=20.0, help="ATC MSPS (default 20.0)")
    p.add_argument("--atc-source", choices=["radio", "synthetic"], default="radio", help="default radio")
    p.add_argument("--threshold", type=float, default=8.0, help="ATC squelch, dB (default 8)")
    p.add_argument("--no-clean", action="store_true", help="ATC: plain MP3s only")
    p.add_argument("--adsb-source", choices=["rtl", "synthetic"], default="rtl", help="default rtl")
    p.add_argument("--rtl", default="rtl", help="ADS-B dongle: rtl, or rtl:N (default rtl)")
    p.add_argument("--adsb-gain", default="40", help="RTL-SDR gain, dB (default 40)")
    p.add_argument("--ref", type=float, nargs=2, metavar=("LAT", "LON"), help="your position, for ADS-B")
    p.add_argument("--atc-cores", default="0,1")
    p.add_argument("--clip-cores", default="3")
    p.add_argument("--adsb-cores", default="2")
    p.add_argument("--out", help="output folder (default recordings/station_<UTC time>)")
    args = p.parse_args()

    out = Path(args.out or ROOT / f"recordings/station_{utc(time.time(), files=True)}")
    out.mkdir(parents=True, exist_ok=True)
    dur = f"{args.duration:g}s" if args.duration else "0"
    py = [sys.executable, "-u"]
    atc_cmd = py + [str(ROOT / "radio/atc_listen.py"), "--duration", dur, "--rate", f"{args.rate:g}",
                    "--threshold", f"{args.threshold:g}", "--dsp-cores", args.atc_cores,
                    "--clip-cores", args.clip_cores, "--source", args.atc_source, "--out", str(out / "atc")]
    if args.device:
        atc_cmd += ["--device", args.device]
    if args.no_clean:
        atc_cmd.append("--no-clean")
    adsb_cmd = py + [str(ROOT / "radio/adsb_listen.py"), "--duration", dur, "--source", args.adsb_source,
                     "--rtl", args.rtl, "--gain", args.adsb_gain, "--cores", args.adsb_cores,
                     "--out", str(out / "adsb")]
    if args.ref:
        adsb_cmd += ["--ref", str(args.ref[0]), str(args.ref[1])]

    print(f"Station test {utc(time.time())}: ATC on cores {args.atc_cores} (clips on {args.clip_cores}), "
          f"ADS-B on core {args.adsb_cores}, " + (f"for {args.duration / 60:g} min." if args.duration else
                                                  "until Ctrl-C.") + f"\nSaving to {out}/", flush=True)
    logs = {name: open(out / f"{name}.log", "w") for name in ("atc", "adsb")}
    procs = {name: subprocess.Popen(cmd, stdout=logs[name], stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
             for name, cmd in (("atc", atc_cmd), ("adsb", adsb_cmd))}

    cores_csv = open(out / "cores.csv", "w", buffering=1)
    ticks = core_ticks()
    cores_csv.write("time_utc," + ",".join(f"cpu{k}" for k in sorted(ticks)) + ",temp_c\n")
    started, thr0, busy_rows, temps, last_ticks, k = time.time(), throttled(), [], [], ticks, 0
    loads = {}
    clips, planes = Follow(out / "atc/transmissions.csv"), Follow(out / "adsb/sightings.csv")

    def say(text):
        print(("\r\033[K" if TTY else "") + text, flush=True)

    try:
        while any(pr.poll() is None for pr in procs.values()):
            time.sleep(1)
            k += 1
            for name, pr in procs.items():                # one stopped early: say why, keep the other
                if pr.poll() not in (None, 0) and not getattr(pr, "_reported", False):
                    pr._reported = True
                    print(f"\n{name} stopped (exit {pr.returncode}):\n{tail(out / f'{name}.log')}", flush=True)
            if k % 5 == 0:
                now_ticks = core_ticks()
                loads = {c: 100 * (now_ticks[c][0] - last_ticks[c][0]) / max(1, now_ticks[c][1] - last_ticks[c][1])
                         for c in now_ticks if c in last_ticks}
                last_ticks = now_ticks
                t = temp_c()
                if t:
                    temps.append(t)
                if loads:
                    busy_rows.append(loads)
                    cores_csv.write(f"{utc(time.time())}," + ",".join(f"{loads[c]:.0f}" for c in sorted(loads))
                                    + f",{'' if t is None else t}\n")
            for r in clips.new():
                say(f"{clock(r.get('time_utc', ''))}  ATC    {r.get('name', ''):<11} {r.get('mhz', '')} MHz  "
                    f"{r.get('seconds', '')} s  {r.get('peak_db', '')} dB")
            for r in planes.new():
                km = r.get("km_away")
                alt = r.get("altitude_ft")
                say(f"{clock(r.get('time_utc', ''))}  ADS-B  {r.get('icao', '')} {r.get('callsign') or '-':<8} "
                    + (f"{alt} ft  " if alt else "") + f"{r.get('lat', '')}, {r.get('lon', '')}"
                    + (f"  {float(km):.0f} km away" if km else ""))
            if TTY:
                a, b = last_row(out / "atc/health.csv"), last_row(out / "adsb/health.csv")
                line = f"  {time.time() - started:5.0f} s"
                line += (f" | ATC stream {float(a['stream_pct']):5.1f}% rx {a['receiver_busy']}"
                         f" {clips.count} clips" if a else " | ATC starting")
                line += (f" | ADS-B stream {float(b['stream_pct']):5.1f}% dec {b['decoder_busy']}"
                         f" {b['msgs_per_s']} msg/s {b['aircraft_in_view']} in view {planes.count} seen"
                         if b else " | ADS-B starting")
                if loads:
                    line += " | cores " + " ".join(f"{loads[c]:.0f}" for c in sorted(loads)) + "%"
                if temps:
                    line += f" | {temps[-1]:.0f} C"
                print("\r\033[K" + line[: shutil.get_terminal_size((160, 20)).columns - 1], end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping both (they finish their clips and summaries)...", flush=True)
    for pr in procs.values():
        try:
            pr.wait(timeout=900)
        except subprocess.TimeoutExpired:
            pr.terminate()
    for f in logs.values():
        f.close()
    cores_csv.close()
    elapsed, thr1 = time.time() - started, throttled()

    def summary_of(name):
        text = tail(out / name / "summary.txt", 200)
        return text or f"(no summary: see {name}.log)\n{tail(out / f'{name}.log')}"

    def first(text, key):
        return next((l.strip() for l in text.splitlines() if l.strip().startswith(key)), f"{key} ?")

    atc, adsb = summary_of("atc"), summary_of("adsb")
    mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    per_day = mb / max(elapsed / 3600, 1e-9) / 1e3 * 24
    lines = [f"Station test {utc(started)}, {elapsed / 60:.1f} min",
             f"ATC   {first(atc, 'uptime:')}", f"ADS-B {first(adsb, 'uptime:')}"]
    if busy_rows:
        cores = sorted(busy_rows[0])
        avg = {c: sum(r[c] for r in busy_rows) / len(busy_rows) for c in cores}
        peak = {c: max(r[c] for r in busy_rows) for c in cores}
        lines.append("cores (average / busiest 5 s): " + ", ".join(f"cpu{c} {avg[c]:.0f}/{peak[c]:.0f}%" for c in cores))
    if temps:
        lines.append(f"Pi: {temps[0]:.0f} C at start, peak {max(temps):.0f} C"
                     + ("" if thr1 is None else "; throttling " + (
                         "YES: fit the active cooler" if thr1 & 0x6 or (thr1 & ~(thr0 or 0)) & 0x60000 else "none")))
    lines.append(f"storage, both: {mb:.1f} MB = {per_day:.2f} GB/day, {per_day * 7:.1f} GB/week, "
                 f"{per_day * 30:.0f} GB/month at this traffic")
    lines += ["", "== ATC", atc, "", "== ADS-B", adsb]
    report = "\n".join(lines)
    print("\n\n" + report)
    (out / "summary.txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
