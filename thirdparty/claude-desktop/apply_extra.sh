#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Runs once at install (and update) time inside the sandbox, without network,
# in /app/extra, where Flatpak has put Anthropic's .deb after checking its
# size and SHA-256. Unpacks the Electron app into /app/extra/claude.
set -eu
deb=claude-desktop.deb
app_id=io.github.stdenis1980.ClaudeDesktop

rm -rf deb claude export
mkdir deb
# A .deb is an ar archive; bsdtar (libarchive, in the runtime) reads it and
# whatever compression data.tar has.
bsdtar -xOf "$deb" 'data.tar.*' | bsdtar -xf - -C deb
rm -f "$deb"

# The app folder is the one with Electron's resources/app.asar, wherever the
# package puts it (/usr/lib/..., /opt/...).
asar="$(find deb -path '*/resources/app.asar' -type f | head -n 1)"
if [ -z "$asar" ]; then
  echo "apply_extra: no Electron app (resources/app.asar) in the package" >&2
  exit 1
fi
appdir="$(dirname "$(dirname "$asar")")"
# zypak takes the place of Chromium's setuid sandbox, which cannot work here.
rm -f "$appdir/chrome-sandbox"
mv "$appdir" claude

# The main program: the largest executable file in the app folder (Electron
# itself; the helpers next to it are small).
main=""
largest=0
for f in claude/*; do
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
ln -s "$(basename "$main")" claude/.main

# Anthropic's own icon, taken from the package on this computer (we do not
# ship it): Flatpak exports what is under /app/extra/export.
icon="$(find deb -path '*/icons/hicolor/256x256/apps/*.png' -type f | head -n 1)"
[ -n "$icon" ] || icon="$(find deb -path '*/icons/hicolor/*/apps/*.png' -type f | sort | tail -n 1)"
if [ -n "$icon" ]; then
  size="$(basename "$(dirname "$(dirname "$icon")")")"
  mkdir -p "export/share/icons/hicolor/$size/apps"
  cp "$icon" "export/share/icons/hicolor/$size/apps/$app_id.png"
fi

rm -rf deb
