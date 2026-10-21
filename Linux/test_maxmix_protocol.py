#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# test_maxmix_protocol.py - end-to-end test of the maxmix.py daemon against a
# simulated firmware running on a pseudo-terminal.
#
# The fake firmware mirrors Embedded/MaxMix/Communications.cpp:
#   - replies to TEST with the command byte + version + CRLF
#   - reads the payload of every known command, then always replies OK
#   - can be told to "turn the knob" (send VOLUME_CURR_CHANGE)
#
# Run:  .venv/bin/python test_maxmix_protocol.py
# ---------------------------------------------------------------------------

import os
import select
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import maxmix
from maxmix import (
    CMD_TEST, CMD_OK, CMD_SETTINGS, CMD_SESSION_INFO, CMD_CURRENT_SESSION,
    CMD_PREVIOUS_SESSION, CMD_NEXT_SESSION, CMD_MODE_STATES,
    CMD_VOLUME_CURR_CHANGE, PAYLOAD_SIZES, MaxMixDaemon, unpack_volume_data,
)


class FakeFirmware(threading.Thread):
    VERSION = b"1.5.6"

    def __init__(self, master_fd):
        super().__init__(daemon=True)
        self.master_fd = master_fd
        self.rx_payloads = {}   # command -> last payload
        self.rx_order = []      # commands in arrival order
        self.saw_keepalive = False
        self._rx = b""

    def send_ok(self):
        os.write(self.master_fd, bytes([CMD_OK]))

    def turn_knob(self, item_id, volume, muted=False, is_default=False):
        os.write(self.master_fd, bytes([CMD_VOLUME_CURR_CHANGE]) +
                 maxmix.pack_volume_data(item_id, is_default, volume, muted))

    def run(self):
        while True:
            r, _, _ = select.select([self.master_fd], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.master_fd, 512)
            except OSError:
                return
            if not data:
                return
            self._rx += data
            while self._rx:
                command = self._rx[0]
                if command == CMD_TEST:
                    self._rx = self._rx[1:]
                    os.write(self.master_fd, bytes([CMD_TEST]) + self.VERSION + b"\r\n")
                    continue
                size = PAYLOAD_SIZES.get(command, 0)
                if len(self._rx) < 1 + size:
                    break
                payload = self._rx[1 : 1 + size]
                self._rx = self._rx[1 + size :]
                self.rx_payloads[command] = payload
                self.rx_order.append(command)
                if command == CMD_OK:
                    self.saw_keepalive = True
                self.send_ok()


def main():
    import argparse

    master_fd, slave_fd = os.openpty()
    port = os.ttyname(slave_fd)

    args = argparse.Namespace(
        port=port, mode="output", sleep_after=5, acceleration=60,
        continuous_scroll=True, volume_min_color=0x0000FF, volume_max_color=0xFF0000,
        channel_a_color=0x0000FF, channel_b_color=0xFF00FF, force=False, verbose=False,
    )

    fake = FakeFirmware(master_fd)
    fake.start()

    daemon = MaxMixDaemon(args)
    thread = threading.Thread(target=lambda: daemon.run(install_signals=False), daemon=True)
    thread.start()

    try:
        deadline = time.monotonic() + 5
        required = {CMD_SETTINGS, CMD_CURRENT_SESSION, CMD_PREVIOUS_SESSION,
                    CMD_NEXT_SESSION, CMD_MODE_STATES, CMD_SESSION_INFO}
        while time.monotonic() < deadline and not required.issubset(fake.rx_payloads):
            time.sleep(0.05)
        assert required.issubset(fake.rx_payloads), "daemon never pushed full state: %s" % fake.rx_order

        # SETTINGS payload should carry the defaults: sleep=5, accel=60|0x80
        settings = fake.rx_payloads[CMD_SETTINGS]
        assert settings[0] == 5 and settings[1] == 0xBC, settings.hex()

        # SESSION_INFO: mode=output(1), current points at default sink, counts sane
        info = fake.rx_payloads[CMD_SESSION_INFO]
        mode, current, out_count, in_count, app_count = info
        assert mode == 1, info.hex()  # MODE_OUTPUT == 1 in the firmware enum

        # CURRENT_SESSION must contain the name of a real sink
        current_data = fake.rx_payloads[CMD_CURRENT_SESSION]
        name = current_data[:30].split(b"\x00")[0].decode()
        item_id, is_default, volume, muted = unpack_volume_data(current_data[30:32])
        assert item_id == current, (item_id, current)
        print("device shows: %r vol=%d default=%s" % (name, volume, is_default))
        assert name and len(name) > 0

        # Keepalive traffic observed
        time.sleep(1.4)
        assert fake.saw_keepalive, "no keepalive OK seen"

        # Knob turn: volume of session 0 should hit the real default sink
        import pulsectl
        pulse = pulsectl.Pulse("test")
        sink_name = pulse.server_info().default_sink_name
        sink = pulse.get_sink_by_name(sink_name)
        before = round(sink.volume.value_flat * 100)
        fake.turn_knob(item_id, 51)
        time.sleep(1.0)
        after = round(pulse.get_sink_by_name(sink_name).volume.value_flat * 100)
        print("sink %r: %d%% -> %d%%" % (sink_name, before, after))
        assert after == 51, "knob turn did not set volume to 51"

        # Host-side volume step: with ratio 2.0, a +1 detent becomes +2, and
        # the daemon must push the corrected value back to the device so its
        # internal state stays in sync.
        daemon.settings.volume_step = 2.0
        fake.turn_knob(item_id, 52)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            now_vol = round(pulse.get_sink_by_name(sink_name).volume.value_flat * 100)
            if now_vol == 53:
                break
            time.sleep(0.05)
        assert now_vol == 53, "step scaling did not double the detent (got %s)" % now_vol
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and fake.rx_payloads.get(CMD_VOLUME_CURR_CHANGE) is None:
            time.sleep(0.05)
        echo = fake.rx_payloads.get(CMD_VOLUME_CURR_CHANGE)
        assert echo == maxmix.pack_volume_data(item_id, True, 53, False), (
            "no device re-sync echo: %s" % (echo and echo.hex())
        )
        print("volume step 2.0x: detent +1 -> %d%%, device re-synced" % now_vol)
        daemon.settings.volume_step = 1.0

        # restore
        pulse.sink_volume_set(sink.index, pulsectl.PulseVolumeInfo([before / 100.0] * len(sink.volume.values)))
        pulse.close()

        print("PROTOCOL TEST PASSED")
        return 0
    finally:
        daemon.stop()
        thread.join(timeout=3)


if __name__ == "__main__":
    sys.exit(main())
