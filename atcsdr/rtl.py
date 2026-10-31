"""RTL-SDR (RTL2832U dongles such as the Nooelec NESDR) as a source for the
airband receiver, with the same read / tune / flush / reconnect interface as
atcsdr.pluto.Pluto.

It runs the `rtl_sdr` tool (conda install -c conda-forge rtl-sdr) and reads its
8-bit I/Q from a pipe, so no Python bindings are needed. Retuning restarts the
tool, which takes about half a second. An RTL-SDR streams about 2.4 MSPS
reliably, roughly 2 MHz of spectrum per capture window.
"""

import os
import shutil
import subprocess
import sys
import time

import numpy as np

RTL_MAX_RATE = 2_400_000
READ_SAMPLES = 1 << 18       # ~0.1 s at 2.4 MSPS


def is_rtl(uri):
    return uri.startswith("rtl")


def rtl_index(uri):
    """'rtl' -> 0, 'rtl:1' -> 1."""
    return int(uri.split(":", 1)[1]) if ":" in uri else 0


def _rtl_sdr():
    local = os.path.join(os.path.dirname(sys.executable), "rtl_sdr")
    path = local if os.path.exists(local) else shutil.which("rtl_sdr")
    if not path:
        sys.exit("rtl_sdr not found. Install it with: conda install -c conda-forge rtl-sdr")
    return path


class RtlSdr:
    """Streams int16 I/Q (scaled like the Pluto's 12 bits) from an RTL-SDR."""

    def __init__(self, uri, center, rate, gain="40"):
        self.uri, self.index, self.rate, self.gain = uri, rtl_index(uri), int(rate), gain
        self.center = center
        self.proc = None
        self._start()

    def _start(self):
        cmd = [_rtl_sdr(), "-d", str(self.index), "-f", str(int(self.center)),
               "-s", str(self.rate)]
        if self.gain not in ("auto", "fast"):
            cmd += ["-g", str(float(self.gain))]
        self.proc = subprocess.Popen(cmd + ["-"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        # Fail fast if the dongle isn't there.
        time.sleep(0.3)
        if self.proc.poll() is not None:
            raise OSError(f"rtl_sdr exited (is RTL-SDR {self.index} plugged in and free?)")

    def read(self):
        want = 2 * READ_SAMPLES
        data = bytearray()
        while len(data) < want:
            chunk = self.proc.stdout.read(want - len(data))
            if not chunk:
                raise OSError("rtl_sdr stopped streaming")
            data += chunk
        u8 = np.frombuffer(bytes(data), np.uint8)
        return ((u8.astype(np.int16) - 127) * 16).astype(np.int16)

    def tune(self, center):
        self.center = center
        self.close()
        self._start()

    def flush(self):
        pass                 # a restart already starts from fresh samples

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def reconnect(self, stop):
        self.close()
        down = time.time()
        while not stop.is_set():
            time.sleep(1)
            try:
                self._start()
                return time.time() - down
            except OSError:
                self.close()
        return None


def open_rtl(uri, center, rate, gain="40"):
    try:
        return RtlSdr(uri, center, rate, gain)
    except OSError as e:
        sys.exit(f"Could not open the RTL-SDR: {e}")
