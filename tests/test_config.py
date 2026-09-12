"""Configuration schema, defaults, validation and safe writes."""

from __future__ import annotations

import os

import pytest

from gateway.config import (
    ConfigManager,
    atomic_write,
    load_config_text,
    parse_config,
)
from gateway.errors import ConfigError

from .conftest import BASE_CONFIG


def test_paths_have_sensible_defaults():
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x/v1/"}
            }
        }
    )
    provider = cfg.providers["p"]
    assert provider.base_url == "https://x/v1"
    assert provider.url_for("responses") == "https://x/v1/responses"
    assert provider.url_for("chat_completions") == "https://x/v1/chat/completions"
    assert provider.url_for("models") == "https://x/v1/models"
    assert provider.timeout == 600


def test_anthropic_default_message_path():
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "anthropic_messages", "base_url": "https://x"}
            }
        }
    )
    assert cfg.providers["p"].url_for("messages") == "https://x/v1/messages"


def test_path_overrides():
    cfg = parse_config(
        {
            "providers": {
                "p": {
                    "protocol": "openai_responses",
                    "base_url": "https://x/v1",
                    "paths": {"responses": "/custom/responses"},
                }
            }
        }
    )
    assert cfg.providers["p"].url_for("responses") == "https://x/v1/custom/responses"


def test_unsupported_protocol_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {
                "providers": {
                    "p": {"protocol": "totally_fake", "base_url": "https://x"}
                }
            }
        )


def test_invalid_base_url_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {"providers": {"p": {"protocol": "openai_responses", "base_url": "x"}}}
        )


def test_unknown_provider_reference_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {"models": {"m": {"provider": "ghost", "upstream_model": "u"}}}
        )


def test_default_reasoning_must_be_supported():
    with pytest.raises(ConfigError):
        parse_config(
            {
                "providers": {
                    "p": {"protocol": "openai_responses", "base_url": "https://x"}
                },
                "models": {
                    "m": {
                        "provider": "p",
                        "upstream_model": "u",
                        "reasoning": {"supported": ["low"], "default": "high"},
                    }
                },
            }
        )


def test_mapping_derives_supported_levels():
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x"}
            },
            "models": {
                "m": {
                    "provider": "p",
                    "upstream_model": "u",
                    "reasoning": {"mapping": {"max": {"reasoning_effort": "max"}}},
                }
            },
        }
    )
    assert cfg.models["m"].reasoning.supported == ["max"]
    assert cfg.models["m"].reasoning.default == "max"


def test_at_sign_in_ids_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {
                "providers": {
                    "a@b": {"protocol": "openai_responses", "base_url": "https://x"}
                }
            }
        )


def test_load_config_text_roundtrip():
    cfg = load_config_text(BASE_CONFIG)
    assert set(cfg.providers) == {
        "responses_provider",
        "chat_provider",
        "anthropic_provider",
    }
    text = cfg.to_yaml()
    again = load_config_text(text)
    assert again.to_dict() == cfg.to_dict()


def test_invalid_yaml_raises_config_error():
    with pytest.raises(ConfigError):
        load_config_text("providers: [unclosed")


def test_atomic_write(tmp_path):
    target = tmp_path / "config.yaml"
    atomic_write(target, "hello: world\n")
    assert target.read_text(encoding="utf-8") == "hello: world\n"
    # no leftover temp files
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_config_manager_hot_reload(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    manager = ConfigManager(path)
    manager.load()
    assert "foo" in manager.config.models

    updated = BASE_CONFIG.replace("bar:", "bar2:")
    path.write_text(updated, encoding="utf-8")
    os.utime(path, (os.path.getmtime(path) + 2, os.path.getmtime(path) + 2))
    assert manager.maybe_reload() is True
    assert "bar2" in manager.config.models


def test_invalid_reload_keeps_previous_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    manager = ConfigManager(path)
    manager.load()
    assert "foo" in manager.config.models

    path.write_text("providers: [broken", encoding="utf-8")
    os.utime(path, (os.path.getmtime(path) + 2, os.path.getmtime(path) + 2))
    assert manager.maybe_reload() is False
    # previous good config is still active and the error is recorded
    assert "foo" in manager.config.models
    assert manager.load_error is not None
