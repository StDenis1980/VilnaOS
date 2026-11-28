# SPDX-License-Identifier: GPL-3.0-or-later
"""Keys: the repository password, the recovery key and storage credentials.

design.md, section 4. The repository password is 256 random bits made on the
device and kept in GNOME Keyring through libsecret (``secret-tool``); in the
Vilna image it moves to systemd-creds with TPM2. restic reads it itself
through ``RESTIC_PASSWORD_COMMAND``, so the password is never put in an
environment variable or a command line, and nothing here ever returns it to
Gloxi.

The recovery key is a second restic key, shown to the person once and
written down on the recovery sheet.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

# Item names inside the store, per repository.
REPO_PASSWORD = "repo-password"
# Storage credentials, given to restic as environment variables.
CREDENTIALS = {
    "rest": ("RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD"),
    "s3": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
}

# Crockford base32 without I, L, O, U: easy to read and write by hand.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RECOVERY_GROUPS = 8
RECOVERY_GROUP_LEN = 4  # 8 x 4 characters x 5 bits = 160 bits


# One-time code that joins a new device (devices.md, "Add a device"):
# 4 x 4 characters x 5 bits = 80 bits, valid for minutes only.
PAIRING_GROUPS = 4


class KeysError(Exception):
    """The key store is unavailable or refused."""


def new_repo_password() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


def new_recovery_key() -> str:
    chars = "".join(secrets.choice(_ALPHABET) for _ in range(RECOVERY_GROUPS * RECOVERY_GROUP_LEN))
    return format_recovery_key(chars)


def format_recovery_key(chars: str) -> str:
    n = RECOVERY_GROUP_LEN
    return "-".join(chars[i : i + n] for i in range(0, len(chars), n))


def normalise_recovery_key(text: str) -> str:
    """What the person typed, read forgivingly: case, spaces, dashes, O/0, I/L/1."""
    return _normalise(text, RECOVERY_GROUPS, "a recovery key")


def new_pairing_code() -> str:
    chars = "".join(secrets.choice(_ALPHABET) for _ in range(PAIRING_GROUPS * RECOVERY_GROUP_LEN))
    return format_recovery_key(chars)


def normalise_pairing_code(text: str) -> str:
    return _normalise(text, PAIRING_GROUPS, "a code to add a device")


def _normalise(text: str, groups: int, what: str) -> str:
    # ASCII spaces and dashes are forgiven, \r\n from Windows too. Anything not
    # ASCII is refused before upper(): "ı".upper() is "I" and "ß".upper() is "SS".
    if not text.isascii():
        raise KeysError(f"{what} has a sign that is not on the sheet (only A-Z and 0-9)")
    cleaned = "".join(c for c in text.upper() if not c.isspace() and c != "-")
    cleaned = cleaned.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"}))
    if len(cleaned) != groups * RECOVERY_GROUP_LEN or any(c not in _ALPHABET for c in cleaned):
        raise KeysError(f"{what} is {groups} groups of {RECOVERY_GROUP_LEN} letters and digits")
    return format_recovery_key(cleaned)


def as_from_env(password_env: Mapping[str, str]) -> dict[str, str]:
    """The same password, for the repository restic copies *from*."""
    names = {
        "RESTIC_PASSWORD": "RESTIC_FROM_PASSWORD",
        "RESTIC_PASSWORD_FILE": "RESTIC_FROM_PASSWORD_FILE",
        "RESTIC_PASSWORD_COMMAND": "RESTIC_FROM_PASSWORD_COMMAND",
    }
    return {names[k]: v for k, v in password_env.items() if k in names}


class KeyStore(Protocol):
    def put(self, repo_id: str, item: str, value: str) -> None: ...

    def get(self, repo_id: str, item: str) -> str | None: ...

    def delete(self, repo_id: str, item: str) -> None: ...

    def password_env(self, repo_id: str) -> dict[str, str]:
        """Environment that lets restic fetch the repository password itself."""
        ...


Runner = Callable[..., subprocess.CompletedProcess]


def _key_name(name: str) -> bool:
    return isinstance(name, str) and name.isascii() and name.isalnum()


def _check_names(repo_id: str, item: str) -> None:
    if not _key_name(repo_id) or not _key_name(item.replace("-", "").replace("_", "")):
        raise KeysError("bad key name")


class LibsecretStore:
    """GNOME Keyring through ``secret-tool`` (libsecret-tools)."""

    def __init__(self, tool: str = "secret-tool", run: Runner = subprocess.run) -> None:
        self.tool = tool
        self.run = run

    @staticmethod
    def attributes(repo_id: str, item: str) -> list[str]:
        return ["vilna", "backup-repo", "repo", repo_id, "item", item]

    def put(self, repo_id: str, item: str, value: str) -> None:
        _check_names(repo_id, item)
        label = f"Vilna Backup ({item})"
        cmd = [self.tool, "store", f"--label={label}", *self.attributes(repo_id, item)]
        self._call(cmd, value)

    def get(self, repo_id: str, item: str) -> str | None:
        _check_names(repo_id, item)
        out = self._call([self.tool, "lookup", *self.attributes(repo_id, item)], None, check=False)
        return out or None

    def delete(self, repo_id: str, item: str) -> None:
        _check_names(repo_id, item)
        self._call([self.tool, "clear", *self.attributes(repo_id, item)], None, check=False)

    def password_env(self, repo_id: str) -> dict[str, str]:
        tool = shutil.which(self.tool) or self.tool
        cmd = [tool, "lookup", *self.attributes(repo_id, REPO_PASSWORD)]
        return {"RESTIC_PASSWORD_COMMAND": shlex.join(cmd)}

    def _call(self, cmd: list[str], stdin: str | None, *, check: bool = True) -> str:
        try:
            p = self.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            raise KeysError(f"the key store is unavailable: {e}") from None
        if check and p.returncode != 0:
            raise KeysError(f"the key store refused: {p.stderr.strip() or p.returncode}")
        return p.stdout if p.returncode == 0 else ""


class FileStore:
    """Keys in files readable by the user only. For tests and headless trials.

    Not for real use: anything running as the user can read them. Selected
    with ``VILNA_BACKUP_KEYSTORE=file:<folder>``.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def _path(self, repo_id: str, item: str) -> Path:
        if not _key_name(repo_id) or not _key_name(item.replace("-", "").replace("_", "")):
            raise KeysError("bad key name")
        return self.folder / repo_id / item

    def put(self, repo_id: str, item: str, value: str) -> None:
        path = self._path(repo_id, item)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Never through a link, and owner-only even if the file was there before.
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(value)
        except OSError as e:
            raise KeysError(f"cannot write the key: {e}") from None

    def get(self, repo_id: str, item: str) -> str | None:
        try:
            with open(self._path(repo_id, item), encoding="utf-8", newline="") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise KeysError(f"cannot read the key: {e}") from None

    def delete(self, repo_id: str, item: str) -> None:
        self._path(repo_id, item).unlink(missing_ok=True)

    def password_env(self, repo_id: str) -> dict[str, str]:
        return {"RESTIC_PASSWORD_FILE": str(self._path(repo_id, REPO_PASSWORD))}


def default_store(env: Mapping[str, str] | None = None) -> KeyStore:
    env = os.environ if env is None else env
    choice = env.get("VILNA_BACKUP_KEYSTORE", "")
    if choice.startswith("file:"):
        return FileStore(Path(choice.removeprefix("file:")))
    if choice not in ("", "libsecret"):
        raise KeysError("VILNA_BACKUP_KEYSTORE must be libsecret or file:<folder>")
    return LibsecretStore()


def credential_env(store: KeyStore, repo_id: str, kind: str) -> dict[str, str]:
    """Storage login for restic (rest-server or S3), if one was saved."""
    names = CREDENTIALS.get(kind)
    if not names:
        return {}
    env = {}
    for var in names:
        value = store.get(repo_id, var.lower())
        if value:
            env[var] = value
    return env
