# SPDX-License-Identifier: Apache-2.0
"""Privacy gateway: mask personal data on the device before it reaches a cloud model.

>>> from privacy_gateway import Masker
>>> m = Masker()
>>> m.mask("Write to olena@example.com")
'Write to [EMAIL_1]'
>>> m.unmask("Sent to [EMAIL_1].")
'Sent to olena@example.com.'
"""

from privacy_gateway.detectors import Finding, Kind, detect
from privacy_gateway.masking import Masker

__all__ = ["Finding", "Kind", "Masker", "detect"]
