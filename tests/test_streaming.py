"""Streaming: Responses passthrough and Chat/Anthropic conversion."""

from __future__ import annotations

import json

import httpx

from .conftest import json_body, sse_response

RESPONSES_SSE = [
    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}\n\n',
    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hel"}\n\n',
    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"lo"}\n\n',
    'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n',
]


def _parse(resp_text: str) -> list[tuple[str, dict]]:
    events = []
    for block in resp_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data is not None:
            events.append((event, json.loads(data)))
    return events


async def test_responses_stream_is_passthrough(build_app, client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(RESPONSES_SSE)

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    async with client.stream(
        "POST", "/v1/responses", json={"model": "foo@max", "input": "hi", "stream": True}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join([chunk async for chunk in response.aiter_text()])

    # Byte content is preserved exactly.
    assert text == "".join(RESPONSES_SSE)


async def test_chat_stream_converted_to_responses_events(build_app, client_factory):
    chunks = [
        'data: {"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
        'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        assert body["stream"] is True
        assert body["model"] == "bar-real"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        return sse_response(chunks)

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    async with client.stream(
        "POST", "/v1/responses", json={"model": "bar", "input": "hi", "stream": True}
    ) as response:
        text = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse(text)
    names = [e[0] for e in events]
    assert "response.created" in names
    assert "response.output_text.delta" in names
    assert names[-1] == "response.completed"
    deltas = [d["delta"] for n, d in events if n == "response.output_text.delta"]
    assert "".join(deltas) == "Hello"
    completed = next(d for n, d in events if n == "response.completed")
    assert completed["response"]["status"] == "completed"


async def test_anthropic_stream_converted_to_responses_events(build_app, client_factory):
    chunks = [
        'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","usage":{"input_tokens":5,"output_tokens":0}}}\n\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        assert body["model"] == "claude-real"
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert request.headers["x-api-key"] == "sk-anthropic-secret"
        return sse_response(chunks)

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    async with client.stream(
        "POST", "/v1/responses", json={"model": "baz", "input": "hi", "stream": True}
    ) as response:
        text = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse(text)
    deltas = [d["delta"] for n, d in events if n == "response.output_text.delta"]
    assert "".join(deltas) == "Hi"
    completed = [d for n, d in events if n == "response.completed"]
    assert completed
    assert completed[0]["response"]["usage"]["input_tokens"] == 5
    assert completed[0]["response"]["usage"]["output_tokens"] == 2


async def test_stream_error_before_response_is_http_error(build_app, client_factory):
    """A missing API key must not produce a half-open stream."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream must not be contacted")

    harness = build_app(handler=handler, env_map={})
    client = client_factory(harness.app)
    response = await client.post(
        "/v1/responses", json={"model": "foo@max", "input": "hi", "stream": True}
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "provider_auth_error"
