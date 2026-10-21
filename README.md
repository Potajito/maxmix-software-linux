# MaxMix for Linux

Linux companion software for the [MaxMix USB volume controller](https://github.com/t3knomanzer/maxmix-software) —
the knob-and-display device that lets you control your computer's audio with
a rotary encoder. The official desktop app is Windows-only (C#/WPF, bound to
Windows audio APIs), so this project reimplements the companion software in
Python for Linux: it speaks the device's binary serial protocol and drives
volume through **PulseAudio**, which also covers **PipeWire** (via
`pipewire-pulse`) on every modern distribution.

Everything the Windows app does works here: output / input / application
modes, per-app volume, default-device switching, mute, live two-way sync,
LED colours, sleep, encoder acceleration and more.

> ## ⚠️ This project is 100% vibe coded
>
> This codebase was written entirely by AI coding assistants through
> iterative prompting — no line was lovingly hand-crafted by a human. It
> works on the author's machine (Bazzite/Fedora, PipeWire, a real MaxMix
> unit with CH340 firmware 1.5.6) and is verified by automated protocol
> tests, but it has never been reviewed line-by-line by a person.
>
> Use at your own risk. Read the code before you trust it. Issues and
> pull requests (human- or AI-authored) are welcome.

## Download

Ready-to-run AppImages (CLI daemon and configuration GUI) are on the
[Releases page](https://github.com/Potajito/maxmix-software-linux/releases) —
`chmod +x` and run, nothing to install. Building from source is covered
[below](#quick-start).

## What's in the box

| File | Purpose |
|------|---------|
| `maxmix.py` | **CLI daemon** for a real MaxMix device (Arduino Nano + CH340-style serial, firmware 1.5.x). Full feature parity with the Windows app. |
| `maxmix_gui.py` | **PySide6 configuration GUI** — embeds the daemon and lets you change device settings live. |
| `maxmix_hid.py` | Bonus tool for *other* knob devices that enumerate as HID joysticks (e.g. Leonardo-based macro knobs). Translates knob rotation / button into volume / mute. Not needed for a real MaxMix. |
| `test_maxmix_protocol.py` | End-to-end test: runs `maxmix.py` against a simulated firmware on a pty. No hardware needed. |
| `test_maxmix_sessions.py` | Unit tests for the daemon's session-list bookkeeping. No hardware, no audio server needed. |
| `maxmix.service` | systemd user service template for autostart. |
| `build-appimage.sh` | Builds the portable AppImages (CLI + GUI). |
| `icon.png` | The MaxMix logo used by the GUI and the AppImages. |

## CLI tool vs GUI — which one do you want?

Both talk to the device; they serve different jobs.

**`maxmix.py` (CLI daemon)** is the piece that does the actual work. It
runs headless in a terminal (or as a systemd service), connects to the
device, and continuously mirrors your audio state onto it: turn the knob to
change volume, rotate to move between devices/apps, double-press to mute,
hold to switch mode. Everything is configured through command-line flags at
startup.

**`maxmix_gui.py` (GUI)** is a settings app, not a replacement for the
daemon — it *embeds* the same daemon in a worker thread and adds a window
on top:

- **LED ring colours** — solid colour or volume gradient (the firmware
  lerps low → high as the ring fills; when muted it pulses between the
  two), plus game-mode channel A/B colours and a live preview.
- **Behaviour settings** — display sleep timeout, encoder acceleration,
  **volume step per detent** (10%–500% of the firmware default; the daemon
  scales each knob delta and re-syncs the device display to the real
  value), continuous scroll.

Every change is pushed to the device immediately, like the Windows app,
and saved to `~/.config/maxmix/settings.json`.

The practical differences:

| | CLI (`maxmix.py`) | GUI (`maxmix_gui.py`) |
|---|---|---|
| Purpose | Run permanently, control audio | Configure the device's look & feel |
| Settings source | `~/.config/maxmix/settings.json`, flags override | Clicks in a window |
| Volume control | ✅ yes | ✅ yes (same embedded daemon) |
| LED colours / step size / sleep / acceleration | Via settings file or flags | Via widgets, applied live |
| Runs headless (server, autostart) | ✅ yes | ❌ needs a display |
| Dependencies | `pyserial`, `pulsectl` | + `PySide6` |
| AppImage size | ~11 MB | ~68 MB |

> **Important:** the CLI daemon and the GUI both talk to the device —
> run only **one** of them at a time, or the second one will report
> *device not found*.

### Settings persistence

Both programs read their device settings from
`~/.config/maxmix/settings.json`; the GUI keeps that file updated
whenever you change a setting (and on quit). So the typical setup is:
use the GUI once to dial in your colours and step size, quit it, and run
the plain CLI daemon with **no flags at all** — it starts with exactly
the settings you chose. Command-line flags still override the file for
one run, which is handy in the systemd `ExecStart` line. Delete the file
to go back to the built-in defaults.

## Portable AppImages

`MaxMix-x86_64.AppImage` (CLI daemon, ~11 MB) and
`MaxMix-GUI-x86_64.AppImage` (configuration GUI, ~68 MB) are single
self-contained files — no Python, no installation, no venv. Just run them:

```bash
chmod +x MaxMix-x86_64.AppImage
./MaxMix-x86_64.AppImage          # same options as maxmix.py
./MaxMix-x86_64.AppImage -v
./MaxMix-GUI-x86_64.AppImage      # configuration GUI
```

They only need what any desktop already has: `libpulse` (audio control),
access to `/dev/ttyUSB*` / `/dev/ttyACM*` (`dialout` group), and libfuse2
to mount the AppImage — or run with `APPIMAGE_EXTRACT_AND_RUN=1` on
systems without it. They are built on a recent distribution, so they run on
that glibc or newer.

For autostart, point the systemd service at the AppImage:

```ini
ExecStart=%h/path/to/MaxMix-x86_64.AppImage
```

Rebuild both yourself with:

```bash
./build-appimage.sh
```

On Wayland the GUI's window icon is resolved by application id from the
icon theme, so the GUI installs `icon.png` once to
`~/.local/share/icons/hicolor/256x256/apps/maxmix-gui.png` on startup.

## Quick start

### From source (CLI daemon)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python maxmix.py
```

The daemon auto-detects the device (it shows up as `/dev/ttyUSB*` with a
CH340, or `/dev/ttyACM*` for 32u4-based boards) and starts pushing your
audio sessions to it. Turn the knob — volume changes. Exactly like on
Windows.

Requires firmware 1.5.0 – 1.5.6 (same constraint as the Windows app) and
read/write access to the serial port — add yourself to the `dialout` group:

```bash
sudo usermod -aG dialout $USER    # log out/in afterwards
```

### From source (GUI)

```bash
.venv/bin/pip install -r requirements-gui.txt
.venv/bin/python maxmix_gui.py
```

Headless self-test: `maxmix_gui.py --selftest`.

## CLI options

For the device settings (everything below except `--port`, `--mode`,
`--force` and `--verbose`) the "Default" column is what applies when the
settings file has no value for it; a saved setting takes precedence, and
an explicit flag takes precedence over both.

| Flag | Description | Default |
|------|-------------|---------|
| `-p, --port` | Serial port of the device | auto-detect |
| `-m, --mode` | Startup mode: `output`, `input`, `application` | `output` |
| `-s, --sleep-after N` | Display sleep after N seconds idle, `0` disables | `5` |
| `-a, --acceleration P` | Encoder acceleration 0–100 | `60` |
| `--volume-step RATIO` | Volume change per detent as a multiplier of the firmware default (0.1–10; `0.5` = half as fast) | `1.0` |
| `--no-continuous-scroll` | Don't wrap around at the ends of the list | off |
| `--volume-min-color RRGGBB` | LED color at volume 0 | `0000FF` |
| `--volume-max-color RRGGBB` | LED color at volume 100 | `FF0000` |
| `--channel-a-color RRGGBB` | Game-mode channel A color | `0000FF` |
| `--channel-b-color RRGGBB` | Game-mode channel B color | `FF00FF` |
| `-f, --force` | Connect even if the firmware version is unknown | off |
| `-v, --verbose` | Log serial protocol traffic | off |

## Autostart with systemd

Copy `maxmix.service` to `~/.config/systemd/user/`, edit `ExecStart` to
point at your checkout (or the AppImage), then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now maxmix.service
journalctl --user -u maxmix.service -f   # follow logs
```

## Testing without hardware

```bash
.venv/bin/python test_maxmix_protocol.py
.venv/bin/python test_maxmix_sessions.py
.venv/bin/python test_maxmix_settings.py
```

The first spawns a fake firmware on a pseudo-terminal and verifies the
handshake, the initial session push, keepalives, and that a knob turn
actually changes the default sink volume. The second unit-tests the
session-list bookkeeping (e.g. the "wrong output after locking the screen"
class of bug, when sinks vanish and come back with new indexes). The third
unit-tests the settings file round-trip: GUI saves, daemon loads, flags
override, corrupt values fall back.

## How it works

The firmware (`Embedded/MaxMix` in the upstream repo) speaks a compact
binary protocol at 76800 baud: one command byte followed by a fixed-size
packed struct (see `Communications.cpp` / `Messages.cs`, mirrored by the
packing helpers at the top of `maxmix.py`). Notable behaviours the daemon
replicates from the Windows app:

- The device ACKs every host command with `OK`; the host sends one message
  at a time and waits for the ACK (stop-and-wait).
- Command byte and payload must be written in a **single** `write()` — the
  firmware reads the payload with a 10 ms serial timeout after the command
  byte.
- With no serial traffic for 5 s the device resets to its splash screen, so
  the daemon sends a 1-second keepalive, like the Windows app.
- `DisplayMode` on the wire: `0=splash, 1=output, 2=input, 3=application,
  4=game` — the device ignores knob input while in splash mode.

Audio enumeration and control use the PulseAudio API via `pulsectl`; on
PipeWire systems this talks to `pipewire-pulse` transparently. Application
streams are grouped by application name, like the Windows app groups them
by process.

## Troubleshooting

- **Device not found** — check `ls /dev/ttyUSB* /dev/ttyACM*`, try `-p`.
- **Permission denied** — `dialout` group (see above).
- **Screen shows the logo and the knob does nothing** — the device is in
  splash mode; that means no app is talking to it. Run the daemon with `-v`
  and check you see `TX 4 01…` (mode byte `01` = output) after connecting.
- **Volume >100% or wrong values with old pulsectl** — `pulsectl` ≥ 24
  changed `PulseVolumeInfo` to take fractions (0..1); the daemon already
  uses the new form.
- **Arduino IDE serial monitor open?** Only one program can hold the port.
- **GUI says device not found** — the CLI daemon is probably still running.
  Stop it first (`systemctl --user stop maxmix.service` or Ctrl-C).
