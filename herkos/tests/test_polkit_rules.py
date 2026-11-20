# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos's polkit actions and rules (permissions.md 9.2).

The rules run in node with a stand-in for polkit; the actions are checked
against the manifests that name them. Checked with the real polkitd 124 too
(pkcheck as the user herkos about another user's process), see the PR.
"""

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from herkos.manifest import ManifestRegistry

SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"
POLICY = SYSTEMD / "org.vilna.herkos.policy"
LANGUAGES = {"uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"}  # and English (ADR 0009)

# polkit runs every rules file in the order of the file names; the first
# answer that is not NOT_HANDLED wins, else the action's defaults decide.
HARNESS = """
const fs = require("fs");
const rules = [];
const polkit = {Result: {YES: "yes", NO: "no", NOT_HANDLED: "not_handled"},
                addRule: (f) => { rules.push(f); }, addAdminRule: () => {}};
for (const file of JSON.parse(process.argv[1])) {
  eval(fs.readFileSync(file, "utf8"));
}
const decide = ([id, s, details]) => {
  const subject = {user: s.user, active: !!s.active, local: !!s.local,
                   isInGroup: (g) => (s.groups || []).includes(g)};
  const action = {id, lookup: (k) => (details || {})[k]};
  for (const rule of rules) {
    const r = rule(action, subject);
    if (r !== "not_handled") return r;
  }
  return "defaults";
};
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(decide)));
"""

RULES = ["40-herkos-gloxi.rules", "50-herkos-actions.rules", "50-herkos.rules"]


def decide(cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    files = json.dumps([str(SYSTEMD / f) for f in sorted(RULES)])
    out = subprocess.run(
        [node, "-e", HARNESS, files, json.dumps(cases)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


ADMIN = {"user": "anna", "groups": ["wheel"], "active": True, "local": True}
PERSON = {"user": "bohdan", "groups": ["users"], "active": True, "local": True}
APPS = "org.vilna.herkos.apps.change"
UNITS = "org.freedesktop.systemd1.manage-units"


def test_an_administrator_at_the_computer_changes_apps_without_a_password():
    assert decide(
        [
            [APPS, ADMIN],
            # Anybody else: the defaults, an administrator's password.
            [APPS, PERSON],
            [APPS, {**ADMIN, "active": False}],
            [APPS, {**ADMIN, "local": False}],
            # Other actions: the defaults.
            ["org.vilna.herkos.system.rollback", ADMIN],
            ["org.vilna.herkos.network.weaken", PERSON],
        ]
    ) == ["yes", "defaults", "defaults", "defaults", "defaults", "defaults"]


def test_gloxi_gets_nothing_from_polkit():
    gloxi = {"user": "gloxi-1000", "groups": ["gloxi", "wheel"], "active": True, "local": True}
    llm = {"user": "vilna-llm", "groups": ["gloxi"]}
    assert decide(
        [
            [APPS, gloxi],
            [UNITS, gloxi, {"unit": "vilna-mcp-files@1000.service", "verb": "start"}],
            ["org.freedesktop.NetworkManager.network-control", gloxi],
            ["org.freedesktop.Flatpak.app-install", llm, {"origin": "flathub"}],
            # Not Gloxi: people and Herkos keep their rules.
            [UNITS, {"user": "gloxi"}, {"unit": "x.service", "verb": "start"}],
            [UNITS, {"user": "herkos"}, {"unit": "vilna-mcp-files@1000.service", "verb": "start"}],
            [UNITS, {"user": "anna-gloxi-1"}, {"unit": "x.service", "verb": "start"}],
        ]
    ) == ["no", "no", "no", "no", "defaults", "yes", "defaults"]


def actions():
    root = ET.parse(POLICY).getroot()
    return {a.get("id"): a for a in root.findall("action")}


def test_every_action_a_manifest_names_is_declared_and_owned_by_herkos():
    declared = actions()
    named = {
        op.polkit
        for manifest in ManifestRegistry.builtin()
        for op in manifest.operations.values()
        if op.polkit
    }
    assert named == set(declared)
    for action_id, a in declared.items():
        owner = {n.get("key"): n.text for n in a.findall("annotate")}
        assert owner.get("org.freedesktop.policykit.owner") == "unix-user:herkos", action_id
        # Nobody from elsewhere, and never without asking anybody at all.
        d = a.find("defaults")
        assert d.findtext("allow_any") == "no", action_id
        assert d.findtext("allow_inactive") == "no", action_id
        lang = "{http://www.w3.org/XML/1998/namespace}lang"
        for tag in ("description", "message"):
            seen = {e.get(lang) for e in a.findall(tag)}
            assert seen == LANGUAGES | {None}, (action_id, tag)
    rollback = declared["org.vilna.herkos.system.rollback"].find("defaults")
    assert rollback.findtext("allow_active") == "auth_admin"  # every time


def test_herkos_starts_every_built_in_tool_but_not_their_bus_filters():
    """ADR 0031: the D-Bus filter units come only as a dependency of a tool unit."""
    herkos = {"user": "herkos"}
    units = sorted(
        m.launch.program
        for m in ManifestRegistry.builtin()
        if m.launch is not None and m.launch.flatpak is None
    )
    assert {"vilna-mcp-desktop", "vilna-mcp-portals", "vilna-mcp-atspi"} <= set(units)
    cases = [[UNITS, herkos, {"unit": f"{u}@1000.service", "verb": "start"}] for u in units]
    filters = [
        [UNITS, herkos, {"unit": f"vilna-dbus-proxy-{t}@1000.service", "verb": "start"}]
        for t in ("desktop", "portals", "atspi")
    ]
    assert decide(cases + filters) == ["yes"] * len(cases) + ["no"] * len(filters)


def test_every_tool_with_dbus_has_its_filter_unit():
    for m in ManifestRegistry.builtin():
        if not m.dbus_calls():
            continue
        short = m.launch.program.removeprefix("vilna-mcp-")
        tool = (SYSTEMD / f"{m.launch.program}@.service").read_text()
        proxy = (SYSTEMD / f"vilna-dbus-proxy-{short}@.service").read_text()
        assert f"Requires=vilna-dbus-proxy-{short}@%i.service" in tool
        # The tool sees neither the person's runtime folder nor abstract sockets.
        assert "TemporaryFileSystem=/run/user:ro" in tool and "PrivateNetwork=yes" in tool
        assert f"/run/herkos-bus/%i/{short}/session.bus" in tool
        herkosd = "/usr/libexec/herkos/herkosd"
        assert f"ExecStart={herkosd} dbus-proxy {m.tool} /run/herkos-bus/%i/{short}" in proxy
        assert f"RuntimeDirectory=herkos-bus/%i/{short}" in proxy
