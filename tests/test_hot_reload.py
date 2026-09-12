"""Config hot reload through the running app (no restart)."""

from __future__ import annotations

from .conftest import BASE_CONFIG


async def test_hot_reload_adds_model(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    before = [m["id"] for m in (await client.get("/v1/models")).json()["data"]]
    assert "model-b" not in before

    updated = BASE_CONFIG + """
  model-b:
    display_name: Model B
    provider: responses_provider
    upstream_model: b-real
    enabled: true
"""
    harness.write_config(updated)

    after = [m["id"] for m in (await client.get("/v1/models")).json()["data"]]
    assert "model-b" in after


async def test_hot_reload_invalid_keeps_serving(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    assert (await client.get("/v1/models")).status_code == 200

    harness.write_config("providers: [this is broken")

    # Gateway keeps serving the previous valid config.
    response = await client.get("/v1/models")
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["data"]]
    assert "foo" in ids

    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["config"] == "invalid"
    assert health.json()["config_error"]


async def test_hot_reload_via_admin_save(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    new_yaml = BASE_CONFIG.replace("foo-real", "foo-real-v2")
    await client.put("/api/admin/config", json={"yaml": new_yaml})

    preview = (
        await client.post("/api/admin/preview", json={"model": "foo", "body": {"model": "foo"}})
    ).json()
    assert preview["upstream_model"] == "foo-real-v2"
