#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Cursor's terminal and agent run commands in $SHELL. Inside the Flatpak that
# would be the runtime's sh without git, compilers or the person's tools, so
# this opens the person's own login shell on the system instead (decision of
# Denys on Cursor's access, 2026-11-27, thirdparty/cursor/README.md).
set -eu
user_shell="$(flatpak-spawn --host getent passwd "$(id -un)" | cut -d: -f7)"
exec flatpak-spawn --host --watch-bus --env=TERM="${TERM:-xterm-256color}" \
  "${user_shell:-/bin/bash}" "$@"
