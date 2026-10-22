"""PySide6 main window."""

import logging
import os
import shutil
import sys
import time

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
    QStatusBar, QVBoxLayout, QWidget)

from . import __version__
from .radio import RadioController
from .rigctld import RigctldServer
from .sinks import (HAVE_PYAV, HAVE_SOUNDDEVICE, PULSE_PREFIX, create_null_sink,
                    default_recording_name, guess_virtual_cable, list_output_devices,
                    sounddevice_error)
from .state import DEFAULT_PASSBAND, MODES, ServerSpec

log = logging.getLogger(__name__)

PUBLIC_LIST_URL = "http://rx.kiwisdr.com/"
STEPS_HZ = [10, 100, 500, 1000, 5000, 9000, 10000, 100000]
MAX_HISTORY = 15


def s_units(dbm):
    """dBm -> 'S7', 'S9+20' (HF convention: S9 = -73 dBm, 6 dB per S-unit)."""
    if dbm >= -73:
        over = int(round(dbm + 73))
        return "S9+%d" % over if over >= 1 else "S9"
    s = max(0, 9 + int(round((dbm + 73) / 6)))
    return "S%d" % s


class _Bridge(QObject):
    """Carries events from worker/rigctld threads to the GUI thread."""
    event = Signal(str, object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KiwiSDR Remote %s" % __version__)
        self.settings = QSettings("KiwiSDR-Remote", "KiwiSDR Remote")
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event, Qt.QueuedConnection)
        emit = self.bridge.event.emit
        self.radio = RadioController(on_event=emit,
                                     compression=self.settings.value("compression", True, type=bool))
        self.rigctld = None
        self._rec_started = None

        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(self._build_server_box())
        root.addWidget(self._build_tune_box())
        root.addWidget(self._build_audio_box())
        root.addWidget(self._build_record_box())
        root.addWidget(self._build_rigctl_box())
        root.addStretch(1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        exit_btn = QPushButton("Exit")
        exit_btn.setToolTip("Disconnect, stop recording and close the application")
        exit_btn.clicked.connect(self.close)    # closeEvent saves settings and shuts down
        bottom.addWidget(exit_btn)
        root.addLayout(bottom)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self._load_settings()
        self._timer = QTimer(self, interval=500, timeout=self._tick)
        self._timer.start()

        if not HAVE_SOUNDDEVICE:
            self.audio_enable.setEnabled(False)
            self.audio_enable.setToolTip("PortAudio not available: %s" % sounddevice_error())
        if self.rig_enable.isChecked():
            self._toggle_rigctld(True)

    # ================================================================== layout
    def _build_server_box(self):
        box = QGroupBox("KiwiSDR server")
        g = QGridLayout(box)
        self.server_combo = QComboBox(editable=True)
        self.server_combo.setInsertPolicy(QComboBox.NoInsert)
        self.server_combo.lineEdit().setPlaceholderText("host:port   e.g. mykiwi.example.net:8073")
        self.server_combo.lineEdit().returnPressed.connect(self._connect_clicked)
        self.password = QLineEdit(echoMode=QLineEdit.Password, placeholderText="(optional)")
        self.user_name = QLineEdit(placeholderText="shown in the Kiwi user list")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._connect_clicked)
        find_btn = QPushButton("Find receivers…")
        find_btn.setToolTip(PUBLIC_LIST_URL)
        find_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(PUBLIC_LIST_URL)))
        self.compression = QCheckBox("Compressed audio (ADPCM, ~1/4 the bandwidth)")
        self.conn_label = QLabel("Disconnected")
        self.smeter = QProgressBar(minimum=-127, maximum=-13, textVisible=True)
        self.smeter.setFormat("—")
        self.smeter.setValue(-127)

        g.addWidget(QLabel("Server"), 0, 0)
        g.addWidget(self.server_combo, 0, 1, 1, 2)
        g.addWidget(self.connect_btn, 0, 3)
        g.addWidget(QLabel("Password"), 1, 0)
        g.addWidget(self.password, 1, 1)
        g.addWidget(find_btn, 1, 3)
        g.addWidget(QLabel("Your name"), 2, 0)
        g.addWidget(self.user_name, 2, 1)
        g.addWidget(self.compression, 3, 1, 1, 3)
        g.addWidget(self.conn_label, 4, 0, 1, 3)
        g.addWidget(self.smeter, 5, 0, 1, 4)
        g.setColumnStretch(1, 1)
        return box

    def _build_tune_box(self):
        box = QGroupBox("Tuning")
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        self.freq = QDoubleSpinBox()
        self.freq.setDecimals(3)
        self.freq.setRange(1.0, 32000.0)
        self.freq.setSuffix(" kHz")
        self.freq.setKeyboardTracking(False)          # commit on Enter / arrows only
        self.freq.setAccelerated(True)
        f = QFont(self.freq.font())
        f.setPointSize(f.pointSize() + 8)
        f.setBold(True)
        self.freq.setFont(f)
        self.freq.setMinimumWidth(260)
        self.freq.valueChanged.connect(self._freq_edited)
        self.step = QComboBox()
        for s in STEPS_HZ:
            self.step.addItem(("%d Hz" % s) if s < 1000 else ("%g kHz" % (s / 1000)), s)
        self.step.currentIndexChanged.connect(self._step_changed)
        row.addWidget(self.freq, 1)
        row.addWidget(QLabel("Step"))
        row.addWidget(self.step)
        v.addLayout(row)

        row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for name in MODES:
            b = QRadioButton(name)
            self.mode_group.addButton(b)
            self.mode_buttons[name] = b
            row.addWidget(b)
        self.mode_group.buttonClicked.connect(self._mode_clicked)
        row.addSpacing(20)
        row.addWidget(QLabel("Bandwidth"))
        self.passband = QSpinBox(minimum=100, maximum=12000, singleStep=100, suffix=" Hz")
        self.passband.setKeyboardTracking(False)
        self.passband.valueChanged.connect(self._passband_edited)
        row.addWidget(self.passband)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _build_audio_box(self):
        box = QGroupBox("Audio output device (virtual cable / speakers)")
        g = QGridLayout(box)
        self.audio_enable = QCheckBox("Play to")
        self.audio_enable.toggled.connect(self._audio_toggled)
        self.device_combo = QComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_devices)
        self.volume = QSlider(Qt.Horizontal, minimum=0, maximum=200, value=100)
        self.volume.setToolTip("Output gain (100% = unity)")
        self.volume.sliderReleased.connect(self._volume_changed)
        self.volume_label = QLabel("100%")
        self.volume.valueChanged.connect(lambda v: self.volume_label.setText("%d%%" % v))
        self.audio_stats = QLabel("")
        self.audio_stats.setStyleSheet("color: gray")
        g.addWidget(self.audio_enable, 0, 0)
        g.addWidget(self.device_combo, 0, 1)
        g.addWidget(refresh, 0, 2)
        if sys.platform.startswith("linux") and shutil.which("pactl"):
            mk = QPushButton("Create 'KiwiSDR' virtual sink")
            mk.setToolTip("pactl load-module module-null-sink sink_name=KiwiSDR\n"
                          "Other programs record from 'Monitor of KiwiSDR'.")
            mk.clicked.connect(self._create_pulse_sink)
            g.addWidget(mk, 3, 1, 1, 2)
        g.addWidget(QLabel("Gain"), 1, 0)
        g.addWidget(self.volume, 1, 1)
        g.addWidget(self.volume_label, 1, 2)
        g.addWidget(self.audio_stats, 2, 1, 1, 2)
        g.setColumnStretch(1, 1)
        return box

    def _build_record_box(self):
        box = QGroupBox("Record to file")
        g = QGridLayout(box)
        self.rec_format = QComboBox()
        self.rec_format.addItem("WAV (16-bit PCM, native rate)", ".wav")
        self.rec_format.addItem("M4A (AAC in MP4 container)", ".m4a")
        self.rec_format.addItem("MP4 (AAC, .mp4 extension)", ".mp4")
        if not HAVE_PYAV:
            for i in (1, 2):
                item = self.rec_format.model().item(i)
                item.setEnabled(False)
                item.setToolTip("Install PyAV:  pip install av")
        self.rec_dir = QLineEdit()
        browse = QPushButton("Folder…")
        browse.clicked.connect(self._choose_dir)
        self.rec_btn = QPushButton("● Record")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._record_toggled)
        self.rec_label = QLabel("")
        g.addWidget(QLabel("Format"), 0, 0)
        g.addWidget(self.rec_format, 0, 1, 1, 2)
        g.addWidget(QLabel("Folder"), 1, 0)
        g.addWidget(self.rec_dir, 1, 1)
        g.addWidget(browse, 1, 2)
        g.addWidget(self.rec_btn, 2, 0)
        g.addWidget(self.rec_label, 2, 1, 1, 2)
        g.setColumnStretch(1, 1)
        return box

    def _build_rigctl_box(self):
        box = QGroupBox("Hamlib rigctld (CAT control for WSJT-X, fldigi, JS8Call …)")
        g = QGridLayout(box)
        self.rig_enable = QCheckBox("Enable")
        self.rig_enable.toggled.connect(self._toggle_rigctld)
        self.rig_addr = QComboBox(editable=True)
        self.rig_addr.addItems(["127.0.0.1", "0.0.0.0", "::1"])
        self.rig_addr.setToolTip("127.0.0.1 = this computer only; 0.0.0.0 = allow LAN clients")
        self.rig_port = QSpinBox(minimum=1, maximum=65535, value=4532)
        self.rig_status = QLabel("Off")
        hint = QLabel("In the client choose rig 'Hamlib NET rigctl' (model 2), "
                      "address 127.0.0.1:<port>.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        g.addWidget(self.rig_enable, 0, 0)
        g.addWidget(QLabel("Address"), 0, 1)
        g.addWidget(self.rig_addr, 0, 2)
        g.addWidget(QLabel("Port"), 0, 3)
        g.addWidget(self.rig_port, 0, 4)
        g.addWidget(self.rig_status, 1, 0, 1, 5)
        g.addWidget(hint, 2, 0, 1, 5)
        return box

    # ================================================================== settings
    def _load_settings(self):
        s = self.settings
        hist = s.value("server_history", [], type=list) or []
        self.server_combo.addItems(hist)
        self.server_combo.setCurrentText(s.value("server", hist[0] if hist else ""))
        self.user_name.setText(s.value("user", "kiwiremote"))
        self.compression.setChecked(s.value("compression", True, type=bool))
        mode = s.value("mode", "USB")
        mode = mode if mode in MODES else "USB"
        pb = s.value("passband", DEFAULT_PASSBAND[mode], type=int)
        self.radio.set_mode(mode, pb)
        self.radio.set_frequency(s.value("freq_hz", 7_074_000, type=int))
        self._show_tune(self.radio.tune)
        idx = self.step.findData(s.value("step", 1000, type=int))
        self.step.setCurrentIndex(idx if idx >= 0 else 3)
        self._step_changed()
        self._refresh_devices(select=s.value("audio_device", ""))
        self.volume.setValue(s.value("volume", 100, type=int))
        i = self.rec_format.findData(s.value("rec_format", ".wav"))
        self.rec_format.setCurrentIndex(i if i >= 0 and self.rec_format.model().item(i).isEnabled() else 0)
        default_dir = os.path.join(os.path.expanduser("~"), "Music", "KiwiSDR")
        self.rec_dir.setText(s.value("rec_dir", default_dir))
        self.rig_addr.setCurrentText(s.value("rig_addr", "127.0.0.1"))
        self.rig_port.setValue(s.value("rig_port", 4532, type=int))
        self.rig_enable.blockSignals(True)
        self.rig_enable.setChecked(s.value("rig_enable", True, type=bool))
        self.rig_enable.blockSignals(False)
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if s.value("audio_enable", False, type=bool) and self.device_combo.currentData():
            self.audio_enable.setChecked(True)

    def _save_settings(self):
        s = self.settings
        hist = [self.server_combo.itemText(i) for i in range(self.server_combo.count())]
        s.setValue("server_history", hist)
        s.setValue("server", self.server_combo.currentText())
        s.setValue("user", self.user_name.text())
        s.setValue("compression", self.compression.isChecked())
        st = self.radio.tune
        s.setValue("freq_hz", st.freq_hz)
        s.setValue("mode", st.mode)
        s.setValue("passband", st.passband)
        s.setValue("step", self.step.currentData())
        s.setValue("audio_device", self.device_combo.currentData() or "")
        s.setValue("audio_enable", self.audio_enable.isChecked())
        s.setValue("volume", self.volume.value())
        s.setValue("rec_format", self.rec_format.currentData())
        s.setValue("rec_dir", self.rec_dir.text())
        s.setValue("rig_enable", self.rig_enable.isChecked())
        s.setValue("rig_addr", self.rig_addr.currentText())
        s.setValue("rig_port", self.rig_port.value())
        s.setValue("geometry", self.saveGeometry())

    # ================================================================== server
    def _connect_clicked(self, *_):
        if self.radio.connected or self.connect_btn.text() == "Disconnect":
            self.radio.disconnect()
            self.connect_btn.setText("Connect")
            return
        text = self.server_combo.currentText()
        try:
            spec = ServerSpec.parse(text, self.password.text(), self.user_name.text().strip())
        except ValueError as e:
            QMessageBox.warning(self, "Server", "Invalid server address: %s" % e)
            return
        label = spec.label()
        i = self.server_combo.findText(label)
        if i >= 0:
            self.server_combo.removeItem(i)
        self.server_combo.insertItem(0, label)
        while self.server_combo.count() > MAX_HISTORY:
            self.server_combo.removeItem(self.server_combo.count() - 1)
        self.server_combo.setCurrentIndex(0)
        self.radio._compression = self.compression.isChecked()
        self.radio.connect(spec)
        self.connect_btn.setText("Disconnect")

    # ================================================================== tuning
    def _show_tune(self, st):
        for w in (self.freq, self.passband):
            w.blockSignals(True)
        self.freq.setValue(st.freq_hz / 1000.0)
        self.passband.setValue(st.passband)
        self.mode_buttons[st.mode].setChecked(True)
        for w in (self.freq, self.passband):
            w.blockSignals(False)

    def _step_changed(self, *_):
        step_hz = self.step.currentData() or 1000
        self.freq.setSingleStep(step_hz / 1000.0)

    @Slot(float)
    def _freq_edited(self, khz):
        try:
            self.radio.set_frequency(khz * 1000.0)
        except ValueError as e:
            self.statusBar().showMessage(str(e), 5000)
            self._show_tune(self.radio.tune)

    def _mode_clicked(self, button):
        mode = button.text()
        self.radio.set_mode(mode, DEFAULT_PASSBAND[mode])
        self._show_tune(self.radio.tune)

    @Slot(int)
    def _passband_edited(self, hz):
        self.radio.set_mode(self.radio.tune.mode, hz)

    # ================================================================== audio device
    def _refresh_devices(self, select=None):
        current = select if isinstance(select, str) and select else self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        devices = list_output_devices()
        for key, label in devices:
            self.device_combo.addItem(label, key)
        if not devices:
            self.device_combo.addItem("(no output devices found)", None)
        idx = self.device_combo.findData(current) if current else -1
        if idx < 0:
            guess = guess_virtual_cable(devices)
            idx = self.device_combo.findData(guess) if guess else 0
        self.device_combo.setCurrentIndex(max(idx, 0))
        self.device_combo.blockSignals(False)

    @Slot(bool)
    def _audio_toggled(self, on):
        key = self.device_combo.currentData() if on else None
        if on and not key:
            self.audio_enable.setChecked(False)
            return
        try:
            self.radio.set_audio_device(key, self.volume.value() / 100.0)
        except Exception as e:
            QMessageBox.warning(self, "Audio device", str(e))
            self.audio_enable.blockSignals(True)
            self.audio_enable.setChecked(False)
            self.audio_enable.blockSignals(False)
            self.audio_stats.setText("")

    def _create_pulse_sink(self, *_):
        try:
            name = create_null_sink("KiwiSDR")
        except Exception as e:
            QMessageBox.warning(self, "Virtual sink", str(e))
            return
        self._refresh_devices(select=PULSE_PREFIX + name)
        self._device_changed()

    def _device_changed(self, *_):
        if self.audio_enable.isChecked():
            self._audio_toggled(True)

    def _volume_changed(self, *_):
        sink = self.radio._sinks.get("card")
        if sink is not None:
            sink.gain = self.volume.value() / 100.0

    # ================================================================== recording
    def _choose_dir(self, *_):
        d = QFileDialog.getExistingDirectory(self, "Recording folder", self.rec_dir.text())
        if d:
            self.rec_dir.setText(d)

    @Slot(bool)
    def _record_toggled(self, on):
        if not on:
            info = self.radio.stop_recording()
            self.rec_btn.setText("● Record")
            self._rec_started = None
            if info:
                self.rec_label.setText("Saved %s (%.0f s)" % (os.path.basename(info["path"]), info["seconds"]))
            return
        folder = self.rec_dir.text().strip() or os.path.expanduser("~")
        try:
            os.makedirs(folder, exist_ok=True)
            st = self.radio.tune
            path = os.path.join(folder, default_recording_name(st.freq_hz, st.mode,
                                                               self.rec_format.currentData()))
            self.radio.start_recording(path)
        except Exception as e:
            QMessageBox.warning(self, "Record", str(e))
            self.rec_btn.blockSignals(True)
            self.rec_btn.setChecked(False)
            self.rec_btn.blockSignals(False)
            return
        self._rec_started = time.monotonic()
        self.rec_btn.setText("■ Stop")
        self.rec_label.setText(os.path.basename(path))

    # ================================================================== rigctld
    @Slot(bool)
    def _toggle_rigctld(self, on):
        if self.rigctld is not None:
            self.rigctld.stop()
            self.rigctld = None
        self.rig_addr.setEnabled(not on)
        self.rig_port.setEnabled(not on)
        if not on:
            self.rig_status.setText("Off")
            return
        try:
            self.rigctld = RigctldServer(self.radio, self.rig_addr.currentText().strip(),
                                         self.rig_port.value(), on_activity=self._rig_activity)
            self.rigctld.start()
        except OSError as e:
            self.rigctld = None
            self.rig_status.setText("Could not listen: %s" % e)
            self.rig_enable.blockSignals(True)
            self.rig_enable.setChecked(False)
            self.rig_enable.blockSignals(False)
            self.rig_addr.setEnabled(True)
            self.rig_port.setEnabled(True)
            return
        self._rig_activity("clients", (0, None, False))

    def _rig_activity(self, kind, payload):
        # called from rigctld threads -> hop to GUI thread
        self.bridge.event.emit("rig_" + kind, payload)

    # ================================================================== events (GUI thread)
    @Slot(str, object)
    def _on_event(self, kind, payload):
        if kind == "tune":
            st, source = payload
            self._show_tune(st)
            if source == "rigctl":
                self.statusBar().showMessage("rigctl: %.3f kHz %s %d Hz"
                                             % (st.freq_hz / 1e3, st.mode, st.passband), 3000)
        elif kind == "status":
            text, level = payload
            self.conn_label.setText(text)
            self.conn_label.setStyleSheet("color: #c0392b" if level == "error" else "")
        elif kind == "stopped":
            t = self.radio._thread
            if t is None or not t.is_alive():      # ignore the old worker after a reconnect
                self.connect_btn.setText("Connect")
        elif kind == "connected":
            if payload:
                self.connect_btn.setText("Disconnect")
            else:
                self.smeter.setValue(-127)
                self.smeter.setFormat("—")
                if self.radio._thread is None:
                    self.connect_btn.setText("Connect")
        elif kind == "smeter":
            dbm = float(payload)
            self.smeter.setValue(int(max(-127, min(-13, dbm))))
            self.smeter.setFormat("%s   %.0f dBm" % (s_units(dbm), dbm))
        elif kind == "limits":
            lo, hi = payload
            self.freq.blockSignals(True)
            self.freq.setRange(lo / 1000.0, hi / 1000.0)
            self.freq.blockSignals(False)
        elif kind == "sink_failed":
            if payload == "card":
                self.audio_enable.blockSignals(True)
                self.audio_enable.setChecked(False)
                self.audio_enable.blockSignals(False)
            elif payload == "file":
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.setText("● Record")
                self.rec_btn.blockSignals(False)
        elif kind == "rig_clients":
            n = payload[0]
            if self.rigctld is not None:
                self.rig_status.setText("Listening on %s:%d — %d client%s"
                                        % (self.rigctld.host, self.rigctld.port, n, "" if n == 1 else "s"))
        elif kind == "rig_command":
            pass

    def _tick(self, *_):
        secs = self.radio.recording_seconds()
        if secs is not None and self._rec_started is not None:
            wall = time.monotonic() - self._rec_started
            self.rec_label.setText("Recording  %02d:%02d:%02d" % (wall // 3600, wall % 3600 // 60, wall % 60)
                                   + ("" if self.radio.connected else "  (waiting for audio)"))
        stats = self.radio.audio_stats()
        if stats:
            self.audio_stats.setText("buffer %.0f ms   clock trim %+.0f ppm   underruns %d"
                                     % (stats["buffer_ms"], stats["trim_ppm"], stats["underruns"]))
        elif not self.audio_enable.isChecked():
            self.audio_stats.setText("")

    def closeEvent(self, ev):
        self._save_settings()
        if self.rigctld is not None:
            self.rigctld.stop()
        self.radio.shutdown()
        super().closeEvent(ev)


def run(argv):
    app = QApplication(argv)
    app.setApplicationName("KiwiSDR Remote")
    w = MainWindow()
    w.show()
    return app.exec()
