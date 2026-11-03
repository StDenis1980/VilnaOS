# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/release.py: the tag, pyproject.toml and the metainfo agree."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "release.py"


@pytest.fixture(scope="module")
def release():
    spec = importlib.util.spec_from_file_location("release_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_repository_agrees_with_itself(release):
    version = release.project_version()
    assert release.check(f"gloxi-v{version}") == version


@pytest.mark.parametrize(
    ("pep440", "appstream"),
    [("0.1.0", "0.1.0"), ("0.1.0.dev0", "0.1.0~dev0"), ("0.2.0rc1", "0.2.0~rc1")],
)
def test_appstream_version(release, pep440, appstream):
    assert release.appstream_version(pep440) == appstream


def test_previews(release):
    assert release.is_prerelease("0.1.0")
    assert release.is_prerelease("1.0.0rc1")
    assert not release.is_prerelease("1.0.0")


def fake_tree(tmp_path: Path, version: str, metainfo_version: str) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    name = f"{tmp_path}/data/io.github.stdenis1980.Gloxi.metainfo.xml"
    Path(name).write_text(
        f"""<component><releases>
        <release version="{metainfo_version}" date="2027-01-31"><description>
          <p>First   preview.</p><p xml:lang="uk">Перша.</p>
          <ul><li>Local model</li><li xml:lang="uk">Локальна</li></ul>
        </description></release>
        <release version="0.0.1" date="2026-01-01"/>
        </releases></component>"""
    )
    return tmp_path


def test_mismatches_are_refused(release, tmp_path):
    tree = fake_tree(tmp_path, "0.1.0", "0.1.0~dev0")
    with pytest.raises(release.ReleaseError, match="metainfo"):
        release.check("gloxi-v0.1.0", tree)
    with pytest.raises(release.ReleaseError, match="pyproject"):
        release.check("gloxi-v0.1.1", tree)
    with pytest.raises(release.ReleaseError, match="starts with"):
        release.check("v0.1.0", tree)


def test_notes_are_the_english_description_and_how_to_install(release, tmp_path):
    tree = fake_tree(tmp_path, "0.1.0", "0.1.0")
    text = release.notes("0.1.0", tree)
    assert text.startswith("First preview.\n\n- Local model\n")
    assert "Перша" not in text and "Локальна" not in text
    assert "sha256sum -c gloxi.flatpak.sha256" in text
    assert "preview for testers" in text


def test_main_writes_notes_and_outputs(release, tmp_path, capsys):
    version = release.project_version()
    out = tmp_path / "notes.md"
    assert release.main(["release.py", f"gloxi-v{version}", str(out)]) == 0
    assert f"version={version}" in capsys.readouterr().out
    assert "## Install" in out.read_text()
    assert release.main(["release.py", "gloxi-v9.9.9", str(out)]) == 1
