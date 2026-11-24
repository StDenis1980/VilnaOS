# SPDX-License-Identifier: GPL-3.0-or-later
"""The real computer's programs: their output is read untranslated."""

from __future__ import annotations

import subprocess

from vilna_mcp_installer import machine


def test_programs_whose_output_is_read_run_untranslated():
    env = machine.untranslated_env({"LANGUAGE": "uk", "LANG": "uk_UA.UTF-8", "PATH": "/usr/bin"})
    assert "LANGUAGE" not in env
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["PATH"] == "/usr/bin"


def test_nmcli_runs_untranslated_when_the_installer_speaks_ukrainian(monkeypatch):
    """The installer sets LANGUAGE for GTK; «connected» must still read as such."""
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, stdout="ethernet:connected\n", stderr="")

    monkeypatch.setenv("LANGUAGE", "uk")
    monkeypatch.setattr(machine.subprocess, "run", run)
    assert machine.Machine().wired()
    assert "LANGUAGE" not in seen and seen["LC_ALL"] == "C.UTF-8"
