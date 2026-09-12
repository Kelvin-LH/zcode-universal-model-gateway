"""Responses -> Responses passthrough (non-streaming) and request shaping."""

from __future__ import annotations

import httpx

from .conftest import json_body


async def test_responses_passthrough_rewrites_model_and_merges_reasoning(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "object": "response",
                "status": "completed",
                "model": "foo-real",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/v1/responses",
        json={
            "model": "foo@max",
            "input": "Fix all failing tests.",
            "temperature": 0.4,
            "metadata": {"trace": "abc"},
            "unknown_future_field": {"keep": True},
        },
    )
    assert response.status_code == 200
    assert seen["url"] == "https://responses.example/v1/responses"
    assert seen["headers"]["authorization"] == "Bearer sk-responses-secret"
    assert seen["headers"]["x-client"] == "zumg"

    body = seen["body"]
    assert body["model"] == "foo-real"
    assert body["input"] == "Fix all failing tests."
    assert body["temperature"] == 0.4
    assert body["metadata"] == {"trace": "abc"}
    # unknown fields are preserved (minimal-change principle)
    assert body["unknown_future_field"] == {"keep": True}
    # reasoning mapping wins over the alias
    assert body["reasoning"] == {"effort": "max"}

    # response is returned unchanged
    payload = response.json()
    assert payload["id"] == "resp_123"
    assert payload["output"][0]["content"][0]["text"] == "hello"


async def test_responses_uses_provider_default_when_no_alias(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_body(request)
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": []})

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    await client.post("/v1/responses", json={"model": "foo", "input": "hi"})
    assert seen["body"]["model"] == "foo-real"
    assert seen["body"]["reasoning"] == {"effort": "high"}


async def test_models_endpoint_lists_virtual_models(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    response = await client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    ids = [m["id"] for m in payload["data"]]
    assert "foo" in ids and "foo@max" in ids and "bar" in ids
    assert "disabled-model" not in ids


async def test_get_single_model(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    ok = await client.get("/v1/models/foo@high")
    assert ok.status_code == 200
    missing = await client.get("/v1/models/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "unknown_model"


async def test_healthz(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    response = await client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["config"] == "valid"
