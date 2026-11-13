# SPDX-License-Identifier: Apache-2.0
"""The polkit rule for vilna-apps, run in node with a stand-in for polkit."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RULE = Path(__file__).resolve().parents[1] / "data" / "50-vilna-apps.rules"

HARNESS = """
const fs = require("fs");
let rule;
const polkit = {Result: {YES: "yes", NO: "no", NOT_HANDLED: "not_handled"},
                addRule: (f) => { rule = f; }};
eval(fs.readFileSync(process.argv[1], "utf8"));
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(([user, id, details]) =>
  rule({id, lookup: (k) => details[k]}, {user}))));
"""


def decide(cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "-e", HARNESS, str(RULE), json.dumps(cases)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


F = "org.freedesktop.Flatpak."


def test_vilna_apps_may_install_update_and_remove_from_flathub_and_vilna():
    cases = [
        ["vilna-apps", F + "app-install", {"origin": "flathub"}],
        ["vilna-apps", F + "app-install", {"origin": "vilna"}],
        ["vilna-apps", F + "runtime-install", {"origin": "flathub"}],
        ["vilna-apps", F + "app-update", {}],
        ["vilna-apps", F + "app-uninstall", {"ref": "app/org.kde.kdenlive/x86_64/stable"}],
        ["vilna-apps", F + "appstream-update", {}],
    ]
    assert decide(cases) == ["yes"] * len(cases)


def test_vilna_apps_may_not_add_sources_install_files_or_go_elsewhere():
    cases = [
        ["vilna-apps", F + "app-install", {"origin": "somebody"}],
        ["vilna-apps", F + "configure-remote", {}],
        ["vilna-apps", F + "configure", {}],
        ["vilna-apps", F + "install-bundle", {"path": "/tmp/x.flatpak"}],
        ["vilna-apps", F + "runtime-uninstall", {}],
        ["vilna-apps", F + "override-parental-controls", {}],
        ["vilna-apps", "org.freedesktop.systemd1.manage-units", {}],
        ["vilna-apps", "org.freedesktop.packagekit.package-install", {}],
    ]
    assert decide(cases) == ["no"] * len(cases)


def test_people_keep_their_own_rules():
    cases = [
        ["denys", F + "app-install", {"origin": "flathub"}],
        ["gloxi-denys", F + "configure", {}],
    ]
    assert decide(cases) == ["not_handled", "not_handled"]
