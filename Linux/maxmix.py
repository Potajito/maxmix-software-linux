#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# maxmix.py - Linux companion daemon for the MaxMix USB volume controller.
#
# Mirrors the behaviour of the Windows MaxMix desktop application:
#   - Talks the same binary serial protocol (76800 baud) as the firmware.
#   - Enumerates audio endpoints / application streams.
#   - Pushes the sessions around the "current" one to the device and applies
#     volume / mute / default-device changes coming from the device knob.
#
# Audio backend is PulseAudio, which also covers PipeWire through its
# pipewire-pulse compatibility server present on every modern distribution.
#
# Requires Python 3.8+ with `pyserial` and `pulsectl`:
#   pip install pyserial pulsectl
# ---------------------------------------------------------------------------

import argparse
import glob
import logging
import os
import signal
import sys
import time
import zlib
from collections import OrderedDict

try:
    import serial
except ImportError:  # pragma: no cover
    sys.exit("pyserial is required: pip install pyserial")

try:
    import pulsectl
except ImportError:  # pragma: no cover
    sys.exit("pulsectl is required: pip install pulsectl")


log = logging.getLogger("maxmix")

# ---------------------------------------------------------------------------
# Protocol constants (must match Embedded/MaxMix/Enums.h and Messages.cs)
# ---------------------------------------------------------------------------

BAUD_RATE = 76800

CMD_TEST = 1
CMD_OK = 2
CMD_SETTINGS = 3
CMD_SESSION_INFO = 4
CMD_CURRENT_SESSION = 5
CMD_ALTERNATE_SESSION = 6
CMD_PREVIOUS_SESSION = 7
CMD_NEXT_SESSION = 8
CMD_VOLUME_CURR_CHANGE = 9
CMD_VOLUME_ALT_CHANGE = 10
CMD_VOLUME_PREV_CHANGE = 11
CMD_VOLUME_NEXT_CHANGE = 12
CMD_MODE_STATES = 13

PAYLOAD_SIZES = {
    CMD_SETTINGS: 14,
    CMD_SESSION_INFO: 5,
    CMD_CURRENT_SESSION: 32,
    CMD_ALTERNATE_SESSION: 32,
    CMD_PREVIOUS_SESSION: 32,
    CMD_NEXT_SESSION: 32,
    CMD_VOLUME_CURR_CHANGE: 2,
    CMD_VOLUME_ALT_CHANGE: 2,
    CMD_VOLUME_PREV_CHANGE: 2,
    CMD_VOLUME_NEXT_CHANGE: 2,
    CMD_MODE_STATES: 5,
}

# DisplayMode values must match the firmware enum exactly:
# MODE_SPLASH=0, MODE_OUTPUT=1, MODE_INPUT=2, MODE_APPLICATION=3, MODE_GAME=4
MODE_SPLASH = 0
MODE_OUTPUT = 1
MODE_INPUT = 2
MODE_APPLICATION = 3
MODE_GAME = 4

COMPATIBLE_FIRMWARE = {"1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5", "1.5.6"}

KEEPALIVE_INTERVAL = 1.0   # firmware resets to splash after 5s of serial silence
DEVICE_TIMEOUT = 5.0       # consider the device gone after 5s without any read
RECONNECT_INTERVAL = 1.0
PULSE_POLL_INTERVAL = 0.25
TICK_INTERVAL = 0.01

# Device slots for the CURRENT/ALTERNATE/PREVIOUS/NEXT sessions.
SLOT_CURRENT = 0
SLOT_ALTERNATE = 1
SLOT_PREVIOUS = 2
SLOT_NEXT = 3

# ---------------------------------------------------------------------------
# Message packing helpers (byte-for-byte identical to the packed C structs in
# Embedded/MaxMix/Structs.h)
# ---------------------------------------------------------------------------


def pack_volume_data(item_id, is_default, volume, is_muted):
    return bytes(
        [
            (item_id & 0x7F) | (0x80 if is_default else 0x00),
            (int(volume) & 0x7F) | (0x80 if is_muted else 0x00),
        ]
    )


def unpack_volume_data(data):
    return (data[0] & 0x7F, bool(data[0] & 0x80), data[1] & 0x7F, bool(data[1] & 0x80))


def pack_session_data(item_id, name, is_default, volume, is_muted):
    encoded = name.encode("utf-8")
    if len(encoded) > 30:
        # Cut on a UTF-8 character boundary so the display never sees a
        # dangling multi-byte sequence.
        encoded = encoded[:30].decode("utf-8", "ignore").encode("utf-8")
    return encoded.ljust(30, b"\x00") + pack_volume_data(item_id, is_default, volume, is_muted)


def pack_session_info(mode, current, output_count, input_count, application_count):
    return bytes(
        [mode & 0xFF, current & 0xFF, output_count & 0xFF, input_count & 0xFF, application_count & 0xFF]
    )


def pack_mode_states():
    # splash=LOGO, output=EDIT, input=EDIT, application=NAVIGATE, game=SELECT_A
    return bytes([0, 1, 1, 0, 0])


class Settings:
    """Device settings the GUI (or CLI args) can change at runtime.

    volume_step is host-side only: a multiplier applied to the volume delta
    the firmware reports per detent (1.0 = firmware default).
    """

    __slots__ = (
        "sleep_after",
        "acceleration",
        "continuous_scroll",
        "volume_min_color",
        "volume_max_color",
        "channel_a_color",
        "channel_b_color",
        "volume_step",
    )

    def __init__(
        self,
        sleep_after=5,
        acceleration=60,
        continuous_scroll=True,
        volume_min_color=0x0000FF,
        volume_max_color=0xFF0000,
        channel_a_color=0x0000FF,
        channel_b_color=0xFF00FF,
        volume_step=1.0,
    ):
        self.sleep_after = sleep_after
        self.acceleration = acceleration
        self.continuous_scroll = continuous_scroll
        self.volume_min_color = volume_min_color
        self.volume_max_color = volume_max_color
        self.channel_a_color = channel_a_color
        self.channel_b_color = channel_b_color
        self.volume_step = volume_step


def apply_volume_step(old_volume, new_volume, ratio):
    """Scale a device-reported volume change by `ratio` (host-side step size).

    The firmware advances its session volume by 1 (+ acceleration) per
    detent; the host can scale that delta. The result is clamped to 0..100
    and never collapses to zero movement (minimum 1 per detent).
    """
    if ratio == 1.0 or new_volume == old_volume:
        return new_volume
    delta = new_volume - old_volume
    scaled = delta * ratio
    rounded = int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)
    if rounded == 0:
        rounded = 1 if delta > 0 else -1
    return max(0, min(100, old_volume + rounded))


def pack_device_settings(settings):
    def color(value):
        return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])  # R, G, B

    flags = (min(max(settings.acceleration, 0), 100) & 0x7F) | (
        0x80 if settings.continuous_scroll else 0x00
    )
    return (
        bytes([settings.sleep_after & 0xFF, flags])
        + color(settings.volume_min_color)
        + color(settings.volume_max_color)
        + color(settings.channel_a_color)
        + color(settings.channel_b_color)
    )


def parse_color(text):
    try:
        value = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError("color must be hexadecimal RRGGBB, e.g. 00FF00")
    if not 0 <= value <= 0xFFFFFF:
        raise argparse.ArgumentTypeError("color must be in range 000000-FFFFFF")
    return value


def parse_volume_step(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("volume step must be a number, e.g. 0.5")
    if not 0.1 <= value <= 10.0:
        raise argparse.ArgumentTypeError("volume step must be in range 0.1-10")
    return value


# ---------------------------------------------------------------------------
# Audio model
# ---------------------------------------------------------------------------

class Session:
    """One item the device can display and edit."""

    __slots__ = ("sid", "name", "volume", "muted", "is_default", "mode", "stream_indexes")

    def __init__(self, sid, name, volume, muted, is_default, mode, stream_indexes=None):
        self.sid = sid                # stable host-side id
        self.name = name
        self.volume = volume          # 0-100
        self.muted = muted
        self.is_default = is_default
        self.mode = mode              # mode this session was enumerated for
        self.stream_indexes = stream_indexes or []  # sink_input indexes (app mode)

    def __repr__(self):
        return "Session({!r}, vol={}, muted={}, default={})".format(
            self.name, self.volume, self.muted, self.is_default
        )


def app_group_key(proplist):
    return (
        proplist.get("application.name")
        or proplist.get("application.process.binary")
        or proplist.get("media.name")
        or "Unknown"
    )


def app_group_id(key):
    return zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF


class PulseAudioAdapter:
    """Thin wrapper around pulsectl that survives server restarts."""

    def __init__(self):
        self.pulse = None
        self.connected = False
        self.retry_at = 0.0

    def connect(self):
        if time.monotonic() < self.retry_at:
            return False
        self.retry_at = time.monotonic() + RECONNECT_INTERVAL
        try:
            self.pulse = pulsectl.Pulse("maxmix")
            self.connected = True
            log.info("Connected to PulseAudio/PipeWire server")
            return True
        except Exception as exc:
            self.connected = False
            log.debug("Pulse connection failed: %s", exc)
            return False

    def disconnect(self):
        self.connected = False
        if self.pulse is not None:
            try:
                self.pulse.close()
            except Exception:
                pass
            self.pulse = None

    def _run(self, fn):
        """Run a pulse operation, returning None (and reconnecting) on failure."""
        if not self.connected and not self.connect():
            return None
        try:
            return fn()
        except Exception as exc:
            log.warning("Pulse operation failed (%s), reconnecting", exc)
            self.disconnect()
            return None

    # --- enumeration -------------------------------------------------------

    def snapshot(self):
        """Return {mode: [Session, ...]} or None when the server is unreachable."""

        def collect():
            server = self.pulse.server_info()
            result = {MODE_OUTPUT: [], MODE_INPUT: [], MODE_APPLICATION: [], MODE_GAME: []}

            for sink in self.pulse.sink_list():
                result[MODE_OUTPUT].append(
                    Session(
                        sink.index,
                        sink.description or sink.name,
                        round(sink.volume.value_flat * 100),
                        bool(sink.mute),
                        sink.name == server.default_sink_name,
                        MODE_OUTPUT,
                    )
                )

            for source in self.pulse.source_list():
                if source.name.endswith(".monitor"):
                    continue
                result[MODE_INPUT].append(
                    Session(
                        source.index,
                        source.description or source.name,
                        round(source.volume.value_flat * 100),
                        bool(source.mute),
                        source.name == server.default_source_name,
                        MODE_INPUT,
                    )
                )

            groups = OrderedDict()
            for stream in self.pulse.sink_input_list():
                key = app_group_key(stream.proplist)
                group = groups.get(key)
                if group is None:
                    groups[key] = group = Session(
                        app_group_id(key),
                        key,
                        round(stream.volume.value_flat * 100),
                        bool(stream.mute),
                        False,
                        MODE_APPLICATION,
                        [stream.index],
                    )
                else:
                    group.stream_indexes.append(stream.index)

            apps = list(groups.values())
            apps.sort(key=lambda s: s.name.lower())
            result[MODE_APPLICATION] = apps
            result[MODE_GAME] = [
                Session(a.sid, a.name, a.volume, a.muted, a.is_default, MODE_GAME, list(a.stream_indexes))
                for a in apps
            ]

            result[MODE_OUTPUT].sort(key=lambda s: s.sid)
            result[MODE_INPUT].sort(key=lambda s: s.sid)
            return result

        return self._run(collect)

    # --- mutation ----------------------------------------------------------

    def apply(self, session, volume=None, muted=None, set_default=False):
        """Apply volume / mute / default-device changes to a session."""

        def act():
            if session.mode == MODE_APPLICATION or session.mode == MODE_GAME:
                targets = []
                for index in session.stream_indexes:
                    try:
                        targets.append(self.pulse.sink_input_info(index))
                    except Exception:
                        pass  # stream vanished since enumeration
            elif session.mode == MODE_INPUT:
                targets = [s for s in self.pulse.source_list() if s.index == session.sid]
            else:
                targets = [s for s in self.pulse.sink_list() if s.index == session.sid]

            if not targets:
                log.debug("No pulse object matches session %r", session.name)
                return False
            for info in targets:
                if volume is not None:
                    # pulsectl expects fractions (0..1), not raw pa_volume_t units
                    vol = pulsectl.PulseVolumeInfo([volume / 100.0] * len(info.volume.values))
                    self.pulse.volume_set(info, vol)
                if muted is not None:
                    self.pulse.mute(info, muted)
                if set_default and not info.name.endswith(".monitor"):
                    try:
                        self.pulse.default_set(info)
                    except Exception as exc:
                        log.warning("Could not set default device: %s", exc)
            return True

        return self._run(act) or False


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


class MaxMixDaemon:
    def __init__(self, args):
        self.args = args
        self.audio = PulseAudioAdapter()
        self.settings = Settings(
            sleep_after=args.sleep_after,
            acceleration=args.acceleration,
            continuous_scroll=args.continuous_scroll,
            volume_min_color=args.volume_min_color,
            volume_max_color=args.volume_max_color,
            channel_a_color=args.channel_a_color,
            channel_b_color=args.channel_b_color,
            volume_step=getattr(args, "volume_step", 1.0),
        )

        self.ser = None
        self.connected = False
        self.ready = False  # stop-and-wait: true once the device ACKed the last message
        self.last_write = 0.0
        self.last_read = 0.0
        self.rx_buffer = b""
        self.outbox = OrderedDict()  # command byte -> payload bytes (replaced if re-queued)
        self.firmware = None

        self.mode = {
            "output": MODE_OUTPUT,
            "input": MODE_INPUT,
            "application": MODE_APPLICATION,
        }[args.mode]
        self.current = 0
        self.sessions = []  # [Session] for the active mode
        self.next_pulse_poll = 0.0
        self.next_connect_attempt = 0.0

    # ---------------------------------------------------------------- serial

    def candidate_ports(self):
        if self.args.port:
            return [self.args.port]
        ports = []
        for pattern in ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*"):
            ports.extend(sorted(glob.glob(pattern)))
        unique, seen = [], set()
        for port in ports:
            real = os.path.realpath(port)
            if real not in seen:
                seen.add(real)
                unique.append(port)
        return unique

    def try_connect(self, now):
        if now < self.next_connect_attempt:
            return
        self.next_connect_attempt = now + RECONNECT_INTERVAL

        for port in self.candidate_ports():
            firmware = self.handshake(port)
            if firmware is None:
                continue
            if firmware not in COMPATIBLE_FIRMWARE:
                log.warning(
                    "Device on %s reports firmware %s (known good: %s)",
                    port,
                    firmware,
                    ", ".join(sorted(COMPATIBLE_FIRMWARE)),
                )
                if not self.args.force:
                    self.close_serial()
                    continue
            self.firmware = firmware
            self.connected = True
            self.ready = True
            self.last_read = now
            self.last_write = now
            log.info("MaxMix connected on %s (firmware %s)", port, firmware)
            self.on_device_connected()
            return

    def handshake(self, port):
        """Open a port and perform the TEST handshake. Returns firmware string or None."""
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
                write_timeout=0.5,
            )
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception as exc:
            log.debug("Cannot open %s: %s", port, exc)
            self.close_serial()
            return None

        try:
            # Opening the port resets classic Arduino Nanos; they sit in the
            # bootloader for ~2s, so retry the TEST message for a while.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                self.ser.write(bytes([CMD_TEST]))
                reply = self.ser.read(1)
                if reply == bytes([CMD_TEST]):
                    line = b""
                    while time.monotonic() < deadline and len(line) < 32:
                        chunk = self.ser.read(1)
                        if not chunk:
                            continue
                        if chunk == b"\n":
                            break
                        if chunk != b"\r":
                            line += chunk
                    firmware = line.decode("ascii", "replace").strip()
                    if firmware:
                        return firmware
            return None
        except Exception as exc:
            log.debug("Handshake with %s failed: %s", port, exc)
            return None
        finally:
            if not (self.ser and self.ser.is_open):
                self.close_serial()

    def close_serial(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def disconnect(self, reason):
        if self.connected or self.ser is not None:
            log.info("Device disconnected (%s)", reason)
        self.connected = False
        self.ready = False
        self.close_serial()
        self.rx_buffer = b""
        self.outbox.clear()

    def write_message(self, command, payload=b""):
        # Command byte and payload MUST go out in one write: the firmware
        # reads the payload with a 10ms serial timeout after the command byte.
        try:
            self.ser.write(bytes([command]) + payload)
            self.ser.flush()
        except Exception as exc:
            log.debug("Write failed: %s", exc)
            self.disconnect("write error")
            return False
        self.last_write = time.monotonic()
        self.ready = False
        log.debug("TX %-2d %s", command, payload.hex())
        return True

    def maybe_write(self, now):
        if not (self.connected and self.ser is not None and self.ready):
            return
        if now - self.last_write > KEEPALIVE_INTERVAL:
            # A plain OK byte keeps the firmware heartbeat alive.
            self.write_message(CMD_OK)
            return
        if self.outbox:
            command, payload = self.outbox.popitem(last=False)
            self.write_message(command, payload)

    # ------------------------------------------------------------------ read

    def read_serial(self, now):
        try:
            waiting = self.ser.in_waiting
            if waiting <= 0:
                return
            self.rx_buffer += self.ser.read(waiting)
        except Exception as exc:
            log.debug("Read failed: %s", exc)
            self.disconnect("read error")
            return

        while self.rx_buffer:
            command = self.rx_buffer[0]
            if command == CMD_TEST:
                # TEST is followed by a version line terminated by \n.
                index = self.rx_buffer.find(b"\n")
                if index < 0:
                    return  # wait for the rest of the line
                self.rx_buffer = self.rx_buffer[index + 1 :]
                self.last_read = now
                continue
            if command == CMD_OK:
                # Bare command byte, no payload: the device ACKed our last
                # message (or replied to our keepalive).
                self.rx_buffer = self.rx_buffer[1:]
                self.last_read = now
                self.ready = True
                log.debug("RX OK")
                continue

            size = PAYLOAD_SIZES.get(command)
            if size is None:
                # Unknown/garbage byte - drop it and resync.
                log.debug("RX unknown command byte 0x%02x, dropping", command)
                self.rx_buffer = self.rx_buffer[1:]
                continue
            if len(self.rx_buffer) < 1 + size:
                return  # wait for the full payload

            payload = self.rx_buffer[1 : 1 + size]
            self.rx_buffer = self.rx_buffer[1 + size :]
            self.last_read = now
            log.debug("RX %-2d %s", command, payload.hex())
            self.handle_message(command, payload)

    def handle_message(self, command, payload):
        if command == CMD_OK:
            self.ready = True
        elif CMD_VOLUME_CURR_CHANGE <= command <= CMD_VOLUME_NEXT_CHANGE:
            self.on_volume_change(command - CMD_VOLUME_CURR_CHANGE, payload)
        elif command == CMD_SESSION_INFO:
            mode, current, _out, _in, _app = payload
            self.on_session_info(mode, current)
        elif command == CMD_ALTERNATE_SESSION:
            log.debug("Game mode alternate: %s", payload[:30].split(b"\x00")[0].decode("utf-8", "replace"))
        elif command == CMD_MODE_STATES:
            pass  # informational only
        else:
            log.debug("Ignoring inbound command %d", command)

    # ----------------------------------------------------------------- state

    def queue_message(self, command, payload=b""):
        # Replacing a pending message of the same type matches the Windows app.
        self.outbox[command] = payload

    def enumerate_sessions(self):
        snapshot = self.audio.snapshot()
        if snapshot is None:
            return False
        self.latest_snapshot = snapshot
        self.sessions = snapshot[self.mode]
        return True

    def session_counts(self):
        snapshot = getattr(self, "latest_snapshot", None)
        if snapshot is None:
            return 0, 0, 0
        return len(snapshot[MODE_OUTPUT]), len(snapshot[MODE_INPUT]), len(snapshot[MODE_APPLICATION])

    def queue_session_info(self):
        out, inp, app = self.session_counts()
        self.queue_message(CMD_SESSION_INFO, pack_session_info(self.mode, self.current, out, inp, app))

    def queue_settings(self):
        self.queue_message(CMD_SETTINGS, pack_device_settings(self.settings))

    def push_settings(self):
        """Public hook for the GUI: re-send device settings (safe anytime)."""
        self.queue_settings()

    def slot_list_indices(self):
        """[(slot, list_index)] for CURRENT/PREVIOUS/NEXT around self.current."""
        count = len(self.sessions)
        if count == 0:
            return []
        index = min(max(self.current, 0), count - 1)
        return [
            (SLOT_CURRENT, index),
            (SLOT_PREVIOUS, (index - 1 + count) % count),
            (SLOT_NEXT, (index + 1) % count),
        ]

    def flush_sessions(self):
        """Queue CURRENT/PREVIOUS/NEXT session data around the current index."""
        count = len(self.sessions)
        if 0 <= self.current < count:
            index = self.current
        else:
            index = 0
            self.current = 0
            self.queue_session_info()

        if count == 0:
            for command in (CMD_CURRENT_SESSION, CMD_PREVIOUS_SESSION, CMD_NEXT_SESSION):
                self.queue_message(command, pack_session_data(0, "", False, 0, False))
            return

        for slot, list_index in self.slot_list_indices():
            session = self.sessions[list_index]
            self.queue_message(
                CMD_CURRENT_SESSION + slot,
                pack_session_data(list_index, session.name, session.is_default, session.volume, session.muted),
            )

    def on_device_connected(self):
        if not self.enumerate_sessions():
            log.warning("Audio server not reachable yet; will sync once it appears")
            self.sessions = []
        self.current = self.default_session_index()
        self.queue_settings()
        self.flush_sessions()
        self.queue_message(CMD_MODE_STATES, pack_mode_states())
        self.queue_session_info()

    def default_session_index(self):
        for index, session in enumerate(self.sessions):
            if session.is_default:
                return index
        return 0

    def on_session_info(self, mode, current):
        # The device changed its current index, or switched mode entirely.
        if mode not in (MODE_OUTPUT, MODE_INPUT, MODE_APPLICATION, MODE_GAME):
            return  # splash is device-internal, nothing to enumerate
        new_mode = mode != self.mode
        self.current = current
        if new_mode:
            self.mode = mode
            if not self.enumerate_sessions():
                return
            self.current = self.default_session_index()
            self.flush_sessions()
            self.queue_session_info()
        else:
            self.flush_sessions()

    def on_volume_change(self, slot, payload):
        item_id, is_default, volume, is_muted = unpack_volume_data(payload)
        if item_id >= len(self.sessions):
            log.debug("Volume change for unknown session id %d", item_id)
            return
        session = self.sessions[item_id]
        was_default = session.is_default
        make_default = is_default and not was_default and session.mode in (MODE_OUTPUT, MODE_INPUT)

        # Host-side volume step: scale the delta the firmware reports.
        scaled = apply_volume_step(session.volume, volume, self.settings.volume_step)

        # Update the host-side cache first so the next pulse poll does not
        # echo these same values back to the device.
        session.volume = scaled
        session.muted = is_muted
        if make_default:
            session.is_default = True

        log.info(
            "Device: %s -> volume %d%%%s%s",
            session.name,
            scaled,
            " (muted)" if is_muted else "",
            " (default)" if make_default else "",
        )
        self.audio.apply(session, volume=scaled, muted=is_muted, set_default=make_default)

        # The firmware advanced its own copy of the volume by the unscaled
        # step; push the real value back so the next detent is computed from
        # where we actually ended up.
        if scaled != volume:
            for slot_, list_index in self.slot_list_indices():
                if list_index == item_id:
                    self.queue_message(
                        CMD_VOLUME_CURR_CHANGE + slot_,
                        pack_volume_data(item_id, session.is_default, scaled, is_muted),
                    )
                    break

    # ----------------------------------------------------------------- pulse

    def pulse_poll(self, now):
        if now < self.next_pulse_poll:
            return
        self.next_pulse_poll = now + PULSE_POLL_INTERVAL

        snapshot = self.audio.snapshot()
        if snapshot is None:
            return

        fresh = snapshot[self.mode]
        old_ids = [s.sid for s in self.sessions]
        new_ids = [s.sid for s in fresh]

        if old_ids != new_ids:
            self.on_sessions_changed(fresh)
            return

        # Same members: forward volume / mute / name / default changes.
        for index, (session, new_session) in enumerate(zip(self.sessions, fresh)):
            if (
                session.volume == new_session.volume
                and session.muted == new_session.muted
                and session.is_default == new_session.is_default
                and session.name == new_session.name
            ):
                continue
            name_changed = session.name != new_session.name
            session.volume = new_session.volume
            session.muted = new_session.muted
            session.is_default = new_session.is_default
            session.name = new_session.name
            if name_changed:
                base_command, payload = CMD_CURRENT_SESSION, pack_session_data(
                    index, session.name, session.is_default, session.volume, session.muted
                )
            else:
                base_command, payload = CMD_VOLUME_CURR_CHANGE, pack_volume_data(
                    index, session.is_default, session.volume, session.muted
                )
            for slot, list_index in self.slot_list_indices():
                if list_index == index:
                    self.queue_message(base_command + slot, payload)
                    break

    def on_sessions_changed(self, fresh):
        """Called when members of the active mode were added or removed."""
        log.debug("Mode %d sessions: %s", self.mode, [s.name for s in fresh])
        previous = self.sessions
        self.latest_sessions_fresh = fresh

        if not fresh:
            # Like the Windows app, advance to the next non-empty mode.
            self.mode = self.mode + 1 if self.mode < MODE_GAME else MODE_OUTPUT
            snapshot = self.audio.snapshot()
            if snapshot is not None and snapshot[self.mode]:
                self.sessions = snapshot[self.mode]
                self.current = self.default_session_index()
                self.flush_sessions()
                self.queue_session_info()
                return
            self.sessions = fresh
        else:
            # Keep pointing at the same item; if it disappeared, fall back to
            # the default one. Jump to brand-new applications, like Windows.
            current_sid = self.sessions[self.current].sid if 0 <= self.current < len(self.sessions) else None
            new_sids = {s.sid for s in fresh}
            if current_sid not in new_sids and previous is not fresh:
                self.current = next(
                    (i for i, s in enumerate(fresh) if s.is_default),
                    min(self.current, len(fresh) - 1),
                )
            elif self.mode in (MODE_APPLICATION, MODE_GAME):
                added = [s for s in fresh if s.sid not in {p.sid for p in previous}]
                if len(added) == 1:
                    self.current = next(i for i, s in enumerate(fresh) if s.sid == added[0].sid)
                elif current_sid is not None:
                    self.current = next(i for i, s in enumerate(fresh) if s.sid == current_sid)
            elif current_sid is not None:
                # Sinks around the current one appeared or vanished (screen
                # lock, monitor standby, USB audio re-enumeration), so the
                # list re-sorted: self.current is a position and must be
                # re-anchored to the session it pointed at, or the device
                # silently slides onto another output.
                self.current = next(i for i, s in enumerate(fresh) if s.sid == current_sid)
            self.sessions = fresh

        self.flush_sessions()
        self.queue_session_info()

    # ------------------------------------------------------------------ loop

    def stop(self, *_):
        self._stop = True

    def run(self, install_signals=True):
        self._stop = False
        if install_signals:
            try:
                signal.signal(signal.SIGINT, self.stop)
                signal.signal(signal.SIGTERM, self.stop)
            except ValueError:  # not the main thread
                pass

        log.info("MaxMix daemon started (port: %s)", self.args.port or "auto-detect")
        while not self._stop:
            now = time.monotonic()

            if not self.connected:
                self.try_connect(now)
            else:
                self.read_serial(now)
                if self.connected and now - self.last_read > DEVICE_TIMEOUT:
                    self.disconnect("timeout")
                if self.connected:
                    self.pulse_poll(now)
                    self.maybe_write(now)

            time.sleep(TICK_INTERVAL)

        self.disconnect("shutdown")
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Companion daemon for the MaxMix USB volume controller on Linux."
    )
    parser.add_argument("-p", "--port", help="serial port of the device (default: auto-detect)")
    parser.add_argument(
        "-m",
        "--mode",
        choices=("output", "input", "application"),
        default="output",
        help="mode the device starts in (default: output)",
    )
    parser.add_argument(
        "-s",
        "--sleep-after",
        type=int,
        default=5,
        metavar="SECONDS",
        help="sleep the display after N seconds of inactivity, 0 disables (default: 5)",
    )
    parser.add_argument(
        "-a",
        "--acceleration",
        type=int,
        default=60,
        metavar="PERCENT",
        help="encoder acceleration 0-100 (default: 60)",
    )
    parser.add_argument(
        "--volume-step",
        type=parse_volume_step,
        default=1.0,
        metavar="RATIO",
        help="volume change per knob detent as a multiplier of the firmware "
        "default (0.1-10, default: 1.0; 0.5 = half as fast per detent)",
    )
    parser.add_argument(
        "--no-continuous-scroll",
        dest="continuous_scroll",
        action="store_false",
        help="do not loop around when scrolling past the first/last item",
    )
    parser.add_argument(
        "--volume-min-color", type=parse_color, default=0x0000FF, metavar="RRGGBB",
        help="LED color at volume 0 (default: 0000FF)",
    )
    parser.add_argument(
        "--volume-max-color", type=parse_color, default=0xFF0000, metavar="RRGGBB",
        help="LED color at volume 100 (default: FF0000)",
    )
    parser.add_argument(
        "--channel-a-color", type=parse_color, default=0x0000FF, metavar="RRGGBB",
        help="game mode channel A LED color (default: 0000FF)",
    )
    parser.add_argument(
        "--channel-b-color", type=parse_color, default=0xFF00FF, metavar="RRGGBB",
        help="game mode channel B LED color (default: FF00FF)",
    )
    parser.add_argument("-f", "--force", action="store_true", help="connect even to unknown firmware versions")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging (shows protocol traffic)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return MaxMixDaemon(args).run()


if __name__ == "__main__":
    sys.exit(main())
