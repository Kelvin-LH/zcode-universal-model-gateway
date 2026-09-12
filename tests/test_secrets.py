"""Secret handling: keys are resolved but never exposed."""

from __future__ import annotations

from gateway.secrets import SecretStore


def test_env_key_resolution():
    store = SecretStore({"OPENAI_API_KEY": "sk-secret"})
    assert store.get("openai", "OPENAI_API_KEY") == "sk-secret"
    status = store.status("openai", "OPENAI_API_KEY")
    assert status["available"] is True
    assert status["source"] == "environment"
    # The value itself is never part of the status payload.
    assert "sk-secret" not in str(status)


def test_missing_env_key():
    store = SecretStore({})
    assert store.get("openai", "OPENAI_API_KEY") is None
    status = store.status("openai", "OPENAI_API_KEY")
    assert status["available"] is False
    assert status["api_key_env"] == "OPENAI_API_KEY"


def test_temporary_key_wins_and_is_only_reported_as_active():
    store = SecretStore({"OPENAI_API_KEY": "env-value"})
    store.set_temporary("openai", "temp-value")
    assert store.get("openai", "OPENAI_API_KEY") == "temp-value"
    status = store.status("openai", "OPENAI_API_KEY")
    assert status["temporary_key_active"] is True
    assert "temp-value" not in str(status)
    assert "env-value" not in str(status)

    store.clear_temporary("openai")
    assert store.get("openai", "OPENAI_API_KEY") == "env-value"
    assert store.status("openai", "OPENAI_API_KEY")["temporary_key_active"] is False
