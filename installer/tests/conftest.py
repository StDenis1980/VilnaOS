# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared by the installer's tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _language_kept(monkeypatch):
    """A window sets LANGUAGE for GTK (start.gtk_language): each test gets it back as it was."""
    if "LANGUAGE" in os.environ:
        monkeypatch.setenv("LANGUAGE", os.environ["LANGUAGE"])
    else:
        monkeypatch.delenv("LANGUAGE", raising=False)
