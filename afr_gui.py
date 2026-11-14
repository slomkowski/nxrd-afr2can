#!/usr/bin/env python3
"""Live AFR / lambda display for a rusEFI/mck1117 wideband, on top of afr_logger.

Starts the compiled ./afr_logger (see afr_logger.c, built by `make`) as a child
process, reads its CSV from stdout and shows:
  * current AFR and lambda (big readouts) with a rich/lean indication,
  * a scrolling AFR chart (left axis AFR, right axis lambda),
  * sensor temperature and diagnostics (ESR, Nernst, pump, heater, status).

Lambda comes straight from the controller; AFR = lambda * stoichiometric ratio,
computed here so the fuel type can be changed live (it also re-scales history).

    ./afr_gui.py                    # start logging immediately
    ./afr_gui.py --demo             # synthetic data, no cable needed
    ./afr_gui.py --baud 500000 --no-log

Requires: PySide6, pyqtgraph, numpy.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import math
import os
from pathlib import Path
import random
import signal
import sys
import time

import numpy as np

# pyqtgraph picks a Qt binding on import (PyQt6 is preferred when both are
# installed). Mixing PyQt6 and PySide6 in one process segfaults, so pin it to
# PySide6, which the rest of this file uses, before pyqtgraph is imported.
os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
import pyqtgraph as pg  # noqa: E402

HEADER = "time_s,controller,valid,lambda,afr,temp_c,esr_ohm,nernst_v,pump_pct,heater_pct,status"

FUELS = [
    ("Gasoline", 14.7),
    ("E10", 14.1),
    ("E85", 9.76),
    ("Diesel", 14.5),
]
WINDOWS = [("10 s", 10), ("30 s", 30), ("1 min", 60), ("5 min", 300), ("10 min", 600), ("All", None)]

C_RICH = "#ff9f43"
C_LEAN = "#4aa3ff"
C_OK = "#3ddc84"
C_DIM = "#6b7078"
C_BAD = "#ff5f5f"

STALE_AFTER_S = 1.5


# --------------------------------------------------------------------------- data


@dataclass
class Sample:
    t: float
    ctrl: int
    valid: bool
    lam: float
    temp: int
    esr: float | None = None
    nernst: float | None = None
    pump: float | None = None
    heater: float | None = None
    status: int | None = None


def parse_line(line: str) -> Sample | None:
    """Parse one afr_logger CSV line; None for the header or anything malformed."""
    line = line.strip()
    if not line or line.startswith("time_s"):
        return None
    f = line.split(",")
    if len(f) < 11:
        return None
    try:
        s = Sample(t=float(f[0]), ctrl=int(f[1]), valid=f[2] == "1",
                   lam=float(f[3]), temp=int(f[5]))
        if f[6]:
            s.esr = float(f[6])
            s.nernst = float(f[7])
            s.pump = float(f[8])
            s.heater = float(f[9])
            s.status = int(f[10], 16)
    except ValueError:
        return None
    return s


class Series:
    """Append-only time series with O(log n) windowing; invalid samples are NaN."""

    def __init__(self) -> None:
        self.n = 0
        self.t = np.empty(8192)
        self.lam = np.empty(8192)

    def add(self, t: float, lam: float) -> None:
        if self.n == len(self.t):
            self.t = np.resize(self.t, self.n * 2)
            self.lam = np.resize(self.lam, self.n * 2)
        self.t[self.n] = t
        self.lam[self.n] = lam
        self.n += 1

    def window(self, t_from: float | None) -> tuple[np.ndarray, np.ndarray]:
        i = 0 if t_from is None else int(np.searchsorted(self.t[:self.n], t_from))
        return self.t[i:self.n], self.lam[i:self.n]


# --------------------------------------------------------------------------- widgets


class Card(QtWidgets.QFrame):
    def __init__(self, title: str, size_px: int) -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(0)
        self.title = QtWidgets.QLabel(title)
        self.title.setStyleSheet(f"color:{C_DIM}; font-size:13px; font-weight:600;")
        self.value = QtWidgets.QLabel("--")
        self.value.setStyleSheet(f"font-size:{size_px}px; font-weight:700; font-family:monospace;")
        self.value.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.value.setMinimumWidth(int(size_px * 3.2))
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        self._size = size_px

    def set(self, text: str, color: str) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(
            f"font-size:{self._size}px; font-weight:700; font-family:monospace; color:{color};")


def apply_dark_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    R = QtGui.QPalette.ColorRole
    G = QtGui.QPalette.ColorGroup
    c = QtGui.QColor
    p = QtGui.QPalette()
    p.setColor(R.Window, c("#1b1e23"))
    p.setColor(R.WindowText, c("#d7dbe0"))
    p.setColor(R.Base, c("#14161a"))
    p.setColor(R.AlternateBase, c("#1b1e23"))
    p.setColor(R.Text, c("#d7dbe0"))
    p.setColor(R.Button, c("#2a2e35"))
    p.setColor(R.ButtonText, c("#d7dbe0"))
    p.setColor(R.ToolTipBase, c("#2a2e35"))
    p.setColor(R.ToolTipText, c("#d7dbe0"))
    p.setColor(R.Highlight, c("#2f6fdb"))
    p.setColor(R.HighlightedText, c("#ffffff"))
    p.setColor(G.Disabled, R.ButtonText, c("#6b7078"))
    p.setColor(G.Disabled, R.Text, c("#6b7078"))
    app.setPalette(p)
    app.setStyleSheet("QFrame#card { background:#23272e; border-radius:8px; }")


# --------------------------------------------------------------------------- window


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, binary: Path, baud: int, stoich: float, window_s: int | None,
                 log: bool, log_dir: Path, demo: bool) -> None:
        super().__init__()
        self.setWindowTitle("AFR logger" + (" (demo)" if demo else ""))
        self.resize(1100, 720)

        self.binary = binary
        self.baud = baud
        self.log_dir = log_dir
        self.demo = demo

        self.series: dict[int, Series] = {}
        self.latest: dict[int, Sample] = {}
        self.last_rx: dict[int, float] = {}
        self.dirty = False
        self.running = False
        self.user_stop = False
        self.last_err = ""
        self._buf = b""
        self._log_file = None
        self._log_path: Path | None = None
        self._demo_t0 = 0.0

        self._build_ui(stoich, window_s, log)

        self.proc = QtCore.QProcess(self)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_proc_error)

        self.demo_timer = QtCore.QTimer(self)
        self.demo_timer.setInterval(20)
        self.demo_timer.timeout.connect(self._demo_tick)

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(33)
        self.refresh_timer.timeout.connect(self._refresh)
        self.refresh_timer.start()

    # ---- UI ----

    def _build_ui(self, stoich: float, window_s: int | None, log: bool) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        v = QtWidgets.QVBoxLayout(root)
        v.setContentsMargins(12, 12, 12, 8)
        v.setSpacing(10)

        # controls
        bar = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_start.clicked.connect(self.toggle_run)
        self.btn_pause = QtWidgets.QPushButton("Pause chart")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip("Freeze the chart so you can pan/zoom it with the mouse. "
                                  "Data keeps being recorded.")
        self.btn_clear = QtWidgets.QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_data)
        self.cmb_ctrl = QtWidgets.QComboBox()
        self.cmb_ctrl.setToolTip("Wideband controller index (CAN id 0x190 + 2N)")
        self.cmb_ctrl.setMinimumContentsLength(13)
        self.cmb_ctrl.currentIndexChanged.connect(lambda _i: self._mark_dirty())

        self.cmb_fuel = QtWidgets.QComboBox()
        for name, ratio in FUELS:
            self.cmb_fuel.addItem(f"{name} ({ratio:g})", ratio)
        self.cmb_fuel.addItem("Custom", None)
        self.spin_stoich = QtWidgets.QDoubleSpinBox()
        self.spin_stoich.setRange(1.0, 40.0)
        self.spin_stoich.setDecimals(2)
        self.spin_stoich.setSingleStep(0.1)
        self.spin_stoich.setPrefix("Stoich AFR ")
        self.spin_stoich.setValue(stoich)
        self.cmb_fuel.currentIndexChanged.connect(self._fuel_chosen)
        self.spin_stoich.valueChanged.connect(self._stoich_changed)

        self.cmb_win = QtWidgets.QComboBox()
        for label, secs in WINDOWS:
            self.cmb_win.addItem(label, secs)
        idx = next((i for i, (_l, s) in enumerate(WINDOWS) if s == window_s), 1)
        self.cmb_win.setCurrentIndex(idx)
        self.cmb_win.currentIndexChanged.connect(lambda _i: self._mark_dirty())

        self.chk_log = QtWidgets.QCheckBox("Log to CSV")
        self.chk_log.setChecked(log)
        self.chk_log.toggled.connect(self._log_toggled)

        for w in (self.btn_start, self.btn_pause, self.btn_clear, QtWidgets.QLabel("Chart:"),
                  self.cmb_ctrl, self.cmb_fuel, self.spin_stoich, QtWidgets.QLabel("Window:"),
                  self.cmb_win, self.chk_log):
            bar.addWidget(w)
        bar.addStretch(1)
        v.addLayout(bar)

        # readouts
        row = QtWidgets.QHBoxLayout()
        self.card_afr = Card("AFR", 72)
        self.card_lam = Card("LAMBDA", 72)
        self.card_temp = Card("SENSOR TEMP", 40)
        self.card_state = Card("STATE", 32)
        for c in (self.card_afr, self.card_lam, self.card_temp, self.card_state):
            row.addWidget(c)
        v.addLayout(row)

        # chart
        pg.setConfigOptions(antialias=True, background="#14161a", foreground="#c8ccd2")
        self.plot = pg.PlotWidget()
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.25)
        pi.setLabel("left", "AFR")
        pi.setLabel("bottom", "Time", units="s")
        pi.showAxis("right")
        pi.getAxis("right").setLabel("Lambda")
        pi.setDownsampling(auto=True, mode="peak")
        pi.setClipToView(True)
        self.curve = pi.plot(pen=pg.mkPen("#4aa3ff", width=2), connect="finite")
        self.stoich_line = pg.InfiniteLine(
            pos=stoich, angle=0, pen=pg.mkPen("#8a8f98", width=1, style=QtCore.Qt.PenStyle.DashLine))
        pi.addItem(self.stoich_line)
        self.plot_item = pi
        v.addWidget(self.plot, stretch=1)
        self._apply_stoich(stoich)

        # diagnostics
        drow = QtWidgets.QHBoxLayout()
        self.diag = {}
        for key, title in (("esr", "ESR"), ("nernst", "Nernst"), ("pump", "Pump"),
                           ("heater", "Heater"), ("status", "Status")):
            lab = QtWidgets.QLabel(f"{title}: --")
            lab.setStyleSheet("font-family:monospace; font-size:14px;")
            lab.setMinimumWidth(130)
            self.diag[key] = (title, lab)
            drow.addWidget(lab)
        drow.addStretch(1)
        v.addLayout(drow)

        self.log_label = QtWidgets.QLabel("")
        self.statusBar().addPermanentWidget(self.log_label)
        self.statusBar().showMessage("Ready")

    # ---- fuel / stoich ----

    def _fuel_chosen(self, _i: int) -> None:
        ratio = self.cmb_fuel.currentData()
        if ratio is not None:
            self.spin_stoich.setValue(ratio)

    def _stoich_changed(self, value: float) -> None:
        match = next((i for i in range(self.cmb_fuel.count())
                      if self.cmb_fuel.itemData(i) is not None
                      and abs(self.cmb_fuel.itemData(i) - value) < 1e-9), None)
        self.cmb_fuel.blockSignals(True)
        self.cmb_fuel.setCurrentIndex(match if match is not None else self.cmb_fuel.count() - 1)
        self.cmb_fuel.blockSignals(False)
        self._apply_stoich(value)

    def _apply_stoich(self, value: float) -> None:
        self.stoich = value
        self.stoich_line.setValue(value)
        self.plot_item.getAxis("right").setScale(1.0 / value)
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        self.dirty = True

    # ---- run control ----

    def toggle_run(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.running:
            return
        if not self.demo and not os.access(self.binary, os.X_OK):
            msg = f"{self.binary} not found or not executable - build it first with `make`."
            self.statusBar().showMessage(msg)
            self._show_error("afr_logger not found", msg)
            return
        self.clear_data()
        self.user_stop = False
        self.last_err = ""
        self._buf = b""
        self.running = True
        self.btn_start.setText("Stop")
        if self.chk_log.isChecked():
            self._open_log()
        if self.demo:
            self._demo_t0 = time.monotonic()
            self.demo_timer.start()
            self.statusBar().showMessage("Demo mode: synthetic data")
        else:
            self.proc.start(str(self.binary), ["-b", str(self.baud)])
            self.statusBar().showMessage(f"Started {self.binary.name} at {self.baud:,} bit/s")

    def stop(self) -> None:
        if not self.running:
            return
        self.user_stop = True
        if self.demo:
            self.demo_timer.stop()
            self._finish("Stopped")
            return
        if self.proc.state() == QtCore.QProcess.ProcessState.NotRunning:
            return
        # afr_logger exits cleanly (closes the cable) on SIGINT, not on SIGTERM.
        try:
            os.kill(int(self.proc.processId()), signal.SIGINT)
        except OSError:
            self.proc.terminate()
        QtCore.QTimer.singleShot(2000, self._kill_if_running)

    def _kill_if_running(self) -> None:
        if self.proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.proc.kill()

    def _finish(self, message: str) -> None:
        self.running = False
        self.btn_start.setText("Start")
        self._close_log()
        self.statusBar().showMessage(message)
        self._mark_dirty()

    def clear_data(self) -> None:
        self.series.clear()
        self.latest.clear()
        self.last_rx.clear()
        self.cmb_ctrl.blockSignals(True)
        self.cmb_ctrl.clear()
        self.cmb_ctrl.blockSignals(False)
        self.curve.setData([], [])
        self._mark_dirty()

    # ---- process I/O ----

    def _on_stdout(self) -> None:
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            self._feed_line(raw.decode("ascii", "replace"))

    def _on_stderr(self) -> None:
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace").strip()
        if text:
            self.last_err = text.splitlines()[-1]
            self.statusBar().showMessage(self.last_err)

    def _on_finished(self, code: int, _status) -> None:
        if self.user_stop or code == 0:
            self._finish("Stopped")
            return
        detail = self.last_err or "no message"
        self._finish(f"afr_logger exited with code {code}: {detail}")
        text = f"afr_logger exited with code {code}:\n\n{detail}"
        if "BUSY" in detail or "busy" in detail:
            text += ("\n\nOnly one program can open the cable at a time. Close any running "
                     "afr_logger, wideband_probe.sh / wideband_flash.py or other afr_gui window, "
                     "then press Start.")
        elif "PassThruOpen" in detail:
            text += "\n\nCheck that the OpenPort cable is plugged in (without the microSD card)."
        self._show_error("Cannot read the wideband", text)

    def _on_proc_error(self, err) -> None:
        if err == QtCore.QProcess.ProcessError.FailedToStart:
            self._finish(f"Could not start {self.binary}")
            self._show_error("afr_logger failed to start",
                             f"Could not start {self.binary}:\n\n{self.proc.errorString()}")

    def _show_error(self, title: str, text: str) -> None:
        QtWidgets.QMessageBox.critical(self, title, text)

    # ---- data ----

    def _feed_line(self, line: str) -> None:
        s = parse_line(line)
        if s is None:
            return
        ser = self.series.get(s.ctrl)
        if ser is None:
            ser = self.series[s.ctrl] = Series()
            self.cmb_ctrl.blockSignals(True)
            self.cmb_ctrl.addItem(f"Controller {s.ctrl}", s.ctrl)
            self.cmb_ctrl.blockSignals(False)
        ser.add(s.t, s.lam if s.valid else float("nan"))
        self.latest[s.ctrl] = s
        self.last_rx[s.ctrl] = time.monotonic()
        self.dirty = True
        if self._log_file is not None:
            esr = "" if s.esr is None else f"{s.esr:.0f},{s.nernst:.3f},{s.pump:.1f},{s.heater:.1f},0x{s.status:02X}"
            self._log_file.write(
                f"{s.t:.3f},{s.ctrl},{int(s.valid)},{s.lam:.4f},{s.lam * self.stoich:.2f},{s.temp},"
                + (esr if esr else ",,,,") + "\n")

    def _selected(self) -> int | None:
        return self.cmb_ctrl.currentData() if self.cmb_ctrl.count() else None

    # ---- CSV log ----

    def _log_toggled(self, on: bool) -> None:
        if not self.running:
            return
        if on:
            self._open_log()
        else:
            self._close_log()

    def _open_log(self) -> None:
        if self._log_file is not None:
            return
        self._log_path = self.log_dir / datetime.now().strftime("afr_log_%Y%m%d_%H%M%S.csv")
        try:
            self._log_file = open(self._log_path, "w", buffering=1)
        except OSError as exc:
            self.statusBar().showMessage(f"Cannot write log: {exc}")
            self._log_path = None
            return
        self._log_file.write(HEADER + "\n")
        self.log_label.setText(f"Logging to {self._log_path.name}")

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
            self.log_label.setText(f"Saved {self._log_path.name}" if self._log_path else "")

    # ---- demo ----

    def _demo_tick(self) -> None:
        t = time.monotonic() - self._demo_t0
        temp = min(780, int(20 + t * 90))
        valid = temp >= 780 or t > 8.0
        lam = 1.0 + 0.12 * math.sin(t * 0.7) + 0.03 * math.sin(t * 4.1) + random.gauss(0, 0.004)
        lam = lam if valid else 0.0
        self._feed_line(
            f"{t:.3f},0,{int(valid)},{lam:.4f},{lam * 14.7:.2f},{temp},"
            f"{300 + int(random.gauss(0, 3))},{0.45 + random.gauss(0, 0.002):.3f},"
            f"{50 + 20 * math.sin(t):.1f},{35.0 if valid else 100.0:.1f},0x00")

    # ---- refresh ----

    def _refresh(self) -> None:
        sel = self._selected()
        s = self.latest.get(sel) if sel is not None else None
        age = time.monotonic() - self.last_rx[sel] if sel in self.last_rx else math.inf

        if not self.running:
            state, state_c = "STOPPED", C_DIM
        elif s is None or age > STALE_AFTER_S:
            state, state_c = "NO DATA", C_BAD
        elif not s.valid:
            state, state_c = "NOT VALID", C_RICH
        elif s.lam < 0.99:
            state, state_c = "RICH", C_RICH
        elif s.lam > 1.01:
            state, state_c = "LEAN", C_LEAN
        else:
            state, state_c = "STOICH", C_OK
        self.card_state.set(state, state_c)

        live = s is not None and s.valid and self.running and age <= STALE_AFTER_S
        if live:
            self.card_afr.set(f"{s.lam * self.stoich:5.2f}", state_c)
            self.card_lam.set(f"{s.lam:5.3f}", state_c)
        else:
            self.card_afr.set("--.--", C_DIM)
            self.card_lam.set("-.---", C_DIM)
        self.card_temp.set(f"{s.temp} °C" if s is not None else "-- °C",
                           C_DIM if s is None else "#d7dbe0")

        fmt = {"esr": "{:.0f} Ω", "nernst": "{:.3f} V", "pump": "{:.1f} %", "heater": "{:.1f} %",
               "status": "0x{:02X}"}
        for key, (title, lab) in self.diag.items():
            val = getattr(s, key, None) if s is not None else None
            lab.setText(f"{title}: " + ("--" if val is None else fmt[key].format(val)))

        if not self.dirty or self.btn_pause.isChecked():
            return
        self.dirty = False
        ser = self.series.get(sel) if sel is not None else None
        if ser is None or ser.n == 0:
            return
        win = self.cmb_win.currentData()
        t_last = float(ser.t[ser.n - 1])
        x, lam = ser.window(None if win is None else t_last - win)
        y = lam * self.stoich
        self.curve.setData(x, y)

        if win is None:
            self.plot_item.enableAutoRange(axis="x")
        else:
            # Fill left-to-right until the window is full, then scroll.
            t0 = float(ser.t[0])
            x_lo = t0 if t_last - t0 < win else t_last - win
            self.plot_item.setXRange(x_lo, x_lo + win, padding=0)
        finite = y[np.isfinite(y)]
        lo = min(float(finite.min()), self.stoich) if finite.size else self.stoich - 3
        hi = max(float(finite.max()), self.stoich) if finite.size else self.stoich + 3
        span = max(hi - lo, self.stoich * 0.2)
        mid = (hi + lo) / 2
        pad = span * 0.08
        self.plot_item.setYRange(mid - span / 2 - pad, mid + span / 2 + pad, padding=0)

    # ---- shutdown ----

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.refresh_timer.stop()
        self.demo_timer.stop()
        if not self.demo and self.proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.user_stop = True
            try:
                os.kill(int(self.proc.processId()), signal.SIGINT)
            except OSError:
                self.proc.terminate()
            if not self.proc.waitForFinished(2500):
                self.proc.kill()
                self.proc.waitForFinished(1000)
        self._close_log()
        event.accept()


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Live AFR / lambda GUI for afr_logger")
    ap.add_argument("--binary", type=Path, default=here / "afr_logger", help="path to afr_logger")
    ap.add_argument("--baud", type=int, default=500000, help="CAN bit rate (default 500000)")
    ap.add_argument("--stoich", type=float, default=14.7,
                    help="stoichiometric AFR (default 14.7 gasoline; E85 9.76)")
    ap.add_argument("--window", type=int, default=30,
                    help="initial chart window in seconds (default 30; 0 = all)")
    ap.add_argument("--log-dir", type=Path, default=here, help="where CSV logs are written")
    ap.add_argument("--no-log", action="store_true", help="do not write a CSV log")
    ap.add_argument("--no-autostart", action="store_true", help="do not start logging on launch")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no cable/afr_logger needed")
    args = ap.parse_args(argv)
    if args.baud <= 0 or args.stoich <= 0:
        ap.error("--baud and --stoich must be positive")

    app = QtWidgets.QApplication(sys.argv[:1])
    apply_dark_theme(app)
    win = MainWindow(args.binary, args.baud, args.stoich, args.window or None,
                     log=not args.no_log, log_dir=args.log_dir, demo=args.demo)
    win.show()
    signal.signal(signal.SIGINT, lambda *_: win.close())
    # Let Python handle Ctrl-C while Qt's loop is running.
    keepalive = QtCore.QTimer()
    keepalive.start(250)
    keepalive.timeout.connect(lambda: None)
    if not args.no_autostart:
        QtCore.QTimer.singleShot(0, win.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
