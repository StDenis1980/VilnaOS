# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    """Source (English) strings whatever the developer's locale, and no real engine."""
    monkeypatch.setenv("LANGUAGE", "en")
    # Never start a real llama-server from the tests, even if one is installed.
    monkeypatch.setenv("GLOXI_START_SERVER", "0")
