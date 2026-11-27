#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Cursor's terminal and agent use the person's shell on the system.
export SHELL=/app/bin/host-shell
exec /app/bin/launch "$@"
