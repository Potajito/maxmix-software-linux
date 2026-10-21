#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# maxmix_gui.py - PySide6 configuration GUI for the MaxMix device on Linux.
#
# Embeds the maxmix.py daemon in a worker thread; every setting change is
# pushed to the device live (the firmware applies LED colours, sleep and
# encoder behaviour immediately, just like the Windows app) and saved to
# ~/.config/maxmix/settings.json, which the plain CLI daemon loads on start.
#
# Requires: PySide6 in addition to pyserial + pulsectl.
# ---------------------------------------------------------------------------

import argparse
import logging
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6 import QtCore, QtGui, QtWidgets

import maxmix
from maxmix import MaxMixDaemon, Settings, CMD_SETTINGS, pack_device_settings


# ---------------------------------------------------------------------------
# Small custom widgets
# ---------------------------------------------------------------------------


def color_int(qcolor):
    return (qcolor.red() << 16) | (qcolor.green() << 8) | qcolor.blue()


def color_qcolor(value):
    return QtGui.QColor((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


class ColorButton(QtWidgets.QPushButton):
    """Button showing a colour swatch; opens a colour dialog on click."""

    colorChanged = QtCore.Signal(int)

    def __init__(self, value=0xFFFFFF, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 28)
        self.value = value
        self.clicked.connect(self._pick)

    def set_value(self, value, notify=True):
        if value == self.value:
            return
        self.value = value
        self._refresh()
        if notify:
            self.colorChanged.emit(value)

    def _pick(self):
        qcolor = QtWidgets.QColorDialog.getColor(color_qcolor(self.value), self, "Pick LED colour")
        if qcolor.isValid():
            self.set_value(color_int(qcolor))

    def _refresh(self):
        self.setStyleSheet(
            "QPushButton { background-color: #%06x; border: 1px solid #666; border-radius: 4px; }"
            "QPushButton:hover { border: 2px solid #000; }" % self.value
        )


class GradientBar(QtWidgets.QWidget):
    """Live preview of the LED ring fill, mirroring the firmware behaviour:
    the ring fills up with the volume and each pixel is lerped between the
    low and high colour."""

    LEVEL_UNSET = -1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.low = 0x0000FF
        self.high = 0xFF0000
        self.level = self.LEVEL_UNSET  # percent 0-100, or -1 when unknown
        self.setMinimumHeight(26)

    def set_colors(self, low, high):
        self.low, self.high = low, high
        self.update()

    def set_level(self, level):
        self.level = level
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        low, high = color_qcolor(self.low), color_qcolor(self.high)
        filled = 0 if self.level < 0 else int(w * max(0, min(100, self.level)) / 100)
        for x in range(w):
            t = x / max(1, w - 1)
            color = self._lerp(low, high, t) if x <= filled else QtGui.QColor(40, 40, 40)
            painter.setPen(color)
            painter.drawLine(x, 2, x, h - 3)
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 120), 1))
        painter.drawRect(0, 2, w - 1, h - 5)
        painter.end()

    @staticmethod
    def _lerp(c1, c2, t):
        return QtGui.QColor(
            round(c1.red() + (c2.red() - c1.red()) * t),
            round(c1.green() + (c2.green() - c1.green()) * t),
            round(c1.blue() + (c2.blue() - c1.blue()) * t),
        )


class QtLogHandler(logging.Handler, QtCore.QObject):
    message = QtCore.Signal(str)

    def __init__(self, widget):
        logging.Handler.__init__(self)
        QtCore.QObject.__init__(self)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        self.message.connect(widget.appendPlainText)

    def emit(self, record):
        try:
            self.message.emit(self.format(record))
        except Exception:
            pass


def find_logo():
    """Locate the MaxMix logo: PyInstaller bundle, then this directory."""
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", ""), "icon.png"),
        os.path.join(os.path.dirname(os.path.abspath(maxmix.__file__)), "icon.png"),
        os.path.join(os.path.dirname(os.path.abspath(maxmix.__file__)),
                     "..", ".github", "resources", "color-dark-2048.png"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def install_theme_icon(logo_path):
    """Install the icon into the user's hicolor theme so Wayland compositors
    can resolve it by application id (Wayland has no per-window icons; the
    window decoration looks the icon up via the .desktop file name)."""
    if not logo_path:
        return
    pixmap = QtGui.QPixmap(logo_path)
    if pixmap.isNull():
        return
    base = os.path.join(
        QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.GenericDataLocation
        ),
        "icons", "hicolor", "256x256", "apps",
    )
    try:
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, "maxmix-gui.png")
        if not os.path.exists(path):
            pixmap.scaled(
                256, 256,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            ).save(path, "PNG")
    except OSError as exc:
        log.debug("Could not install theme icon: %s", exc)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.setWindowTitle("MaxMix for Linux")
        logo = find_logo()
        if logo:
            self.setWindowIcon(QtGui.QIcon(logo))
        self.args = args

        # The daemon runs in a worker thread; created here so the UI can
        # read/modify its settings, started last so no log line is missed.
        self.daemon = MaxMixDaemon(args)

        # last gradient pair, remembered while "solid" mode is selected
        self.gradient_low = self.daemon.settings.volume_min_color
        self.gradient_high = self.daemon.settings.volume_max_color

        # debounce for settings pushes (the firmware tolerates bursts, but
        # sliders change many times per second). Created before the UI:
        # _build_ui() triggers change handlers that rely on it.
        self.push_timer = QtCore.QTimer(singleShot=True, interval=150)
        self.push_timer.timeout.connect(self._push_settings)

        self._build_ui()

        self.status_timer = QtCore.QTimer(interval=250)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start()

        logging.getLogger("maxmix").addHandler(QtLogHandler(self.log_view))
        logging.getLogger("maxmix").setLevel(logging.DEBUG if args.verbose else logging.INFO)

        self.daemon_thread = threading.Thread(
            target=lambda: self.daemon.run(install_signals=False), daemon=True
        )
        self.daemon_thread.start()
        self._refresh_status()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)

        # --- LED ring group ---
        led_group = QtWidgets.QGroupBox("LED ring")
        led_layout = QtWidgets.QGridLayout(led_group)

        self.radio_solid = QtWidgets.QRadioButton("Solid colour")
        self.radio_gradient = QtWidgets.QRadioButton("Volume gradient")
        self.radio_solid.toggled.connect(self._mode_changed)

        self.solid_button = ColorButton()
        self.solid_button.colorChanged.connect(self._solid_changed)
        self.low_button = ColorButton(self.gradient_low)
        self.low_button.colorChanged.connect(self._low_changed)
        self.high_button = ColorButton(self.gradient_high)
        self.high_button.colorChanged.connect(self._high_changed)

        led_layout.addWidget(self.radio_solid, 0, 0)
        led_layout.addWidget(self.solid_button, 0, 1)
        led_layout.addWidget(QtWidgets.QLabel(""), 0, 2)
        led_layout.addWidget(self.radio_gradient, 1, 0)
        led_layout.addWidget(QtWidgets.QLabel("low"), 1, 1, QtCore.Qt.AlignmentFlag.AlignRight)
        led_layout.addWidget(self.low_button, 1, 2)
        led_layout.addWidget(QtWidgets.QLabel("high"), 1, 3, QtCore.Qt.AlignmentFlag.AlignRight)
        led_layout.addWidget(self.high_button, 1, 4)

        self.preview = GradientBar()
        led_layout.addWidget(self.preview, 2, 0, 1, 5)

        chan_row = QtWidgets.QHBoxLayout()
        chan_row.addWidget(QtWidgets.QLabel("Game channels:"))
        self.chan_a = ColorButton(self.daemon.settings.channel_a_color)
        self.chan_a.colorChanged.connect(lambda v: self._setting_changed("channel_a_color", v))
        self.chan_b = ColorButton(self.daemon.settings.channel_b_color)
        self.chan_b.colorChanged.connect(lambda v: self._setting_changed("channel_b_color", v))
        chan_row.addWidget(QtWidgets.QLabel("A"))
        chan_row.addWidget(self.chan_a)
        chan_row.addWidget(QtWidgets.QLabel("B"))
        chan_row.addWidget(self.chan_b)
        chan_row.addStretch()
        led_layout.addLayout(chan_row, 3, 0, 1, 5)

        # --- Behaviour group ---
        behaviour_group = QtWidgets.QGroupBox("Behaviour")
        behaviour_layout = QtWidgets.QGridLayout(behaviour_group)

        behaviour_layout.addWidget(QtWidgets.QLabel("Display sleep after"), 0, 0)
        self.sleep_spin = QtWidgets.QSpinBox()
        self.sleep_spin.setRange(0, 255)
        self.sleep_spin.setSuffix(" s")
        self.sleep_spin.setSpecialValueText("never")
        self.sleep_spin.setValue(self.daemon.settings.sleep_after)
        self.sleep_spin.valueChanged.connect(lambda v: self._setting_changed("sleep_after", v))
        behaviour_layout.addWidget(self.sleep_spin, 0, 1)
        behaviour_layout.addWidget(QtWidgets.QLabel("Encoder acceleration"), 1, 0)
        self.accel_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.accel_slider.setRange(0, 100)
        self.accel_slider.setValue(self.daemon.settings.acceleration)
        self.accel_label = QtWidgets.QLabel(f"{self.daemon.settings.acceleration} %")
        self.accel_slider.valueChanged.connect(self._accel_changed)
        behaviour_layout.addWidget(self.accel_slider, 1, 1)
        behaviour_layout.addWidget(self.accel_label, 1, 2)
        behaviour_layout.addWidget(QtWidgets.QLabel("Volume step per detent"), 2, 0)
        self.step_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.step_slider.setRange(10, 500)
        self.step_slider.setSingleStep(10)
        self.step_slider.setPageStep(50)
        self.step_slider.setValue(round(self.daemon.settings.volume_step * 100))
        self.step_label = QtWidgets.QLabel(f"{self.step_slider.value()} %")
        self.step_slider.valueChanged.connect(self._step_changed)
        behaviour_layout.addWidget(self.step_slider, 2, 1)
        behaviour_layout.addWidget(self.step_label, 2, 2)
        self.scroll_check = QtWidgets.QCheckBox("Continuous scroll (wrap around ends)")
        self.scroll_check.setChecked(self.daemon.settings.continuous_scroll)
        self.scroll_check.toggled.connect(lambda v: self._setting_changed("continuous_scroll", v))
        behaviour_layout.addWidget(self.scroll_check, 3, 0, 1, 3)

        # --- Log ---
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        self.log_view.setMaximumHeight(140)
        log_group = QtWidgets.QGroupBox("Device log")
        log_layout = QtWidgets.QVBoxLayout(log_group)
        log_layout.addWidget(self.log_view)

        layout.addWidget(led_group)
        layout.addWidget(behaviour_group)
        layout.addWidget(log_group)
        self.setCentralWidget(central)

        self.status_label = QtWidgets.QLabel("connecting…")
        self.statusBar().addWidget(self.status_label, 1)

        # initial mode reflects current device colours (silent: no signal
        # handlers must fire while populating, they would flatten the
        # gradient to a solid colour)
        solid = self.daemon.settings.volume_min_color == self.daemon.settings.volume_max_color
        self.radio_solid.blockSignals(True)
        self.radio_gradient.blockSignals(True)
        self.radio_solid.setChecked(solid)
        self.radio_gradient.setChecked(not solid)
        self.radio_solid.blockSignals(False)
        self.radio_gradient.blockSignals(False)
        self.solid_button.set_value(self.daemon.settings.volume_min_color, notify=False)
        self._mode_changed(solid)
        self._refresh_previews()

    # -------------------------------------------------------------- actions

    def _setting_changed(self, name, value):
        setattr(self.daemon.settings, name, value)
        self.push_timer.start()

    def _accel_changed(self, value):
        self.accel_label.setText(f"{value} %")
        self._setting_changed("acceleration", value)

    def _step_changed(self, value):
        self.step_label.setText(f"{value} %")
        self._setting_changed("volume_step", value / 100.0)

    def _solid_changed(self, value):
        self.daemon.settings.volume_min_color = value
        self.daemon.settings.volume_max_color = value
        self.push_timer.start()
        self._refresh_previews()

    def _low_changed(self, value):
        self.gradient_low = value
        self.daemon.settings.volume_min_color = value
        self.push_timer.start()
        self._refresh_previews()

    def _high_changed(self, value):
        self.gradient_high = value
        self.daemon.settings.volume_max_color = value
        self.push_timer.start()
        self._refresh_previews()

    def _mode_changed(self, solid):
        self.solid_button.setEnabled(solid)
        self.low_button.setEnabled(not solid)
        self.high_button.setEnabled(not solid)
        if solid:
            self._solid_changed(self.solid_button.value)
        else:
            self._low_changed(self.gradient_low)
            self._high_changed(self.gradient_high)

    def _push_settings(self):
        self.daemon.push_settings()
        self._save_settings()
        self.log_view.appendPlainText(
            f"{time.strftime('%H:%M:%S')} INFO    settings -> "
            f"min=#{self.daemon.settings.volume_min_color:06x} "
            f"max=#{self.daemon.settings.volume_max_color:06x}"
        )

    def _save_settings(self):
        try:
            maxmix.save_settings(self.daemon.settings)
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    def _refresh_previews(self):
        self.preview.set_colors(
            self.daemon.settings.volume_min_color, self.daemon.settings.volume_max_color
        )

    def _refresh_status(self):
        daemon = self.daemon
        bits = []
        if daemon.connected:
            bits.append(f"<b>connected</b> (firmware {daemon.firmware})")
        else:
            bits.append("<b>device not found</b>")
        bits.append("audio: " + ("ok" if daemon.audio.connected else "offline"))
        if daemon.connected and daemon.sessions and 0 <= daemon.current < len(daemon.sessions):
            session = daemon.sessions[daemon.current]
            level = session.volume if not session.muted else 0
            bits.append(f"{session.name[:32]}: {session.volume}%{' (muted)' if session.muted else ''}")
            self.preview.set_level(level)
        else:
            self.preview.set_level(GradientBar.LEVEL_UNSET)
        self.status_label.setText(" — ".join(bits))

    # -------------------------------------------------------------- shutdown

    def closeEvent(self, event):
        # Persist even if the debounce timer never fired (change then quit
        # within 150 ms).
        self._save_settings()
        self.daemon.stop()
        self.daemon_thread.join(timeout=3)
        event.accept()


# ---------------------------------------------------------------------------
# Self-test (headless): verifies that UI actions translate into the exact
# protocol bytes queued for the device. Run: maxmix_gui.py --selftest
# ---------------------------------------------------------------------------


def selftest():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Redirect the config dir so the persistence checks below never touch
    # the user's real ~/.config/maxmix/settings.json.
    config_dir = tempfile.mkdtemp(prefix="maxmix-selftest-config-")
    os.environ["XDG_CONFIG_HOME"] = config_dir
    app = QtWidgets.QApplication([])

    # A port that can never exist keeps the worker daemon off the real serial
    # ports: with an empty port it would glob the actual devices, and if it
    # won the handshake race against a running instance it would drain its
    # own outbox, breaking the outbox assertion below.
    args = argparse.Namespace(
        port="/dev/maxmix-selftest-none", mode="output", sleep_after=5, acceleration=60,
        continuous_scroll=True, volume_min_color=0x0000FF, volume_max_color=0xFF0000,
        channel_a_color=0x0000FF, channel_b_color=0xFF00FF, force=False, verbose=False,
    )
    window = MainWindow(args)

    # default gradient: low=blue, high=red
    assert window.daemon.settings.volume_min_color == 0x0000FF
    assert window.daemon.settings.volume_max_color == 0xFF0000

    # switch to solid green -> both colours equal, payload queued
    window.radio_solid.setChecked(True)
    window.solid_button.set_value(0x00FF00)
    assert window.daemon.settings.volume_min_color == 0x00FF00
    assert window.daemon.settings.volume_max_color == 0x00FF00

    # back to gradient: remembered pair restored
    window.radio_gradient.setChecked(True)
    assert window.daemon.settings.volume_min_color == 0x0000FF
    assert window.daemon.settings.volume_max_color == 0xFF0000

    window._low_changed(0x123456)
    window._setting_changed("sleep_after", 9)
    window._setting_changed("continuous_scroll", False)
    window._accel_changed(75)
    settings = window.daemon.settings
    assert (settings.volume_min_color, settings.sleep_after, settings.continuous_scroll,
            settings.acceleration) == (0x123456, 9, False, 75)

    # volume step slider drives the host-side multiplier
    window.step_slider.setValue(200)
    assert settings.volume_step == 2.0, settings.volume_step
    window.step_slider.setValue(50)
    assert settings.volume_step == 0.5, settings.volume_step
    from maxmix import apply_volume_step
    assert apply_volume_step(50, 51, 1.0) == 51          # passthrough at 100%
    assert apply_volume_step(50, 51, 0.5) == 51          # half of 1 still >= 1
    assert apply_volume_step(50, 52, 0.5) == 51          # 2 * 0.5 = 1
    assert apply_volume_step(50, 54, 2.0) == 58          # 4 * 2 = 8 -> 58
    assert apply_volume_step(99, 100, 2.0) == 100        # clamped high
    assert apply_volume_step(1, 0, 2.0) == 0             # clamped low
    assert apply_volume_step(50, 60, 0.1) == 51          # min movement enforced

    payload = pack_device_settings(settings)
    assert payload == bytes([9, 75, 0x12, 0x34, 0x56, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x00, 0xFF]), payload.hex()

    # let the debounce timer fire so the payload lands in the daemon outbox
    deadline = time.monotonic() + 5.0
    while window.daemon.outbox.get(CMD_SETTINGS) != payload and time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.02)
    assert window.daemon.outbox.get(CMD_SETTINGS) == payload

    # settings persist to the config file the plain CLI daemon reads
    config_path = maxmix.settings_config_path()
    assert config_path.startswith(config_dir), config_path
    window._save_settings()
    saved = maxmix.load_saved_settings(config_path)
    assert saved["sleep_after"] == 9, saved
    assert saved["acceleration"] == 75, saved
    assert saved["continuous_scroll"] is False, saved
    assert saved["volume_min_color"] == 0x123456, saved
    assert saved["volume_step"] == 0.5, saved

    # a fresh daemon start (flag parsing in maxmix.py) picks the saved values up
    reloaded = maxmix.parse_args([])
    assert reloaded.sleep_after == 9, reloaded.sleep_after
    assert reloaded.continuous_scroll is False, reloaded.continuous_scroll
    assert reloaded.volume_step == 0.5, reloaded.volume_step
    assert reloaded.volume_min_color == 0x123456, reloaded.volume_min_color
    # the GUI start-up path loads the same file
    gui_args = parse_args([])
    assert gui_args.sleep_after == 9, gui_args.sleep_after
    # explicit flags still override the saved file
    overridden = maxmix.parse_args(["--sleep-after", "3"])
    assert overridden.sleep_after == 3, overridden.sleep_after

    window.close()
    shutil.rmtree(config_dir, ignore_errors=True)
    print("GUI SELFTEST PASSED")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(description="MaxMix configuration GUI for Linux.")
    parser.add_argument("-p", "--port", help="serial port of the device (default: auto-detect)")
    parser.add_argument("--selftest", action="store_true", help="run headless self-test and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    cli = parser.parse_args(argv)
    # the daemon consumes the full CLI namespace of maxmix.py; start from the
    # settings saved by a previous run, falling back to the built-in defaults
    cli.mode = "output"
    cli.force = False
    saved = maxmix.load_saved_settings()
    for name, default in maxmix.SETTINGS_DEFAULTS.items():
        setattr(cli, name, saved.get(name, default))
    return cli


def main(argv=None):
    args = parse_args(argv)
    if args.selftest:
        return selftest()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logo = find_logo()

    # Wayland has no per-window icons: the compositor resolves the window
    # icon from the application id (desktop file base name) via the icon
    # theme. Set both, before any window exists.
    app = QtWidgets.QApplication(sys.argv)
    app.setDesktopFileName("maxmix-gui")
    if logo:
        app.setWindowIcon(QtGui.QIcon(logo))
    install_theme_icon(logo)

    window = MainWindow(args)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
