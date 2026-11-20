# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from vilna_mcp_desktop.protect import Process, identify, protected

ME = os.getuid()


def proc(tmp_path, pid, exe, argv, cgroup="0::/user.slice/app.slice\n", uid=ME):
    d = tmp_path / str(pid)
    d.mkdir()
    (d / "exe").symlink_to(exe)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "cgroup").write_text(cgroup)
    return identify(pid, tmp_path)


@pytest.mark.parametrize(
    ("exe", "argv", "cgroup"),
    [
        # Security and secrets.
        ("/usr/bin/gnome-shell", ["/usr/bin/gnome-shell"], ""),
        ("/usr/libexec/gcr-prompter", ["/usr/libexec/gcr-prompter"], ""),
        ("/usr/bin/keepassxc", ["keepassxc"], ""),
        # Vilna's own windows, also when Python runs them.
        ("/usr/bin/python3.13", ["/usr/bin/python3", "/usr/bin/herkos-dialog"], ""),
        ("/usr/bin/python3.13", ["python3", "-m", "herkos.dialog"], ""),
        ("/usr/bin/python3.13", ["python3", "-I", "/usr/bin/vilna-installer"], ""),
        ("/usr/bin/python3.13", ["/usr/bin/gloxi"], ""),
        # Administration and terminals.
        ("/usr/bin/gnome-control-center", ["gnome-control-center"], ""),
        ("/usr/bin/ptyxis", ["ptyxis"], ""),
        ("/usr/bin/bash", ["/usr/bin/bash", "/usr/sbin/yast2"], ""),
        # Flatpak, by the app id in its cgroup, whatever its program is called.
        (
            "/app/bin/whatever",
            ["whatever"],
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "app-flatpak-org.keepassxc.KeePassXC-12345.scope\n",
        ),
        (
            "/app/bin/gloxi",
            ["gloxi"],
            "0::/app.slice/app-flatpak-io.github.stdenis1980.VilnaBackup-9.scope\n",
        ),
    ],
)
def test_protected_apps(tmp_path, exe, argv, cgroup):
    p = proc(tmp_path, 4242, exe, argv, cgroup or "0::/\n")
    assert protected(p, own_uid=ME, own_pid=1) == "a protected app"


def test_ordinary_apps_are_usable_and_named_by_flatpak_id(tmp_path):
    p = proc(
        tmp_path,
        4243,
        "/app/lib/firefox/firefox",
        ["/app/lib/firefox/firefox", "/home/x/vilna-notes.html"],
        "0::/app.slice/app-flatpak-org.mozilla.firefox-77.scope\n",
    )
    assert protected(p, own_uid=ME, own_pid=1) is None
    assert p.label == "org.mozilla.firefox"
    # A file it opened does not make it protected; the program name does.
    editor = proc(tmp_path, 4244, "/usr/bin/gnome-text-editor", ["gnome-text-editor", "vilna.txt"])
    assert protected(editor, own_uid=ME, own_pid=1) is None
    assert editor.label == "gnome-text-editor"


def test_unknown_other_users_and_itself_are_protected(tmp_path):
    assert protected(None) == "the app could not be told"
    assert identify(0, tmp_path) is None
    assert identify(999999, tmp_path) is None  # gone
    other = proc(tmp_path, 4245, "/usr/bin/gedit", ["gedit"], uid=ME + 1)
    assert protected(other, own_uid=ME, own_pid=1) == "an app of another user"
    me = Process(pid=7, uid=ME, exe="python3", argv=("python3",))
    assert protected(me, own_uid=ME, own_pid=7) == "this tool itself"
    unknown_uid = Process(pid=8, uid=None, exe="gedit")
    assert protected(unknown_uid, own_uid=ME, own_pid=1) is not None


def test_this_process_is_identified_from_the_real_proc():
    p = identify(os.getpid())
    assert p is not None and p.uid == ME and p.exe.startswith("python")
    assert protected(p) == "this tool itself"
