"""Model alias resolution and model listing."""

from __future__ import annotations

import pytest

from gateway.config import load_config_text, parse_config
from gateway.errors import (
    ModelDisabledError,
    ProviderDisabledError,
    UnknownModelError,
    UnknownReasoningLevelError,
)
from gateway.models import list_models, resolve_model, split_alias

from .conftest import BASE_CONFIG


@pytest.fixture
def config():
    return load_config_text(BASE_CONFIG)


def test_split_alias():
    assert split_alias("foo@max") == ("foo", "max")
    assert split_alias("foo") == ("foo", None)
    assert split_alias("foo@") == ("foo", None)
    assert split_alias("a@b@c") == ("a@b", "c")


def test_resolve_default_reasoning(config):
    resolved = resolve_model(config, "foo")
    assert resolved.logical_id == "foo"
    assert resolved.reasoning_level == "high"
    assert resolved.upstream_model == "foo-real"
    assert resolved.provider_id == "responses_provider"
    assert resolved.protocol == "openai_responses"
    assert resolved.mapping == {"reasoning": {"effort": "high"}}


def test_resolve_explicit_reasoning(config):
    resolved = resolve_model(config, "foo@max")
    assert resolved.reasoning_level == "max"
    assert resolved.mapping == {"reasoning": {"effort": "max"}}
    assert resolved.target_url == "https://responses.example/v1/responses"


def test_arbitrary_reasoning_levels_not_hardcoded():
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x/v1"}
            },
            "models": {
                "m": {
                    "provider": "p",
                    "upstream_model": "u",
                    "reasoning": {
                        "supported": ["minimal", "xhigh", "ultra"],
                        "default": "xhigh",
                        "mapping": {
                            "minimal": {"reasoning_effort": "minimal"},
                            "xhigh": {"thinkingConfig": {"thinkingBudget": 64000}},
                            "ultra": {"reasoning_effort": "ultra"},
                        },
                    },
                }
            },
        }
    )
    assert resolve_model(cfg, "m@ultra").mapping == {"reasoning_effort": "ultra"}
    assert resolve_model(cfg, "m").mapping == {"thinkingConfig": {"thinkingBudget": 64000}}
    assert resolve_model(cfg, "m@minimal").reasoning_level == "minimal"


def test_unknown_model(config):
    with pytest.raises(UnknownModelError):
        resolve_model(config, "does-not-exist")


def test_unknown_reasoning_level(config):
    with pytest.raises(UnknownReasoningLevelError):
        resolve_model(config, "foo@xhigh")


def test_reasoning_level_on_model_without_reasoning(config):
    with pytest.raises(UnknownReasoningLevelError):
        resolve_model(config, "bar@high")


def test_disabled_model(config):
    with pytest.raises(ModelDisabledError):
        resolve_model(config, "disabled-model")


def test_disabled_provider():
    cfg = parse_config(
        {
            "providers": {
                "p": {
                    "protocol": "openai_responses",
                    "base_url": "https://x/v1",
                    "enabled": False,
                }
            },
            "models": {"m": {"provider": "p", "upstream_model": "u"}},
        }
    )
    with pytest.raises(ProviderDisabledError):
        resolve_model(cfg, "m")


def test_list_models_includes_all_levels(config):
    ids = [entry["id"] for entry in list_models(config)]
    assert ids == [
        "foo",
        "foo@off",
        "foo@low",
        "foo@high",
        "foo@max",
        "bar",
        "baz",
    ]
    # disabled models are not advertised
    assert "disabled-model" not in ids


def test_precedence_alias_overrides_client_reasoning(config):
    resolved = resolve_model(config, "foo@max")
    body = resolved.effective_body(
        {"model": "foo@max", "input": "x", "reasoning": {"effort": "low"}},
        "alias",
    )
    assert body["reasoning"]["effort"] == "max"
    assert body["model"] == "foo-real"


def test_precedence_client_keeps_client_reasoning():
    cfg = parse_config(
        {
            "settings": {"reasoning_precedence": "client"},
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x/v1"}
            },
            "models": {
                "m": {
                    "provider": "p",
                    "upstream_model": "u",
                    "reasoning": {
                        "supported": ["max"],
                        "default": "max",
                        "mapping": {"max": {"reasoning": {"effort": "max"}}},
                    },
                }
            },
        }
    )
    resolved = resolve_model(cfg, "m@max")
    body = resolved.effective_body({"reasoning": {"effort": "low"}}, "client")
    assert body["reasoning"]["effort"] == "low"


def test_request_overrides_and_remove_fields():
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x/v1"}
            },
            "models": {
                "m": {
                    "provider": "p",
                    "upstream_model": "u",
                    "request_overrides": {"temperature": 1, "extra_body": {"foo": "bar"}},
                    "remove_fields": ["top_p", "reasoning.summary"],
                }
            },
        }
    )
    resolved = resolve_model(cfg, "m")
    body = resolved.effective_body(
        {
            "temperature": 0.2,
            "top_p": 0.9,
            "reasoning": {"effort": "low", "summary": "auto"},
        }
    )
    assert body["temperature"] == 1
    assert body["extra_body"] == {"foo": "bar"}
    assert "top_p" not in body
    assert "summary" not in body["reasoning"]
    assert body["reasoning"]["effort"] == "low"
    assert body["model"] == "u"
