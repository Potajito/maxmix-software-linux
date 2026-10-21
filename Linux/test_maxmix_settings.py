#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# test_maxmix_settings.py - unit tests for the settings file that the GUI
# writes and the CLI daemon reads (~/.config/maxmix/settings.json).
#
# No hardware, no audio server, no display needed.
# Run:  .venv/bin/python test_maxmix_settings.py
# ---------------------------------------------------------------------------

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import maxmix
from maxmix import (
    SETTINGS_DEFAULTS, MaxMixDaemon, Settings, load_saved_settings,
    parse_args, save_settings, settings_config_path,
)


class TempConfigDir:
    """Point XDG_CONFIG_HOME at a fresh directory for the duration."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="maxmix-settings-test-")
        self._old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.dir
        return self

    def __exit__(self, *_exc):
        if self._old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)


def main():
    # --- roundtrip: everything the GUI saves comes back unchanged ---------
    with TempConfigDir() as tmp:
        assert load_saved_settings() == {}, "no file yet, expected {}"
        settings = Settings(sleep_after=9, acceleration=75, continuous_scroll=False,
                            volume_min_color=0x123456, volume_max_color=0x654321,
                            channel_a_color=0x000001, channel_b_color=0xABCDEF,
                            volume_step=2.5)
        save_settings(settings)
        path = settings_config_path()
        assert path == os.path.join(tmp.dir, "maxmix", "settings.json"), path
        assert load_saved_settings(path) == settings.as_dict()
        assert settings.as_dict().keys() == SETTINGS_DEFAULTS.keys()

        # atomic write: no temp files left behind
        leftovers = os.listdir(os.path.dirname(path))
        assert leftovers == ["settings.json"], leftovers

    # --- daemon start with no file: built-in defaults ---------------------
    with TempConfigDir():
        args = parse_args([])
        assert args.sleep_after == 5 and args.acceleration == 60, vars(args)
        assert args.volume_min_color == 0x0000FF and args.volume_max_color == 0xFF0000
        assert args.continuous_scroll is True and args.volume_step == 1.0

        # --- saved settings replace the defaults, flags still win ---------
        save_settings(Settings(sleep_after=9, volume_step=0.5, continuous_scroll=True))
        args = parse_args([])
        assert args.sleep_after == 9, args.sleep_after
        assert args.volume_step == 0.5, args.volume_step
        assert args.continuous_scroll is True
        args = parse_args(["-s", "3", "--no-continuous-scroll", "--volume-step", "2"])
        assert args.sleep_after == 3, args.sleep_after
        assert args.volume_step == 2.0, args.volume_step
        assert args.continuous_scroll is False
        # unspecified fields still come from the file
        assert args.acceleration == 60

        # the daemon builds its Settings from the parsed args
        daemon = MaxMixDaemon(parse_args([]))
        assert daemon.settings.sleep_after == 9
        assert daemon.settings.volume_step == 0.5

        # --- corrupt entries fall back per-field, not per-file ------------
        with open(settings_config_path(), "w", encoding="utf-8") as fh:
            json.dump({
                "sleep_after": "nine",          # wrong type
                "acceleration": 999,            # out of range
                "volume_step": 0,               # below 0.1
                "volume_min_color": 0x12345678, # > 0xFFFFFF
                "continuous_scroll": False,     # valid
            }, fh)
        saved = load_saved_settings()
        assert saved == {"continuous_scroll": False}, saved
        args = parse_args([])
        assert args.sleep_after == 5 and args.volume_step == 1.0
        assert args.volume_min_color == 0x0000FF
        assert args.continuous_scroll is False and args.acceleration == 60

        # --- unreadable / corrupt file: behave as if absent ---------------
        with open(settings_config_path(), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert load_saved_settings() == {}
        args = parse_args([])
        assert args.sleep_after == 5

    print("SETTINGS TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
