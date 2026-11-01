"""ADALM-PLUTO (and AD936x clones) through libiio's Python bindings.

GNU Radio's Pluto source tops out near 6.5 MSPS on this machine, so the
multichannel and ADS-B tools read the Pluto directly with libiio instead.
"""

import gc
import sys
import time

import numpy as np

USB_MAX_RATE = 8_160_000     # sustained over USB 2.0 here (measured; 10 MSPS drops ~16%)
NET_MAX_RATE = 12_000_000     # over Ethernet (an ip: URI), measured: 100% to 12 MSPS; above that
                              # the board's network server levels off at ~14-17.5 MSPS
GAIN_MODES = {"auto": "slow_attack", "fast": "fast_attack"}


def _iio():
    try:
        import iio
    except ImportError:
        sys.exit("libiio Python bindings not found (conda install -c conda-forge libiio).")
    return iio


def find_uri(device=""):
    """`device` if given, else the first Pluto libiio can see (USB first)."""
    if device:
        return device
    iio = _iio()
    found = [u for u, d in iio.scan_contexts().items()
             if "PlutoSDR" in d or "ADALM-PLUTO" in d or "ad936" in d.lower()]
    return found[0] if found else FALLBACK_URI


USB_NETWORK = "ip:192.168.2."   # the Pluto's network-over-USB address: still a USB link
FALLBACK_URI = "ip:192.168.2.1"


PACKED_MAX_RATE = 21_600_000  # packed:<address>, via iqstream: the whole band, measured in full


def is_packed(uri):
    return uri.startswith("packed:")


def is_ethernet(uri):
    return is_packed(uri) or (uri.startswith("ip:") and not uri.startswith(USB_NETWORK))


def max_rate(uri):
    """Highest sample rate the link to this Pluto can carry."""
    if is_packed(uri):
        return PACKED_MAX_RATE
    return NET_MAX_RATE if is_ethernet(uri) else USB_MAX_RATE


def to_complex(raw):
    """Interleaved int16 I/Q -> complex64 scaled to +-1, without an extra copy."""
    return raw.astype(np.float32).view(np.complex64) * np.float32(1 / 2048)


class Pluto:
    """One receive channel (RX1) of a Pluto, streaming int16 I/Q.

    read() raises OSError when the USB link drops. Callers should leave their
    `except` block before calling reconnect(): the exception's traceback still
    references the old connection, which keeps the USB interface claimed."""

    def __init__(self, uri, center, rate, gain="40", bandwidth=None,
                 buffer_size=1 << 19, kernel_buffers=8):
        self.uri, self.center, self.rate, self.gain = uri, center, int(rate), gain
        self.bandwidth = int(bandwidth or rate)
        self.buffer_size, self.kernel_buffers = buffer_size, kernel_buffers
        self.ctx = self.buf = self._lo = self._rx_phy = None
        self._open()

    def _open(self):
        iio = _iio()
        self.ctx = iio.Context(self.uri)
        rx = self.ctx.find_device("cf-ad9361-lpc")
        self._lo, self._rx_phy = configure_rx(self.ctx, self.rate, self.bandwidth, self.gain)
        for name in ("voltage0", "voltage1"):
            rx.find_channel(name, False).enabled = True
        rx.set_kernel_buffers_count(self.kernel_buffers)
        self.buf = iio.Buffer(rx, self.buffer_size, False)
        self.tune(self.center)

    def set_gain(self, gain):
        """A gain in dB, or one of GAIN_MODES."""
        set_gain(self._rx_phy, gain)
        self.gain = gain

    def tune(self, center):
        self._lo.attrs["frequency"].value = str(int(center))
        self.center = center

    def flush(self):
        """Discard the samples already queued (e.g. from before a retune)."""
        for _ in range(self.kernel_buffers + 1):
            self.buf.refill()

    def read(self):
        """The next buffer of interleaved int16 I/Q."""
        self.buf.refill()
        return np.frombuffer(self.buf.read(), dtype=np.int16).copy()

    def close(self):
        self.buf = self.ctx = self._lo = self._rx_phy = None
        gc.collect()

    def reconnect(self, stop):
        """Reopen after a USB error, retrying every second until it works or
        `stop` is set. Returns how long the Pluto was gone (s), or None."""
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


def configure_rx(ctx, rate, bandwidth, gain):
    """Set RX1's sample rate, RF bandwidth and gain. Returns (LO channel, RX channel)."""
    phy = ctx.find_device("ad9361-phy")
    ch = phy.find_channel("voltage0", False)
    ch.attrs["sampling_frequency"].value = str(int(rate))
    ch.attrs["rf_bandwidth"].value = str(int(bandwidth))
    set_gain(ch, gain)
    return phy.find_channel("altvoltage0", True), ch


def set_gain(ch, gain):
    """A gain in dB, or one of GAIN_MODES, on an RX channel."""
    if gain in GAIN_MODES:
        ch.attrs["gain_control_mode"].value = GAIN_MODES[gain]
    else:
        ch.attrs["gain_control_mode"].value = "manual"
        ch.attrs["hardwaregain"].value = str(float(gain))


def make(uri, center, rate, gain="40", **kwargs):
    """The radio behind `uri`: packed:<address> streams through iqstream (12-bit,
    see atcsdr.iqstream); anything else through libiio / iiod. Raises OSError."""
    if is_packed(uri):
        from .iqstream import PackedPluto
        kwargs.pop("kernel_buffers", None)
        return PackedPluto(uri, center, rate, gain, **kwargs)
    return Pluto(uri, center, rate, gain, **kwargs)


def open_pluto(uri, center, rate, gain="40", **kwargs):
    """A Pluto, or exit with a hint if it can't be opened."""
    try:
        return make(uri, center, rate, gain, **kwargs)
    except OSError as e:
        sys.exit(f"Could not open Pluto at {uri}: {e}\nTry `iio_info -s` to list devices. "
                 "Only one program can use the Pluto at a time.")
