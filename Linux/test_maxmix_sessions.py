#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# test_maxmix_sessions.py - unit tests for the daemon's session-list bookkeeping
# (no serial device and no PulseAudio server needed).
#
# Regression tests for the "wrong output after locking the screen" bug:
# sinks can disappear and come back with new PipeWire indexes while the
# session is locked (monitor standby, USB audio autosuspend, ...).  The list
# re-sorts, and self.current - a plain position - must follow the *same
# session*, not the same position.
#
# Run:  .venv/bin/python test_maxmix_sessions.py
# ---------------------------------------------------------------------------

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from maxmix import (
    CMD_SESSION_INFO, MODE_OUTPUT, MaxMixDaemon, Session,
)


def make_daemon():
    args = argparse.Namespace(
        port=None, mode="output", sleep_after=5, acceleration=60,
        continuous_scroll=True, volume_min_color=0x0000FF, volume_max_color=0xFF0000,
        channel_a_color=0x0000FF, channel_b_color=0xFF0000, force=False, verbose=False,
        volume_step=1.0,
    )
    daemon = MaxMixDaemon(args)
    daemon.connected = True  # on_sessions_changed queues device messages
    return daemon


def sink(sid, name, is_default=False):
    return Session(sid, name, 50, False, is_default, MODE_OUTPUT)


# Sink layout as on the real machine: Scarlett (77) sorts before the Ryzen
# default (79), VirtualSink (142) and HDMI (232) after it.
FULL = [sink(77, "Scarlett"), sink(79, "Ryzen", True), sink(142, "VirtualSink"), sink(232, "HDMI")]


def current_sid(daemon):
    return daemon.sessions[daemon.current].sid


def test_sink_before_current_removed():
    """A sink before the current one vanishes (e.g. USB audio at lock): the
    knob must stay on the same output, not slide onto the next one."""
    daemon = make_daemon()
    daemon.sessions = list(FULL)
    daemon.current = 1  # Ryzen, the default

    fresh = [FULL[1], FULL[2], FULL[3]]  # Scarlett gone
    daemon.on_sessions_changed(fresh)

    assert current_sid(daemon) == 79, (
        "current drifted to %r after a preceding sink disappeared" % daemon.sessions[daemon.current].name
    )


def test_sink_before_current_returned():
    """A sink that vanished during the lock returns with a NEW index that
    sorts before the current one: the knob must stay on the same output."""
    daemon = make_daemon()
    daemon.sessions = [FULL[1], FULL[2], FULL[3]]  # Scarlett already gone
    daemon.current = 0  # Ryzen, still the default

    fresh = [FULL[0], sink(78, "Scarlett"), FULL[1], FULL[2], FULL[3]]
    daemon.on_sessions_changed(fresh)

    assert current_sid(daemon) == 79, (
        "current drifted to %r after a sink returned before it" % daemon.sessions[daemon.current].name
    )


def test_current_sink_removed_falls_back_to_default():
    """The selected sink itself vanishes: fall back to the default output."""
    daemon = make_daemon()
    daemon.sessions = list(FULL)
    daemon.current = 3  # HDMI, NOT the default

    daemon.on_sessions_changed([FULL[0], FULL[1], FULL[2]])  # HDMI gone

    assert current_sid(daemon) == 79, "should have fallen back to the default sink"


def test_removed_and_no_default_clamps():
    """Selected sink gone and no default flagged (transient server state):
    the index must at least stay inside the list."""
    daemon = make_daemon()
    daemon.sessions = list(FULL)
    daemon.current = 3  # HDMI, also the only one flagged default here
    daemon.sessions[1].is_default = False
    daemon.sessions[3].is_default = True

    daemon.on_sessions_changed([FULL[0], FULL[1], FULL[2]])

    assert 0 <= daemon.current < len(daemon.sessions), "index out of range after removal"


def test_session_info_requeued_with_corrected_index():
    """Whatever correction happens, the device must be told the new index."""
    daemon = make_daemon()
    daemon.sessions = list(FULL)
    daemon.current = 1

    daemon.on_sessions_changed([FULL[1], FULL[2], FULL[3]])

    payload = daemon.outbox[CMD_SESSION_INFO]
    assert payload[1] == daemon.current, "device not told the corrected current index"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print("PASS %s" % test.__name__)
    print("SESSION TESTS PASSED (%d)" % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
