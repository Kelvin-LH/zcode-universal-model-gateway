"""Reasoning-level comparison: /api/admin/compare NDJSON stream."""

from __future__ import annotations

import json

import httpx

from .conftest import json_body, sse_response

CHAT_WITH_LEVELS = """
providers:
  chat_provider:
    display_name: Chat Provider
    protocol: openai_chat
    base_url: https://chat.example/v1
    api_key_env: CHAT_KEY

  anthropic_provider:
    display_name: Anthropic Provider
    protocol: anthropic_messages
    base_url: https://anthropic.example
    api_key_env: ANTHROPIC_KEY

models:
  thinker:
    display_name: Thinker
    provider: chat_provider
    upstream_model: thinker-real
    reasoning:
      supported: [off, low, max]
      default: low
      mapping:
        off:
          reasoning_effort: none
        low:
          reasoning_effort: low
        max:
          reasoning_effort: xhigh
"""


def parse_ndjson(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _responses_payload(effort: str, reasoning_len: int, answer: str = "答案") -> dict:
    return {
        "id": "resp_cmp",
        "object": "response",
        "status": "completed",
        "model": "foo-real",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "思" * reasoning_len}],
            },
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer, "annotations": []}],
            },
        ],
        "output_text": answer,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 100 + reasoning_len,
            "output_tokens_details": {"reasoning_tokens": reasoning_len},
            "total_tokens": 110 + reasoning_len,
        },
    }


async def test_compare_runs_every_level_and_reports_reasoning_metrics(
    build_app, client_factory
):
    """One prompt x all levels; reasoning length/tokens reported per level."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        seen.append(body)
        effort = body.get("reasoning", {}).get("effort")
        length = {"none": 1, "low": 4, "high": 16, "max": 64}[effort]
        return httpx.Response(200, json=_responses_payload(effort, length))

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/compare",
        json={"model": "foo", "body": {"input": "讲个笑话"}},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    events = parse_ndjson(response.text)
    assert events[0]["type"] == "started"
    assert events[0]["levels"] == ["off", "low", "high", "max"]
    results = [e for e in events if e["type"] == "result"]
    assert {r["level"] for r in results} == {"off", "low", "high", "max"}
    assert events[-1]["type"] == "done"
    assert events[-1]["count"] == 4

    by_level = {r["level"]: r for r in results}
    for level, effort in (("off", "none"), ("high", "high"), ("max", "max")):
        result = by_level[level]
        assert result["ok"] is True
        assert result["status"] == 200
        assert result["virtual_model"] == f"foo@{level}"
        assert result["upstream_model"] == "foo-real"
        assert result["protocol"] == "openai_responses"
        assert result["provider"] == "responses_provider"
        # the level's own mapping is what the UI shows as evidence
        assert result["mapping"] == {"reasoning": {"effort": effort}}
        assert result["reasoning_tokens"] == {"none": 1, "low": 4, "high": 16, "max": 64}[effort]
        assert result["reasoning_chars"] == {
            "off": 1,
            "low": 4,
            "high": 16,
            "max": 64,
        }[level]
        assert result["output_chars"] == len("答案")
        assert result["output_text"] == "答案"
        assert result["reasoning_text"]
        assert result["elapsed_ms"] >= 0

    assert by_level["high"]["is_default"] is True
    assert by_level["off"]["is_default"] is False

    # Each level reached the upstream as its own virtual model, same question.
    upstream_models = {b["model"] for b in seen}
    assert upstream_models == {"foo-real"}
    assert all(b["input"] == "讲个笑话" for b in seen)
    efforts = sorted(b["reasoning"]["effort"] for b in seen)
    assert efforts == ["high", "low", "max", "none"]


async def test_compare_accepts_virtual_model_and_level_subset(build_app, client_factory):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        seen.append(body["reasoning"]["effort"])
        return httpx.Response(200, json=_responses_payload("x", 2))

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/compare",
        json={
            "model": "foo@max",  # alias accepted; comparison still spans levels
            "levels": ["low", "max", "low"],  # duplicates collapse
            "body": {"input": "hi"},
        },
    )
    events = parse_ndjson(response.text)
    assert events[0]["levels"] == ["low", "max"]
    assert sorted(seen) == ["low", "max"]


async def test_compare_validation_errors(build_app, client_factory):
    harness = build_app()
    client = client_factory(harness.app)

    missing = await client.post("/api/admin/compare", json={"body": {"input": "x"}})
    assert missing.status_code == 400

    unknown_model = await client.post(
        "/api/admin/compare", json={"model": "nope", "body": {"input": "x"}}
    )
    assert unknown_model.status_code == 404

    unknown_level = await client.post(
        "/api/admin/compare",
        json={"model": "foo", "levels": ["ultra"], "body": {"input": "x"}},
    )
    assert unknown_level.status_code == 400

    no_levels = await client.post(
        "/api/admin/compare", json={"model": "bar", "body": {"input": "x"}}
    )
    assert no_levels.status_code == 409

    bad_body = await client.post(
        "/api/admin/compare", json={"model": "foo", "body": "not-an-object"}
    )
    assert bad_body.status_code == 400


async def test_compare_one_failed_level_does_not_cancel_others(
    build_app, client_factory
):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        if body["reasoning"]["effort"] == "none":
            return httpx.Response(
                400,
                json={"error": {"message": "thinking 不支持关闭", "type": "bad_request"}},
            )
        return httpx.Response(200, json=_responses_payload("x", 3))

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/compare", json={"model": "foo", "body": {"input": "hi"}}
    )
    events = parse_ndjson(response.text)
    results = {e["level"]: e for e in events if e["type"] == "result"}
    assert len(results) == 4
    assert results["off"]["ok"] is False
    assert results["off"]["status"] == 400
    assert "不支持关闭" in results["off"]["error"]
    assert results["low"]["ok"] is True
    assert events[-1]["count"] == 4


async def test_compare_streaming_for_chat_provider(build_app, client_factory):
    """Streaming path: reasoning deltas, inline usage and include_usage flag."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        seen.append(body)
        effort = body.get("reasoning_effort")
        think = "think-" * (10 if effort == "xhigh" else 2)
        usage_chunk = (
            'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":5,'
            '"completion_tokens":7,"total_tokens":12,'
            '"completion_tokens_details":{"reasoning_tokens":3}}}\n\n'
        )
        chunks = [
            'data: {"id":"c1","choices":[{"index":0,"delta":{"reasoning_content":"'
            + think[:6]
            + '"}}]}\n\n',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"reasoning_content":"'
            + think[6:]
            + '"}}]}\n\n',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"答"}}]}\n\n',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"案"}}]}\n\n',
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            usage_chunk,
            "data: [DONE]\n\n",
        ]
        return sse_response(chunks)

    harness = build_app(config_yaml=CHAT_WITH_LEVELS, handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/compare",
        json={"model": "thinker", "body": {"input": "hi"}, "stream": True},
    )
    events = parse_ndjson(response.text)
    results = {e["level"]: e for e in events if e["type"] == "result"}
    assert set(results) == {"off", "low", "max"}

    # Chat levels use the chat mapping field, not a nested reasoning object.
    assert sorted(b.get("reasoning_effort") for b in seen) == ["low", "none", "xhigh"]
    assert all(b["stream"] is True for b in seen)
    assert all(b.get("stream_options") == {"include_usage": True} for b in seen)

    smart = results["max"]
    assert smart["ok"] is True
    assert smart["reasoning_chars"] == len("think-" * 10)
    assert smart["reasoning_tokens"] == 3
    assert smart["output_chars"] == 2
    assert smart["output_text"] == "答案"
    assert smart["elapsed_ms"] >= 0


async def test_compare_streaming_without_reasoning_usage_reports_null(
    build_app, client_factory
):
    """No usage in the stream => tokens are 'not reported', not zero."""

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        return sse_response(chunks)

    harness = build_app(config_yaml=CHAT_WITH_LEVELS, handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/compare",
        json={"model": "thinker", "levels": ["low"], "body": {"input": "hi"}, "stream": True},
    )
    result = next(e for e in parse_ndjson(response.text) if e["type"] == "result")
    assert result["ok"] is True
    assert result["reasoning_tokens"] is None
    assert result["reasoning_chars"] == 0
    assert result["usage_reported"] is False


async def test_compare_requires_no_secrets_in_response(build_app, client_factory):
    """The comparison report must never leak an API key."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_responses_payload("low", 2))

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/api/admin/compare",
        json={"model": "foo", "levels": ["low"], "body": {"input": "hi"}},
    )
    assert "sk-responses-secret" not in response.text
