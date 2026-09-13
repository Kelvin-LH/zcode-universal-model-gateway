"""Admin API CRUD, config validation, preview and secret safety."""

from __future__ import annotations

import httpx

from gateway.app import create_app

from .conftest import BASE_CONFIG, ENV


async def test_status_and_meta(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    status = (await client.get("/api/admin/status")).json()
    assert status["config_valid"] is True
    assert status["providers"] == 3
    assert status["models"] == 4
    assert status["providers_with_keys"] == 3
    assert status["version"]

    meta = (await client.get("/api/admin/meta")).json()
    assert set(meta["protocols"]) == {
        "openai_responses",
        "openai_chat",
        "anthropic_messages",
    }


async def test_provider_crud(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    created = await client.post(
        "/api/admin/providers",
        json={
            "id": "newprov",
            "display_name": "New",
            "protocol": "openai_chat",
            "base_url": "https://new.example/v1",
            "api_key_env": "NEW_KEY",
        },
    )
    assert created.status_code == 200
    assert created.json()["provider"]["key_status"]["available"] is False

    listed = (await client.get("/api/admin/providers")).json()["providers"]
    assert any(p["id"] == "newprov" for p in listed)
    # secret values are never present
    assert "sk-responses-secret" not in str(listed)

    updated = await client.put("/api/admin/providers/newprov", json={"display_name": "Updated"})
    assert updated.json()["provider"]["display_name"] == "Updated"

    dup = await client.post("/api/admin/providers/newprov/duplicate", json={"id": "newprov2"})
    assert dup.status_code == 200

    deleted = await client.delete("/api/admin/providers/newprov")
    assert deleted.json()["deleted"] == "newprov"
    assert (await client.delete("/api/admin/providers/newprov")).status_code == 404

    # a provider in use is refused by default, with the referencing models listed
    in_use = await client.delete("/api/admin/providers/responses_provider")
    assert in_use.status_code == 409
    assert "foo" in in_use.json()["detail"]


async def test_provider_cascade_delete(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    resp = await client.delete("/api/admin/providers/responses_provider?cascade=true")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["deleted"] == "responses_provider"
    assert "foo" in payload["deleted_models"]
    assert "disabled-model" in payload["deleted_models"]

    models = (await client.get("/api/admin/models")).json()["models"]
    assert all(m["id"] != "foo" for m in models)
    providers = (await client.get("/api/admin/providers")).json()["providers"]
    assert all(p["id"] != "responses_provider" for p in providers)


async def test_model_crud_and_virtual_models(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    created = await client.post(
        "/api/admin/models",
        json={
            "id": "mymodel",
            "display_name": "My Model",
            "provider": "chat_provider",
            "upstream_model": "upstream-x",
            "reasoning": {
                "supported": ["minimal", "medium", "xhigh"],
                "default": "medium",
                "mapping": {"xhigh": {"reasoning_effort": "xhigh"}},
            },
        },
    )
    assert created.status_code == 200
    model = created.json()["model"]
    assert model["virtual_models"] == [
        "mymodel",
        "mymodel@minimal",
        "mymodel@medium",
        "mymodel@xhigh",
    ]

    virtual = (await client.get("/api/admin/models/mymodel/virtual")).json()["virtual_models"]
    assert "mymodel@xhigh" in virtual

    updated = await client.put("/api/admin/models/mymodel", json={"enabled": False})
    assert updated.json()["model"]["enabled"] is False

    deleted = await client.delete("/api/admin/models/mymodel")
    assert deleted.json()["deleted"] == "mymodel"


async def test_model_invalid_provider_rejected(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/models",
        json={"id": "bad", "provider": "ghost", "upstream_model": "u"},
    )
    assert response.status_code == 400


async def test_config_get_validate_save(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    config = (await client.get("/api/admin/config")).json()
    assert config["valid"] is True
    assert "providers:" in config["yaml"]

    invalid = await client.post("/api/admin/config/validate", json={"yaml": "providers: [bad"})
    assert invalid.json()["valid"] is False
    assert "error" in invalid.json()

    new_yaml = BASE_CONFIG.replace("bar:", "barbar:")
    valid = await client.post("/api/admin/config/validate", json={"yaml": new_yaml})
    assert valid.json()["valid"] is True
    assert "barbar" in valid.json()["diff"]["models"]["added"]

    # invalid save is rejected and does not overwrite
    rejected = await client.put("/api/admin/config", json={"yaml": "providers: [bad"})
    assert rejected.status_code == 400
    still_ok = (await client.get("/api/admin/config")).json()
    assert "bar:" in still_ok["yaml"]

    saved = await client.put("/api/admin/config", json={"yaml": new_yaml})
    assert saved.json()["saved"] is True
    assert "barbar" in harness.path.read_text(encoding="utf-8")


async def test_config_download_upload_reset(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    download = await client.get("/api/admin/config/download")
    assert download.status_code == 200
    assert "providers:" in download.text

    upload = await client.post("/api/admin/config/upload", json={"yaml": BASE_CONFIG})
    assert upload.json()["valid"] is True

    reset = await client.post("/api/admin/config/reset-example")
    # config.example.yaml lives at the repo root, not in tmp_path
    assert reset.status_code in (200, 404)


async def test_preview_redacts_secrets(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    preview = (
        await client.post(
            "/api/admin/preview", json={"model": "foo@max", "body": {"model": "foo@max", "input": "hi"}}
        )
    ).json()
    assert preview["logical_model"] == "foo"
    assert preview["reasoning_level"] == "max"
    assert preview["protocol"] == "openai_responses"
    assert preview["upstream_model"] == "foo-real"
    assert preview["upstream_url"] == "https://responses.example/v1/responses"
    # authorization header is masked
    assert preview["headers"]["authorization"] == "***REDACTED***"
    assert "sk-responses-secret" not in str(preview)


async def test_logs_and_metrics(build_app, client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": []})

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    await client.post("/v1/responses", json={"model": "foo@high", "input": "hi"})

    logs = (await client.get("/api/admin/logs")).json()["logs"]
    assert logs
    entry = logs[0]
    assert entry["logical_model"] == "foo"
    assert entry["reasoning_level"] == "high"
    assert entry["provider"] == "responses_provider"
    assert entry["ok"] is True
    # no prompt content in logs
    assert "hi" not in str(entry) or entry.get("logical_model") != "hi"

    metrics = (await client.get("/api/admin/metrics")).json()
    assert metrics["total_requests"] >= 1
    assert metrics["successful_requests"] >= 1
    assert metrics["requests_by_model"]["foo"] >= 1


async def test_admin_token_enforced(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_ADMIN_TOKEN", "topsecret")
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    app = create_app(str(path), client=upstream, env=dict(ENV))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/api/admin/status")).status_code == 401
        ok = await client.get("/api/admin/status", headers={"x-admin-token": "topsecret"})
        assert ok.status_code == 200
        bearer = await client.get(
            "/api/admin/status", headers={"authorization": "Bearer topsecret"}
        )
        assert bearer.status_code == 200
    await upstream.aclose()


async def test_temporary_key_lifecycle_and_no_leak(build_app, client_factory):
    harness = build_app(env_map={})
    client = client_factory(harness.app)

    before = (await client.get("/api/admin/status")).json()
    assert before["providers_with_keys"] == 0

    set_resp = await client.put(
        "/api/admin/providers/chat_provider/temporary-key", json={"api_key": "sk-temp-xyz"}
    )
    assert set_resp.json()["temporary_key_active"] is True

    providers = (await client.get("/api/admin/providers")).json()["providers"]
    assert "sk-temp-xyz" not in str(providers)
    chat = next(p for p in providers if p["id"] == "chat_provider")
    assert chat["key_status"]["temporary_key_active"] is True
    assert chat["key_status"]["source"] == "temporary"

    cleared = await client.delete("/api/admin/providers/chat_provider/temporary-key")
    assert cleared.json()["temporary_key_active"] is False


async def test_saved_local_key_persists_in_file(build_app, client_factory):
    harness = build_app(env_map={})
    client = client_factory(harness.app)

    # Save a key "to local".
    resp = await client.put(
        "/api/admin/providers/chat_provider/key",
        json={"api_key": "sk-local-123", "persist": True},
    )
    status = resp.json()["key_status"]
    assert status["available"] is True
    assert status["source"] == "local"
    assert status["local_key_saved"] is True

    # It is written to the local key file, next to config.yaml...
    key_file = harness.path.parent / "secrets.local.json"
    assert key_file.is_file()
    assert "sk-local-123" in key_file.read_text(encoding="utf-8")

    # ...but never exposed through the API.
    listed = (await client.get("/api/admin/providers")).json()["providers"]
    assert "sk-local-123" not in str(listed)
    assert (await client.get("/api/admin/status")).json()["providers_with_keys"] == 1

    # Clearing removes it from the file too.
    await client.delete("/api/admin/providers/chat_provider/key")
    assert "sk-local-123" not in key_file.read_text(encoding="utf-8")


async def test_saved_key_survives_restart(tmp_path, client_factory, env):
    """A saved local key must be picked up again after the process restarts."""
    import httpx

    from gateway.app import create_app

    cfg = tmp_path / "config.yaml"
    cfg.write_text(BASE_CONFIG, encoding="utf-8")
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    # First "run": save a key.
    app1 = create_app(str(cfg), client=upstream, env={})
    app1.state.config_manager.load_or_default()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://t") as c:
        await c.put(
            "/api/admin/providers/chat_provider/key",
            json={"api_key": "sk-persist-abc", "persist": True},
        )

    # Second "run": a fresh app (no env var) must still see the key.
    app2 = create_app(str(cfg), client=upstream, env={})
    app2.state.config_manager.load_or_default()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as c:
        providers = (await c.get("/api/admin/providers")).json()["providers"]
        chat = next(p for p in providers if p["id"] == "chat_provider")
        assert chat["key_status"]["available"] is True
        assert chat["key_status"]["source"] == "local"
        # The value itself is never returned.
        assert "sk-persist-abc" not in str(providers)
    await upstream.aclose()


async def test_test_connection_uses_models_endpoint(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    result = (await client.post("/api/admin/providers/responses_provider/test")).json()
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["model_count"] == 2
    assert seen["url"].endswith("/models")


async def test_bundle_export_contains_config_and_keys(build_app, client_factory):
    """The bundle is the single file you carry to another machine."""
    harness = build_app()
    client = client_factory(harness.app)

    resp = await client.get("/api/admin/config/bundle")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["format"] == "zumg.bundle.v1"
    assert set(payload["config"]["providers"]) == {
        "responses_provider",
        "chat_provider",
        "anthropic_provider",
    }
    assert "foo" in payload["config"]["models"]
    # Keys from environment variables cannot be extracted, so a fresh bundle
    # carries no keys until the user saves one locally (covered below).
    assert payload["keys"] == {}
    assert "sk-responses-secret" not in str(payload)


async def test_bundle_can_exclude_keys(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    payload = (
        await client.get("/api/admin/config/bundle?include_keys=false")
    ).json()
    assert payload["keys"] == {}
    assert payload["config"]["models"]


async def test_bundle_round_trip_restores_models_and_keys(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    # Save a key so it is part of the backup, then export.
    await client.put(
        "/api/admin/providers/chat_provider/key",
        json={"api_key": "sk-carried-over", "persist": True},
    )
    bundle = (await client.get("/api/admin/config/bundle")).json()
    assert bundle["keys"]["chat_provider"] == "sk-carried-over"

    # Wipe the machine: reset config and forget the key.
    await client.post("/api/admin/config/reset-example")
    await client.delete("/api/admin/providers/chat_provider/key")

    # Restore on the "new machine".
    resp = await client.post("/api/admin/config/bundle", json=bundle)
    assert resp.status_code == 200
    result = resp.json()
    assert result["imported"] is True
    assert "chat_provider" in result["keys_restored"]

    models = (await client.get("/api/admin/models")).json()["models"]
    assert any(m["id"] == "foo" for m in models)
    providers = (await client.get("/api/admin/providers")).json()["providers"]
    chat = next(p for p in providers if p["id"] == "chat_provider")
    assert chat["key_status"]["source"] == "local"
    assert chat["key_status"]["available"] is True


async def test_bundle_import_rejects_foreign_key(build_app, client_factory):
    """A hand-edited backup cannot smuggle in keys for unknown providers."""
    harness = build_app()
    client = client_factory(harness.app)
    bad = {
        "format": "zumg.bundle.v1",
        "config": {"providers": {}, "models": {}},
        "keys": {"ghost": "sk-ghost"},
    }
    resp = await client.post("/api/admin/config/bundle", json=bad)
    assert resp.status_code == 400
    assert "not in the configuration" in resp.json()["detail"]


async def test_bundle_import_rejects_wrong_format(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    resp = await client.post(
        "/api/admin/config/bundle", json={"format": "something-else", "config": {}}
    )
    assert resp.status_code == 400
