# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    """Tests expect the source (English) strings, whatever the developer's locale."""
    monkeypatch.setenv("LANGUAGE", "en")
