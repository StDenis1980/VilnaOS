# SPDX-License-Identifier: Apache-2.0
import pytest
from fakes import FakeBus, raise_tool_error

from vilna_mcp_desktop.mcp import ToolError
from vilna_mcp_desktop.portals import Portals, state

STORE = "org.freedesktop.impl.portal.PermissionStore"


def store_bus(tables):
    def lookup(args):
        if args not in tables:
            raise ToolError(
                f"{STORE}.Lookup: GDBus.Error:org.freedesktop.portal.Error.NotFound: No entry"
            )
        return (dict(tables[args]), False)

    def set_permission(args):
        table, _create, id_, app, value = args
        tables.setdefault((table, id_), {})[app] = list(value)
        return ()

    def delete(args):
        table, id_, app = args
        tables.get((table, id_), {}).pop(app, None)
        return ()

    return FakeBus(
        {
            f"{STORE}.Lookup": lookup,
            f"{STORE}.SetPermission": set_permission,
            f"{STORE}.DeletePermission": delete,
        }
    )


@pytest.fixture
def tables():
    return {
        ("devices", "camera"): {"org.example.Chat": ["yes"], "": ["yes"], "bad id": ["yes"]},
        ("location", "location"): {"org.gnome.Maps": ["CITY", "1700000000"]},
        ("background", "background"): {"org.example.Chat": ["ask"]},
    }


def test_list_reads_seven_permissions_and_skips_odd_entries(tables):
    apps = Portals(store_bus(tables)).list()["apps"]
    assert apps == {
        "org.example.Chat": {"camera": "allowed", "background": "ask"},
        "org.gnome.Maps": {"location": "allowed"},
    }


def test_revoke_and_undo(tables):
    p = Portals(store_bus(tables))
    out = p.revoke("org.gnome.Maps", "location")
    # Location keeps its last use; only the accuracy changes.
    assert tables[("location", "location")]["org.gnome.Maps"] == ["NONE", "1700000000"]
    assert out["undo_arguments"] == {
        "app": "org.gnome.Maps",
        "permission": "location",
        "previous": ["CITY", "1700000000"],
    }
    p.restore(**out["undo_arguments"])
    assert tables[("location", "location")]["org.gnome.Maps"] == ["CITY", "1700000000"]


def test_grant_unset_then_undo_forgets(tables):
    p = Portals(store_bus(tables))
    out = p.grant("org.example.New", "microphone")
    assert tables[("devices", "microphone")] == {"org.example.New": ["yes"]}
    assert out["before"] == "unset" and out["now"] == "allowed"
    p.restore(**out["undo_arguments"])
    assert tables[("devices", "microphone")] == {}


@pytest.mark.parametrize(
    ("permission", "previous"),
    [
        ("camera", ["maybe"]),
        ("camera", ["yes", "no"]),
        ("camera", [1]),
        ("location", ["EXACT"]),
        ("location", ["FAR", "1"]),
        ("location", ["EXACT", "soon"]),
        ("camera", []),
    ],
)
def test_restore_takes_only_real_values(tables, permission, previous):
    with pytest.raises(ToolError):
        Portals(store_bus(tables)).restore("org.example.Chat", permission, previous)


def test_app_ids_are_checked(tables):
    p = Portals(store_bus(tables))
    for bad in ("", "chat", "org.example", "../etc", "org.example.Chat;x"):
        with pytest.raises(ToolError):
            p.revoke(bad, "camera")


def test_other_store_errors_are_reported():
    bus = FakeBus({f"{STORE}.Lookup": raise_tool_error("AccessDenied")})
    with pytest.raises(ToolError, match="AccessDenied"):
        Portals(bus).list()


def test_states():
    assert state("camera", None) == "unset"
    assert state("background", ["ask"]) == "ask"
    assert state("location", ["NONE", "0"]) == "denied"
    assert state("location", ["EXACT", "0"]) == "allowed"
