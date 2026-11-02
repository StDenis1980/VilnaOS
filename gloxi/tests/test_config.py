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


def test_cloud_and_router_from_file_and_env(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[cloud]\nurl = "https://api.example.eu/v1/"\nmodel = "big"\n'
        '[router]\nmode = "auto"\ncloud_allowed = true\nlong_message_chars = 800\n'
    )
    s = load_settings(path, env={"GLOXI_CLOUD_API_KEY": "sk-env"})
    assert s.cloud.url == "https://api.example.eu/v1"
    assert s.cloud.api_key == "sk-env"
    assert s.router.cloud_allowed is True
    assert s.router.long_message_chars == 800
    s = load_settings(path, env={"GLOXI_CLOUD_ALLOWED": "0", "GLOXI_ROUTER_MODE": "local"})
    assert (s.router.cloud_allowed, s.router.mode) == (False, "local")


def test_cloud_is_off_and_absent_by_default(tmp_path):
    s = load_settings(tmp_path / "none.toml", env={})
    assert s.cloud is None
    assert s.router.cloud_allowed is False


def test_cloud_allowed_without_cloud_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no \\[cloud\\]"):
        load_settings(tmp_path / "none.toml", env={"GLOXI_CLOUD_ALLOWED": "true"})


def test_bad_router_mode_and_missing_cloud_model(tmp_path):
    with pytest.raises(ConfigError, match="router mode"):
        load_settings(tmp_path / "none.toml", env={"GLOXI_ROUTER_MODE": "turbo"})
    with pytest.raises(ConfigError, match="model"):
        load_settings(tmp_path / "none.toml", env={"GLOXI_CLOUD_URL": "https://x/v1"})


def test_masking_is_on_by_default_and_can_be_turned_off(tmp_path):
    assert load_settings(tmp_path / "x.toml", env={}).router.mask_cloud
    s = load_settings(tmp_path / "x.toml", env={"GLOXI_MASK_CLOUD": "false"})
    assert not s.router.mask_cloud
    path = tmp_path / "config.toml"
    path.write_text("[router]\nmask_cloud = false\n")
    assert not load_settings(path, env={}).router.mask_cloud


def test_tools_flag(tmp_path):
    path = tmp_path / "c.toml"
    assert load_settings(path, env={}).tools_enabled is True
    path.write_text("[tools]\nenabled = false\n")
    assert load_settings(path, env={}).tools_enabled is False
    assert load_settings(path, env={"GLOXI_TOOLS": "1"}).tools_enabled is True


def test_start_server(tmp_path):
    path = tmp_path / "config.toml"
    assert load_settings(path, env={}).start_server is True
    path.write_text("[local]\nstart_server = false\n")
    assert load_settings(path, env={}).start_server is False
    assert load_settings(path, env={"GLOXI_START_SERVER": "yes"}).start_server is True
