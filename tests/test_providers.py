"""Each provider protocol: URL, auth header, protocol-specific body/response."""

from __future__ import annotations

import httpx

from .conftest import json_body


async def test_responses_provider_uses_bearer_auth(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["x_client"] = request.headers.get("x-client")
        seen["body"] = json_body(request)
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": []})

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    await client.post("/v1/responses", json={"model": "foo", "input": "hi"})
    assert seen["url"] == "https://responses.example/v1/responses"
    assert seen["auth"] == "Bearer sk-responses-secret"
    assert seen["x_client"] == "zumg"


async def test_chat_provider_converts_request_and_response(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1700000001,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            },
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/v1/responses",
        json={
            "model": "bar",
            "instructions": "Be terse.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
            ],
            "max_output_tokens": 128,
            "temperature": 0.3,
        },
    )
    assert seen["url"] == "https://chat.example/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-chat-secret"
    body = seen["body"]
    assert body["model"] == "bar-real"
    assert body["messages"][0] == {"role": "system", "content": "Be terse."}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.3

    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["text"] == "hi there"
    assert payload["output_text"] == "hi there"
    assert payload["usage"]["input_tokens"] == 7
    assert payload["usage"]["output_tokens"] == 3
    assert payload["usage"]["total_tokens"] == 10


async def test_anthropic_provider_converts_request_and_response(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "answer"},
                ],
                "usage": {"input_tokens": 11, "output_tokens": 5},
            },
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/v1/responses",
        json={"model": "baz", "instructions": "System!", "input": "question"},
    )
    assert seen["url"] == "https://anthropic.example/v1/messages"
    assert seen["x_api_key"] == "sk-anthropic-secret"
    assert seen["version"] == "2023-06-01"
    body = seen["body"]
    assert body["model"] == "claude-real"
    assert body["system"] == "System!"
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "question"}]}]
    assert body["max_tokens"] == 4096

    payload = response.json()
    assert payload["object"] == "response"
    types = [item["type"] for item in payload["output"]]
    assert types == ["reasoning", "message"]
    assert payload["output_text"] == "answer"
    assert payload["usage"]["input_tokens"] == 11


async def test_anthropic_tool_roundtrip(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 4},
            },
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/v1/responses",
        json={
            "model": "baz",
            "input": [
                {"role": "user", "content": "read a.py"},
                {"type": "function_call", "call_id": "toolu_0", "name": "read_file", "arguments": '{"path":"a.py"}'},
                {"type": "function_call_output", "call_id": "toolu_0", "output": "print(1)"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )
    body = seen["body"]
    assert body["tools"][0]["name"] == "read_file"
    assert body["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    # assistant tool_use + user tool_result grouping
    assert body["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "read a.py"}]}
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][1]["content"][0]["input"] == {"path": "a.py"}
    assert body["messages"][2]["role"] == "user"
    assert body["messages"][2]["content"][0]["tool_use_id"] == "toolu_0"

    out = response.json()["output"][0]
    assert out["type"] == "function_call"
    assert out["call_id"] == "toolu_1"
    assert out["name"] == "read_file"
    import json

    assert json.loads(out["arguments"]) == {"path": "a.py"}
