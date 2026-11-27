#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Launcher of the VS Code-based editors (thirdparty/cursor,
# thirdparty/devin-desktop): their terminal and agent use the person's shell
# on the system (host-shell.sh).
set -eu
export SHELL=/app/bin/host-shell
# Links such as cursor:// (sign-in) go to --open-url, as the vendors' own
# *-url-handler.desktop files do.
case "${1:-}" in
  [a-z]*://*) exec /app/bin/launch --open-url "$@" ;;
esac
exec /app/bin/launch "$@"
