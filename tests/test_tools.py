"""Tool calling must survive translation, especially Responses -> Responses."""

from __future__ import annotations

import json

import httpx

from .conftest import json_body, sse_response

TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
]


async def test_responses_tools_not_corrupted(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": '{"path":"a.py"}',
                    }
                ],
            },
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    payload_in = {
        "model": "foo@high",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "read a.py"}]}
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    response = await client.post("/v1/responses", json=payload_in)
    assert response.status_code == 200

    body = seen["body"]
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True
    assert body["input"] == payload_in["input"]

    out = response.json()["output"][0]
    assert out["type"] == "function_call"
    assert out["name"] == "read_file"
    assert json.loads(out["arguments"]) == {"path": "a.py"}


async def test_responses_tool_result_and_call_roundtrip(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_body(request)
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": []})

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    items = [
        {"role": "user", "content": "read a.py"},
        {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"a.py"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "print(1)"},
    ]
    await client.post("/v1/responses", json={"model": "foo", "input": items, "tools": TOOLS})
    assert seen["body"]["input"] == items


async def test_chat_tools_translation(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"b.py"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    response = await client.post(
        "/v1/responses",
        json={
            "model": "bar",
            "input": "read b.py",
            "tools": TOOLS,
            "tool_choice": {"type": "function", "name": "read_file"},
            "parallel_tool_calls": False,
        },
    )
    assert response.status_code == 200
    sent = seen["body"]
    assert sent["tools"][0]["function"]["name"] == "read_file"
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}
    assert sent["parallel_tool_calls"] is False

    out = response.json()["output"][0]
    assert out["type"] == "function_call"
    assert out["call_id"] == "call_9"
    assert out["name"] == "read_file"
    assert json.loads(out["arguments"]) == {"path": "b.py"}
    assert response.json()["usage"]["input_tokens"] == 10


async def test_chat_function_call_and_output_input_conversion(build_app, client_factory):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json_body(request)
        return httpx.Response(
            200,
            json={"id": "c", "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
        )

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    await client.post(
        "/v1/responses",
        json={
            "model": "bar",
            "input": [
                {"role": "user", "content": "read a.py"},
                {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"a.py"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "print(1)"},
            ],
        },
    )
    messages = seen["body"]["messages"]
    assert messages[0] == {"role": "user", "content": "read a.py"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["id"] == "call_1"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "print(1)",
    }


async def test_chat_stream_tool_call_deltas(build_app, client_factory):
    chunks = [
        'data: {"id":"c","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_file","arguments":""}}]}}]}\n\n',
        'data: {"id":"c","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":"}}]}}]}\n\n',
        'data: {"id":"c","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"a.py\\"}"}}]}}]}\n\n',
        'data: {"id":"c","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        "data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(chunks)

    harness = build_app(handler=handler)
    client = client_factory(harness.app)
    async with client.stream(
        "POST", "/v1/responses", json={"model": "bar", "input": "x", "stream": True, "tools": TOOLS}
    ) as response:
        text = "".join([chunk async for chunk in response.aiter_text()])

    assert "response.output_item.added" in text
    assert '"function_call"' in text
    assert "response.function_call_arguments.delta" in text
    assert "response.function_call_arguments.done" in text
    assert "a.py" in text
    # arguments are reassembled intact in the closing event
    done = [
        json.loads(line[5:].strip())
        for block in text.split("\n\n")
        for line in block.split("\n")
        if line.startswith("data:") and "function_call_arguments.done" in line
    ]
    assert json.loads(done[0]["arguments"]) == {"path": "a.py"}
