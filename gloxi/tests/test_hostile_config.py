# SPDX-License-Identifier: GPL-3.0-or-later
"""Hostile input for Gloxi's settings, its local key and the network gate.

Covers ``config.py``, ``localkey.py``, ``network.py`` and ``privacy.py``. Every key,
address and file here is made up; nothing reads or writes outside a temporary folder and
nothing leaves the computer. Only behaviour that is already right is asserted, and no test
is skipped or expected to fail. The groups:

* TOML: broken syntax, values of the wrong kind, tables that are not tables, huge files;
* precedence: defaults, then the file, then the environment, and what an empty value means;
* addresses: ``file:``, plain ``http://`` away from this computer, look-alike host names,
  ports 0 and 65536; Gloxi's own key goes to this computer and nowhere else;
* the key file of the shared model: invisible and non-ASCII characters, line breaks, size,
  directories, permissions;
* the key file Gloxi writes for its own server: mode 0600, location, clean-up;
* the internet gate (``network.py``): the exact commands, hostile JSON-RPC, hostile answers
  from the security layer;
* the privacy switch: a settings mistake never turns masking off quietly.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from gloxi import localkey, network
from gloxi.config import (
    DEFAULT_LOCAL_URL,
    ConfigError,
    ModelEndpoint,
    RouterPolicy,
    config_path,
    load_settings,
)
from gloxi.engine import listening
from gloxi.llm import ChatClient, ModelError
from gloxi.privacy import guard_factory
from gloxi.router import NetworkRefused

# Made-up values. None of them is, or looks like, a working secret.
FAKE_LOCAL_KEY = "fake-local-key-0123456789abcdef"
FAKE_CLOUD_KEY = "fake-cloud-key-for-tests-only"


CLOUD_TOML = (
    f'[cloud]\nurl = "https://api.example.eu/v1"\nmodel = "m"\napi_key = "{FAKE_CLOUD_KEY}"\n'
)


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def settings_from(tmp_path: Path, text: str, env: dict[str, str] | None = None):
    return load_settings(write_config(tmp_path, text), env=env or {})


def key_settings(tmp_path: Path, content: bytes, name: str = "model.key"):
    path = tmp_path / name
    path.write_bytes(content)
    return load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(path)})


# -- TOML ---------------------------------------------------------------------------------

BROKEN_TOML = [
    "[local",
    "[local]\nurl = ",
    '[local]\nurl = "unterminated',
    "[local]\nurl = 'a'\n[local]\nurl = 'b'",
    "[local]\nurl = 'a'\nurl = 'b'",
    "[local]\ntimeout = 1__0",
    "[local]\ntimeout = 0x",
    '[local]\nmodel = "bad \\q escape"',
    "[local]\nmodel = 'a' 'b'",
    "[router]\ncloud_allowed = TRUE",
    "[router]\nmode = auto",
    "key without value",
    "= 1",
    "[]",
    '[local]\nurl = """never closed',
    "[local]\nwhen = 2026-13-45",
    "\x00",
    '[local]\nmodel = "\x01"',
]


@pytest.mark.parametrize("text", BROKEN_TOML)
def test_broken_toml_is_a_config_error_that_names_the_file(tmp_path, text):
    path = write_config(tmp_path, text)
    with pytest.raises(ConfigError) as info:
        load_settings(path, env={})
    assert str(path) in str(info.value)


def test_a_byte_order_mark_does_not_pass_as_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf[local]\nmodel = 'x'\n")
    with pytest.raises(ConfigError):
        load_settings(path, env={})


@pytest.mark.parametrize(
    "text",
    [
        "local = 5",
        'local = "text"',
        "local = [1, 2]",
        "[[local]]\nurl = 'x'",
        "cloud = true",
        "cloud = []",
        "router = 1.5",
        "[[router]]\nmode = 'auto'",
        'tools = "yes"',
        "tools = [true]",
    ],
)
def test_a_section_that_is_not_a_table_is_a_config_error(tmp_path, text):
    with pytest.raises(ConfigError, match="must be a table"):
        settings_from(tmp_path, text)


@pytest.mark.parametrize(
    "text",
    [
        '[router]\nmode = "AUTO"',
        '[router]\nmode = " auto"',
        '[router]\nmode = "auto\\n"',
        '[router]\nmode = "аuto"',  # the first letter is Cyrillic
        '[router]\nmode = "cloud\\u200b"',
        '[router]\nmode = "everywhere"',
        "[router]\nmode = 1",
        "[router]\nmode = ['auto']",
        "[router]\nmode = { a = 1 }",
    ],
)
def test_the_router_mode_must_be_exactly_one_of_the_three(tmp_path, text):
    with pytest.raises(ConfigError, match="router mode"):
        settings_from(tmp_path, text)


@pytest.mark.parametrize("name", ["cloud_allowed", "mask_cloud", "mask_strict"])
@pytest.mark.parametrize("value", ['"maybe"', "2", "-1", "1.0", "[]", '"tru"', '"yes please"'])
def test_a_flag_that_is_not_a_flag_is_a_config_error(tmp_path, name, value):
    cloud = '[cloud]\nurl = "https://api.example.eu/v1"\nmodel = "big"\n'
    with pytest.raises(ConfigError, match="true or false"):
        settings_from(tmp_path, f"{cloud}[router]\n{name} = {value}\n")


@pytest.mark.parametrize("value", ['"maybe"', "2", "[]", '"nope!"'])
def test_tools_and_start_server_flags_are_checked_too(tmp_path, value):
    with pytest.raises(ConfigError, match="true or false"):
        settings_from(tmp_path, f"[tools]\nenabled = {value}\n")
    with pytest.raises(ConfigError, match="true or false"):
        settings_from(tmp_path, f"[local]\nstart_server = {value}\n")


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ('[local]\ntimeout = "soon"', "timeout"),
        ("[local]\ntimeout = []", "timeout"),
        ("[local]\ntimeout = { a = 1 }", "timeout"),
        ('[cloud]\nurl = "https://x.example/v1"\nmodel = "m"\ntimeout = "never"', "timeout"),
        ('[router]\nlong_message_chars = "many"', "long_message_chars"),
        ("[router]\nlong_message_chars = []", "long_message_chars"),
        ("[local]\nslot = 1.5", "slot"),
        ('[local]\nslot = "first"', "slot"),
        ("[local]\nslot = true", "slot"),
        ("[local]\nslot = []", "slot"),
        ("[local]\nrepeat_penalty = nan", "repeat_penalty"),
        ("[local]\nrepeat_penalty = inf", "repeat_penalty"),
        ("[local]\nrepeat_penalty = 2.5", "repeat_penalty"),
        ('[local]\nrepeat_penalty = "high"', "repeat_penalty"),
    ],
)
def test_numbers_of_the_wrong_kind_are_config_errors(tmp_path, text, word):
    with pytest.raises(ConfigError, match=word):
        settings_from(tmp_path, text)


@pytest.mark.parametrize(
    ("env_name", "value", "word"),
    [
        ("GLOXI_LOCAL_TIMEOUT", "soon", "timeout"),
        ("GLOXI_LOCAL_TIMEOUT", "1,5", "timeout"),
        ("GLOXI_CLOUD_TIMEOUT", "never", "timeout"),
        ("GLOXI_LOCAL_SLOT", "1.5", "slot"),
        ("GLOXI_LOCAL_SLOT", "zero", "slot"),
        ("GLOXI_LOCAL_REPEAT_PENALTY", "nan", "repeat_penalty"),
        ("GLOXI_LOCAL_REPEAT_PENALTY", "9", "repeat_penalty"),
        ("GLOXI_ROUTER_MODE", "sometimes", "router mode"),
        ("GLOXI_CLOUD_ALLOWED", "perhaps", "true or false"),
        ("GLOXI_MASK_CLOUD", "perhaps", "true or false"),
        ("GLOXI_MASK_STRICT", "perhaps", "true or false"),
        ("GLOXI_TOOLS", "perhaps", "true or false"),
        ("GLOXI_START_SERVER", "perhaps", "true or false"),
    ],
)
def test_environment_values_of_the_wrong_kind_are_config_errors(tmp_path, env_name, value, word):
    env = {env_name: value}
    if env_name == "GLOXI_CLOUD_TIMEOUT":
        env["GLOXI_CLOUD_URL"] = "https://api.example.eu/v1"
        env["GLOXI_CLOUD_MODEL"] = "big"
    with pytest.raises(ConfigError, match=word):
        load_settings(tmp_path / "none.toml", env=env)


def test_a_cloud_section_without_a_model_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="model"):
        settings_from(tmp_path, '[cloud]\nurl = "https://api.example.eu/v1"\n')


def test_the_cloud_cannot_be_allowed_without_an_address(tmp_path):
    with pytest.raises(ConfigError, match="cloud_allowed"):
        settings_from(tmp_path, "[router]\ncloud_allowed = true\n")
    with pytest.raises(ConfigError, match="cloud_allowed"):
        load_settings(tmp_path / "none.toml", env={"GLOXI_CLOUD_ALLOWED": "1"})


def test_a_cloud_model_without_an_address_leaves_the_cloud_off(tmp_path):
    s = settings_from(tmp_path, '[cloud]\nmodel = "big"\napi_key = "' + FAKE_CLOUD_KEY + '"\n')
    assert s.cloud is None
    assert s.router.cloud_allowed is False


def test_defaults_are_the_private_ones(tmp_path):
    s = load_settings(tmp_path / "none.toml", env={})
    assert s.local.url == DEFAULT_LOCAL_URL
    assert s.cloud is None
    assert s.router.cloud_allowed is False
    assert s.router.mask_cloud is True
    assert s.router.mask_strict is False
    assert s.router.mode == "auto"
    assert s.local.api_key is None


def test_an_empty_file_gives_the_defaults(tmp_path):
    assert settings_from(tmp_path, "") == load_settings(tmp_path / "none.toml", env={})
    assert settings_from(tmp_path, "# only a comment\n\n\n") == load_settings(
        tmp_path / "none.toml", env={}
    )


def test_unknown_keys_and_tables_are_ignored(tmp_path):
    text = "unknown = 1\n[local]\nflavour = 'x'\n[mystery]\na = [1, 2, 3]\n[router.deeper]\nx = 1\n"
    assert settings_from(tmp_path, text) == load_settings(tmp_path / "none.toml", env={})


def test_a_missing_config_file_or_a_folder_in_its_place_gives_the_defaults(tmp_path):
    defaults = load_settings(tmp_path / "none.toml", env={})
    folder = tmp_path / "folder.toml"
    folder.mkdir()
    assert load_settings(folder, env={}) == defaults
    dangling = tmp_path / "dangling.toml"
    dangling.symlink_to(tmp_path / "nowhere.toml")
    assert load_settings(dangling, env={}) == defaults


def test_a_symlink_loop_in_place_of_the_config_file_does_not_hang_or_crash(tmp_path):
    loop = tmp_path / "loop.toml"
    loop.symlink_to(loop)
    start = time.monotonic()
    with contextlib.suppress(ConfigError):
        load_settings(loop, env={})
    assert time.monotonic() - start < 5


def test_an_unreadable_config_file_is_a_config_error(tmp_path, monkeypatch):
    path = write_config(tmp_path, "[local]\nmodel = 'x'\n")

    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(path, env={})


def test_a_large_config_file_is_read_in_reasonable_time(tmp_path):
    filler = "# " + "x" * 98 + "\n"
    text = filler * 50_000 + "[local]\nmodel = 'after the comments'\n"  # about 5 MB
    start = time.monotonic()
    s = settings_from(tmp_path, text)
    assert time.monotonic() - start < 10
    assert s.local.model == "after the comments"


def test_many_unknown_keys_are_read_in_reasonable_time(tmp_path):
    text = "[mystery]\n" + "".join(f"k{i} = {i}\n" for i in range(100_000))
    start = time.monotonic()
    settings_from(tmp_path, text)
    assert time.monotonic() - start < 10


def test_a_huge_system_prompt_and_unusual_text_survive_as_they_are(tmp_path):
    prompt = "Ти — Gloxi. " * 100_000 + "\u202e rtl \U0001f600 \u200b"
    path = tmp_path / "config.toml"
    path.write_text("system_prompt = " + json.dumps(prompt, ensure_ascii=False) + "\n", "utf-8")
    assert load_settings(path, env={}).system_prompt == prompt


def test_model_names_keep_unicode_exactly(tmp_path):
    s = settings_from(tmp_path, '[local]\nmodel = "модель-\U0001f600-\u202e"\n')
    assert s.local.model == "модель-\U0001f600-\u202e"


def test_an_empty_system_prompt_falls_back_to_the_default(tmp_path):
    default = load_settings(tmp_path / "none.toml", env={}).system_prompt
    assert settings_from(tmp_path, 'system_prompt = ""\n').system_prompt == default


def test_many_trailing_slashes_are_dropped_from_addresses(tmp_path):
    text = (
        '[local]\nurl = "http://127.0.0.1:8080/v1////"\n'
        '[cloud]\nurl = "https://api.example.eu/v1//"\nmodel = "big"\n'
    )
    s = settings_from(tmp_path, text)
    assert s.local.url == "http://127.0.0.1:8080/v1"
    assert s.cloud is not None
    assert s.cloud.url == "https://api.example.eu/v1"


def test_negative_slot_and_a_penalty_of_one_or_less_mean_the_server_default(tmp_path):
    s = settings_from(tmp_path, "[local]\nslot = -7\nrepeat_penalty = 0.5\n")
    assert s.local.slot is None
    assert s.local.repeat_penalty is None
    s = settings_from(tmp_path, "[local]\nslot = 3\nrepeat_penalty = 2\n")
    assert (s.local.slot, s.local.repeat_penalty) == (3, 2.0)


# -- precedence ---------------------------------------------------------------------------


def test_the_environment_beats_the_file_and_the_file_beats_the_defaults(tmp_path):
    text = (
        '[local]\nurl = "http://127.0.0.1:1111/v1"\nmodel = "from-file"\ntimeout = 11\n'
        '[router]\nmode = "local"\nmask_cloud = false\n'
    )
    path = write_config(tmp_path, text)
    s = load_settings(path, env={})
    assert (s.local.url, s.local.model, s.local.timeout) == (
        "http://127.0.0.1:1111/v1",
        "from-file",
        11.0,
    )
    assert (s.router.mode, s.router.mask_cloud) == ("local", False)
    env = {
        "GLOXI_LOCAL_URL": "http://127.0.0.1:2222/v1/",
        "GLOXI_LOCAL_MODEL": "from-env",
        "GLOXI_LOCAL_TIMEOUT": "22",
        "GLOXI_ROUTER_MODE": "auto",
        "GLOXI_MASK_CLOUD": "true",
    }
    s = load_settings(path, env=env)
    assert (s.local.url, s.local.model, s.local.timeout) == (
        "http://127.0.0.1:2222/v1",
        "from-env",
        22.0,
    )
    assert (s.router.mode, s.router.mask_cloud) == ("auto", True)


def test_an_empty_environment_value_does_not_override_the_file(tmp_path):
    path = write_config(tmp_path, '[local]\nmodel = "from-file"\n')
    env = {"GLOXI_LOCAL_MODEL": "", "GLOXI_LOCAL_URL": "", "GLOXI_ROUTER_MODE": ""}
    s = load_settings(path, env=env)
    assert s.local.model == "from-file"
    assert s.local.url == DEFAULT_LOCAL_URL
    assert s.router.mode == "auto"


def test_an_empty_string_in_the_file_means_not_set(tmp_path):
    s = settings_from(tmp_path, '[local]\nurl = ""\nmodel = ""\n[router]\nmode = ""\n')
    assert s.local.url == DEFAULT_LOCAL_URL
    assert s.local.model == "local"
    assert s.router.mode == "auto"


@pytest.mark.parametrize("text", ["1", "true", "TRUE", " Yes ", "on"])
def test_every_spelling_of_yes_turns_a_flag_on(tmp_path, text):
    s = load_settings(tmp_path / "none.toml", env={"GLOXI_MASK_STRICT": text, "GLOXI_TOOLS": text})
    assert s.router.mask_strict is True
    assert s.tools_enabled is True


@pytest.mark.parametrize("text", ["0", "false", "FALSE", " No ", "off"])
def test_every_spelling_of_no_turns_a_flag_off(tmp_path, text):
    s = load_settings(tmp_path / "none.toml", env={"GLOXI_MASK_CLOUD": text, "GLOXI_TOOLS": text})
    assert s.router.mask_cloud is False
    assert s.tools_enabled is False


def test_the_cloud_can_be_set_up_from_the_environment_alone(tmp_path):
    env = {
        "GLOXI_CLOUD_URL": "https://api.example.eu/v1/",
        "GLOXI_CLOUD_MODEL": "big",
        "GLOXI_CLOUD_API_KEY": FAKE_CLOUD_KEY,
        "GLOXI_CLOUD_ALLOWED": "yes",
    }
    s = load_settings(tmp_path / "none.toml", env=env)
    assert s.cloud is not None
    assert (s.cloud.url, s.cloud.model, s.cloud.api_key) == (
        "https://api.example.eu/v1",
        "big",
        FAKE_CLOUD_KEY,
    )
    assert s.router.cloud_allowed is True


def test_the_cloud_key_from_the_environment_beats_the_one_in_the_file(tmp_path):
    text = CLOUD_TOML
    path = write_config(tmp_path, text)
    assert load_settings(path, env={}).cloud.api_key == FAKE_CLOUD_KEY
    assert load_settings(path, env={"GLOXI_CLOUD_API_KEY": "other-fake-key"}).cloud.api_key == (
        "other-fake-key"
    )


def test_keys_do_not_show_up_in_reprs_or_error_texts(tmp_path):
    text = CLOUD_TOML
    s = settings_from(tmp_path, text)
    assert FAKE_CLOUD_KEY not in repr(s)
    assert FAKE_CLOUD_KEY not in repr(s.cloud)
    assert FAKE_CLOUD_KEY not in str(s.cloud)
    broken = text + "[router]\nmode = 'bad'\n"
    with pytest.raises(ConfigError) as info:
        settings_from(tmp_path, broken)
    assert FAKE_CLOUD_KEY not in str(info.value)


def test_config_path_follows_xdg_and_falls_back_to_home(tmp_path):
    assert config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "gloxi" / "config.toml"
    home = Path.home()
    assert config_path({}) == home / ".config" / "gloxi" / "config.toml"
    assert config_path({"XDG_CONFIG_HOME": ""}) == home / ".config" / "gloxi" / "config.toml"


def test_load_settings_finds_the_file_through_the_given_environment(tmp_path):
    folder = tmp_path / "gloxi"
    folder.mkdir()
    (folder / "config.toml").write_text("[local]\nmodel = 'via-xdg'\n", encoding="utf-8")
    assert load_settings(env={"XDG_CONFIG_HOME": str(tmp_path)}).local.model == "via-xdg"


# -- addresses ----------------------------------------------------------------------------

LOOPBACK_URLS = [
    "http://127.0.0.1:8080/v1",
    "http://127.0.0.1/v1",
    "http://localhost:8080/v1",
    "http://LOCALHOST:8080/v1",
    "http://LocalHost/v1",
    "http://[::1]:8080/v1",
    "https://127.0.0.1:8443/v1",
    "http://user:secret@127.0.0.1:8080/v1",
    "http://127.0.0.1:0/v1",
    "http://127.0.0.1:65536/v1",
]

# Hosts that are not this computer, however much they look like it.
FOREIGN_URLS = [
    "http://203.0.113.7:8080/v1",
    "http://model.example.org/v1",
    "https://api.example.eu/v1",
    "http://127.0.0.1.example.org/v1",
    "http://localhost.example.org/v1",
    "http://127.0.0.1@example.org/v1",
    "http://localhost@example.org:8080/v1",
    "http://example.org#@127.0.0.1/v1",
    "http://example.org?@localhost/v1",
    "http://127.0.0.1%2eexample.org/v1",
    "http://%6c%6f%63%61%6c%68%6f%73%74/v1",
    "http://localhost./v1",
    "http://0.0.0.0:8080/v1",
    "http://127.0.0.2:8080/v1",
    "http://[::ffff:127.0.0.1]:8080/v1",
    "http://10.0.0.5/v1",
    "http://192.0.2.1/v1",
    "http://[2001:db8::1]/v1",
    "//example.org/v1",
]


# The same, in forms the HTTP client library cannot parse at all.
FULL_WIDTH_HOST = "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/v1"


@pytest.mark.parametrize("url", LOOPBACK_URLS)
def test_loopback_addresses_are_this_computer_whatever_the_port(url):
    assert localkey.is_local_url(url) is True


@pytest.mark.parametrize("url", [*FOREIGN_URLS, FULL_WIDTH_HOST])
def test_look_alike_hosts_are_not_this_computer(url):
    assert localkey.is_local_url(url) is False


def test_the_key_is_not_given_to_a_full_width_look_alike_of_localhost():
    assert localkey.headers_for(FULL_WIDTH_HOST) == {}


@pytest.mark.parametrize("url", FOREIGN_URLS)
def test_gloxis_own_key_never_goes_to_another_host(url):
    assert localkey.headers_for(url) == {}
    endpoint = ModelEndpoint(url=url, model="m")
    with ChatClient(endpoint) as client:
        assert "authorization" not in {k.lower() for k in client._http.headers}
        assert localkey.KEY not in str(dict(client._http.headers))


@pytest.mark.parametrize("url", ["http://203.0.113.7/v1", "http://model.example.org/v1"])
def test_plain_http_away_from_this_computer_gets_no_key_of_ours_for_a_local_endpoint(tmp_path, url):
    s = settings_from(tmp_path, f'[local]\nurl = "{url}"\n')
    assert s.local.url == url
    assert s.local.api_key is None
    assert localkey.headers_for(s.local.url, s.local.api_key) == {}


def test_an_explicit_key_is_sent_as_given_and_a_missing_one_means_ours_only_for_loopback():
    assert localkey.headers_for("https://api.example.eu/v1", FAKE_CLOUD_KEY) == {
        "Authorization": f"Bearer {FAKE_CLOUD_KEY}"
    }
    assert localkey.headers_for("http://127.0.0.1:8080/v1", FAKE_LOCAL_KEY) == {
        "Authorization": f"Bearer {FAKE_LOCAL_KEY}"
    }
    assert localkey.headers_for("http://127.0.0.1:8080/v1") == {
        "Authorization": f"Bearer {localkey.KEY}"
    }
    assert localkey.headers_for("http://127.0.0.1:8080/v1", "") == {
        "Authorization": f"Bearer {localkey.KEY}"
    }
    assert localkey.headers_for("https://api.example.eu/v1", "") == {}
    assert localkey.headers_for("https://api.example.eu/v1", None) == {}


def test_the_cloud_client_never_carries_gloxis_local_key(tmp_path):
    text = CLOUD_TOML
    s = settings_from(tmp_path, text)
    with ChatClient(s.cloud) as client:
        assert client._http.headers["Authorization"] == f"Bearer {FAKE_CLOUD_KEY}"
        assert localkey.KEY not in client._http.headers["Authorization"]


def test_a_redirect_from_the_model_is_not_followed_and_the_key_stays_home():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://example.org/steal"})

    endpoint = ModelEndpoint(url="http://127.0.0.1:8080/v1", model="m")
    transport = httpx.MockTransport(handler)
    with ChatClient(endpoint, transport=transport) as client, pytest.raises(ModelError):
        client.complete([{"role": "user", "content": "hi"}])
    assert [r.url.host for r in seen] == ["127.0.0.1"]


@pytest.mark.parametrize("url", ["file:///etc/hostname", "file:/tmp/x", "ftp://127.0.0.1/v1"])
def test_an_address_of_another_kind_never_reads_a_file_and_fails_as_unavailable(url):
    endpoint = ModelEndpoint(url=url, model="m", timeout=2.0)
    with ChatClient(endpoint) as client, pytest.raises(ModelError) as info:
        client.complete([{"role": "user", "content": "hi"}])
    assert localkey.KEY not in str(info.value)
    assert "root:" not in str(info.value)


def test_an_error_about_an_unreachable_model_does_not_print_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    endpoint = ModelEndpoint(url="http://127.0.0.1:8080/v1", model="m")
    transport = httpx.MockTransport(handler)
    with ChatClient(endpoint, transport=transport) as client, pytest.raises(ModelError) as info:
        client.complete([{"role": "user", "content": "hi"}])
    assert localkey.KEY not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://127.0.0.1:99999999999/v1",
        "http://127.0.0.1:-1/v1",
    ],
)
def test_ports_out_of_range_never_make_the_start_check_say_yes(url):
    assert listening(url, timeout=0.5) is False


@pytest.mark.parametrize("port", [0, 65536, 70000, 4294967296])
def test_port_numbers_are_kept_in_the_address_and_do_not_change_the_host_decision(tmp_path, port):
    s = settings_from(tmp_path, f'[local]\nurl = "http://127.0.0.1:{port}/v1"\n')
    assert s.local.url == f"http://127.0.0.1:{port}/v1"
    assert localkey.is_local_url(s.local.url) is True


def test_a_file_address_in_the_settings_does_not_turn_on_the_cloud_or_change_masking(tmp_path):
    s = settings_from(tmp_path, '[local]\nurl = "file:///etc/hostname"\n')
    assert s.cloud is None
    assert s.router.cloud_allowed is False
    assert s.router.mask_cloud is True


# -- the key file of the shared model ------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"two words",
        b"tab\tinside",
        b"line one\nline two\n",
        b"abc\r\nX-Injected: 1\r\n",
        b"abc\rdef",
        b"abc\x0bdef",
        b"abc\x0cdef",
        b"abc\x1cdef",
        b"abc\x1ddef",
        b"abc\x1edef",
        b"abc\x1fdef",
        b"Bearer abc",
        b"",
        b"\n",
        b" \t \r\n\n",
        b"a" * 513,
        b"a" * (64 * 1024),
    ],
)
def test_a_key_file_that_does_not_hold_one_key_is_a_config_error(tmp_path, content):
    with pytest.raises(ConfigError, match="does not hold a key"):
        key_settings(tmp_path, content)


@pytest.mark.parametrize(
    "content",
    [
        "abc\u200bdef".encode(),  # zero-width space
        "abc\u200ddef".encode(),  # zero-width joiner
        "abc\ufeffdef".encode(),  # zero-width no-break space
        b"\xef\xbb\xbfabcdef",  # byte-order mark
        "abc\u00a0def".encode(),  # no-break space
        "abc\u2028def".encode(),  # line separator
        "abc\u202edef".encode(),  # right-to-left override
        "abc\u00addef".encode(),  # soft hyphen
        "ключ-доступу".encode(),
        "ключ".encode("utf-16"),
        "ключ".encode("cp1251"),
        b"abc\xe9def",  # Latin-1
        "\U0001f511".encode(),
        b"\xff\xfe\x00\x00",
    ],
)
def test_a_key_with_invisible_or_non_ascii_characters_is_a_config_error(tmp_path, content):
    with pytest.raises(ConfigError, match="cannot read the local model key"):
        key_settings(tmp_path, content)


def test_the_error_for_a_rejected_key_does_not_repeat_the_key(tmp_path):
    for content in (b"visible-secret-part and more", b"v" * 600):
        with pytest.raises(ConfigError) as info:
            key_settings(tmp_path, content)
        assert content.decode()[:12] not in str(info.value)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"abc123\n", "abc123"),
        (b"abc123\r\n", "abc123"),
        (b"  abc123  \n\n", "abc123"),
        (b"a" * 512, "a" * 512),
        (b"A-Za-z0-9._~+/=-", "A-Za-z0-9._~+/=-"),
    ],
)
def test_a_plain_key_is_read_and_trimmed(tmp_path, content, expected):
    assert key_settings(tmp_path, content).local.api_key == expected


def test_a_key_file_that_is_not_there_yet_means_no_key_not_an_error(tmp_path):
    s = load_settings(
        tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(tmp_path / "later.key")}
    )
    assert s.local.api_key is None


def test_an_empty_key_file_setting_means_no_key_file(tmp_path):
    s = load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": ""})
    assert s.local.api_key is None


def test_a_dangling_link_counts_as_a_key_file_that_is_not_there_yet(tmp_path):
    link = tmp_path / "link.key"
    link.symlink_to(tmp_path / "nowhere.key")
    s = load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(link)})
    assert s.local.api_key is None


def test_a_link_to_a_real_key_file_is_followed(tmp_path):
    real = tmp_path / "real.key"
    real.write_text("linked-fake-key\n", encoding="ascii")
    link = tmp_path / "link.key"
    link.symlink_to(real)
    s = load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(link)})
    assert s.local.api_key == "linked-fake-key"


def test_a_folder_in_place_of_the_key_file_is_a_config_error(tmp_path):
    folder = tmp_path / "folder.key"
    folder.mkdir()
    with pytest.raises(ConfigError, match="cannot read the local model key"):
        load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(folder)})


def test_a_key_file_that_cannot_be_read_is_a_config_error(tmp_path, monkeypatch):
    path = tmp_path / "model.key"
    path.write_text("fake-key\n", encoding="ascii")

    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(ConfigError, match="cannot read the local model key"):
        load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(path)})


def test_a_key_in_the_config_file_is_not_a_local_key(tmp_path):
    s = settings_from(tmp_path, f'[local]\napi_key = "{FAKE_LOCAL_KEY}"\n')
    assert s.local.api_key is None


def test_the_local_key_survives_into_the_authorization_header_unchanged(tmp_path):
    s = key_settings(tmp_path, (FAKE_LOCAL_KEY + "\n").encode())
    assert localkey.headers_for(s.local.url, s.local.api_key) == {
        "Authorization": f"Bearer {FAKE_LOCAL_KEY}"
    }


# -- the key file Gloxi writes for its own server ------------------------------------------


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077])
def test_the_key_file_is_private_whatever_the_umask(tmp_path, monkeypatch, umask):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    old = os.umask(umask)
    try:
        with localkey.key_file() as path:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert path.stat().st_uid == os.getuid()
    finally:
        os.umask(old)


def test_the_key_file_is_private_even_in_a_folder_everyone_can_write(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(shared))
    with localkey.key_file() as path:
        assert path.parent == shared
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_key_file_holds_exactly_the_key_in_ascii(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with localkey.key_file() as path:
        raw = path.read_bytes()
    assert raw == localkey.KEY.encode("ascii") + b"\n"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", localkey.KEY)


def test_the_key_file_is_removed_after_a_normal_exit_and_after_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with localkey.key_file() as path:
        assert path.exists()
    assert not path.exists()
    with pytest.raises(RuntimeError), localkey.key_file() as path:
        raise RuntimeError("the server did not start")
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_key_file_is_removed_after_an_interrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(KeyboardInterrupt), localkey.key_file() as path:
        raise KeyboardInterrupt
    assert not path.exists()


def test_leaving_the_block_is_fine_when_someone_already_removed_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with localkey.key_file() as path:
        path.unlink()
    assert not path.exists()


def test_every_key_file_has_a_new_unpredictable_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    names = []
    for _ in range(20):
        with localkey.key_file() as path:
            names.append(path.name)
    assert len(set(names)) == 20
    assert all(n.startswith("gloxi-key-") for n in names)


def test_a_name_an_attacker_put_in_the_folder_is_never_written_through(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    bait = tmp_path / "bait.txt"
    bait.write_text("untouched", encoding="ascii")
    for i in range(50):
        (tmp_path / f"gloxi-key-{i}").symlink_to(bait)
    with localkey.key_file() as path:
        assert not path.is_symlink()
        assert path.read_text(encoding="ascii").strip() == localkey.KEY
    assert bait.read_text(encoding="ascii") == "untouched"


def test_the_key_file_goes_to_the_temporary_folder_when_the_runtime_folder_is_not_usable(
    tmp_path, monkeypatch
):
    elsewhere = tmp_path / "temp"
    elsewhere.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(elsewhere))
    not_a_folder = tmp_path / "plain-file"
    not_a_folder.write_text("x", encoding="ascii")
    for value in (None, "", str(tmp_path / "missing"), str(not_a_folder)):
        if value is None:
            monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        else:
            monkeypatch.setenv("XDG_RUNTIME_DIR", value)
        with localkey.key_file() as path:
            assert path.parent == elsewhere
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not path.exists()


def test_a_runtime_folder_with_unusual_characters_is_fine(tmp_path, monkeypatch):
    folder = tmp_path / "чат run \U0001f600"
    folder.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(folder))
    with localkey.key_file() as path:
        assert path.parent == folder
        assert path.read_bytes().strip().decode("ascii") == localkey.KEY


def test_when_the_key_file_cannot_be_made_the_error_is_raised_and_nothing_is_left(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("tempfile.mkstemp", refuse)
    with pytest.raises(OSError), localkey.key_file():
        pytest.fail("the block must not run without a key file")
    assert list(tmp_path.iterdir()) == []


def test_the_key_is_long_random_and_different_in_every_run_of_the_module():
    assert len(localkey.KEY) >= 40
    spec = importlib.util.spec_from_file_location("localkey_second_copy", localkey.__file__)
    assert spec is not None and spec.loader is not None
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)
    assert other.KEY != localkey.KEY
    assert len(other.KEY) >= 40


# -- the internet gate: the tool (network.py) -----------------------------------------------


class Systemctl:
    """A stand-in for systemctl: records what would have been run."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.mark.parametrize("uid", [-1, 0, 1, 999, 60000, 65534, 4294967295, 10**30])
def test_only_a_persons_uid_is_accepted(uid):
    with pytest.raises(ValueError, match="not a person's uid"):
        network.Network(uid)


@pytest.mark.parametrize("uid", [True, False, float("nan"), float("inf"), -0.0])
def test_odd_numbers_are_not_a_persons_uid(uid):
    with pytest.raises(ValueError):
        network.Network(uid)


@pytest.mark.parametrize("uid", ["1000", None, b"1000", [1000], {"uid": 1000}])
def test_a_uid_that_is_not_a_number_is_refused(uid):
    with pytest.raises((TypeError, ValueError)):
        network.Network(uid)


@pytest.mark.parametrize("uid", [1000, 1001, 59999])
def test_the_unit_name_is_built_from_the_number_only(uid):
    assert network.Network(uid).unit == f"gloxi@{uid}.service"


def test_the_commands_are_exactly_these_and_touch_one_unit():
    run = Systemctl("::/0 0.0.0.0/0\n")
    net = network.Network(1234, run)
    net.status()
    net.allow_cloud()
    net.block_cloud()
    unit = "gloxi@1234.service"
    show = ["systemctl", "show", "--property=IPAddressAllow", "--value", unit]
    assert run.calls == [
        show,
        ["systemctl", "set-property", unit, "IPAddressAllow=any"],
        show,
        ["systemctl", "set-property", unit, "IPAddressAllow=", "IPAddressAllow=localhost"],
        show,
    ]
    assert all(call[0] == "systemctl" for call in run.calls)


@pytest.mark.parametrize(
    ("output", "cloud"),
    [
        ("::/0 0.0.0.0/0\n", True),
        ("0.0.0.0/0", True),
        ("::/0\n", False),
        ("", False),
        ("localhost\n", False),
        ("10.0.0.0/8 127.0.0.0/8", False),
        ("0.0.0.0/0x", False),
        ("x0.0.0.0/0", False),
        ("0.0.0.0/01", False),
        ("any", False),
        ("0.0.0.0/0,10.0.0.0/8", False),
    ],
)
def test_the_state_is_read_from_whole_words_only(output, cloud):
    assert network.Network(1000, Systemctl(output)).status() == {"cloud": cloud}


def test_a_failing_systemctl_is_a_network_error_with_its_own_message():
    net = network.Network(1000, Systemctl(returncode=1, stderr="  Unit not found.\n"))
    with pytest.raises(network.NetworkError, match="Unit not found"):
        net.allow_cloud()
    net = network.Network(1000, Systemctl(returncode=5, stderr=""))
    with pytest.raises(network.NetworkError, match="failed"):
        net.status()


@pytest.mark.parametrize(
    "failure",
    [OSError("no such file"), subprocess.TimeoutExpired("systemctl", 30), PermissionError("no")],
)
def test_a_systemctl_that_cannot_run_is_a_network_error(failure):
    def run(cmd):
        raise failure

    with pytest.raises(network.NetworkError):
        network.Network(1000, run).block_cloud()


def test_a_failed_change_does_not_claim_success_by_reading_the_state():
    run = Systemctl("::/0 0.0.0.0/0", returncode=1, stderr="denied")
    with pytest.raises(network.NetworkError):
        network.Network(1000, run).allow_cloud()
    assert len(run.calls) == 1


def make_server(stdout: str = "") -> tuple[network.Server, Systemctl]:
    run = Systemctl(stdout)
    return network.Server(network.Network(1000, run)), run


@pytest.mark.parametrize("message", [[], 5, 1.5, "text", None, True, [{"method": "ping", "id": 1}]])
def test_a_message_that_is_not_an_object_is_an_invalid_request(message):
    server, run = make_server()
    answer = server.handle(message)
    assert answer is not None
    assert answer["error"]["code"] == -32600
    assert answer["id"] is None
    assert run.calls == []


def test_a_message_without_an_id_is_a_notification_and_gets_no_answer():
    server, run = make_server()
    for method in ("tools/call", "ping", "initialize", "notifications/initialized", "x"):
        assert server.handle({"jsonrpc": "2.0", "method": method}) is None
    assert server.handle({"method": "tools/call", "params": {"name": "allow_cloud"}}) is None
    assert run.calls == []


@pytest.mark.parametrize("method", [None, 5, ["tools/list"], {"a": 1}, "", "TOOLS/LIST", "tools/"])
def test_an_unknown_or_odd_method_is_an_error_not_a_crash(method):
    server, _ = make_server()
    answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": method})
    assert answer is not None
    assert answer["error"]["code"] == -32601
    assert answer["id"] == 1


@pytest.mark.parametrize("params", ["x", 5, True, [1, 2]])
def test_params_that_are_not_an_object_are_refused(params):
    server, run = make_server()
    answer = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params})
    assert answer is not None
    assert answer["error"]["code"] == -32602
    assert run.calls == []


@pytest.mark.parametrize("name", [None, 5, ["allow_cloud"], {"a": 1}, "", "ALLOW_CLOUD"])
def test_a_tool_name_of_the_wrong_kind_is_a_tool_error_and_runs_nothing(name):
    server, run = make_server()
    answer = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name}}
    )
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert run.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "allow_cloud\n",
        " allow_cloud",
        "allow_cloud ",
        "allow_cloud\x00",
        "allow\u200b_cloud",
        "аllow_cloud",  # the first letter is Cyrillic
        "__class__",
        "__init__",
        "run",
        "handle",
        "../allow_cloud",
        "allow_cloud;reboot",
        "allow_cloud && reboot",
        "$(reboot)",
    ],
)
def test_a_look_alike_tool_name_runs_nothing(name):
    server, run = make_server()
    answer = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name}}
    )
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert run.calls == []


@pytest.mark.parametrize(
    "arguments",
    [{"a": 1}, {"uid": 1001}, {"unit": "gloxi@0.service"}, [], [1], "x", 0, 1, False, True, 1.5],
)
@pytest.mark.parametrize("name", ["status", "allow_cloud", "block_cloud"])
def test_the_tools_take_no_arguments_so_nothing_can_steer_them(name, arguments):
    server, run = make_server()
    answer = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert "no arguments" in answer["result"]["content"][0]["text"]
    assert run.calls == []


@pytest.mark.parametrize("arguments", [None, {}])
def test_no_arguments_or_an_empty_object_is_the_normal_call(arguments):
    server, run = make_server("::/0 0.0.0.0/0")
    message = {"jsonrpc": "2.0", "id": 6, "method": "tools/call"}
    message["params"] = {"name": "allow_cloud", "arguments": arguments}
    answer = server.handle(message)
    assert answer is not None
    assert answer["result"]["isError"] is False
    assert answer["result"]["structuredContent"] == {"cloud": True}
    assert json.loads(answer["result"]["content"][0]["text"]) == {"cloud": True}
    assert ["systemctl", "set-property", "gloxi@1000.service", "IPAddressAllow=any"] in run.calls


def test_an_error_of_the_tool_comes_back_as_a_tool_error_with_no_trace():
    run = Systemctl(returncode=1, stderr="Access denied")
    server = network.Server(network.Network(1000, run))
    answer = server.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "block_cloud"}}
    )
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert answer["result"]["content"][0]["text"] == "Access denied"
    assert "Traceback" not in json.dumps(answer)


@pytest.mark.parametrize(
    "ident", [1, 0, -1, "a", "", "x" * 10_000, "\u202e\x00", 1.5, [1], {"a": 1}]
)
def test_the_id_comes_back_as_it_was_sent(ident):
    server, _ = make_server()
    answer = server.handle({"jsonrpc": "2.0", "id": ident, "method": "ping"})
    assert answer == {"jsonrpc": "2.0", "id": ident, "result": {}}


@pytest.mark.parametrize("asked", [None, 5, "1999-01-01", "", ["2025-06-18"], {"a": 1}, "\u202e"])
def test_an_unknown_protocol_version_gets_the_newest_known_one(asked):
    server, _ = make_server()
    answer = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": asked}}
    )
    assert answer is not None
    assert answer["result"]["protocolVersion"] == network.PROTOCOL_VERSIONS[0]


def test_a_known_protocol_version_is_echoed():
    server, _ = make_server()
    for version in network.PROTOCOL_VERSIONS:
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": version},
            }
        )
        assert answer is not None
        assert answer["result"]["protocolVersion"] == version


def test_the_tool_list_says_which_tools_change_something_and_that_they_take_nothing():
    server, _ = make_server()
    answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert answer is not None
    tools = {t["name"]: t for t in answer["result"]["tools"]}
    assert set(tools) == {"status", "allow_cloud", "block_cloud"}
    assert tools["status"]["annotations"]["readOnlyHint"] is True
    assert tools["allow_cloud"]["annotations"]["readOnlyHint"] is False
    assert tools["block_cloud"]["annotations"]["readOnlyHint"] is False
    for tool in tools.values():
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"] == {}


def serve(server: network.Server, text: str) -> list[dict]:
    out = io.StringIO()
    server.serve(io.StringIO(text), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_serve_survives_bad_lines_blank_lines_and_odd_json():
    server, run = make_server()
    lines = [
        "{not json",
        "",
        "   ",
        "\x00",
        "'single quotes'",
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"',
        '{"jsonrpc": "2.0", "id": 2, "method": "ping"}',
        "[]",
        '"text"',
        "null",
        '{"jsonrpc": "2.0", "id": 3, "method": "ping"}',
        "\u202e\u200b",
    ]
    answers = serve(server, "\n".join(lines) + "\n")
    ids = [a["id"] for a in answers if "result" in a]
    assert ids == [2, 3]
    codes = [a["error"]["code"] for a in answers if "error" in a]
    assert -32700 in codes
    assert -32600 in codes
    assert run.calls == []


def test_serve_answers_a_thousand_requests_in_order_in_reasonable_time():
    server, _ = make_server()
    text = "".join(f'{{"jsonrpc":"2.0","id":{i},"method":"ping"}}\n' for i in range(1000))
    start = time.monotonic()
    answers = serve(server, text)
    assert time.monotonic() - start < 10
    assert [a["id"] for a in answers] == list(range(1000))


def test_serve_reads_a_very_long_line_without_a_crash():
    server, _ = make_server()
    text = '{"jsonrpc":"2.0","id":1,"method":"ping","pad":"' + "x" * 5_000_000 + '"}\n'
    assert serve(server, text)[0]["result"] == {}


def test_two_requests_for_one_change_run_the_command_each_time_and_the_same_way():
    server, run = make_server("::/0 0.0.0.0/0")
    message = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"allow_cloud"}}\n'
    ) * 2
    answers = serve(server, message)
    assert [a["result"]["isError"] for a in answers] == [False, False]
    sets = [c for c in run.calls if c[1] == "set-property"]
    assert sets[0] == sets[1]


def test_main_refuses_a_uid_that_is_not_a_person_with_status_2(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    for uid in ("0", "999", "60000", "-5"):
        assert network.main([uid]) == 2
        err = capsys.readouterr().err
        assert err.startswith("vilna-mcp-gloxi-network: ")
        assert "uid" in err


@pytest.mark.parametrize("argv", [[], ["abc"], ["1000", "extra"], ["1e3"], ["1000.5"], [""]])
def test_main_with_bad_arguments_exits_with_a_usage_error(argv, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as info:
        network.main(argv)
    assert info.value.code == 2
    capsys.readouterr()


def test_main_serves_the_requests_it_is_given_and_returns_0(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":9,"method":"ping"}\n'))
    assert network.main(["1000"]) == 0
    assert json.loads(capsys.readouterr().out) == {"jsonrpc": "2.0", "id": 9, "result": {}}


# -- the internet gate: gloxid's side --------------------------------------------------------

ALLOWED = {"decision": "allowed", "code": "allowed"}


def gate(answer):
    calls: list[tuple[str, str]] = []

    def request(operation: str, reason: str):
        calls.append((operation, reason))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return network.CloudNetwork(request), calls


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        "allowed",
        b"allowed",
        0,
        True,
        {},
        {"decision": "allowed"},
        {"code": "allowed"},
        {"decision": "ALLOWED", "code": "allowed"},
        {"decision": "allowed ", "code": "allowed"},
        {"decision": "allowed", "code": "Allowed"},
        {"decision": ["allowed"], "code": "allowed"},
        {"decision": "allowed", "code": ["allowed"]},
        {"decision": True, "code": True},
        {"decision": "denied", "code": "allowed"},
        {"decision": "allowed", "code": "denied"},
        {"decision": "asked", "code": "allowed"},
        {"decision": "allowed", "code": "timeout"},
        {"decision": None, "code": None},
        {"result": {"decision": "allowed", "code": "allowed"}},
    ],
)
def test_anything_but_a_clear_yes_keeps_the_way_closed(answer):
    net, calls = gate(answer)
    with pytest.raises(NetworkRefused):
        net()
    assert net.open is False
    assert [c[0] for c in calls] == ["allow_cloud"]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("boom"), ConnectionError("gone"), OSError("no socket"), TimeoutError("slow")],
)
def test_a_broken_connection_to_the_security_layer_is_a_refusal_not_a_crash(failure):
    net, _ = gate(failure)
    with pytest.raises(NetworkRefused):
        net()
    assert net.open is False


def test_a_clear_yes_opens_the_way_and_it_is_asked_only_once():
    net, calls = gate(ALLOWED)
    net()
    net()
    net()
    assert net.open is True
    assert [c[0] for c in calls] == ["allow_cloud"]


def test_a_refusal_is_asked_again_next_time_and_a_later_yes_works():
    answers = [{"decision": "denied", "code": "denied"}, ALLOWED]
    calls: list[str] = []

    def request(operation, reason):
        calls.append(operation)
        return answers.pop(0)

    net = network.CloudNetwork(request)
    with pytest.raises(NetworkRefused):
        net()
    net()
    assert net.open is True
    assert calls == ["allow_cloud", "allow_cloud"]


def test_twenty_threads_at_once_make_one_question():
    calls: list[str] = []

    def request(operation, reason):
        calls.append(operation)
        time.sleep(0.05)
        return ALLOWED

    net = network.CloudNetwork(request)
    threads = [threading.Thread(target=net) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert calls == ["allow_cloud"]
    assert net.open is True


def test_the_reasons_given_to_the_person_are_plain_text_without_user_data():
    net, calls = gate(ALLOWED)
    net()
    net.close()
    for _, reason in calls:
        assert reason.isprintable()
        assert "\n" not in reason
        assert len(reason) < 200


def closing(status, block=None):
    calls: list[str] = []

    def request(operation, reason):
        calls.append(operation)
        if operation == "status":
            if isinstance(status, BaseException):
                raise status
            return status
        if isinstance(block, BaseException):
            raise block
        return block

    net = network.CloudNetwork(request)
    net.open = True
    return net, calls


def open_status(cloud):
    return {"decision": "allowed", "result": {"structuredContent": {"cloud": cloud}}}


def test_closing_marks_the_way_shut_and_blocks_when_the_cloud_was_open():
    net, calls = closing(open_status(True), {"decision": "allowed"})
    assert net.close() is True
    assert net.open is False
    assert calls == ["status", "block_cloud"]


def test_closing_does_not_block_again_when_the_cloud_is_already_off():
    net, calls = closing(open_status(False))
    assert net.close() is True
    assert calls == ["status"]


@pytest.mark.parametrize(
    "block",
    [
        None,
        {},
        {"decision": "denied"},
        {"decision": "ALLOWED"},
        {"decision": ["allowed"]},
        "allowed",
        RuntimeError("gone"),
    ],
)
def test_closing_says_no_when_the_block_was_not_clearly_allowed(block):
    net, calls = closing(open_status(True), block)
    assert net.close() is False
    assert net.open is False
    assert calls == ["status", "block_cloud"]


def test_closing_says_no_when_the_security_layer_cannot_be_reached():
    net, calls = closing(RuntimeError("no socket"))
    assert net.close() is False
    assert net.open is False
    assert calls == ["status"]


@pytest.mark.parametrize("cloud", [True, "yes", 1, "false", "0", [0], {"a": 1}])
def test_any_truthy_state_means_the_way_gets_blocked(cloud):
    net, calls = closing(open_status(cloud), {"decision": "allowed"})
    assert net.close() is True
    assert calls == ["status", "block_cloud"]


@pytest.mark.parametrize(
    "result",
    [
        {"cloud": True},
        {"structuredContent": {"cloud": True}},
    ],
)
def test_both_shapes_of_a_tool_result_are_understood(result):
    net, calls = closing({"decision": "allowed", "result": result}, {"decision": "allowed"})
    assert net.close() is True
    assert calls == ["status", "block_cloud"]


def test_without_the_security_layer_there_is_no_gate(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "herkos.client", None)
    assert network.herkos_network() is None


def test_a_missing_socket_means_no_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERKOS_SOCKET", str(tmp_path / "no-such.sock"))
    assert network.herkos_network() is None


def test_a_relative_socket_setting_is_not_trusted(monkeypatch, tmp_path):
    (tmp_path / "x.sock").write_text("", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERKOS_SOCKET", "x.sock")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert network.herkos_network() is None


# -- the privacy switch --------------------------------------------------------------------


def test_masking_is_on_by_default_and_hides_an_address_before_it_leaves(tmp_path):
    s = load_settings(tmp_path / "none.toml", env={})
    factory = guard_factory(s.router)
    assert factory is not None
    masker = factory()
    text = "Write to anna.example@example.org, please."
    masked = masker.mask(text)
    assert "example.org" not in masked
    assert masker.unmask(masked) == text


@pytest.mark.parametrize("value", ["false", "FALSE", "no", "off", "0", " Off "])
def test_masking_is_off_only_for_a_clear_no(tmp_path, value):
    s = load_settings(tmp_path / "none.toml", env={"GLOXI_MASK_CLOUD": value})
    assert guard_factory(s.router) is None


@pytest.mark.parametrize(
    "value", ["fasle", "nope", "2", "-1", "n", "disabled", "false!", "f", "None", "null"]
)
def test_a_mistyped_masking_switch_is_an_error_and_never_turns_masking_off(tmp_path, value):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "none.toml", env={"GLOXI_MASK_CLOUD": value})
    with pytest.raises(ConfigError):
        settings_from(tmp_path, f'[router]\nmask_cloud = "{value}"\n')


def test_the_file_can_turn_masking_off_with_a_string_or_a_boolean(tmp_path):
    for text in ("false", '"false"', '"off"', "0"):
        s = settings_from(tmp_path, f"[router]\nmask_cloud = {text}\n")
        assert guard_factory(s.router) is None


def test_every_conversation_gets_a_masker_of_its_own(tmp_path):
    s = load_settings(tmp_path / "none.toml", env={})
    factory = guard_factory(s.router)
    assert factory is not None
    first, second = factory(), factory()
    assert first is not second
    first.mask("anna.example@example.org")
    assert len(first) == 1
    assert len(second) == 0


def test_the_strict_switch_reaches_the_masker():
    plain = guard_factory(RouterPolicy(mask_cloud=True, mask_strict=False))
    strict = guard_factory(RouterPolicy(mask_cloud=True, mask_strict=True))
    assert plain is not None and strict is not None
    assert plain().strict is False
    assert strict().strict is True


def test_the_masker_does_not_print_what_it_hid():
    factory = guard_factory(RouterPolicy())
    assert factory is not None
    masker = factory()
    masker.mask("anna.example@example.org")
    assert "example.org" not in repr(masker)
