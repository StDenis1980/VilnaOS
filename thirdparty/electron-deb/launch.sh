#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Starts the app unpacked by apply_extra under zypak. Wayland whenever the
# session has a Wayland display: Electron's "auto" hint goes by
# XDG_SESSION_TYPE, which a start from outside the session (SSH, a service)
# does not set, and then it fails with "Missing X server".
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
  platform=--ozone-platform=wayland
else
  platform=--ozone-platform=x11
fi
exec zypak-wrapper /app/extra/app/.main "$platform" "$@"
