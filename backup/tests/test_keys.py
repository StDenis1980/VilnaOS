# SPDX-License-Identifier: GPL-3.0-or-later
import stat
import subprocess

import pytest

from vilna_backup import keys


def test_recovery_key_shape():
    k = keys.new_recovery_key()
    groups = k.split("-")
    assert len(groups) == 8 and all(len(g) == 4 for g in groups)
    assert keys.normalise_recovery_key(k.lower().replace("-", " ")) == k


def test_recovery_key_typing_mistakes_are_forgiven():
    k = "0123-4567-89AB-CDEF-GHJK-MNPQ-RSTV-WXYZ"
    typed = "o123 4567 89ab cdef ghjk mnpq rstu wxyz"  # O for 0, U for V
    assert keys.normalise_recovery_key(typed) == k


@pytest.mark.parametrize("bad", ["", "1234", "0123-4567-89AB-CDEF-GHJK-MNPQ-RSTV-WXY!"])
def test_bad_recovery_keys(bad):
    with pytest.raises(keys.KeysError):
        keys.normalise_recovery_key(bad)


def test_repo_password_is_long():
    assert len(keys.new_repo_password()) >= 43  # 256 bits in base64


def test_file_store(tmp_path):
    store = keys.FileStore(tmp_path)
    store.put("abc123", keys.REPO_PASSWORD, "pw")
    assert store.get("abc123", keys.REPO_PASSWORD) == "pw"
    mode = (tmp_path / "abc123" / keys.REPO_PASSWORD).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600
    assert store.password_env("abc123") == {
        "RESTIC_PASSWORD_FILE": str(tmp_path / "abc123" / keys.REPO_PASSWORD)
    }
    store.delete("abc123", keys.REPO_PASSWORD)
    assert store.get("abc123", keys.REPO_PASSWORD) is None
    with pytest.raises(keys.KeysError):
        store.put("../x", "item", "v")


class FakeSecretTool:
    def __init__(self):
        self.items = {}
        self.calls = []

    def __call__(self, cmd, input=None, **kw):
        self.calls.append(cmd)
        attrs = tuple(a for a in cmd[2:] if not a.startswith("--"))
        if cmd[1] == "store":
            self.items[attrs] = input
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1] == "lookup":
            if attrs in self.items:
                return subprocess.CompletedProcess(cmd, 0, self.items[attrs], "")
            return subprocess.CompletedProcess(cmd, 1, "", "")
        self.items.pop(attrs, None)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_libsecret_store_never_puts_the_secret_on_the_command_line():
    tool = FakeSecretTool()
    store = keys.LibsecretStore(run=tool)
    store.put("r1", keys.REPO_PASSWORD, "s3cret")
    assert store.get("r1", keys.REPO_PASSWORD) == "s3cret"
    assert all("s3cret" not in " ".join(c) for c in tool.calls)
    env = store.password_env("r1")
    assert set(env) == {"RESTIC_PASSWORD_COMMAND"}
    assert "lookup vilna backup-repo repo r1 item repo-password" in env["RESTIC_PASSWORD_COMMAND"]
    store.delete("r1", keys.REPO_PASSWORD)
    assert store.get("r1", keys.REPO_PASSWORD) is None


def test_libsecret_unavailable():
    def broken(cmd, **kw):
        raise FileNotFoundError("secret-tool")

    with pytest.raises(keys.KeysError):
        keys.LibsecretStore(run=broken).put("r1", "x", "y")


def test_default_store(tmp_path):
    assert isinstance(keys.default_store({}), keys.LibsecretStore)
    assert isinstance(
        keys.default_store({"VILNA_BACKUP_KEYSTORE": f"file:{tmp_path}"}), keys.FileStore
    )
    with pytest.raises(keys.KeysError):
        keys.default_store({"VILNA_BACKUP_KEYSTORE": "plaintext"})
