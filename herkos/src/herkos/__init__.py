# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos-lite: the permission gate of Vilna OS for the Gloxi 0.1 preview.

Herkos-lite runs in the user's session. It is the reference implementation of
the contracts in ``herkos/docs/`` (protocol, action request, manifests); the
system Herkos in Rust will implement the same contracts.
"""

__version__ = "0.1.0.dev0"

# Version of the Gloxi <-> Herkos protocol (herkos/docs/protocol.md).
PROTOCOL_VERSION = "0.1"
