"""Sample streaming from the Pluto through iqstream, in place of iiod.

iiod lands on whichever radio core the scheduler picks, sometimes the one that
handles the Ethernet interrupts, and then tops out near 62 MB/s (15.5 MSPS).
iqstream (C, in radio/iqstream/) runs on the radio from RAM, maps the DMA
blocks and sends them, straight from the block, from a thread pinned away
from the interrupts. Measured: 21.6 MSPS in full (87 MB/s, the sender at 75% of
a core) with 1 connection, 524288-sample blocks and a 4 MB send buffer, the
defaults here; iiod managed 62-74 MB/s.
It can also pack to 12 bits (-f 12), but packing costs the radio's ARM more
than the bytes it saves, so 16-bit is the default.

PackedPluto has the same read / tune / flush / close / reconnect interface as
atcsdr.pluto.Pluto, so the receivers take it as --device packed:<address>.
Tuning, gain and sample rate still go through iiod; only the samples don't.
It copies the binary (build/iqstream-armhf, from pi/build_iqstream.sh) to the
radio's /tmp when it isn't there or has changed, and starts it over SSH (root,
default password analog). Nothing is written to the radio's flash.
"""

import hashlib
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import pluto

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build" / "iqstream-armhf"
NATIVE = ROOT / "build" / "iqstream-native"
REMOTE = "/tmp/iqstream"
PORT = 5300
HEADER = struct.Struct("<IIIIIQ")    # magic, seq, samples, flags, lost, t_ns
MAGIC = 0x31535149                   # "IQS1"
F_PACKED12, F_TEST = 1, 2
SLOTS = 8                            # iqstream's ring
KERNEL_CHUNKS = 4                    # iqstream's kernel buffer, in chunks
_CLOSED = object()                   # queued when a connection ends

try:
    import numba
except ImportError:
    numba = None


# ------------------------------------------------------------------ packing

def pack12(iq):
    """int16 I, Q, I, Q ... (12-bit values) -> 3 bytes a sample, as iqstream packs."""
    iq = np.asarray(iq, np.int16)
    i = iq[0::2].astype(np.uint16) & 0xFFF
    q = iq[1::2].astype(np.uint16) & 0xFFF
    out = np.empty((len(i), 3), np.uint8)
    out[:, 0] = i & 0xFF
    out[:, 1] = (i >> 8) | ((q & 0xF) << 4)
    out[:, 2] = q >> 4
    return out.tobytes()


def _unpack12_numpy(buf):
    b = np.frombuffer(buf, np.uint8).reshape(-1, 3).astype(np.int16)
    out = np.empty(2 * len(b), np.int16)
    out[0::2] = b[:, 0] | ((b[:, 1] & 0x0F) << 8)
    out[1::2] = (b[:, 1] >> 4) | (b[:, 2] << 4)
    out ^= 0x800                                 # 12-bit two's complement -> int16
    out -= 0x800
    return out


if numba:
    @numba.njit(cache=True)
    def _unpack12_kernel(b, out):
        for j in range(len(b) // 3):
            b0, b1, b2 = np.int32(b[3 * j]), np.int32(b[3 * j + 1]), np.int32(b[3 * j + 2])
            i = b0 | ((b1 & 0x0F) << 8)
            q = (b1 >> 4) | (b2 << 4)
            out[2 * j] = ((i ^ 0x800) - 0x800)
            out[2 * j + 1] = ((q ^ 0x800) - 0x800)

    def unpack12(buf):
        """3 bytes a sample -> int16 I, Q, I, Q ..."""
        b = np.frombuffer(buf, np.uint8)
        out = np.empty(2 * (len(b) // 3), np.int16)
        _unpack12_kernel(b, out)
        return out
else:
    unpack12 = _unpack12_numpy


def test_pattern(k0, n):
    """iqstream -t's samples k0 .. k0+n-1, as int16 I, Q ..."""
    k = np.arange(k0, k0 + n, dtype=np.int64)
    out = np.empty(2 * n, np.int16)
    out[0::2] = ((k * 7) & 0xFFF) - 2048
    out[1::2] = ((k * 13 + 1000) & 0xFFF) - 2048
    return out


# ------------------------------------------------------------------ the radio over SSH

class RadioShell:
    """Shell commands on the Pluto over SSH: key login, else sshpass with the password."""

    OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5"]

    def __init__(self, host, password="analog"):
        self.host, self.pre = host, []
        key = subprocess.run(["ssh", *self.OPTS, "-o", "BatchMode=yes", f"root@{host}", "true"],
                             capture_output=True, text=True)
        if key.returncode != 0:
            if not shutil.which("sshpass"):
                raise OSError("can't log into the radio with a key, and sshpass isn't installed "
                              "(bash pi/setup.sh)")
            self.pre = ["sshpass", "-p", password]
            if self.run("true").returncode != 0:
                raise OSError(f"can't log into root@{host} (password {password!r}?)")

    def cmd(self, command):
        return [*self.pre, "ssh", *self.OPTS, f"root@{self.host}", command]

    def run(self, command, timeout=60, stdin=None):
        return subprocess.run(self.cmd(command), input=stdin, capture_output=True, timeout=timeout)

    def text(self, command, timeout=60):
        return self.run(command, timeout).stdout.decode(errors="replace")

    def deploy(self, local=BINARY, remote=REMOTE):
        """Copy the binary to the radio's RAM unless the same one is already there."""
        if not local.exists():
            raise OSError(f"{local} not built yet: run pi/build_iqstream.sh")
        data = local.read_bytes()
        want = hashlib.md5(data).hexdigest()
        have = self.text(f"md5sum {remote} 2>/dev/null").split()
        if have and have[0] == want:
            return False
        r = self.run(f"cat > {remote}.new && chmod +x {remote}.new && mv {remote}.new {remote}",
                     timeout=120, stdin=data)
        if r.returncode != 0:
            raise OSError(f"couldn't copy iqstream to the radio: {r.stderr.decode()[:200]}")
        return True


# ------------------------------------------------------------------ receiver

def _recv_exact(sock, view):
    got = 0
    while got < len(view):
        n = sock.recv_into(view[got:])
        if not n:
            return False
        got += n
    return True


class PackedPluto:
    """The Pluto's RX1 through iqstream: int16 I/Q from read(), like atcsdr.pluto.Pluto.

    buffer_size is the DMA block / chunk size in samples (524288 by default:
    about 24 ms at 21.6 MSPS). conns = TCP connections (1-2), each from its own pinned thread
    on the radio; cpus = iqstream's -c (reader,sender0,sender1); capture =
    "mmap" (the DMA blocks, no copy) or "read"; copy_first copies each block to
    cached memory before packing; sndbuf sets the radio's socket send buffer
    (bytes, 0 = the kernel's default); piece caps each send() call (bytes, 0 =
    a whole chunk); irq pins the radio's Ethernet interrupt to that core.
    Any of these can also come from the address, for scripts that only take a
    --device: packed:192.168.3.2?piece=65536&irq=0. For tests,
    test_rate (MSPS) makes iqstream send its test pattern instead of the radio's
    samples, and local=True runs build/iqstream-native on this computer."""

    def __init__(self, uri, center, rate, gain="40", bandwidth=None, buffer_size=1 << 19,
                 conns=1, bits=16, cpus=None, password="analog", port=PORT,
                 test_rate=None, local=False, capture="mmap", copy_first=False, sndbuf=4 << 20,
                 piece=0, irq=None):
        base, _, query = uri.partition("?")
        self.uri, self.host = uri, base.split(":", 1)[1]
        opts = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        conns = int(opts.get("conns", conns))
        bits = int(opts.get("bits", bits))
        buffer_size = int(opts.get("block", buffer_size))
        sndbuf = int(opts.get("sndbuf", sndbuf))
        piece = int(opts.get("piece", piece))
        irq = int(opts["irq"]) if "irq" in opts else irq
        cpus = opts.get("cpus", cpus)
        self.center, self.rate, self.gain = center, int(rate), gain
        self.bandwidth = int(bandwidth or rate)
        self.samples, self.conns, self.bits, self.cpus = buffer_size, conns, bits, cpus
        self.password, self.port, self.test_rate, self.local = password, port, test_rate, local
        self.capture, self.copy_first, self.sndbuf = capture, copy_first, sndbuf
        self.piece, self.irq = piece, irq
        self.stats = dict(chunks=0, lost=0)
        self.shell = self.ctx = self._lo = self._rx_phy = self._proc = None
        self._socks, self._threads = [], []
        self._open()

    # -- setup

    def _args(self):
        a = f"-p {self.port} -n {self.conns} -b {self.samples} -s {SLOTS} -f {self.bits} -m {self.capture}"
        if self.copy_first:
            a += " -k"
        if self.sndbuf:
            a += f" -w {self.sndbuf}"
        if self.piece:
            a += f" -S {self.piece}"
        if self.irq is not None:
            a += f" -i {self.irq}"
        if self.cpus:
            a += f" -c {self.cpus}"
        if self.test_rate is not None:
            a += f" -t -r {self.test_rate}"
        return a

    def _open(self):
        self._next, self._skip_until = 0, -1
        self._stop = threading.Event()
        if self.local:
            if not NATIVE.exists():
                raise OSError(f"{NATIVE} not built yet: run pi/build_iqstream.sh")
            self._proc = subprocess.Popen([str(NATIVE), *self._args().split()],
                                          stderr=subprocess.PIPE, text=True)
        else:
            if self.test_rate is None:                  # the real radio: configure it through iiod
                self.ctx = pluto._iio().Context(f"ip:{self.host}")
                self._lo, self._rx_phy = pluto.configure_rx(self.ctx, self.rate, self.bandwidth, self.gain)
                self.tune(self.center)
            self.shell = self.shell or RadioShell(self.host, self.password)
            self.shell.deploy()
            self.shell.run(f"killall iqstream 2>/dev/null; sleep 0.2; "
                           f"( {REMOTE} {self._args()} </dev/null >/tmp/iqstream.log 2>&1 & )")
        deadline = time.time() + 10
        while len(self._socks) < self.conns:
            try:
                s = socket.create_connection((self.host, self.port), timeout=5)
            except OSError:
                if time.time() > deadline:
                    self.close()
                    raise OSError(f"iqstream on {self.host}:{self.port} didn't answer"
                                  + (f" ({self.log()[-200:]})" if not self.local else ""))
                time.sleep(0.2)
                continue
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            s.settimeout(None)
            self._socks.append(s)
        # One queue per connection: each connection's chunks arrive in order, so
        # the next chunk is always at the head of one of them.
        self._queues = [queue.Queue(maxsize=64) for _ in self._socks]
        self._heads = [None] * len(self._socks)
        for s, q in zip(self._socks, self._queues):
            t = threading.Thread(target=self._receive, args=(s, q), daemon=True)
            t.start()
            self._threads.append(t)

    def _receive(self, sock, q):
        head = bytearray(HEADER.size)
        try:
            while not self._stop.is_set():
                if not _recv_exact(sock, memoryview(head)):
                    break
                magic, seq, samples, flags, lost, t_ns = HEADER.unpack(head)
                if magic != MAGIC:
                    raise OSError(f"bad chunk header {magic:#x}")
                payload = bytearray(samples * (3 if flags & F_PACKED12 else 4))
                if not _recv_exact(sock, memoryview(payload)):
                    break
                q.put((seq, flags, lost, t_ns, payload))
        except OSError:
            pass
        q.put(_CLOSED)

    # -- the Pluto interface

    def read(self):
        """The next chunk, in order, as interleaved int16 I/Q (12-bit values)."""
        while True:
            seq, flags, lost, t_ns, payload = self._take()
            self._next += 1
            self.stats["chunks"] += 1
            self.stats["lost"] += lost
            self.last_seq, self.last_t_ns = seq, t_ns      # for checks of the test pattern
            if seq <= self._skip_until:
                continue                              # from before a retune / flush
            if flags & F_PACKED12:
                return unpack12(payload)
            return np.frombuffer(payload, np.int16).copy()

    def _take(self):
        """The chunk numbered self._next, from whichever connection has it."""
        heads, deadline = self._heads, time.time() + 10
        while True:
            for i, q in enumerate(self._queues):
                if heads[i] is None:
                    try:
                        heads[i] = q.get_nowait()
                    except queue.Empty:
                        pass
                if heads[i] is _CLOSED:
                    raise OSError("iqstream connection closed")
            for i, h in enumerate(heads):
                if h is not None and h[0] == self._next:
                    heads[i] = None
                    return h
            if all(h is not None for h in heads):    # every connection is past it: skip it
                i = min(range(len(heads)), key=lambda j: heads[j][0])
                h, heads[i] = heads[i], None
                self._next = h[0]
                return h
            missing = next(i for i, h in enumerate(heads) if h is None)
            try:
                heads[missing] = self._queues[missing].get(timeout=0.05)
            except queue.Empty:
                if time.time() > deadline:
                    raise OSError("no samples from iqstream for 10 s") from None

    def set_gain(self, gain):
        if self._rx_phy is not None:
            pluto.set_gain(self._rx_phy, gain)
        self.gain = gain

    def tune(self, center):
        if self._lo is not None:
            self._lo.attrs["frequency"].value = str(int(center))
        self.center = center
        self.flush()

    def flush(self):
        """Skip what's already captured: iqstream's ring and kernel buffer, plus
        what's in flight."""
        self._skip_until = self._next + SLOTS + KERNEL_CHUNKS + 2

    def log(self):
        """iqstream's own log (radio side)."""
        if self.local and self._proc:
            return ""
        try:
            return self.shell.text("cat /tmp/iqstream.log 2>/dev/null", timeout=10) if self.shell else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def close(self):
        self._stop.set()
        for s in self._socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        self._socks, self._threads = [], []
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._proc = None
        self.ctx = self._lo = self._rx_phy = None

    def reconnect(self, stop):
        """Restart iqstream and reconnect, every second until it works or `stop`
        is set. Returns how long the stream was gone (s), or None."""
        self.close()
        down = time.time()
        while not stop.is_set():
            time.sleep(1)
            try:
                self._open()
                return time.time() - down
            except OSError:
                self.close()
        return None
