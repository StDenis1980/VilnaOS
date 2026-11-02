# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from vilna_backup import plan
from vilna_backup.settings import Settings


def rel(layout, paths):
    return {str(Path(p).relative_to(layout.home)) for p in paths}


def test_only_settings(layout):
    p = plan.build_plan(Settings(preset="settings"), layout)
    got = rel(layout, p.includes)
    assert {".config/dconf", ".var/app", ".config/gloxi", ".local/share/gloxi"} <= got
    assert ".local/state/herkos" in got
    assert "Passwords.kdbx" in got  # KeePassXC database
    assert "Documents" not in got


def test_documents_preset_adds_three_folders(layout):
    got = rel(layout, plan.build_plan(Settings(preset="documents"), layout).includes)
    assert {"Documents", "Pictures"} <= got  # Desktop does not exist in the test home
    assert "Videos" not in got and "Downloads" not in got


def test_home_preset_leaves_out_downloads_caches_and_secrets(layout):
    p = plan.build_plan(Settings(preset="home"), layout)
    assert p.includes[0] == layout.home
    for excluded in (
        "Downloads/setup.iso",
        ".cache/big.bin",
        ".ssh/id_ed25519",
        ".local/share/keyrings/login.keyring",
        ".var/app/org.gnome.Maps/cache/tiles.bin",
        ".local/state/herkos/backups/files/x/meta.json",
        ".local/share/gloxi/history/2026-11-02.jsonl",
        "Documents/models/qwen.gguf",
    ):
        assert p.is_excluded(layout.home / excluded), excluded
    assert not p.is_excluded(layout.home / "Videos/film.mkv")


def test_consents_bring_in_history_and_secrets(layout):
    s = Settings(preset="settings", gloxi_history=True, system_secrets=True)
    p = plan.build_plan(s, layout)
    assert not p.is_excluded(layout.home / ".local/share/gloxi/history/2026-11-02.jsonl")
    assert {".ssh", ".local/share/keyrings"} <= rel(layout, p.includes)


def test_include_and_exclude(layout):
    s = Settings(
        preset="documents",
        include=(str(layout.home / "Videos"),),
        exclude=(str(layout.home / "Pictures/2019"),),
    )
    p = plan.build_plan(s, layout)
    assert "Videos" in rel(layout, p.includes)
    assert p.is_excluded(layout.home / "Pictures/2019/a.jpg")


def test_missing_folders_are_reported_not_backed_up(layout):
    s = Settings(preset="settings", include=(str(layout.home / "Music"),))
    p = plan.build_plan(s, layout)
    assert layout.home / "Music" in p.missing
    assert layout.home / "Music" not in p.includes


def test_ukrainian_user_dirs(home):
    (home / "Документи").mkdir()
    (home / "Документи" / "a.txt").write_text("a")
    (home / ".config" / "user-dirs.dirs").write_text(
        '# comment\nXDG_DOCUMENTS_DIR="$HOME/Документи"\nXDG_DESKTOP_DIR="$HOME/"\n'
    )
    layout = plan.Layout.from_env({}, home=home)
    assert layout.user_dir("DOCUMENTS") == home / "Документи"
    assert layout.user_dir("DESKTOP") == home / "Desktop"  # "$HOME/" itself is ignored
    assert home / "Документи" in plan.build_plan(Settings(), layout).includes


def test_sizes_skip_exclusions(layout):
    p = plan.build_plan(Settings(preset="documents"), layout)
    assert plan.size_of(layout.home / "Documents", p) == len("звіт".encode())
    rows = {r["group"]: r["bytes"] for r in plan.overview(layout, Settings())}
    assert rows["pictures"] == 3200 and rows["videos"] == 5000
    assert rows["home"] >= rows["videos"] + rows["pictures"]
    assert rows["home"] < rows["videos"] + rows["pictures"] + rows["download"]


def test_fit_suggests_large_folders(layout):
    s = Settings(preset="documents", include=(str(layout.home / "Videos"),))
    p = plan.build_plan(s, layout)
    total = plan.plan_size(p)
    out = plan.fit(p, total - 1000, layout)
    assert not out["fits"]
    assert out["suggest_exclude"] == [str(layout.home / "Videos")]
    assert out["bytes_after"] <= total - 1000
    assert plan.fit(p, None, layout)["fits"]
