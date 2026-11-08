#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-permissions.sh - give the desktop user access to the MaxMix device.
#
# The MaxMix shows up as a USB serial port (CH340 -> ttyUSB*, native-USB
# Arduino boards -> ttyACM*). Those nodes are root-owned, and the "proper"
# access mechanism differs per distro and can fail silently:
#
#   - uaccess (TAG+="uaccess"): grants the logged-in session an ACL. Silently
#     stopped working after an OS update on Bazzite in Nov 2026 - no ACL, no
#     error, device just dead.
#   - dialout membership: documented as `sudo usermod -aG dialout $USER`, but
#     on OSTree-based distros (Bazzite, Silverblue) the base groups live in
#     /usr/lib/group, usermod only edits /etc/group, and the command exits 0
#     without changing anything.
#
# This script installs a static udev rule instead: an explicit file mode on
# the exact device plus a /dev/maxmix symlink. A file mode is enforced by the
# kernel and cannot be quietly reinterpreted by a framework update. 0666 means
# any local user may talk to the volume knob; if you want it locked down, use
# MODE:="0660" with GROUP:="dialout" and get yourself into that group.
#
# No groups, no login session, no logout needed - already-plugged devices are
# re-triggered so the rule applies immediately.
#
# Usage:   sudo ./setup-permissions.sh     (idempotent, safe to re-run)
# Verify:  ls -l /dev/maxmix               (mode should read crw-rw-rw-)
# ---------------------------------------------------------------------------
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo $0" >&2
    exit 1
fi

RULE=/etc/udev/rules.d/99-maxmix.rules

cat > "$RULE" <<'EOF'
# MaxMix USB volume controller - serial port access (installed by
# Linux/setup-permissions.sh). Static MODE instead of uaccess (silently broke
# on some distro updates) and instead of dialout membership (silently no-ops
# on OSTree-based distros where base groups live in /usr/lib/group).
# Lock down by replacing MODE:="0666" with MODE:="0660", GROUP:="dialout"
# and adding yourself to the dialout group.

# CH340 / CH341 clones (most common)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE:="0666", SYMLINK+="maxmix"
# FTDI FT232 (older / genuine Nanos)
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", MODE:="0666", SYMLINK+="maxmix"
# Arduino native USB (Leonardo/Micro 32u4 and SAMD clones -> ttyACM*)
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", MODE:="0666", SYMLINK+="maxmix"
# Atmel-based clones
SUBSYSTEM=="tty", ATTRS{idVendor}=="03eb", MODE:="0666", SYMLINK+="maxmix"
EOF

echo "Installed $RULE"
udevadm control --reload

# Apply to serial devices already plugged in (no replug needed).
found=0
for dev in /sys/class/tty/ttyUSB* /sys/class/tty/ttyACM*; do
    [ -e "$dev" ] || continue
    found=1
    udevadm trigger --action=change "$dev"
done

if [ "$found" -eq 0 ]; then
    echo "No serial device currently plugged in - the rule will apply on the next plug."
elif [ -e /dev/maxmix ]; then
    echo "Device node now:"
    ls -l /dev/maxmix
    echo
    echo "Done. The daemon can open the device; no logout needed."
else
    echo "Rule applied; a MaxMix device plugged in now should appear as /dev/maxmix."
fi
