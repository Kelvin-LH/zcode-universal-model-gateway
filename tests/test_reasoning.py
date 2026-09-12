"""Reasoning mapping deep-merge behaviour and precedence."""

from __future__ import annotations

from gateway.config import parse_config
from gateway.merge import deep_merge, remove_path
from gateway.models import resolve_model


def test_deep_merge_nested():
    base = {"a": {"b": 1, "c": 2}, "keep": True}
    override = {"a": {"c": 3, "d": 4}}
    merged = deep_merge(base, override)
    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "keep": True}
    # inputs untouched
    assert base == {"a": {"b": 1, "c": 2}, "keep": True}


def test_deep_merge_list_replaced():
    assert deep_merge({"x": [1, 2]}, {"x": [3]}) == {"x": [3]}


def test_remove_path_nested():
    obj = {"reasoning": {"effort": "high", "summary": "auto"}, "temperature": 1}
    assert remove_path(obj, "reasoning.summary") is True
    assert obj == {"reasoning": {"effort": "high"}, "temperature": 1}
    assert remove_path(obj, "reasoning.missing") is False
    assert remove_path(obj, "nope.deep") is False


def test_reasoning_mapping_deep_merges_into_body():
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
                        "supported": ["max"],
                        "default": "max",
                        "mapping": {
                            "max": {"thinking": {"config": {"budget": 64000}}}
                        },
                    },
                }
            },
        }
    )
    resolved = resolve_model(cfg, "m@max")
    body = resolved.effective_body(
        {"input": "hi", "thinking": {"type": "enabled"}}
    )
    assert body["thinking"] == {
        "type": "enabled",
        "config": {"budget": 64000},
    }


def test_reasoning_mapping_arbitrary_field_names():
    # A vendor-specific Gemini-style field must pass through untouched.
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_chat", "base_url": "https://x/v1"}
            },
            "models": {
                "m": {
                    "provider": "p",
                    "upstream_model": "u",
                    "reasoning": {
                        "supported": ["low", "high"],
                        "default": "high",
                        "mapping": {
                            "low": {"thinkingConfig": {"thinkingBudget": 1024}},
                            "high": {"thinkingConfig": {"thinkingBudget": 24576}},
                        },
                    },
                }
            },
        }
    )
    resolved = resolve_model(cfg, "m@low")
    body = resolved.effective_body({"messages": [{"role": "user", "content": "x"}]})
    assert body["thinkingConfig"] == {"thinkingBudget": 1024}


def test_ui_preset_mappings_reach_upstream_body():
    """The Web UI field presets must produce valid, deep-mergeable mappings."""
    # These mirror REASONING_FIELDS' build() functions in static/app.js.
    presets = {
        "openai_responses": {"reasoning": {"effort": "high"}},
        "openai_chat": {"reasoning_effort": "high"},
        "anthropic_budget": {"thinking": {"type": "enabled", "budget_tokens": 16000}},
        "anthropic_onoff": {"thinking": {"type": "disabled"}},
    }
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
                        "supported": list(presets),
                        "default": "openai_responses",
                        "mapping": presets,
                    },
                }
            },
        }
    )
    for level, expected in presets.items():
        resolved = resolve_model(cfg, f"m@{level}")
        body = resolved.effective_body({"input": "hi"})
        for key, value in expected.items():
            assert body[key] == value, level


def test_ignore_client_reasoning_strips_client_controls():
    """A model can force its level mapping to be the only reasoning control.

    This matters when the client and the mapping use different field names:
    the client's field would otherwise survive the deep merge and could win.
    """
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x/v1"}
            },
            "models": {
                "strict": {
                    "provider": "p",
                    "upstream_model": "u",
                    "ignore_client_reasoning": True,
                    "reasoning": {
                        "supported": ["max"],
                        "default": "max",
                        "mapping": {"max": {"reasoning_effort": "xhigh"}},
                    },
                },
                "loose": {
                    "provider": "p",
                    "upstream_model": "u",
                    "reasoning": {
                        "supported": ["max"],
                        "default": "max",
                        "mapping": {"max": {"reasoning_effort": "xhigh"}},
                    },
                },
            },
        }
    )
    client = {
        "input": "hi",
        "reasoning": {"effort": "low", "summary": "auto"},
        "reasoning_effort": "low",
        "reasoningEffort": "low",
        "reasoning_summary": "auto",
        "thinking": {"type": "disabled", "budget_tokens": 1},
        "thinkingConfig": {"thinkingBudget": 1},
        "thinking_budget": 1,
        "enable_thinking": True,
        "output_config": {"effort": "low"},
    }

    # Strict: every client reasoning control is gone; only the mapping remains.
    strict = resolve_model(cfg, "strict@max").effective_body(dict(client))
    for field in (
        "reasoning",
        "reasoning_effort",
        "reasoningEffort",
        "reasoning_summary",
        "thinking",
        "thinkingConfig",
        "thinking_budget",
        "enable_thinking",
        "output_config",
    ):
        # reasoning_effort is present, but only as the mapping's value.
        if field == "reasoning_effort":
            assert strict[field] == "xhigh"
        else:
            assert field not in strict, field

    # Without the flag, controls the mapping does not use survive the merge --
    # e.g. the client's nested `reasoning.effort` sits next to the mapping's
    # `reasoning_effort`, and can still influence the upstream. This is exactly
    # the case `ignore_client_reasoning` exists to remove.
    loose = resolve_model(cfg, "loose@max").effective_body(dict(client))
    assert loose["reasoning"] == {"effort": "low", "summary": "auto"}
    assert loose["enable_thinking"] is True
    assert loose["reasoning_effort"] == "xhigh"  # same name: mapping wins


def test_ignore_client_reasoning_is_a_noop_without_level_mapping():
    cfg = parse_config(
        {
            "providers": {
                "p": {"protocol": "openai_responses", "base_url": "https://x/v1"}
            },
            "models": {
                "plain": {
                    "provider": "p",
                    "upstream_model": "u",
                    "ignore_client_reasoning": True,
                }
            },
        }
    )
    body = resolve_model(cfg, "plain").effective_body(
        {"reasoning": {"effort": "low"}}
    )
    assert body["reasoning"] == {"effort": "low"}
