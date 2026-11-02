# SPDX-License-Identifier: GPL-3.0-or-later
"""Hook the privacy gateway into the router.

Messages bound for the cloud are masked on this computer (names in labelled
fields, contacts, document numbers, keys...) and the answer gets the real
values back. Masking is on by default; ``mask_cloud = false`` turns it off,
and Gloxi then says so on every cloud answer.
"""

from __future__ import annotations

from gloxi.config import RouterPolicy
from gloxi.router import GuardFactory


def guard_factory(policy: RouterPolicy) -> GuardFactory | None:
    if not policy.mask_cloud:
        return None
    from privacy_gateway import Masker

    return Masker
