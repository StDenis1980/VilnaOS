#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Shared by the extra-data Flatpaks of Electron apps that come as a .deb
# (thirdparty/cursor, thirdparty/grok-bot). Runs once at install (and update)
# time inside the sandbox, without network, in /app/extra, where Flatpak has
# put the vendor's .deb as app.deb after checking its size and SHA-256.
# Unpacks the app into /app/extra/app, unchanged except for Chromium's setuid
# sandbox, which cannot work in Flatpak (zypak takes its place).
set -eu
deb=app.deb
# The Flatpak's id is the name of its one .desktop file.
app_id="$(basename /app/share/applications/*.desktop .desktop)"

rm -rf deb app export
mkdir deb
# A .deb is an ar archive; bsdtar (libarchive, in the runtime) reads it and
# whatever compression data.tar has.
bsdtar -xOf "$deb" 'data.tar.*' | bsdtar -xf - -C deb
rm -f "$deb"

# The app folder is Electron's: the one with chrome-sandbox or resources/
# (app.asar, or the app/ folder of VS Code-based editors), wherever the
# package puts it (/usr/share/..., /opt/...).
marker="$(find deb -name chrome-sandbox -type f | head -n 1)"
[ -n "$marker" ] || marker="$(find deb -path '*/resources/app.asar' -type f | head -n 1)"
[ -n "$marker" ] || marker="$(find deb -path '*/resources/app/package.json' -type f | head -n 1)"
if [ -z "$marker" ]; then
  echo "apply_extra: no Electron app in the package" >&2
  exit 1
fi
case "$marker" in
  */chrome-sandbox) appdir="$(dirname "$marker")" ;;
  */resources/app.asar) appdir="$(dirname "$(dirname "$marker")")" ;;
  *) appdir="$(dirname "$(dirname "$(dirname "$marker")")")" ;;
esac
rm -f "$appdir/chrome-sandbox"
mv "$appdir" app

# The main program: the largest executable file in the app folder (Electron
# itself; the helpers next to it are small).
main=""
largest=0
for f in app/*; do
  [ -f "$f" ] && [ -x "$f" ] || continue
  size="$(stat -c %s "$f")"
  if [ "$size" -gt "$largest" ]; then
    largest="$size"
    main="$f"
  fi
done
if [ -z "$main" ]; then
  echo "apply_extra: no executable in $appdir" >&2
  exit 1
fi
ln -s "$(basename "$main")" app/.main

# The vendor's icon, taken from the package on this computer (we do not ship
# it): Flatpak exports what is under /app/extra/export.
icon="$(find deb -path '*/icons/hicolor/256x256/apps/*.png' -type f | head -n 1)"
[ -n "$icon" ] || icon="$(find deb -path '*/icons/hicolor/*/apps/*.png' -type f | sort | tail -n 1)"
if [ -n "$icon" ]; then
  size="$(basename "$(dirname "$(dirname "$icon")")")"
elif icon="$(find deb -path '*/pixmaps/*.png' -type f | head -n 1)" && [ -n "$icon" ]; then
  size=512x512
fi
if [ -n "$icon" ]; then
  mkdir -p "export/share/icons/hicolor/$size/apps"
  cp "$icon" "export/share/icons/hicolor/$size/apps/$app_id.png"
fi

rm -rf deb
