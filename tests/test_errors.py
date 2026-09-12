"""Error taxonomy, auth failures, upstream status preservation and redaction."""

from __future__ import annotations

import httpx

from gateway.errors import (
    ModelDisabledError,
    UnknownModelError,
    UnsupportedFeatureError,
)


async def test_unknown_model_error_shape(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "ghost", "input": "hi"})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "unknown_model"
    assert "ghost" in error["message"]


async def test_unknown_reasoning_level_error(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "foo@bogus", "input": "hi"})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "unknown_reasoning_level"


async def test_disabled_model_error(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "disabled-model", "input": "hi"})
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "model_disabled"


async def test_missing_api_key_returns_clear_error(build_app, client_factory):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream must not be contacted without a key")

    harness = build_app(handler=handler, env_map={})
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "foo", "input": "hi"})
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "provider_auth_error"
    assert "RESPONSES_KEY" in error["message"]
    # no Python traceback leaks
    assert "Traceback" not in response.text
    assert "KeyError" not in response.text


async def test_upstream_status_and_body_preserved(build_app, client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"type": "rate_limit", "message": "slow down"}},
            headers={"content-type": "application/json"},
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "foo", "input": "hi"})
    assert response.status_code == 429
    # upstream JSON body is passed through untouched
    assert response.json() == {"error": {"type": "rate_limit", "message": "slow down"}}


async def test_upstream_non_json_error_preserved(build_app, client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable", headers={"content-type": "text/plain"})

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "foo", "input": "hi"})
    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["details"]["upstream_status"] == 503


async def test_upstream_timeout_mapped_to_504(build_app, client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post("/v1/responses", json={"model": "foo", "input": "hi"})
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "upstream_timeout"


async def test_unsupported_feature_is_explicit(build_app, client_factory):
    """previous_response_id cannot be safely translated to Chat/Anthropic."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not contact upstream")

    harness = build_app(handler=handler)
    client = client_factory(harness.app)

    for model in ("bar", "baz"):
        response = await client.post(
            "/v1/responses",
            json={"model": model, "input": "hi", "previous_response_id": "resp_old"},
        )
        assert response.status_code == 400, model
        assert response.json()["error"]["type"] == "unsupported_feature", model


def test_error_types_exist():
    assert UnknownModelError.error_type == "unknown_model"
    assert ModelDisabledError.error_type == "model_disabled"
    assert UnsupportedFeatureError.error_type == "unsupported_feature"
