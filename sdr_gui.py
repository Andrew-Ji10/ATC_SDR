#!/usr/bin/env python3
"""
Qt GUI for sdr_receiver: live spectrum + waterfall of a band, click-to-tune,
signal meter, audio on/off, volume / squelch / gain / mode controls.

    python3 sdr_gui.py 118.6                 # airband around 118.6 MHz
    python3 sdr_gui.py 98.5 --span 3.84      # FM broadcast, wider view

Tuning:
    - click anywhere on the spectrum or waterfall, or drag the yellow marker
    - type / scroll the frequency box (step follows the mode's channel spacing)
    - set "Band center" + "Span" to choose which slice of spectrum is displayed;
      tuning outside the band recenters it automatically
"""

import argparse
import signal
import sys
import time

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

import sdr_receiver as sr

# Frequency box step (MHz) and click-to-tune snap (Hz) per mode.
MODE_STEP_MHZ = {"wfm": 0.1, "am": 0.025, "nbfm": 0.0125}
MODE_SNAP_HZ = {"wfm": 100e3, "am": 2.5e3, "nbfm": 2.5e3}
# Width of the shaded channel region on the spectrum (matches the demod filters).
MODE_BW_HZ = {"wfm": 200e3, "am": 10e3, "nbfm": 16e3}

WATERFALL_ROWS = 300
# If the spectrum hasn't changed for this long, assume the SDR stream died
# (e.g. a USB dropout) and rebuild the flowgraph.
STALL_SECONDS = 3.0
DB_RANGE = (-110, -20)
METER_RANGE = (-120, 0)
SQUELCH_SLIDER_MIN = -120  # slider at minimum == squelch off
GAIN_MAX = {"pluto": 73, "osmosdr": 50}

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.rx = None
        # After a dropout: (freq, span) to keep retrying, and when to try next.
        self.retry_target = None
        self.retry_at = 0.0
        self.waterfall = np.full((WATERFALL_ROWS, sr.FFT_SIZE), DB_RANGE[0], dtype=np.float32)
        self.setWindowTitle("ATC SDR")
        self.resize(1200, 760)

        self._build_plots()
        self._build_controls()

        central = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(central)
        layout.addWidget(self.plots, stretch=1)
        layout.addWidget(self.controls)
        self.setCentralWidget(central)

        freq = args.freq * 1e6
        sr.warn_pluto_range(args, freq)
        self.freq_spin.blockSignals(True)
        self.freq_spin.setValue(args.freq)
        self.freq_spin.blockSignals(False)
        # Open the SDR once the window is on screen, so errors have somewhere to show.
        QtCore.QTimer.singleShot(0, lambda: self._open(freq, round(args.span * 1e6)))

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(int(1000 / sr.FFT_FPS))

    # --- Layout ---------------------------------------------------------
    def _build_plots(self):
        self.plots = pg.GraphicsLayoutWidget()

        self.spec_plot = self.plots.addPlot(row=0, col=0)
        self.spec_plot.setLabel("left", "Power", units="dBFS")
        self.spec_plot.showGrid(x=True, y=True, alpha=0.3)
        self.spec_plot.setYRange(*DB_RANGE)
        self.spec_plot.setMouseEnabled(x=False, y=True)
        self.spec_plot.hideButtons()
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen("#4fc3f7", width=1))

        self.chan_region = pg.LinearRegionItem(movable=False,
                                               brush=pg.mkBrush(255, 213, 79, 50))
        self.chan_region.setZValue(-10)
        self.spec_plot.addItem(self.chan_region)
        self.tune_line = pg.InfiniteLine(angle=90, movable=True,
                                         pen=pg.mkPen("#ffd54f", width=2))
        self.tune_line.sigPositionChangeFinished.connect(
            lambda line: self.tune(line.value() * 1e6, snap=True))
        self.spec_plot.addItem(self.tune_line)

        self.wf_plot = self.plots.addPlot(row=1, col=0)
        self.wf_plot.setLabel("bottom", "Frequency", units="MHz")
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_plot.setMouseEnabled(x=False, y=False)
        # Same left-axis width on both plots so their frequency axes line up.
        self.wf_plot.getAxis("left").setStyle(showValues=False)
        self.wf_plot.getAxis("left").setTicks([])
        for plot in (self.spec_plot, self.wf_plot):
            plot.getAxis("left").setWidth(60)
        self.wf_plot.hideButtons()
        self.wf_plot.invertY(True)
        self.wf_image = pg.ImageItem(self.waterfall, levels=DB_RANGE)
        self.wf_image.setLookupTable(pg.colormap.get("viridis").getLookupTable(nPts=256))
        self.wf_plot.addItem(self.wf_image)
        self.wf_line = pg.InfiniteLine(angle=90, movable=False,
                                       pen=pg.mkPen("#ffd54f", width=1))
        self.wf_plot.addItem(self.wf_line)

        self.plots.ci.layout.setRowStretchFactor(0, 2)
        self.plots.ci.layout.setRowStretchFactor(1, 3)
        self.plots.scene().sigMouseClicked.connect(self._on_plot_click)

    def _build_controls(self):
        self.controls = QtWidgets.QWidget()
        self.controls.setFixedWidth(300)
        col = QtWidgets.QVBoxLayout(self.controls)

        # Tuning
        tune_box = QtWidgets.QGroupBox("Tuning")
        form = QtWidgets.QFormLayout(tune_box)
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setRange(24.0, 6000.0)
        self.freq_spin.setDecimals(4)
        self.freq_spin.setSuffix(" MHz")
        self.freq_spin.setKeyboardTracking(False)
        self.freq_spin.valueChanged.connect(lambda mhz: self.tune(mhz * 1e6))
        form.addRow("Frequency", self.freq_spin)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["auto", *sr.MODES])
        self.mode_combo.setCurrentText(self.args.mode)
        self.mode_combo.currentTextChanged.connect(lambda _: self._apply_mode())
        form.addRow("Mode", self.mode_combo)
        col.addWidget(tune_box)

        # Band
        band_box = QtWidgets.QGroupBox("Band (displayed spectrum)")
        form = QtWidgets.QFormLayout(band_box)
        self.center_spin = QtWidgets.QDoubleSpinBox()
        self.center_spin.setRange(24.0, 6000.0)
        self.center_spin.setDecimals(3)
        self.center_spin.setSuffix(" MHz")
        form.addRow("Center", self.center_spin)
        self.span_combo = QtWidgets.QComboBox()
        for s in sr.SPANS:
            self.span_combo.addItem(f"{s / 1e6:g} MHz", s)
        self.span_combo.setCurrentIndex(sr.SPANS.index(round(self.args.span * 1e6)))
        form.addRow("Span", self.span_combo)
        set_band = QtWidgets.QPushButton("Set band")
        set_band.clicked.connect(self._set_band)
        form.addRow(set_band)
        self.peak_label = QtWidgets.QLabel("Strongest: –")
        self.peak_label.setWordWrap(True)
        form.addRow(self.peak_label)
        col.addWidget(band_box)

        # Signal
        sig_box = QtWidgets.QGroupBox("Signal (tuned channel)")
        vbox = QtWidgets.QVBoxLayout(sig_box)
        self.meter = QtWidgets.QProgressBar()
        self.meter.setRange(*METER_RANGE)
        self.meter.setFormat("%v dB")
        vbox.addWidget(self.meter)
        self.sq_led = QtWidgets.QLabel()
        vbox.addWidget(self.sq_led)
        col.addWidget(sig_box)

        # Audio
        audio_box = QtWidgets.QGroupBox("Audio")
        form = QtWidgets.QFormLayout(audio_box)
        self.audio_btn = QtWidgets.QPushButton()
        self.audio_btn.setCheckable(True)
        self.audio_btn.setChecked(True)
        self.audio_btn.toggled.connect(self._set_audio)
        self._set_audio(True)
        form.addRow(self.audio_btn)
        self.vol_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.vol_slider.setRange(0, 300)
        self.vol_slider.setValue(int(self.args.volume * 100))
        self.vol_slider.valueChanged.connect(lambda v: self.rx and self.rx.set_volume(v / 100))
        form.addRow("Volume", self.vol_slider)
        self.sq_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sq_slider.setRange(SQUELCH_SLIDER_MIN, 0)
        self.sq_slider.setValue(max(SQUELCH_SLIDER_MIN, int(self.args.squelch)))
        self.sq_value = QtWidgets.QLabel()
        self.sq_slider.valueChanged.connect(self._set_squelch)
        form.addRow("Squelch", self.sq_slider)
        form.addRow("", self.sq_value)
        col.addWidget(audio_box)

        # RF
        rf_box = QtWidgets.QGroupBox("RF")
        form = QtWidgets.QFormLayout(rf_box)
        self.agc_check = QtWidgets.QCheckBox("Automatic gain (AGC)")
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gain_slider.setRange(0, GAIN_MAX[self.args.sdr])
        self.gain_label = QtWidgets.QLabel()
        if self.args.gain in sr.PLUTO_GAIN_MODES:
            self.agc_check.setChecked(True)
            self.gain_slider.setValue(30)
        else:
            self.gain_slider.setValue(int(float(self.args.gain)))
        self.gain_slider.valueChanged.connect(lambda _: self._apply_gain())
        self.agc_check.toggled.connect(lambda _: self._apply_gain())
        form.addRow(self.agc_check)
        form.addRow("Gain", self.gain_slider)
        form.addRow("", self.gain_label)
        self.connect_btn = QtWidgets.QPushButton("Connect SDR")
        self.connect_btn.clicked.connect(
            lambda: self._open(self.freq_spin.value() * 1e6, self.span_combo.currentData()))
        form.addRow(self.connect_btn)
        col.addWidget(rf_box)

        col.addStretch(1)

    # --- Receiver lifecycle ---------------------------------------------
    def _open(self, freq, span, quiet=False):
        """(Re)build the flowgraph. Needed at startup and when the span changes."""
        self._close()
        self.last_data = time.monotonic()
        self.last_db = None
        mode = self._mode_for(freq)
        self.args.span = span / 1e6
        try:
            self.rx = sr.open_receiver(self.args, freq, mode, spectrum=True)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            self.statusBar().showMessage("Not connected. Plug in the SDR, then click Connect SDR.")
            self.connect_btn.setText("Connect SDR")
            if not quiet:
                QtWidgets.QMessageBox.critical(self, "SDR error", str(e))
            return
        self.connect_btn.setText("Reconnect SDR")
        self.rx.set_muted(not self.audio_btn.isChecked())
        self.rx.set_volume(self.vol_slider.value() / 100)
        self._set_squelch(self.sq_slider.value())
        self._apply_gain()
        self.rx.start()
        self._sync_tuning()

    def _close(self):
        if self.rx is not None:
            self.rx.stop()
            self.rx.wait()
            # Drop the reference so the SDR device is released before reopening.
            self.rx = None

    def closeEvent(self, event):
        self.timer.stop()
        self._close()
        super().closeEvent(event)

    # --- Actions --------------------------------------------------------
    def _mode_for(self, freq):
        choice = self.mode_combo.currentText()
        return sr.auto_mode(freq) if choice == "auto" else choice

    def tune(self, freq, snap=False):
        if self.rx is None:
            return
        mode = self._mode_for(freq)
        if snap:
            step = MODE_SNAP_HZ[mode]
            freq = round(freq / step) * step
        if mode != self.rx.mode:
            self.rx.set_mode(mode)
        self.rx.set_freq(freq)
        self._sync_tuning()

    def _apply_mode(self):
        if self.rx is not None:
            self.tune(self.rx.freq)

    def _set_band(self):
        if self.rx is None:
            return
        center = self.center_spin.value() * 1e6
        span = self.span_combo.currentData()
        if span != self.rx.span:
            freq = self.rx.freq
            self._open(freq, span)
            if self.rx is None:
                return
        self.rx.set_band(center)
        self._sync_tuning()

    def _set_audio(self, on):
        self.audio_btn.setText("🔊  Audio ON" if on else "🔇  Audio OFF")
        if self.rx is not None:
            self.rx.set_muted(not on)

    def _set_squelch(self, value):
        off = value <= SQUELCH_SLIDER_MIN
        self.sq_value.setText("off" if off else f"{value} dB")
        if self.rx is not None:
            self.rx.set_squelch(sr.SQUELCH_OFF if off else float(value))

    def _apply_gain(self):
        agc = self.agc_check.isChecked()
        self.gain_slider.setEnabled(not agc)
        self.gain_label.setText("auto" if agc else f"{self.gain_slider.value()} dB")
        if self.rx is not None:
            self.rx.set_gain("auto" if agc else self.gain_slider.value())

    def _on_plot_click(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        for plot in (self.spec_plot, self.wf_plot):
            if plot.sceneBoundingRect().contains(ev.scenePos()):
                x = plot.vb.mapSceneToView(ev.scenePos()).x()
                self.tune(x * 1e6, snap=True)
                return

    # --- Display --------------------------------------------------------
    def _sync_tuning(self):
        """Push receiver state (freq, band, mode) into the widgets."""
        rx = self.rx
        f_mhz = rx.freq / 1e6
        for spin, value in ((self.freq_spin, f_mhz), (self.center_spin, rx.lo / 1e6)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.freq_spin.setSingleStep(MODE_STEP_MHZ[rx.mode])
        self.sq_slider.setEnabled(rx.mode != "wfm")

        lo_mhz, hi_mhz = (f / 1e6 for f in rx.band())
        self.spec_plot.setXRange(lo_mhz, hi_mhz, padding=0)
        self.wf_image.setRect(QtCore.QRectF(lo_mhz, 0, hi_mhz - lo_mhz, WATERFALL_ROWS))
        self.tune_line.setValue(f_mhz)
        self.wf_line.setValue(f_mhz)
        half_bw = MODE_BW_HZ[rx.mode] / 2e6
        self.chan_region.setRegion((f_mhz - half_bw, f_mhz + half_bw))
        self.statusBar().showMessage(
            f"{rx.mode.upper()}  ·  LO {rx.lo / 1e6:.4f} MHz  ·  "
            f"span {rx.span / 1e6:g} MHz  ·  {self.args.sdr}")

    def _stalled(self, db):
        """True once no new FFT frames have arrived for STALL_SECONDS."""
        now = time.monotonic()
        if db is not None and (self.last_db is None or not np.array_equal(db, self.last_db)):
            self.last_db = db
            self.last_data = now
        return now - self.last_data > STALL_SECONDS

    def _reconnect(self):
        self.retry_at = time.monotonic() + STALL_SECONDS
        self._open(*self.retry_target, quiet=True)
        if self.rx is not None:
            self.retry_target = None
            self.statusBar().showMessage(
                "SDR stream dropped and was reconnected (check USB power / cable)", 5000)
        else:
            self.statusBar().showMessage("SDR stream dropped, retrying...")

    def _refresh(self):
        if self.rx is None:
            if self.retry_target and time.monotonic() >= self.retry_at:
                self._reconnect()
            return
        spectrum = self.rx.spectrum_db()
        if self._stalled(spectrum[1] if spectrum else None):
            print("SDR stream stalled, reconnecting...", file=sys.stderr)
            self.retry_target = (self.rx.freq, self.rx.span)
            self._reconnect()
            return
        if spectrum is None:
            return
        freqs, db = spectrum
        mhz = freqs / 1e6
        self.spec_curve.setData(mhz, db)

        self.waterfall = np.roll(self.waterfall, 1, axis=0)
        self.waterfall[0] = db
        self.wf_image.setImage(self.waterfall, autoLevels=False,
                               levels=self.spec_plot.viewRange()[1])

        # Strongest signal in the band, ignoring the DC / LO-leakage bins.
        masked = db.copy()
        center = sr.FFT_SIZE // 2
        guard = max(2, int(sr.DC_GUARD / (self.rx.span / sr.FFT_SIZE)))
        masked[center - guard:center + guard] = -np.inf
        peak = int(np.argmax(masked))
        self.peak_label.setText(f"Strongest: {mhz[peak]:.4f} MHz, {db[peak]:.0f} dB")

        power = self.rx.power_db()
        self.meter.setValue(int(np.clip(power, *METER_RANGE)))
        if self.rx.mode == "wfm":
            self.sq_led.setText("Squelch: n/a (WFM)")
        elif self.rx.squelch_open():
            self.sq_led.setText("<b style='color:#43a047'>● Squelch OPEN</b>")
        else:
            self.sq_led.setText("<span style='color:#9e9e9e'>○ Squelch closed</span>")


def main():
    p = argparse.ArgumentParser(description="GUI receiver for broadcast FM and airband ATC.")
    p.add_argument("freq", type=float, nargs="?", default=118.6,
                   help="starting frequency in MHz (default 118.6)")
    sr.add_sdr_args(p)
    args = p.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    # Let Ctrl-C in the terminal close the window.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    win = MainWindow(args)
    win.show()
    # Apps launched from a terminal on macOS otherwise open behind other windows.
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
