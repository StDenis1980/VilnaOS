#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Starts Anthropic's app (unpacked by apply_extra) under zypak, Wayland first.
exec zypak-wrapper /app/extra/claude/.main --ozone-platform-hint=auto "$@"
