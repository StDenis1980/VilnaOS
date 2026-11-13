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
// What polkitd does with org.freedesktop.policykit.imply: when the rules do
// not say YES, it asks them again for every action that implies the asked
// one, with the same details (check_authorization_sync, one level deep).
const IMPLIED_BY = {
  "org.freedesktop.Flatpak.app-update": ["org.freedesktop.Flatpak.app-install"],
  "org.freedesktop.Flatpak.runtime-install": [
    "org.freedesktop.Flatpak.app-install", "org.freedesktop.Flatpak.app-update",
    "org.freedesktop.Flatpak.install-bundle"],
  "org.freedesktop.Flatpak.runtime-update": [
    "org.freedesktop.Flatpak.app-install", "org.freedesktop.Flatpak.runtime-install",
    "org.freedesktop.Flatpak.app-update", "org.freedesktop.Flatpak.install-bundle"],
  "org.freedesktop.Flatpak.runtime-uninstall": ["org.freedesktop.Flatpak.app-uninstall"],
};
const ask = (user, id, details) => rule({id, lookup: (k) => details[k]}, {user});
const decide = ([user, id, details]) => {
  const own = ask(user, id, details);
  if (own === "yes") return own;
  if ((IMPLIED_BY[id] || []).some((by) => ask(user, by, details) === "yes")) return "yes";
  return own;
};
console.log(JSON.stringify(cases.map(decide)));
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
APP = "app/org.kde.kdenlive/x86_64/stable"
RUNTIME = "runtime/org.kde.Platform/x86_64/6.8"


def test_vilna_apps_may_install_update_and_remove_from_flathub_and_vilna():
    cases = [
        ["vilna-apps", F + "app-install", {"origin": "flathub"}],
        ["vilna-apps", F + "app-install", {"origin": "vilna"}],
        ["vilna-apps", F + "runtime-install", {"origin": "flathub"}],
        ["vilna-apps", F + "app-update", {"origin": "flathub", "ref": APP}],
        ["vilna-apps", F + "runtime-update", {"origin": "flathub", "ref": RUNTIME}],
        ["vilna-apps", F + "app-uninstall", {"ref": APP}],
        # flatpak-system-helper puts the app's name in "ref" when it knows it.
        ["vilna-apps", F + "app-uninstall", {"ref": "Kdenlive"}],
        ["vilna-apps", F + "appstream-update", {"origin": "flathub", "arch": "x86_64"}],
        ["vilna-apps", F + "update-remote", {"remote": "vilna"}],
        ["vilna-apps", F + "metadata-update", {}],
    ]
    assert decide(cases) == ["yes"] * len(cases)


def test_vilna_apps_may_not_add_sources_install_files_or_go_elsewhere():
    cases = [
        ["vilna-apps", F + "app-install", {"origin": "somebody"}],
        ["vilna-apps", F + "app-install", {}],
        ["vilna-apps", F + "app-update", {"origin": "flathub-beta", "ref": APP}],
        ["vilna-apps", F + "update-remote", {"remote": "somebody"}],
        ["vilna-apps", F + "appstream-update", {"origin": "somebody"}],
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


def test_implied_actions_do_not_open_a_back_door():
    # Seen in a VM (2026-11-13) with an unconditional YES for app-update and
    # app-uninstall: vilna-apps installed a runtime from flathub-beta and
    # removed a runtime, because polkit asks the implying actions too.
    cases = [
        ["vilna-apps", F + "runtime-install", {"origin": "flathub-beta", "ref": RUNTIME}],
        ["vilna-apps", F + "runtime-update", {"origin": "flathub-beta", "ref": RUNTIME}],
        ["vilna-apps", F + "runtime-uninstall", {"ref": RUNTIME}],
    ]
    assert decide(cases) == ["no"] * len(cases)
