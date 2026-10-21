Second release of the Linux companion for the [MaxMix USB volume controller](https://github.com/t3knomanzer/maxmix-software).

## What's new: settings persist 🎉

The biggest complaint about v0.1.0 is gone. The configuration GUI now saves every change to `~/.config/maxmix/settings.json` (as you change it, and again on quit), and the CLI daemon loads that file at startup. The typical setup is now: open the GUI once, dial in your colours and step size, quit it — and run the daemon as a systemd service with **no flags at all**.

- Command-line flags still work and override the saved file for that run — handy for testing or a per-boot override in `ExecStart`
- The GUI itself reopens with your last saved colours
- Corrupt or out-of-range values in the settings file are dropped per-field with a warning; the daemon always starts
- Delete `~/.config/maxmix/settings.json` to go back to built-in defaults
- New hardware-free test suite for the whole round-trip: `test_maxmix_settings.py`

**Upgrading from v0.1.0:** just replace the AppImage files — nothing to migrate (v0.1.0 stored no settings). Your device settings apply as soon as you've touched them once in the GUI.

## Downloads

| File | What it is |
|------|------------|
| `MaxMix-x86_64.AppImage` | CLI daemon — headless, for daily use / autostart (~10 MB) |
| `MaxMix-GUI-x86_64.AppImage` | Configuration GUI — LED colours, sleep, acceleration, volume step (~67 MB) |

Both are self-contained AppImages: no Python, no installation. `chmod +x` and run.
Run only **one** of them at a time — both talk to the device.

**Requirements:** PulseAudio or PipeWire (via `pipewire-pulse`, present on every modern distro), membership in the `dialout` group for serial-port access, and libfuse2 to mount the AppImage (or run with `APPIMAGE_EXTRACT_AND_RUN=1`). x86_64, built on a recent glibc.

**Device firmware:** 1.5.0 – 1.5.6 (same range as the Windows app).

## Quick start

```bash
chmod +x MaxMix-GUI-x86_64.AppImage
./MaxMix-GUI-x86_64.AppImage    # pick colours & step size, then quit
chmod +x MaxMix-x86_64.AppImage
./MaxMix-x86_64.AppImage        # daemon starts with those settings — no flags
```

Turn the knob to change volume, rotate to move between devices/apps, double-press to mute, hold to switch mode — exactly like the Windows app. See the [README](https://github.com/Potajito/maxmix-software-linux/blob/master/README.md) for the settings file, CLI flags, systemd autostart, and troubleshooting.

## ⚠️ Vibe coded

This project is written **entirely by AI coding assistants** through iterative prompting — no line was lovingly hand-crafted by a human. It is live-verified on the author's machine (Bazzite, PipeWire, a real MaxMix with CH340 firmware 1.5.6) and covered by automated protocol tests, but it has never been reviewed line-by-line by a person. Use at your own risk.
