# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from gloxi.config import DEFAULT_LOCAL_URL, ConfigError, config_path, load_settings


def test_defaults_without_file(tmp_path):
    s = load_settings(tmp_path / "missing.toml", env={})
    assert s.local.url == DEFAULT_LOCAL_URL
    assert s.local.model == "local"
    assert s.local.api_key is None


def test_file_then_env_override(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[local]\nurl = "http://box:9000/v1/"\nmodel = "qwen"\ntimeout = 30\n')
    s = load_settings(path, env={})
    assert (s.local.url, s.local.model, s.local.timeout) == ("http://box:9000/v1", "qwen", 30.0)
    s = load_settings(path, env={"GLOXI_LOCAL_URL": "http://other/v1"})
    assert s.local.url == "http://other/v1"
    assert s.local.model == "qwen"


def test_broken_file_is_a_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[local\n")
    with pytest.raises(ConfigError):
        load_settings(path, env={})


def test_bad_timeout_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "x.toml", env={"GLOXI_LOCAL_TIMEOUT": "soon"})


def test_config_path_follows_xdg(tmp_path):
    assert config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "gloxi" / "config.toml"
