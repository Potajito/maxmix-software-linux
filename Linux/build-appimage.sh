#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-appimage.sh - build the portable MaxMix AppImages.
#
# Produces:
#   MaxMix-x86_64.AppImage      CLI daemon (no GUI toolkit, ~11 MB)
#   MaxMix-GUI-x86_64.AppImage  PySide6 configuration GUI (~90 MB)
#
# Requirements: python3 with venv support, curl, and FUSE to run
# appimagetool (or set APPIMAGE_EXTRACT_AND_RUN=1 without it).
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

# --- Python bundle ---------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    .venv/bin/pip install -q pyserial pulsectl pyinstaller PySide6
fi

.venv/bin/pyinstaller --clean --noconfirm --onedir --name maxmixd --console maxmix.py
.venv/bin/pyinstaller --clean --noconfirm --onedir --name maxmix-gui --windowed \
    --add-data "icon.png:." maxmix_gui.py

# --- shared AppDir scaffolding ----------------------------------------------
make_appdir() {  # $1 = binary name, $2 = AppDir path, $3 = terminal (true/false)
    local binary="$1" appdir="$2" terminal="$3"
    local display_name="MaxMix Daemon"
    [ "$binary" = "maxmix-gui" ] && display_name="MaxMix Config"
    rm -rf "$appdir"
    mkdir -p "$appdir/usr/bin"
    cp -r "dist/$binary" "$appdir/usr/bin/"

    cat > "$appdir/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
exec "\$HERE/usr/bin/$binary/$binary" "\$@"
EOF
    chmod +x "$appdir/AppRun"

    cat > "$appdir/$binary.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$display_name
GenericName=Volume controller
Comment=Companion for the MaxMix USB volume controller
Exec=$binary
Icon=$binary
Terminal=$terminal
Categories=AudioVideo;Audio;System;
Keywords=audio;volume;mixer;maxmix;usb;
EOF

    cp icon.png "$appdir/$binary.png"
}

# --- AppImages ---------------------------------------------------------------
if [ ! -x /tmp/appimagetool ]; then
    curl -sL -o /tmp/appimagetool \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x /tmp/appimagetool
fi

make_appdir maxmixd   AppDir       true
make_appdir maxmix-gui AppDir-GUI  false

# Build to a temp name first: rename(2) works even when a previous build is
# still running (e.g. the daemon autostarts from the CLI AppImage), while
# overwriting the file in place fails with "Text file busy".
ARCH=x86_64 /tmp/appimagetool AppDir        MaxMix-x86_64.AppImage.part
ARCH=x86_64 /tmp/appimagetool AppDir-GUI    MaxMix-GUI-x86_64.AppImage.part
mv -f MaxMix-x86_64.AppImage.part      MaxMix-x86_64.AppImage
mv -f MaxMix-GUI-x86_64.AppImage.part  MaxMix-GUI-x86_64.AppImage

echo
echo "Built:"
echo "  $(pwd)/MaxMix-x86_64.AppImage      (CLI daemon)"
echo "  $(pwd)/MaxMix-GUI-x86_64.AppImage  (configuration GUI)"
