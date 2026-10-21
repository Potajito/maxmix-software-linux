#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# maxmix_hid.py - volume knob daemon for HID-joystick volume controllers.
#
# Some volume knobs built on Arduino Leonardo / Pro Micro boards (32u4) do not
# use the MaxMix serial protocol at all: they enumerate as a generic USB
# HID Joystick (32 buttons, 6 axes - the Arduino Joystick library defaults)
# and rely on a desktop application to translate joystick events into volume
# changes. Such a knob previously only worked on Windows; this daemon gives
# it the same behaviour on Linux:
#
#   - rotate knob  -> default output device volume up/down
#   - press knob   -> toggle mute
#
# Requires Python 3.8+ with `pulsectl` (pip install pulsectl).
#
# Run with -v and rotate the knob once to see which axis/button your knob
# uses, then restrict with --axis/--button if needed.
# ---------------------------------------------------------------------------

import argparse
import glob
import logging
import os
import select
import signal
import struct
import sys
import time

try:
    import pulsectl
except ImportError:  # pragma: no cover
    sys.exit("pulsectl is required: pip install pulsectl")


log = logging.getLogger("maxmix-hid")

# --- Linux input protocol -------------------------------------------------

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
INPUT_EVENT = struct.Struct("llHHi")  # 24 bytes on 64-bit

EV_KEY = 0x01
EV_ABS = 0x03

ABS_NAMES = {
    0x00: "X",
    0x01: "Y",
    0x02: "Z",
    0x03: "Rx",
    0x04: "Ry",
    0x05: "Rz",
    0x0A: "Brake",
    0x0B: "Gas",
    0x28: "Misc",
}

# Joystick buttons appear in the BTN_JOYSTICK..BTN_DEAD range (0x120-0x12f).
BTN_MIN = 0x120
BTN_MAX = 0x12F

# Deltas of the opposite direction within this window are treated as the
# knob returning its axis to rest (or contact bounce), not a real turn.
REVERSE_GUARD = 0.15


def eviocgabs(fd, code):
    """Return (value, min, max) for an ABS code, or None."""
    import fcntl

    buf = bytearray(24)
    try:
        fcntl.ioctl(fd, 0x80184540 + code, buf)  # _IOR('E', 0x40+code, 6 * int)
        value, minimum, maximum = struct.unpack("6i", bytes(buf))[:3]
        return value, minimum, maximum
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Volume control (PulseAudio / PipeWire)
# ---------------------------------------------------------------------------


class VolumeController:
    def __init__(self):
        self.pulse = None
        self.retry_at = 0.0

    def _connect(self):
        if time.monotonic() < self.retry_at:
            return False
        self.retry_at = time.monotonic() + 1.0
        try:
            self.pulse = pulsectl.Pulse("maxmix-hid")
            return True
        except Exception as exc:
            log.debug("Pulse connection failed: %s", exc)
            self.pulse = None
            return False

    def _default_sink(self):
        if self.pulse is None and not self._connect():
            return None
        try:
            name = self.pulse.server_info().default_sink_name
            return self.pulse.get_sink_by_name(name)
        except Exception as exc:
            log.warning("Pulse error (%s), reconnecting", exc)
            try:
                self.pulse.close()
            except Exception:
                pass
            self.pulse = None
            return None

    def step(self, delta):
        sink = self._default_sink()
        if sink is None:
            return
        current = round(sink.volume.value_flat * 100)
        target = max(0, min(100, current + delta))
        try:
            # pulsectl expects fractions (0..1), not raw pa_volume_t units
            vol = pulsectl.PulseVolumeInfo([target / 100.0] * len(sink.volume.values))
            self.pulse.volume_set(sink, vol)
            log.info("Volume: %d%% -> %d%%", current, target)
        except Exception as exc:
            log.warning("Volume set failed: %s", exc)

    def toggle_mute(self):
        sink = self._default_sink()
        if sink is None:
            return
        try:
            self.pulse.mute(sink, not sink.mute)
            log.info("Mute: %s", "ON" if not sink.mute else "OFF")
        except Exception as exc:
            log.warning("Mute failed: %s", exc)


# ---------------------------------------------------------------------------
# Knob event handling
# ---------------------------------------------------------------------------


class Knob:
    """Translates joystick ABS/KEY events into volume actions."""

    def __init__(self, controller, step, axis=None, button=None, min_delta=2):
        self.controller = controller
        self.step = step
        self.axis = axis   # ABS code to react to, or None for any
        self.button = button  # BTN code to react to, or None for any
        self.min_delta = max(1, min_delta)  # ignore +-1 jitter
        self.last_value = {}
        self.range = {}
        self.last_direction = {}
        self.last_turn_at = {}

    def handle(self, event_type, code, value):
        if event_type == EV_ABS:
            self.on_abs(code, value)
        elif event_type == EV_KEY:
            self.on_key(code, value)

    def on_abs(self, code, value):
        name = ABS_NAMES.get(code, "ABS_%02x" % code)
        if self.axis is not None and code != self.axis:
            log.debug("Ignoring %s axis (restricted to %s)", name, ABS_NAMES.get(self.axis))
            return

        if code not in self.range:
            # First sighting: remember position, don't act.
            self.last_value[code] = value
            log.info("Knob axis detected: %s (position %d)", name, value)
            return

        lo, hi = self.range[code]
        span = max(2, hi - lo + 1)  # number of distinct values, e.g. 256 for 0..255
        half = span // 2
        delta = ((value - self.last_value[code] + half) % span) - half
        # A jump covering at least half the range in one event is a "pulse"
        # (some knobs slam the axis to the rail and back instead of counting);
        # treat it as a single detent rather than filtered jitter.
        raw_distance = abs(value - self.last_value[code])
        is_pulse = raw_distance >= half
        self.last_value[code] = value
        if delta == 0:
            return
        if not is_pulse and abs(delta) < self.min_delta:
            log.debug("%s jitter %+d, ignored", name, delta)
            return

        now = time.monotonic()
        direction = 1 if delta > 0 else -1
        if (
            direction == -self.last_direction.get(code, 0)
            and now - self.last_turn_at.get(code, 0) < REVERSE_GUARD
        ):
            log.debug("%s returned to rest (%+d), ignored", name, delta)
            return
        self.last_direction[code] = direction
        self.last_turn_at[code] = now

        change = direction * self.step
        log.debug("%s %+d -> volume %+d", name, delta, change)
        self.controller.step(change)

    def on_key(self, code, value):
        if value != 1:  # react to press only
            return
        if not (BTN_MIN <= code <= BTN_MAX):
            log.debug("Ignoring key code 0x%x", code)
            return
        if self.button is not None and code != self.button:
            return
        log.info("Knob button pressed (BTN 0x%x)", code)
        self.controller.toggle_mute()

    def prime(self, fd):
        """Record the current position of every ABS axis so the first turn
        produces a delta instead of being swallowed by first-sighting."""
        for code in ABS_NAMES:
            info = eviocgabs(fd, code)
            if info is not None:
                value, lo, hi = info
                self.last_value[code] = value
                self.range[code] = (lo, hi if hi > lo else 255)
                log.debug("Axis %s primed: value=%d range=[%d..%d]", ABS_NAMES[code], value, lo, hi)


# ---------------------------------------------------------------------------
# Device discovery / main loop
# ---------------------------------------------------------------------------


def find_devices():
    """Return candidate /dev/input event nodes that look like joysticks."""
    candidates = []
    for path in sorted(glob.glob("/dev/input/by-id/*-event-joystick")) + sorted(
        glob.glob("/dev/input/event*")
    ):
        try:
            real = os.path.realpath(path)
            with open("/sys/class/input/%s/device/name" % os.path.basename(real), "r") as fh:
                name = fh.read().strip()
        except OSError:
            continue
        candidates.append((path, name))
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn an HID-joystick volume knob into a Linux volume control."
    )
    parser.add_argument("-d", "--device", help="input event device (default: the only joystick)")
    parser.add_argument("--axis", choices=ABS_NAMES.values(), help="only react to this axis")
    parser.add_argument("--button", type=lambda t: int(t, 0), metavar="CODE",
                        help="only react to this button code (e.g. 0x120)")
    parser.add_argument("--step", type=int, default=4, metavar="PERCENT",
                        help="volume change per knob detent (default: 4)")
    parser.add_argument("--min-delta", type=int, default=2, metavar="N",
                        help="ignore axis deltas smaller than N (jitter filter, default: 2)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    device = args.device
    if device is None:
        candidates = find_devices()
        joysticks = [(path, name) for path, name in candidates if "joystick" in path]
        arduinos = [(path, name) for path, name in joysticks if "arduino" in name.lower()]
        if len(joysticks) == 1:
            device = joysticks[0][0]
        elif len(arduinos) == 1:
            # Several joysticks but only one Arduino - that's the knob.
            device = arduinos[0][0]
        elif not candidates:
            sys.exit("No input devices found. Is the knob plugged in?")
        else:
            print("Multiple joystick devices found, pick one with --device:")
            for path, name in joysticks:
                print("  %-64s %s" % (path, name))
            return 2
    axis_code = next((c for c, n in ABS_NAMES.items() if n == args.axis), None) if args.axis else None

    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        sys.exit("Cannot open %s: %s" % (device, exc))

    controller = VolumeController()
    knob = Knob(controller, max(1, min(50, args.step)), axis_code, args.button, args.min_delta)
    knob.prime(fd)

    running = {"stop": False}

    def stop(*_):
        running["stop"] = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    log.info("Listening on %s (axis=%s step=%d%%)", device, args.axis or "any", args.step)
    while not running["stop"]:
        try:
            readable, _, _ = select.select([fd], [], [], 0.25)
        except InterruptedError:
            continue
        if not readable:
            continue
        try:
            data = os.read(fd, INPUT_EVENT.size * 32)
        except BlockingIOError:
            continue
        except OSError as exc:
            log.error("Read failed: %s", exc)
            break
        for offset in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
            _sec, _usec, event_type, code, value = INPUT_EVENT.unpack_from(data, offset)
            knob.handle(event_type, code, value)

    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
