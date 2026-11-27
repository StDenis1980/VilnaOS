#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Cursor's terminal and agent use the person's shell on the system.
export SHELL=/app/bin/host-shell
# cursor:// links (sign-in) go to --open-url, as Anysphere's own
# cursor-url-handler.desktop does.
case "${1:-}" in
  cursor://*) exec /app/bin/launch --open-url "$@" ;;
esac
exec /app/bin/launch "$@"
