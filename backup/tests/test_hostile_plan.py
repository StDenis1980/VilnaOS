# SPDX-License-Identifier: GPL-3.0-or-later
"""Hostile and broken input for the backup program's plan, schedule, keys and settings.

Nothing here starts a program or touches the network: the key store tool and
systemd are replaced by fakes, and every file lives in a temporary folder.
The tests state what must hold for any input: a clean error of the module's
own kind instead of a crash, secrets left out unless the person agreed, units
that do not depend on the clock or the time zone, and settings files that are
never half-written.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import random
import re
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from vilna_backup import keys, plan, schedule, view
from vilna_backup.service import BackupError
from vilna_backup.settings import (
    DESTINATION_KINDS,
    PRESETS,
    SCHEDULES,
    Destination,
    Settings,
    SettingsError,
    config_dir,
    load_settings,
    parse_destination,
    save_settings,
    state_dir,
)

RECOVERY = "0123-4567-89AB-CDEF-GHJK-MNPQ-RSTV-WXYZ"

# Characters that are invisible or look like plain ones when pasted from a page,
# a chat or a PDF. None of them is a separator the key reader forgives.
INVISIBLE = [
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\u2060",  # word joiner
    "\ufeff",  # byte order mark
    "\u00ad",  # soft hyphen
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
    "\u202e",  # right-to-left override
    "\u2066",  # left-to-right isolate
    "\u00a0",  # no-break space
    "\u3000",  # ideographic space
    "\u2010",  # hyphen
    "\u2013",  # en dash
    "\u2212",  # minus sign
    "\u0301",  # combining acute
    "\U000e0041",  # tag letter A
]
# Cyrillic letters that look like the Latin ones in the key.
LOOKALIKES = {
    "A": "\u0410",
    "B": "\u0412",
    "C": "\u0421",
    "E": "\u0415",
    "H": "\u041d",
    "K": "\u041a",
    "M": "\u041c",
    "P": "\u0420",
    "T": "\u0422",
    "X": "\u0425",
    "Y": "\u0423",
}
# Names a key store must refuse: separators, controls, invisible marks, shell syntax.
HOSTILE_NAMES = [
    "",
    ".",
    "..",
    "../x",
    "a/b",
    "/abs",
    "a\n",
    "ab\r",
    "a b",
    "a\x00b",
    "a\u200bb",
    "\u200b",
    "a\u0306",
    "a;b",
    "$(x)",
    "*",
    "~",
    "-",
    "--",
    "a\\b",
    "a:b",
    "%",
]
TZ_NAMES = [
    "Nonexistent/Zone",
    "",
    "UTC",
    ":/nonexistent/zone",
    "<+0545>-5:45",
    "EST5EDT,M3.2.0,M11.1.0",
    "CET-1CEST,M3.5.0,M10.5.0/3",
]
MADRID = "CET-1CEST,M3.5.0,M10.5.0/3"


# -- helpers -----------------------------------------------------------------


@pytest.fixture
def tz(monkeypatch):
    """Switch the process time zone for one test; put it back afterwards."""

    def switch(value: str) -> None:
        monkeypatch.setenv("TZ", value)
        time.tzset()

    yield switch
    monkeypatch.undo()
    time.tzset()


def local(year, month, day, hour=12, minute=0) -> float:
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def write_raw(path: Path, raw: str | bytes) -> Path:
    if isinstance(raw, bytes):
        path.write_bytes(raw)
    else:
        path.write_text(raw, encoding="utf-8")
    return path


class MemoryStore:
    """A key store in a dict."""

    def __init__(self, items=None):
        self.items = dict(items or {})

    def get(self, repo_id, item):
        return self.items.get((repo_id, item))


class FakeTool:
    """Stands in for secret-tool: remembers every call, answers as told."""

    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises

    def __call__(self, cmd, input=None, **kwargs):
        self.calls.append((list(cmd), input))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def unit_lines(text: str) -> list[tuple[str, str, str]]:
    """(section, key, value) of a unit file; fails on anything that is not that shape."""
    section, out = "", []
    lines = text.split("\n")
    assert lines[0].startswith("# Written by vilna-backup")
    assert lines[-1] == ""
    for line in lines[1:-1]:
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        assert sep and re.fullmatch(r"[A-Za-z]+", key), line
        out.append((section, key, value))
    return out


def backed_up(p: plan.Plan, path: Path) -> bool:
    """Would the file be taken: under an included folder, with nothing on the way left out."""
    inside = any(path == i or path.is_relative_to(i) for i in p.includes)
    return inside and not any(p.is_excluded(x) for x in (path, *path.parents))


@pytest.fixture
def full_home(home: Path) -> Path:
    for rel in (".gnupg/pubring.kbx", ".config/goa-1.0/accounts.conf"):
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("secret", encoding="utf-8")
    return home


# -- recovery key: Cyrillic and invisible characters --------------------------


@pytest.mark.parametrize("mark", INVISIBLE)
def test_invisible_marks_in_a_recovery_key_are_refused(mark):
    positions = [0, 4, 5, 10, len(RECOVERY) - 1, len(RECOVERY)]
    for i in positions:
        with pytest.raises(keys.KeysError):
            keys.normalise_recovery_key(RECOVERY[:i] + mark + RECOVERY[i:])
    # as the only thing between the groups
    with pytest.raises(keys.KeysError):
        keys.normalise_recovery_key(RECOVERY.replace("-", mark))


def test_cyrillic_lookalikes_never_pass_for_latin_letters():
    hits = 0
    for i, char in enumerate(RECOVERY):
        if char in LOOKALIKES:
            hits += 1
            typed = RECOVERY[:i] + LOOKALIKES[char] + RECOVERY[i + 1 :]
            with pytest.raises(keys.KeysError):
                keys.normalise_recovery_key(typed)
            with pytest.raises(keys.KeysError):
                keys.normalise_recovery_key(typed.lower())
    assert hits >= 8


def test_a_whole_key_in_cyrillic_or_fullwidth_letters_is_refused():
    cyrillic = "".join(LOOKALIKES.get(c, c) for c in RECOVERY)
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if c != "-" else c for c in RECOVERY)
    arabic_digits = RECOVERY.translate({ord(d): 0x0660 + int(d) for d in "0123456789"})
    for text in (cyrillic, fullwidth, arabic_digits, "ключ" * 8, "\U0001f511" * 32):
        with pytest.raises(keys.KeysError):
            keys.normalise_recovery_key(text)


@pytest.mark.parametrize(
    "text",
    ["", " ", "-", "\n", "\x00", "A" * 31, "A" * 33, "A" * 10**6, "0123 " * 100_000, "-" * 10**6],
)
def test_wrong_sized_or_huge_recovery_input_is_a_clean_refusal(text):
    started = time.monotonic()
    with pytest.raises(keys.KeysError):
        keys.normalise_recovery_key(text)
    assert time.monotonic() - started < 5


def test_forgiven_forms_of_a_recovery_key():
    messy = [
        RECOVERY.lower(),
        RECOVERY.replace("-", ""),
        RECOVERY.replace("-", " "),
        RECOVERY.replace("-", "\t"),
        RECOVERY.replace("-", "\n"),
        "  " + RECOVERY + "\n",
        RECOVERY.replace("-", " - "),
        RECOVERY.replace("-", "--"),
        RECOVERY.replace("0", "o").replace("1", "l").replace("V", "u"),
        RECOVERY.replace("1", "I"),
    ]
    for text in messy:
        assert keys.normalise_recovery_key(text) == RECOVERY, repr(text)


def test_fresh_keys_survive_the_reader_and_use_the_safe_alphabet():
    seen = set()
    for _ in range(300):
        key = keys.new_recovery_key()
        assert re.fullmatch(r"([0-9A-HJKMNP-TV-Z]{4}-){7}[0-9A-HJKMNP-TV-Z]{4}", key)
        assert not set(key) & set("ILOU")
        assert keys.normalise_recovery_key(key) == key
        assert keys.normalise_recovery_key(key.lower().replace("-", " ")) == key
        assert keys.normalise_recovery_key(keys.normalise_recovery_key(key)) == key
        seen.add(key)
    assert len(seen) == 300


def test_pairing_codes_and_recovery_keys_are_not_interchangeable():
    for _ in range(50):
        code = keys.new_pairing_code()
        assert re.fullmatch(r"([0-9A-HJKMNP-TV-Z]{4}-){3}[0-9A-HJKMNP-TV-Z]{4}", code)
        assert keys.normalise_pairing_code(code.lower()) == code
        with pytest.raises(keys.KeysError):
            keys.normalise_recovery_key(code)
        with pytest.raises(keys.KeysError):
            keys.normalise_pairing_code(keys.new_recovery_key())


@pytest.mark.parametrize("mark", INVISIBLE[:6])
def test_invisible_marks_in_a_pairing_code_are_refused(mark):
    code = keys.new_pairing_code()
    with pytest.raises(keys.KeysError):
        keys.normalise_pairing_code(code[:7] + mark + code[7:])


def test_repository_passwords_are_long_and_never_repeat():
    passwords = {keys.new_repo_password() for _ in range(200)}
    assert len(passwords) == 200
    assert all(len(p) >= 43 and re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in passwords)


# -- password environment and credentials -------------------------------------


def test_from_password_environment_only_renames_password_sources():
    env = {
        "RESTIC_PASSWORD": "a",
        "RESTIC_PASSWORD_FILE": "/f",
        "RESTIC_PASSWORD_COMMAND": "cmd",
        "RESTIC_REST_PASSWORD": "must not follow",
        "AWS_SECRET_ACCESS_KEY": "must not follow",
        "ключ": "значення",
    }
    before = dict(env)
    out = keys.as_from_env(env)
    assert out == {
        "RESTIC_FROM_PASSWORD": "a",
        "RESTIC_FROM_PASSWORD_FILE": "/f",
        "RESTIC_FROM_PASSWORD_COMMAND": "cmd",
    }
    assert env == before
    assert keys.as_from_env({}) == {}
    assert keys.as_from_env({"PATH": "/bin", "\u200bRESTIC_PASSWORD": "x"}) == {}


def test_storage_credentials_only_for_known_kinds_and_only_when_saved():
    store = MemoryStore(
        {
            ("r1", "restic_rest_username"): "me",
            ("r1", "restic_rest_password"): "",
            ("r1", "aws_access_key_id"): "ключ",
        }
    )
    assert keys.credential_env(store, "r1", "rest") == {"RESTIC_REST_USERNAME": "me"}
    assert keys.credential_env(store, "r1", "s3") == {"AWS_ACCESS_KEY_ID": "ключ"}
    for kind in ("local", "sftp", "", "REST", "rest\n", "\u200brest", "../s3"):
        assert keys.credential_env(store, "r1", kind) == {}
    assert keys.credential_env(MemoryStore(), "r1", "rest") == {}


# -- file key store ------------------------------------------------------------


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_file_store_refuses_hostile_names_without_touching_the_disk(tmp_path, name):
    folder = tmp_path / "keys"
    store = keys.FileStore(folder)
    for repo_id, item in ((name, keys.REPO_PASSWORD), ("abc123", name)):
        with pytest.raises(keys.KeysError):
            store.put(repo_id, item, "secret")
        with pytest.raises(keys.KeysError):
            store.get(repo_id, item)
        with pytest.raises(keys.KeysError):
            store.delete(repo_id, item)
    with pytest.raises(keys.KeysError):
        store.password_env(name)
    assert not folder.exists() or not any(folder.rglob("*"))
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_settings_refuse_the_same_names_as_repository_ids(name):
    with pytest.raises(SettingsError):
        Settings(repo_id=name).validate()


def test_whatever_the_file_store_accepts_stays_inside_its_folder_and_private(tmp_path):
    folder = tmp_path / "keys"
    store = keys.FileStore(folder)
    names = ["abc123", "ключ", "Ключ2", "日本語", "٣٣", "a" * 100, "ABC", "é", "и\u0306", "a\u200b"]
    accepted = []
    old = os.umask(0)  # a careless umask must not loosen the key files
    try:
        for i, repo_id in enumerate(names):
            value = f"пароль-{i}-\U0001f511-\u200b"
            try:
                store.put(repo_id, keys.REPO_PASSWORD, value)
            except keys.KeysError:
                assert not (folder / repo_id).exists()
                continue
            accepted.append(repo_id)
            path = folder / repo_id / keys.REPO_PASSWORD
            assert path.resolve().is_relative_to(folder.resolve())
            assert store.get(repo_id, keys.REPO_PASSWORD) == value
            assert (path.stat().st_mode & 0o777) == 0o600
            assert (path.parent.stat().st_mode & 0o777) == 0o700
            assert store.password_env(repo_id) == {"RESTIC_PASSWORD_FILE": str(path)}
    finally:
        os.umask(old)
    assert {"abc123", "ABC", "a" * 100} <= set(accepted)
    assert "a\u200b" not in accepted and "и\u0306" not in accepted  # invisible and combining marks
    assert sorted(p.name for p in folder.iterdir()) == sorted(accepted)


def test_file_store_forgets_and_reports_missing_entries_quietly(tmp_path):
    store = keys.FileStore(tmp_path)
    assert store.get("abc", keys.REPO_PASSWORD) is None
    store.delete("abc", keys.REPO_PASSWORD)  # nothing there: no error
    store.put("abc", keys.REPO_PASSWORD, "one")
    store.put("abc", keys.REPO_PASSWORD, "two")
    assert store.get("abc", keys.REPO_PASSWORD) == "two"
    store.delete("abc", keys.REPO_PASSWORD)
    store.delete("abc", keys.REPO_PASSWORD)
    assert store.get("abc", keys.REPO_PASSWORD) is None


def test_file_store_keeps_other_repositories_apart(tmp_path):
    store = keys.FileStore(tmp_path)
    store.put("one", keys.REPO_PASSWORD, "1")
    store.put("two", keys.REPO_PASSWORD, "2")
    store.put("one", "restic_rest_password", "r")
    store.delete("one", keys.REPO_PASSWORD)
    assert store.get("two", keys.REPO_PASSWORD) == "2"
    assert store.get("one", "restic_rest_password") == "r"


# -- libsecret key store ------------------------------------------------------


def test_libsecret_never_puts_a_hostile_value_on_the_command_line():
    tool = FakeTool()
    store = keys.LibsecretStore(run=tool)
    for value in ("пароль", "a b\nc", "x\x00y", "$(touch /tmp/x)", "\u200b", "--help", "-"):
        store.put("r1", keys.REPO_PASSWORD, value)
        cmd, stdin = tool.calls[-1]
        assert stdin == value
        assert cmd[:2] == ["secret-tool", "store"] and cmd[2].startswith("--label=")
        assert cmd[3:] == ["vilna", "backup-repo", "repo", "r1", "item", keys.REPO_PASSWORD]
        assert value not in cmd


@pytest.mark.parametrize("name", [*HOSTILE_NAMES[1:], "ключ", "a'b", 'a"b', "a`b`", "a$HOME"])
def test_libsecret_lookup_command_for_restic_is_one_safe_shell_line(name):
    store = keys.LibsecretStore(tool="secret-tool", run=FakeTool())
    env = store.password_env(name)
    assert set(env) == {"RESTIC_PASSWORD_COMMAND"}
    words = shlex.split(env["RESTIC_PASSWORD_COMMAND"])
    assert words[1:] == ["lookup", "vilna", "backup-repo", "repo", name, "item", "repo-password"]


def test_libsecret_failures_are_the_programs_own_error():
    for bad in (OSError("no tool"), FileNotFoundError("secret-tool"), PermissionError("x")):
        store = keys.LibsecretStore(run=FakeTool(raises=bad))
        with pytest.raises(keys.KeysError):
            store.put("r1", "x", "y")
        with pytest.raises(keys.KeysError):
            store.get("r1", "x")
        with pytest.raises(keys.KeysError):
            store.delete("r1", "x")
    slow = FakeTool(raises=subprocess.TimeoutExpired("secret-tool", 30))
    with pytest.raises(keys.KeysError):
        keys.LibsecretStore(run=slow).put("r1", "x", "y")


def test_libsecret_refusal_carries_the_tools_words_or_the_code():
    with pytest.raises(keys.KeysError, match="locked"):
        keys.LibsecretStore(run=FakeTool(1, stderr="  keyring locked \n")).put("r1", "x", "y")
    with pytest.raises(keys.KeysError, match="7"):
        keys.LibsecretStore(run=FakeTool(7)).put("r1", "x", "y")


def test_libsecret_lookup_misses_are_none_not_errors():
    assert keys.LibsecretStore(run=FakeTool(1)).get("r1", "x") is None
    assert keys.LibsecretStore(run=FakeTool(0, stdout="")).get("r1", "x") is None
    assert keys.LibsecretStore(run=FakeTool(0, stdout="пароль")).get("r1", "x") == "пароль"
    keys.LibsecretStore(run=FakeTool(1)).delete("r1", "x")  # a failed clear is not an error


@pytest.mark.parametrize(
    "value",
    ["plaintext", "LIBSECRET", " libsecret", "libsecret\n", "libsecret\u200b", "\u200blibsecret"]
    + ["file", "FILE:/x", "файл:/x", "file ", "env", "libsecret:", "none"],
)
def test_key_store_choice_refuses_anything_unlisted(value):
    with pytest.raises(keys.KeysError):
        keys.default_store({"VILNA_BACKUP_KEYSTORE": value})


def test_key_store_choice_accepts_the_two_forms(tmp_path):
    assert isinstance(keys.default_store({}), keys.LibsecretStore)
    choice = {"VILNA_BACKUP_KEYSTORE": "libsecret"}
    assert isinstance(keys.default_store(choice), keys.LibsecretStore)
    store = keys.default_store({"VILNA_BACKUP_KEYSTORE": f"file:{tmp_path}/ключі"})
    assert isinstance(store, keys.FileStore) and store.folder == tmp_path / "ключі"


# -- settings: broken and hostile files ----------------------------------------


BROKEN_FILES = {
    "empty": "",
    "only spaces": "   \n",
    "bom": "\ufeff" + '{"version": 1}',
    "list": "[]",
    "null": "null",
    "number": "1",
    "string": '"version"',
    "future version": '{"version": 2}',
    "no version": "{}",
    "version as text": '{"version": "1"}',
    "truncated": '{"version": 1, "preset": "ho',
    "trailing junk": '{"version": 1} x',
    "nul after the object": '{"version": 1}\x00',
    "nan limit": '{"version": 1, "limit_bytes": NaN}',
    "infinite limit": '{"version": 1, "limit_bytes": Infinity}',
    "fractional limit": '{"version": 1, "limit_bytes": 1.5}',
    "negative limit": '{"version": 1, "limit_bytes": -1}',
    "zero limit": '{"version": 1, "limit_bytes": 0}',
    "text limit": '{"version": 1, "limit_bytes": "10"}',
    "preset as list": '{"version": 1, "preset": ["home"]}',
    "preset null": '{"version": 1, "preset": null}',
    "preset case": '{"version": 1, "preset": "Home"}',
    "schedule as object": '{"version": 1, "schedule": {}}',
    "schedule with newline": '{"version": 1, "schedule": "daily\\n"}',
    "include as text": '{"version": 1, "include": "/home/a"}',
    "include number": '{"version": 1, "include": [1]}',
    "include null": '{"version": 1, "include": null}',
    "include relative": '{"version": 1, "include": ["Documents"]}',
    "include dotdot relative": '{"version": 1, "include": ["../x"]}',
    "include nul": '{"version": 1, "include": ["/a\\u0000b"]}',
    "exclude relative": '{"version": 1, "exclude": ["x"]}',
    "exclude null item": '{"version": 1, "exclude": [null]}',
    "destination as text": '{"version": 1, "destination": "/x"}',
    "destination as list": '{"version": 1, "destination": []}',
    "destination empty": '{"version": 1, "destination": {}}',
    "destination without kind": '{"version": 1, "destination": {"location": "/x"}}',
    "destination without place": '{"version": 1, "destination": {"kind": "local"}}',
    "destination kind list": '{"version": 1, "destination": {"kind": ["local"], "location": "/x"}}',
    "destination kind case": '{"version": 1, "destination": {"kind": "LOCAL", "location": "/x"}}',
    "repo id number": '{"version": 1, "repo_id": 5}',
    "repo id empty": '{"version": 1, "repo_id": ""}',
    "repo id with zero width": '{"version": 1, "repo_id": "ab\\u200bcd"}',
    "repo id with newline": '{"version": 1, "repo_id": "ab\\n"}',
    "repo id with slash": '{"version": 1, "repo_id": "../x"}',
    "unknown key": '{"version": 1, "zzz": 1}',
    "password in file": '{"version": 1, "repo_password": "x"}',
    "unknown key in cyrillic": '{"version": 1, "ключ": 1}',
}


@pytest.mark.parametrize("name", list(BROKEN_FILES))
def test_broken_settings_file_is_a_settings_error_and_stays_as_it_was(tmp_path, name):
    path = write_raw(tmp_path / "settings.json", BROKEN_FILES[name])
    before = path.read_bytes()
    with pytest.raises(SettingsError):
        load_settings(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe{", b"\xff", b"\xc3\x28", b"\x80" * 100, '{"version": 1}'.encode("utf-16")],
)
def test_settings_file_that_is_not_utf8_is_a_settings_error(tmp_path, raw):
    with pytest.raises(SettingsError):
        load_settings(write_raw(tmp_path / "settings.json", raw))


def test_settings_path_that_is_a_folder_is_a_settings_error(tmp_path):
    (tmp_path / "folder").mkdir()
    with pytest.raises(SettingsError):
        load_settings(tmp_path / "folder")


def test_missing_settings_file_means_the_defaults(tmp_path):
    s = load_settings(tmp_path / "nothing" / "settings.json")
    assert (s.preset, s.schedule, s.enabled, s.destination) == ("documents", "daily", False, None)
    assert not (tmp_path / "nothing").exists()
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    assert load_settings(tmp_path / "dangling").preset == "documents"


def test_settings_file_cut_anywhere_is_never_read_as_valid(tmp_path):
    good = tmp_path / "good.json"
    save_settings(
        Settings(
            destination=parse_destination("/media/usb/копії"),
            include=("/home/а/Музика",),
            exclude=("/home/а/Відео",),
            limit_bytes=10**9,
        ),
        good,
    )
    text = good.read_text(encoding="utf-8")
    cut = tmp_path / "cut.json"
    for i in range(len(text.rstrip("\n"))):
        cut.write_text(text[:i], encoding="utf-8")
        with pytest.raises(SettingsError):
            load_settings(cut)
    cut.write_text(text.rstrip("\n"), encoding="utf-8")
    assert load_settings(cut) == load_settings(good)


def test_settings_file_with_one_value_changed_is_read_or_refused_cleanly(tmp_path):
    good = Settings(
        destination=parse_destination("sftp:me@nas:/srv/vilna"),
        include=("/home/a/Music",),
        exclude=("/home/a/Videos",),
        limit_bytes=5,
        device_key_id="abc",
        recovery_key_id="def",
    ).to_json()
    pool = [None, True, False, 0, -1, 1.5, 10**30, "", "x", "ключ", "\u200b", [], {}, [1], {"a": 1}]
    path = tmp_path / "settings.json"
    loaded = 0
    for key in good:
        if key == "version":
            continue
        for value in pool:
            data = {**good, key: value}
            path.write_text(json.dumps(data), encoding="utf-8")
            try:
                s = load_settings(path)
            except SettingsError:
                continue
            loaded += 1
            s.validate()
            assert isinstance(s.include, tuple) and isinstance(s.exclude, tuple)
            assert all(isinstance(p, str) and os.path.isabs(p) for p in (*s.include, *s.exclude))
            assert s.preset in PRESETS and s.schedule in SCHEDULES
    assert loaded > 20  # the harmless changes (the key ids, the repeated defaults) still load


def test_settings_file_with_random_damage_never_crashes(tmp_path):
    rng = random.Random(30)
    path = tmp_path / "settings.json"
    save_settings(
        Settings(destination=parse_destination("/media/usb"), include=("/home/a/Music",)), path
    )
    text = path.read_text(encoding="utf-8")
    junk = '{}[]",:\\\n\u200bю0-.eE\x00'
    for _ in range(800):
        chars = list(text)
        for _ in range(rng.randint(1, 4)):
            i = rng.randrange(len(chars))
            op = rng.choice("rid")
            if op == "r":
                chars[i] = rng.choice(junk)
            elif op == "i":
                chars.insert(i, rng.choice(junk))
            else:
                del chars[i]
        path.write_text("".join(chars), encoding="utf-8")
        with contextlib.suppress(SettingsError):
            load_settings(path)


def test_settings_file_with_a_huge_include_list_loads_quickly(tmp_path):
    paths = [f"/data/п{i}" for i in range(100_000)]
    path = write_raw(tmp_path / "settings.json", json.dumps({"version": 1, "include": paths}))
    started = time.monotonic()
    s = load_settings(path)
    assert len(s.include) == 100_000
    assert time.monotonic() - started < 10


def test_last_of_repeated_keys_wins_and_unknown_destination_kinds_are_refused(tmp_path):
    path = write_raw(
        tmp_path / "settings.json", '{"version": 1, "preset": "home", "preset": "settings"}'
    )
    assert load_settings(path).preset == "settings"
    for kind in ("ftp", "", "Local", "s4"):
        text = json.dumps({"version": 1, "destination": {"kind": kind, "location": "/x"}})
        with pytest.raises(SettingsError):
            load_settings(write_raw(path, text))


# -- settings: saving -----------------------------------------------------------


def test_cyrillic_and_invisible_paths_survive_a_save_and_load(tmp_path):
    include = (
        "/home/діма/Документи",
        "/home/діма/Музика\u200b",  # zero width space at the end
        "/home/діма/\u0439",  # precomposed
        "/home/діма/\u0438\u0306",  # decomposed spelling of the same letter
        "/home/діма/\U0001f511 ключі",
        "/home/діма/a b\tc",
    )
    s = Settings(destination=parse_destination("/media/діма/флешка"), include=include)
    path = tmp_path / "ключ" / "settings.json"
    save_settings(s, path)
    assert load_settings(path) == s
    assert load_settings(path).include == include
    assert "Документи" in path.read_text(encoding="utf-8")  # kept readable, not \u-escaped


def test_saved_settings_are_private_and_leave_no_temporary_files(tmp_path):
    path = tmp_path / "a" / "b" / "settings.json"
    save_settings(Settings(), path)
    save_settings(Settings(preset="home"), path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_saving_invalid_settings_leaves_the_old_file_alone(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(Settings(preset="home"), path)
    before = path.read_bytes()
    mistakes = [
        {"preset": "everything"},
        {"schedule": "minutely"},
        {"include": ("relative",)},
        {"exclude": ("/a\x00b",)},
        {"include": (5,)},
        {"limit_bytes": 0},
        {"limit_bytes": -5},
        {"limit_bytes": 1.5},
        {"limit_bytes": float("nan")},
        {"repo_id": "a/b"},
        {"repo_id": ""},
    ]
    for change in mistakes:
        broken = dataclasses.replace(Settings(), **change)
        with pytest.raises(SettingsError):
            save_settings(broken, path)
        assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_a_failure_while_saving_keeps_the_old_settings_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    save_settings(Settings(preset="home"), path)
    before = path.read_bytes()

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("vilna_backup.settings.os.replace", disk_full)
    with pytest.raises(OSError, match="No space"):
        save_settings(Settings(preset="settings"), path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]
    monkeypatch.undo()

    def dump_fails(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("vilna_backup.settings.json.dump", dump_fails)
    with pytest.raises(KeyboardInterrupt):
        save_settings(Settings(preset="settings"), path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_saving_over_a_link_replaces_the_link_not_what_it_points_at(tmp_path):
    victim = write_raw(tmp_path / "victim.txt", "keep me")
    path = tmp_path / "settings.json"
    path.symlink_to(victim)
    save_settings(Settings(preset="home"), path)
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert not path.is_symlink() and load_settings(path).preset == "home"


def test_saving_below_a_file_is_an_os_error_and_changes_nothing(tmp_path):
    blocker = write_raw(tmp_path / "blocker", "x")
    with pytest.raises(OSError):
        save_settings(Settings(), blocker / "settings.json")
    assert blocker.read_text(encoding="utf-8") == "x"


def test_changed_copies_are_checked_and_the_original_is_untouched():
    s = Settings(preset="home", include=("/home/a/Music",))
    for change in (
        {"preset": "x"},
        {"schedule": "Daily"},
        {"include": ("/a", "b")},
        {"limit_bytes": 0},
        {"limit_bytes": "5"},
        {"repo_id": "\u200b"},
    ):
        with pytest.raises(SettingsError):
            s.with_changes(**change)
    assert s.preset == "home" and s.include == ("/home/a/Music",)
    assert s.with_changes(preset="settings").preset == "settings"
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.preset = "settings"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["Documents", "documents ", "documents\n", "", "all", None, 1, ()])
def test_presets_and_schedules_outside_the_lists_are_refused(bad):
    with pytest.raises(SettingsError):
        dataclasses.replace(Settings(), preset=bad).validate()
    with pytest.raises(SettingsError):
        dataclasses.replace(Settings(), schedule=bad).validate()


def test_settings_and_state_folders_ignore_relative_or_empty_xdg_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for value in ("", "relative/dir", "~/x", ".", "$HOME/x"):
        monkeypatch.setenv("XDG_CONFIG_HOME", value)
        monkeypatch.setenv("XDG_STATE_HOME", value)
        assert config_dir() == tmp_path / ".config" / "vilna-backup"
        assert state_dir() == tmp_path / ".local/state" / "vilna-backup"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "налаштування"))
    assert config_dir() == tmp_path / "налаштування" / "vilna-backup"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state_dir() == tmp_path / ".local/state" / "vilna-backup"


# -- destinations ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\n",
        "relative/path",
        "./here",
        "~/backups",
        "$HOME/backups",
        "ftp:host/x",
        "local:/x",
        "LOCAL:/x",
        "http://example.eu/x",
        "file:///x",
        "sftp:",
        "s3:",
        "rest:",
        "S3:https://x/y",
        "Rest:https://x/y",
        "rest:http://example.eu/",
        "s3:http://example.eu/bucket",
        "rest:ftp://example.eu/",
        "rest:https://me@example.eu/",
        "rest:https://me:pw@example.eu/",
        "rest:https://:pw@example.eu/",
        "rest:https://example.eu\\@good.example/",
        "s3:https://key:secret@s3.example.eu/b",
        "rest:https://ключ:пароль@хост.example/",
        "\u200b/x",
        "\u200bsftp:me@nas:/x",
        "sftp\u200b:me@nas:/x",
        "ключ:/x",
        "abc",
    ],
)
def test_destinations_that_must_not_be_accepted(text):
    with pytest.raises(SettingsError):
        parse_destination(text)


def test_destinations_that_are_accepted_come_out_clean():
    assert parse_destination("  /media/usb/vilna  ").location == "/media/usb/vilna"
    assert parse_destination("/a/b/../c//d/").location == "/a/c/d"
    assert parse_destination("/a/../../etc").location == "/etc"
    assert parse_destination("/media/діма/флешка 2").location == "/media/діма/флешка 2"
    assert parse_destination("sftp:діма@nas.example:/srv/копії").kind == "sftp"
    assert parse_destination("rest:http://127.0.0.1:8000/me/").kind == "rest"
    assert parse_destination("s3:http://localhost:9000/bucket").kind == "s3"
    assert parse_destination("rest:https://example.eu/me/").repository() == (
        "rest:https://example.eu/me/"
    )


def test_destination_text_is_either_refused_cleanly_or_well_formed():
    rng = random.Random(3030)
    pieces = [
        "/", ":", "@", "//", "https://", "http://", "sftp", "s3", "rest", "local", "..", ".",
        "\n", "\x00", " ", "\u200b", "ключ", "a", "127.0.0.1", "localhost", "?", "#", "\\", "%2f",
    ]  # fmt: skip
    for _ in range(4000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        try:
            d = parse_destination(text)
        except SettingsError:
            continue
        assert d.kind in DESTINATION_KINDS
        assert d.describe()
        if d.kind == "local":
            assert os.path.isabs(d.location) and os.path.normpath(d.location) == d.location
            assert d.repository() == d.location
        else:
            assert d.repository() == f"{d.kind}:{d.location}"
        if d.kind in ("s3", "rest"):
            netloc = d.location.split("://", 1)[1].split("/", 1)[0]
            assert "@" not in netloc


@pytest.mark.parametrize(
    ("kind", "location", "secret"),
    [
        ("rest", "https://user:pw@host.example/x", "pw"),
        ("rest", "https://user@host.example/x", "user"),
        ("s3", "https://:pw@host.example/b", "pw"),
        ("rest", "https://ключ:пароль@хост.example/x", "пароль"),
        ("rest", "https://u:p\u200bw@host.example/x", "p\u200bw"),
    ],
)
def test_describing_a_destination_hides_the_login(kind, location, secret):
    text = Destination(kind, location).describe()
    assert secret not in text
    assert "host.example" in text or "хост.example" in text


def test_describing_a_destination_is_for_people_and_never_raises():
    for kind in DESTINATION_KINDS:
        for loc in ("", "x", "://", "a@b", "https://", "https://@", "ключ\n\x00", "@" * 50):
            assert isinstance(Destination(kind, loc).describe(), str)


# -- user folders and KeePass lists --------------------------------------------


def user_dirs_layout(home: Path, text: str | bytes) -> plan.Layout:
    (home / ".config").mkdir(parents=True, exist_ok=True)
    write_raw(home / ".config" / "user-dirs.dirs", text)
    return plan.Layout.from_env({}, home=home)


def test_layout_ignores_relative_and_empty_xdg_values(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "XDG_CONFIG_HOME": "relative",
        "XDG_DATA_HOME": "",
        "XDG_STATE_HOME": "~/state",
    }
    layout = plan.Layout.from_env(env, home=home)
    assert (layout.config, layout.data, layout.state) == (
        home / ".config",
        home / ".local/share",
        home / ".local/state",
    )
    layout = plan.Layout.from_env({"XDG_DATA_HOME": str(tmp_path / "дані")}, home=home)
    assert layout.data == tmp_path / "дані"
    assert layout.export_dir == tmp_path / "дані" / "vilna-backup" / "export"


def test_user_folder_names_in_ukrainian_with_invisible_marks_are_kept_exactly(tmp_path):
    home = tmp_path / "home"
    layout = user_dirs_layout(
        home,
        'XDG_DOCUMENTS_DIR="$HOME/Документи"\n'
        'XDG_PICTURES_DIR="$HOME/Зображення\u200b"\n'
        f'XDG_MUSIC_DIR="{tmp_path}/Музика/\u0438\u0306"\n',
    )
    assert layout.user_dir("DOCUMENTS") == home / "Документи"
    assert layout.user_dir("PICTURES") == home / "Зображення\u200b"
    assert layout.user_dir("MUSIC") == tmp_path / "Музика" / "\u0438\u0306"
    assert layout.user_dir("VIDEOS") == home / "Videos"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        "# comment\n",
        'XDG_DOCUMENTS_DIR="relative"\n',
        'XDG_DOCUMENTS_DIR="Documents2"\n',
        "XDG_DOCUMENTS_DIR=/no/quotes\n",
        'XDG_DOCUMENTS_DIR="/unclosed\n',
        'xdg_documents_dir="/x"\n',
        'XDG_TEMPLATES_DIR="/templates"\n',
        'XDG_PUBLICSHARE_DIR="/share"\n',
        'XDG_DOCUMENTS_DIR=""\n',
        'XDG_DOCUMENTS_DIR="$HOME"\n',
        'XDG_DOCUMENTS_DIR = "/x"\n',
        '\ufeffXDG_DOCUMENTS_DIR="/x"\n',
        "XDG_DOCUMENTS_DIR\n",
        "=\n",
        '"\n' * 1000,
    ],
)
def test_odd_user_folder_files_leave_the_defaults(tmp_path, text):
    home = tmp_path / "home"
    layout = user_dirs_layout(home, text)
    assert layout.user_dir("DOCUMENTS") == home / "Documents"
    names = {"DESKTOP", "DOCUMENTS", "DOWNLOAD", "MUSIC", "PICTURES", "VIDEOS"}
    assert set(layout.user_dirs) == names


def test_user_folder_file_that_is_a_folder_or_missing_leaves_the_defaults(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "user-dirs.dirs").mkdir(parents=True)
    assert plan.Layout.from_env({}, home=home).user_dir("DOCUMENTS") == home / "Documents"
    none = tmp_path / "none"
    assert plan.read_user_dirs(none, none) == {
        k: none / v for k, v in plan._DEFAULT_USER_DIRS.items()
    }


def test_huge_user_folder_file_is_read_quickly(tmp_path):
    home = tmp_path / "home"
    text = "# note\n" * 300_000 + 'XDG_DOCUMENTS_DIR="$HOME/Docs"\n'
    started = time.monotonic()
    layout = user_dirs_layout(home, text)
    assert time.monotonic() - started < 10
    assert layout.user_dir("DOCUMENTS") == home / "Docs"
    layout = user_dirs_layout(home, 'XDG_DOCUMENTS_DIR="$HOME/Docs"\r\nXDG_MUSIC_DIR="$HOME/M"\r\n')
    assert layout.user_dir("MUSIC") == home / "M"  # Windows line ends do no harm


def test_keepass_lists_that_are_garbage_give_nothing_and_no_crash(tmp_path):
    home = tmp_path / "home"
    ini = home / ".config" / "keepassxc" / "keepassxc.ini"
    ini.parent.mkdir(parents=True)
    for text in (
        "",
        "garbage",
        "[a\nx",
        "[a]\nLastOpenedDatabases\n",
        "[a]\nLastOpenedDatabases=%(x)s,%%,${y}\n",
        "[a]\n[a]\nLastOpenedDatabases=/nope.kdbx\n",
        "[a]\nLastOpenedDatabases=" + "," * 100_000 + "\n",
        "[a]\nLastOpenedDatabases=\x00,/x.kdbx\n",
    ):
        ini.write_text(text, encoding="utf-8")
        assert plan.keepass_databases(plan.Layout.from_env({}, home=home)) == []


def test_keepass_lists_name_only_real_absolute_kdbx_files(tmp_path):
    home = tmp_path / "home"
    (home / "бази").mkdir(parents=True)
    real = home / "бази" / "паролі\u200b.kdbx"
    real.write_text("db", encoding="utf-8")
    (home / "folder.kdbx").mkdir()
    (home / "notes.txt").write_text("x", encoding="utf-8")
    (home / "home.kdbx").write_text("db", encoding="utf-8")
    ini = home / ".config" / "keepassxc" / "keepassxc.ini"
    ini.parent.mkdir(parents=True)
    listed = [real, real, home / "gone.kdbx", home / "notes.txt", home / "folder.kdbx"]
    ini.write_text(
        "[General]\nLastOpenedDatabases=" + ",".join([*map(str, listed), "relative.kdbx"]) + "\n",
        encoding="utf-8",
    )
    found = plan.keepass_databases(plan.Layout.from_env({}, home=home))
    assert sorted(found) == sorted([real, home / "home.kdbx"])


# -- what goes into a backup ------------------------------------------------------


SECRET_FILES = (
    ".ssh/id_ed25519",
    ".local/share/keyrings/login.keyring",
    ".gnupg/pubring.kbx",
    ".config/goa-1.0/accounts.conf",
)
SECRET_DIRS = (".ssh", ".local/share/keyrings", ".gnupg", ".config/goa-1.0")


def include_variants(home: Path) -> list[tuple[str, ...]]:
    return [
        (),
        (str(home),),
        (str(home) + "/",),
        (str(home / ".local/share"),),
        (str(home / ".config"),),
        (str(home / ".ssh"),),
        (str(home / ".ssh") + "/",),
        (str(home / "Documents" / ".." / ".ssh"),),
        (str(home / "Documents" / ".." / ".gnupg"),),
        (str(home / ".local" / "share" / "keyrings" / "."),),
        (str(home / ".config" / "goa-1.0"), str(home / ".ssh" / "id_ed25519")),
        (str(home / ".ssh" / "id_ed25519"),),
        (str(home / ".local/share/keyrings/login.keyring"),),
    ]


@pytest.mark.parametrize("preset", PRESETS)
def test_secrets_stay_out_of_every_plan_without_the_separate_consent(full_home, preset):
    layout = plan.Layout.from_env({}, home=full_home)
    for include in include_variants(full_home):
        for history in (False, True):
            s = Settings(preset=preset, include=include, gloxi_history=history)
            p = plan.build_plan(s, layout)
            for rel in SECRET_FILES:
                assert not backed_up(p, full_home / rel), (preset, include, rel)
            lines = p.exclude_lines()
            for rel in SECRET_DIRS:
                assert str(full_home / rel) in lines


def test_secrets_are_backed_up_only_when_the_person_agreed(full_home):
    layout = plan.Layout.from_env({}, home=full_home)
    p = plan.build_plan(Settings(preset="home", system_secrets=True), layout)
    for rel in SECRET_FILES:
        assert backed_up(p, full_home / rel), rel
    assert not any(str(full_home / rel) in p.exclude_lines() for rel in SECRET_DIRS)
    p = plan.build_plan(Settings(preset="documents", system_secrets=True), layout)
    for rel in SECRET_FILES:
        assert backed_up(p, full_home / rel), rel


def test_user_folders_pointed_at_secrets_do_not_bring_them_in(full_home):
    text = "".join(
        f'XDG_{name}_DIR="$HOME/{rel}"\n'
        for name, rel in zip(
            ("DOCUMENTS", "DESKTOP", "PICTURES", "DOWNLOAD"),
            (".ssh", ".local/share/keyrings", ".gnupg", ".config/goa-1.0"),
            strict=True,
        )
    )
    layout = user_dirs_layout(full_home, text)
    for preset in ("documents", "home", "custom", "settings"):
        p = plan.build_plan(Settings(preset=preset), layout)
        for rel in SECRET_FILES:
            assert not backed_up(p, full_home / rel), (preset, rel)


def test_chat_history_and_undo_copies_follow_their_switches(home):
    layout = plan.Layout.from_env({}, home=home)
    history = home / ".local/share/gloxi/history/2026-11-02.jsonl"
    undo = home / ".local/state/herkos/backups/files/x/meta.json"
    journal = home / ".local/state/herkos/journal.jsonl"
    off = plan.build_plan(Settings(preset="settings"), layout)
    on = plan.build_plan(Settings(preset="settings", gloxi_history=True), layout)
    assert not backed_up(off, history) and backed_up(on, history)
    assert backed_up(off, journal)
    for p in (off, on):
        assert not backed_up(p, undo)


def test_unwanted_names_stay_out_wherever_they_are(home):
    layout = plan.Layout.from_env({}, home=home)
    p = plan.build_plan(Settings(preset="home"), layout)
    unwanted = [
        "Documents/models/qwen.gguf",
        "Documents/моделі/ваги.safetensors",
        "x/.cache/a",
        "a/b/node_modules/c",
        ".Trash-1000/x",
        "Documents/.snapshots/1",
        ".var/app/org.gnome.Maps/cache/tiles.bin",
        ".cache/big.bin",
    ]
    for rel in unwanted:
        assert not backed_up(p, home / rel), rel
    for rel in ("Documents/zvit.txt", "Pictures/2019/a.jpg", "Passwords.kdbx", "Videos/film.mkv"):
        assert backed_up(p, home / rel), rel
    assert not backed_up(p, home / "Downloads/setup.iso")  # the home preset leaves Downloads out


def test_a_folder_left_out_does_not_take_its_neighbours_with_it(home):
    layout = plan.Layout.from_env({}, home=home)
    for name in ("Music", "Music2", "Music-old", "MusicX", "Музика", "Музика\u200b"):
        (home / name).mkdir()
        (home / name / "a.mp3").write_text("x", encoding="utf-8")
    s = Settings(preset="home", exclude=(str(home / "Music"), str(home / "Музика")))
    p = plan.build_plan(s, layout)
    assert not backed_up(p, home / "Music" / "a.mp3")
    assert not backed_up(p, home / "Музика" / "a.mp3")
    for name in ("Music2", "Music-old", "MusicX", "Музика\u200b"):
        assert backed_up(p, home / name / "a.mp3"), name


def test_leaving_out_the_whole_home_backs_up_nothing_and_counts_nothing(home):
    layout = plan.Layout.from_env({}, home=home)
    for exclude in ((str(home),), ("/",), (str(home), str(home / "Documents"))):
        p = plan.build_plan(Settings(preset="home", exclude=exclude), layout)
        assert plan.plan_size(p) == 0
        assert not any(backed_up(p, f) for f in home.rglob("*"))
        assert all(line for line in p.exclude_lines())
        assert exclude[0] in p.exclude_lines()


def test_folders_that_do_not_exist_are_listed_as_missing_not_included(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    layout = plan.Layout.from_env({}, home=home)
    s = Settings(preset="documents", include=(str(home / "Музика"), "/definitely/not/here"))
    p = plan.build_plan(s, layout)
    assert p.includes == ()
    assert home / "Музика" in p.missing and Path("/definitely/not/here") in p.missing
    (home / "Музика").mkdir()
    p = plan.build_plan(s, layout)
    assert home / "Музика" in p.includes and home / "Музика" not in p.missing


def test_folders_are_cleaned_of_dots_and_repeats(home):
    layout = plan.Layout.from_env({}, home=home)
    (home / "Music").mkdir()
    include = (
        str(home / "Music"),
        str(home / "Music") + "/",
        str(home / "Documents" / ".." / "Music"),
        str(home / "Music" / "a" / ".."),
    )
    p = plan.build_plan(Settings(preset="custom", include=include), layout)
    assert list(p.includes).count(home / "Music") == 1
    assert all(os.path.normpath(i) == str(i) for i in p.includes)


def test_cleaning_a_long_list_of_folders_drops_repeats_and_keeps_everything_covered():
    rng = random.Random(7)
    names = ["a", "b", "ключ", "x\u200by", "..", ".", "c d"]
    paths = [
        Path("/" + "/".join(rng.choice(names) for _ in range(rng.randint(1, 5))))
        for _ in range(300)
    ]
    out = plan._dedupe(paths)
    norm = {Path(os.path.normpath(p)) for p in paths}
    assert len(out) == len(set(out))
    for a in out:
        assert not any(a != b and a.is_relative_to(b) for b in out)
    for p in norm:
        assert any(p == o or p.is_relative_to(o) for o in out)


def test_exclude_file_lines_are_non_empty_and_in_a_fixed_order(home):
    layout = plan.Layout.from_env({}, home=home)
    s = Settings(preset="home", exclude=(str(home / "Музика"),))
    first = plan.build_plan(s, layout).exclude_lines()
    assert first == plan.build_plan(s, layout).exclude_lines()
    assert all(line and "\x00" not in line for line in first)
    assert first[-len(plan.EXCLUDE_NAMES) :] == list(plan.EXCLUDE_NAMES)
    assert str(home / "Музика") in first


# -- the assistant's limits when it changes the folders ----------------------------


def snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize(
    "raw",
    [
        "relative",
        "",
        "~/Documents",
        "../etc",
        "/etc",
        "/",
        "/tmp",
        "{home}/../etc",
        "{home}/../../etc",
        "{home}/..",
        "{home}/Documents/../../x",
        "{home}/.ssh",
        "{home}/.ssh/",
        "{home}/.ssh/id_ed25519",
        "{home}/Documents/../.ssh",
        "{home}/.gnupg",
        "{home}/.local/share",
        "{home}/.local/share/keyrings",
        "{home}/.config",
        "{home}/.config/goa-1.0/accounts.conf",
        "{home}",
        "{home}/",
        "/{home}/Documents",
        "{home}x",
        "{home}-other/Documents",
        "{home}\x00/Documents",
        "{home}/Documents\x00",
    ],
)
def test_the_assistant_cannot_add_folders_outside_home_or_holding_secrets(backup, home, raw):
    path = raw.format(home=home)
    before = snapshot(backup.settings_file)
    with pytest.raises(BackupError):
        backup.set_paths(add=[path], by_assistant=True)
    assert snapshot(backup.settings_file) == before


@pytest.mark.parametrize("raw", [None, 5, b"/x", ["/x"], {"a": 1}])
def test_the_assistant_gets_a_clean_error_for_folders_that_are_not_text(backup, raw):
    with pytest.raises(BackupError):
        backup.set_paths(add=[raw])
    with pytest.raises(BackupError):
        backup.set_paths(remove=[raw])


def test_the_assistant_can_add_ordinary_home_folders_in_any_language(backup, home):
    names = ["Музика", "Музика\u200b", "a b", "\U0001f3b5", "x\ty", "\u0438\u0306"]
    for name in names:
        backup.set_paths(add=[str(home / name)])
    got = backup.settings.include
    assert got == tuple(str(home / n) for n in names)
    backup.set_paths(add=[str(home / "Documents" / ".." / "Музика")])
    assert backup.settings.include.count(str(home / "Музика")) == 1
    backup.set_paths(add=[str(home / "Docs") + "/"])
    assert str(home / "Docs") in backup.settings.include


def test_the_assistant_cannot_switch_secrets_on_or_pick_an_unknown_preset(backup):
    for value in (True, False, 1, "yes"):
        with pytest.raises(BackupError):
            backup.set_paths(system_secrets=value, by_assistant=True)
    with pytest.raises(BackupError):
        backup.set_paths(preset="everything")
    assert backup.settings.system_secrets is False
    assert not backup.settings_file.exists()


def test_the_person_can_choose_more_than_the_assistant(backup, tmp_path):
    outside = tmp_path / "external disk"
    backup.set_paths(add=[str(outside)], by_assistant=False)
    assert str(outside) in backup.settings.include
    backup.set_paths(system_secrets=True, by_assistant=False)
    assert backup.settings.system_secrets is True
    with pytest.raises(BackupError):
        backup.set_paths(add=["relative"], by_assistant=False)
    with pytest.raises(BackupError):
        backup.set_paths(add=[str(outside) + "\x00"], by_assistant=False)


def test_adding_and_removing_a_folder_moves_it_between_the_two_lists(backup, home):
    folder = str(home / "Музика")
    backup.set_paths(add=[folder])
    assert folder in backup.settings.include and folder not in backup.settings.exclude
    backup.set_paths(remove=[folder])
    assert folder not in backup.settings.include and folder not in backup.settings.exclude
    backup.set_paths(remove=[folder])
    assert folder in backup.settings.exclude
    backup.set_paths(add=[folder])
    assert folder not in backup.settings.exclude and folder not in backup.settings.include


def test_bad_limits_through_the_assistant_are_a_clean_error(backup):
    for limit in (0, -1, 1.5, "10", float("nan"), float("inf"), [1]):
        with pytest.raises(BackupError):
            backup.set_paths(limit_bytes=limit)
    backup.set_paths(limit_bytes=10**12)
    assert backup.settings.limit_bytes == 10**12
    backup.set_paths(limit_bytes=None)
    assert backup.settings.limit_bytes is None


# -- sizes and "does it fit" ---------------------------------------------------------


def test_sizes_do_not_follow_links_or_wait_on_pipes(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "file").write_bytes(b"x" * 10)
    big = tmp_path / "big"
    big.write_bytes(b"y" * 100_000)
    (base / "link-to-file").symlink_to(big)
    (base / "link-to-folder").symlink_to(tmp_path)
    (base / "loop").symlink_to(base)
    (base / "broken").symlink_to(tmp_path / "nowhere")
    os.mkfifo(base / "pipe")
    total = plan.size_of(base)
    assert 10 <= total < 1000  # the link texts are counted, never what they point at
    assert plan.size_of(base / "pipe") == 0
    assert plan.size_of(base / "loop") < 1000
    assert plan.size_of(tmp_path / "nowhere") == 0
    assert plan.size_of(Path("/definitely/not/here")) == 0


def test_sizes_count_odd_names_and_deep_folders(tmp_path):
    base = tmp_path / "base"
    folder = base
    for _ in range(150):
        folder = folder / "глибина"
    folder.mkdir(parents=True)
    for name in ("a\nb", "ключ", "x\u200by", "\U0001f511", " lead", "trail ", "-dash", "--help"):
        (folder / name).write_bytes(b"12345")
    assert plan.size_of(base) == 8 * 5


def test_sizes_skip_what_cannot_be_read_and_keep_counting(tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "closed").mkdir(parents=True)
    (base / "closed" / "f").write_bytes(b"x" * 100)
    (base / "open").mkdir()
    (base / "open" / "f").write_bytes(b"y" * 7)
    real = os.scandir

    def scandir(path):
        if Path(path).name == "closed":
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr("vilna_backup.plan.os.scandir", scandir)
    assert plan.size_of(base) == 7


def test_sizes_respect_the_plan_and_many_files_are_counted_quickly(home):
    layout = plan.Layout.from_env({}, home=home)
    folder = home / "Documents" / "many"
    folder.mkdir()
    for i in range(3000):
        (folder / f"файл-{i}.txt").write_bytes(b"z")
    p = plan.build_plan(Settings(preset="documents", exclude=(str(folder),)), layout)
    started = time.monotonic()
    with_all = plan.size_of(home / "Documents")
    without = plan.size_of(home / "Documents", p)
    assert time.monotonic() - started < 10
    assert with_all - without >= 3000
    assert plan.size_of(folder, p) == 0
    assert plan.size_of(home / "Documents" / "models" / "qwen.gguf", p) == 0


def test_overview_has_one_row_per_group_with_real_sizes(tmp_path):
    home = tmp_path / "home"
    layout = user_dirs_layout(
        home, 'XDG_DOCUMENTS_DIR="$HOME/Документи"\nXDG_MUSIC_DIR="$HOME/Музика"\n'
    )
    (home / "Документи").mkdir()
    (home / "Документи" / "a.txt").write_bytes(b"x" * 11)
    (home / "Документи" / "модель.gguf").write_bytes(b"w" * 999)
    # the person's exclusions are not used for the overview
    rows = plan.overview(layout, Settings(exclude=(str(home),)))
    groups = [r["group"] for r in rows]
    assert groups[0] == "settings" and groups[-1] == "home"
    assert len(groups) == len(set(groups))
    assert "music" not in groups  # that folder does not exist
    docs = next(r for r in rows if r["group"] == "documents")
    assert docs["bytes"] == 11 and docs["paths"] == [str(home / "Документи")]
    assert all(isinstance(r["bytes"], int) and r["bytes"] >= 0 for r in rows)


def test_fit_suggests_only_real_unprotected_folders_that_do_not_overlap(home):
    layout = plan.Layout.from_env({}, home=home)
    p = plan.build_plan(Settings(preset="home"), layout)
    total = plan.plan_size(p)
    assert plan.fit(p, None, layout)["fits"] and plan.fit(p, total, layout)["suggest_exclude"] == []
    protected = {str(x) for x in plan.settings_paths(layout)}
    for limit in (total - 1, total // 2, 1000, 1, 0, -5):
        out = plan.fit(p, limit, layout)
        assert out["bytes"] == total and out["fits"] is False
        picks = [Path(x) for x in out["suggest_exclude"]]
        assert all(x.is_dir() for x in picks)
        assert not protected & {str(x) for x in picks}
        for a in picks:
            assert not any(a != b and (a.is_relative_to(b) or b.is_relative_to(a)) for b in picks)
        assert 0 <= out["bytes_after"] <= total
        if picks:
            after = plan.build_plan(
                Settings(preset="home", exclude=tuple(out["suggest_exclude"])), layout
            )
            assert plan.plan_size(after) == out["bytes_after"]


def test_fit_with_only_protected_folders_suggests_nothing(home):
    layout = plan.Layout.from_env({}, home=home)
    p = plan.build_plan(Settings(preset="settings"), layout)
    out = plan.fit(p, 1, layout)
    assert out["fits"] is False and out["suggest_exclude"] == []
    assert out["bytes_after"] == out["bytes"]


# -- timers: units, schedule values, the clock and the time zone -----------------------


def timer_values(path: Path) -> dict[tuple[str, str], str]:
    return {(s, k): v for s, k, v in unit_lines(path.read_text(encoding="utf-8"))}


@pytest.mark.parametrize("name", [s for s in SCHEDULES if s != "off"])
def test_timer_units_use_system_calendar_words_and_catch_up_after_downtime(tmp_path, name):
    schedule.write_units(name, tmp_path)
    timer = timer_values(tmp_path / "vilna-backup.timer")
    assert timer[("Timer", "OnCalendar")] == name  # hourly, daily, weekly: kept by systemd
    assert timer[("Timer", "Persistent")] == "true"  # a run missed while off or asleep follows
    assert timer[("Install", "WantedBy")] == "timers.target"
    period = {"hourly": 3600, "daily": 86400, "weekly": 7 * 86400}[name]
    delay = timer[("Timer", "RandomizedDelaySec")]
    seconds = int(delay[:-3]) * 60 if delay.endswith("min") else int(delay[:-1]) * 3600
    assert 0 < seconds <= period / 6
    check = timer_values(tmp_path / "vilna-backup-check.timer")
    assert check[("Timer", "OnCalendar")] == "weekly" and check[("Timer", "Persistent")] == "true"


def test_units_hold_no_time_of_day_and_no_time_zone(tmp_path):
    for name in (s for s in SCHEDULES if s != "off"):
        schedule.write_units(name, tmp_path)
        for unit in tmp_path.iterdir():
            text = unit.read_text(encoding="utf-8")
            assert not re.search(r"\b\d{1,2}:\d{2}\b", text), unit.name
            assert not re.search(r"UTC|GMT|Z\b|[+-]\d{2}:?\d{2}\b|Europe/|America/", text)
            assert "*-" not in text and "OnActiveSec" not in text and "OnBootSec" not in text


@pytest.mark.parametrize("value", TZ_NAMES)
def test_units_are_the_same_whatever_the_time_zone_is(tmp_path, tz, value):
    schedule.write_units("daily", tmp_path / "base")
    expected = {p.name: p.read_text() for p in (tmp_path / "base").iterdir()}
    tz(value)
    schedule.write_units("daily", tmp_path / "other")
    assert {p.name: p.read_text() for p in (tmp_path / "other").iterdir()} == expected


def test_units_are_the_same_across_the_days_the_clocks_change(tmp_path, tz, monkeypatch):
    tz(MADRID)
    seen = set()
    for day in ((2026, 3, 28), (2026, 3, 29), (2026, 3, 30), (2026, 10, 24), (2026, 10, 25)):
        stamp = local(*day, 2, 30)
        monkeypatch.setattr(time, "time", lambda s=stamp: s)
        folder = tmp_path / "-".join(map(str, day))
        schedule.write_units("daily", folder)
        seen.add(tuple(sorted((p.name, p.read_text()) for p in folder.iterdir())))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "bad",
    ["Daily", "DAILY", "daily\n", " daily", "daily ", "", "off ", "Off", "hourly; rm -rf /",
     "weekly\nExecStart=/bin/sh", "../daily", "минуло", "daily\u200b", "\u200bdaily", "monthly",
     "*-*-* 02:30:00", "02:30", "daily,weekly", None, 1, ["daily"]],
)  # fmt: skip
def test_unknown_schedule_is_refused_before_anything_is_written(tmp_path, systemd, bad):
    folder = tmp_path / "units"
    with pytest.raises(ValueError):
        schedule.write_units(bad, folder)
    with pytest.raises(ValueError):
        schedule.apply(bad, run=systemd, folder=folder)
    assert not folder.exists()
    assert systemd.calls == []


def test_every_schedule_word_has_a_calendar_word_and_off_does_not():
    assert set(schedule.CALENDAR) == {s for s in SCHEDULES if s != "off"}
    assert all(v == k for k, v in schedule.CALENDAR.items())


def test_units_have_the_right_shape_and_nothing_extra(tmp_path):
    written = schedule.write_units("weekly", tmp_path)
    assert sorted(p.name for p in written) == [
        "vilna-backup-check.service",
        "vilna-backup-check.timer",
        "vilna-backup.service",
        "vilna-backup.timer",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in written)
    for path in written:
        sections = {s for s, _, _ in unit_lines(path.read_text(encoding="utf-8"))}
        assert sections <= {"Unit", "Service", "Timer", "Install"}
    for name in ("vilna-backup.service", "vilna-backup-check.service"):
        body = unit_lines((tmp_path / name).read_text())
        assert [v for s, k, v in body if k == "Type"] == ["oneshot"]
        assert len([1 for s, k, v in body if k == "ExecStart"]) == 1
        extras = ("ExecStartPre", "ExecStartPost", "User", "Environment")
        assert not any(k in extras for _, k, _ in body)


def test_turning_off_removes_only_the_backup_timer_and_can_repeat(tmp_path, systemd):
    schedule.apply("daily", run=systemd, folder=tmp_path)
    for _ in range(3):
        assert schedule.apply("off", run=systemd, folder=tmp_path) == "timer off"
        assert not (tmp_path / "vilna-backup.timer").exists()
        assert (tmp_path / "vilna-backup-check.timer").exists()
    assert schedule.apply("weekly", run=systemd, folder=tmp_path) == "timer on: weekly"
    assert "OnCalendar=weekly" in (tmp_path / "vilna-backup.timer").read_text()


def test_units_are_written_into_a_folder_that_did_not_exist(tmp_path):
    folder = tmp_path / "a" / "дані" / "b"
    schedule.write_units("hourly", folder)
    assert (folder / "vilna-backup.timer").is_file()
    schedule.write_units("hourly", folder)  # again over the old files


def test_units_follow_the_config_folder_but_not_relative_ones(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for value in ("", "relative", "~/x", "."):
        monkeypatch.setenv("XDG_CONFIG_HOME", value)
        assert schedule.unit_dir() == tmp_path / ".config" / "systemd" / "user"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "налаштування"))
    assert schedule.unit_dir() == tmp_path / "налаштування" / "systemd" / "user"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert schedule.unit_dir() == tmp_path / ".config" / "systemd" / "user"


def test_start_command_survives_a_program_path_with_spaces_and_quotes(monkeypatch):
    for exe in ("/opt/my apps/vilna-backup", "/opt/it's/vilna-backup", "/opt/ключ/vilna-backup"):
        monkeypatch.setattr(schedule.shutil, "which", lambda name, exe=exe: exe)
        assert schedule.command() == [exe]
        text = schedule.service_text("Vilna Backup", ["run", "--scheduled"], keystore_env=None)
        line = next(x for x in text.split("\n") if x.startswith("ExecStart="))
        assert shlex.split(line.removeprefix("ExecStart=")) == [exe, "run", "--scheduled"]
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    assert schedule.command()[1:] == ["-m", "vilna_backup"]


def test_key_store_choice_reaches_the_service_unit_as_one_value(tmp_path, monkeypatch):
    values = ("file:/home/a/my keys", "file:/home/it's/ключі", "file:/x/\u200b", "libsecret")
    for number, value in enumerate(values):
        monkeypatch.setenv("VILNA_BACKUP_KEYSTORE", value)
        folder = tmp_path / str(number)
        schedule.write_units("daily", folder)
        for name in ("vilna-backup.service", "vilna-backup-check.service"):
            body = unit_lines((folder / name).read_text(encoding="utf-8"))
            envs = [v for s, k, v in body if k == "Environment"]
            assert [shlex.split(e) for e in envs] == [["VILNA_BACKUP_KEYSTORE=" + value]]
    monkeypatch.setenv("VILNA_BACKUP_KEYSTORE", "")
    schedule.write_units("daily", tmp_path / "empty")
    assert "Environment" not in (tmp_path / "empty" / "vilna-backup.service").read_text()


def test_systemd_trouble_is_a_note_for_the_person_never_a_crash(tmp_path):
    def raising(error):
        def run(cmd, **kwargs):
            raise error

        return run

    for error in (
        FileNotFoundError("systemctl"),
        PermissionError("no"),
        OSError("broken"),
        subprocess.TimeoutExpired("systemctl", 30),
    ):
        for name in SCHEDULES:
            note = schedule.apply(name, run=raising(error), folder=tmp_path)
            assert note.startswith("units written, but systemd is not reachable")
        assert schedule.start_now(run=raising(error)) is False


def test_a_failing_step_stops_the_rest_and_says_which_one(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 5, "", "  Failed to connect to bus: ключ \n")

    note = schedule.apply("daily", run=run, folder=tmp_path)
    assert note == "units written, but daemon-reload failed: Failed to connect to bus: ключ"
    assert len(calls) == 1
    assert all(c[:2] == ["systemctl", "--user"] for c in calls)


def test_switching_off_goes_on_when_disabling_a_timer_that_was_never_on(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        failing = "disable" in cmd
        return subprocess.CompletedProcess(cmd, 1 if failing else 0, "", "not loaded")

    assert schedule.apply("off", run=run, folder=tmp_path) == "timer off"
    assert ["systemctl", "--user", "enable", "--now", "vilna-backup-check.timer"] in calls


def test_only_user_level_systemctl_with_plain_arguments_is_ever_called(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    for name in SCHEDULES:
        schedule.apply(name, run=run, folder=tmp_path)
    schedule.start_now(run=run)
    assert calls
    for cmd, kwargs in calls:
        assert isinstance(cmd, list) and cmd[:2] == ["systemctl", "--user"]
        assert all(isinstance(x, str) and "\n" not in x and ";" not in x for x in cmd)
        assert "shell" not in kwargs or kwargs["shell"] is False
        assert kwargs.get("timeout")


# -- times shown to the person, across zones and clock changes ---------------------------


def test_snapshot_times_are_shown_right_around_the_clock_changes(tz):
    tz(MADRID)
    cases = [
        # spring: the night of 28 to 29 March 2026 has 23 hours
        (local(2026, 3, 28, 23, 59), local(2026, 3, 29, 0, 1), "yesterday at 23:59"),
        (local(2026, 3, 28, 12), local(2026, 3, 29, 12), "yesterday at 12:00"),
        (local(2026, 3, 29, 1, 59), local(2026, 3, 29, 3, 1), "today at 01:59"),
        (local(2026, 3, 28, 12), local(2026, 3, 30, 12), "28 March at 12:00"),
        # autumn: 25 October 2026 has 25 hours
        (local(2026, 10, 24, 23, 30), local(2026, 10, 25, 0, 10), "yesterday at 23:30"),
        (local(2026, 10, 24, 12), local(2026, 10, 25, 12), "yesterday at 12:00"),
        (local(2026, 10, 25, 1, 0), local(2026, 10, 25, 23, 59), "today at 01:00"),
        (local(2026, 10, 25, 12), local(2026, 10, 26, 12), "yesterday at 12:00"),
        (local(2026, 10, 25, 23, 59), local(2026, 10, 26, 0, 1), "yesterday at 23:59"),
        (local(2026, 10, 24, 12), local(2026, 10, 26, 12), "24 October at 12:00"),
    ]
    for stamp, now, expected in cases:
        assert view.when(stamp, now) == expected


def test_restic_times_with_an_offset_are_shown_in_the_local_zone(tz):
    tz(MADRID)
    now = local(2026, 3, 29, 12)
    assert view.when("2026-03-29T01:30:00Z", now) == "today at 03:30"  # after the change
    assert view.when("2026-03-28T22:59:59.123456789+00:00", now) == "yesterday at 23:59"
    assert view.when("2026-03-29T12:30:00.5+02:00", now) == "today at 12:30"
    assert view.when("2026-03-29T07:00:00-05:00", now) == "today at 14:00"
    assert view.when("2028-02-29T12:00:00Z", local(2028, 3, 1)) == "yesterday at 13:00"


@pytest.mark.parametrize("value", TZ_NAMES)
def test_times_are_shown_in_every_zone_even_one_the_system_does_not_know(tz, value):
    tz(value)
    now = local(2026, 11, 27, 12)
    assert view.when(now - 3600, now) == "today at 11:00"
    assert view.when(now - 86400, now) == "yesterday at 12:00"
    assert view.when(now - 5 * 86400, now) == "22 November at 12:00"
    assert view.when("2026-11-27T10:00:00Z", now).startswith("today at ")
    assert view.when(None, now) == "—"


@pytest.mark.parametrize(
    "text",
    [
        "", "x", " ", "T", "Z", "+", "-", ".", "..", "T.Z", "\x00", "\n", "ключ", "\u200b",
        "2026-13-45T25:61:61", "2026-02-30T10:00:00Z", "2027-02-29T12:00:00Z",
        "2026-11-09T15:42:01.5+99:99", "2026-01-01T00:00:00.1-", "2026-01-01T00:00:00.1+1",
        "\u0662\u0660\u0662\u0666-11-09T15:42:01Z", "2026-11-09T15:42:01Z\n",
        "x" * 100_000, "2026-01-01T00:00:00." + "9" * 100_000 + "Z", "9" * 100_000,
        "0001-01-01T00:00:00", "9999-12-31T23:59:59",
    ],
)  # fmt: skip
def test_snapshot_times_that_are_nonsense_give_a_dash_or_a_time_and_never_a_crash(tz, text):
    tz(MADRID)
    shown = view.when(text, local(2026, 11, 27))
    assert isinstance(shown, str) and shown
    parsed = view._parse_iso(text)
    assert parsed is None or isinstance(parsed, float)
    if parsed is None:
        assert shown == "—"


def test_valid_leap_day_parses_and_a_missing_one_does_not(tz):
    tz("UTC")
    assert view._parse_iso("2028-02-29T12:00:00Z") == 1835438400.0
    assert view._parse_iso("2027-02-29T12:00:00Z") is None
    assert view._parse_iso("2100-02-29T12:00:00Z") is None  # 2100 is not a leap year
    assert view._parse_iso("2000-02-29T12:00:00Z") == 951825600.0
