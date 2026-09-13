"""OpenAI Responses -> Chat Completions adapter.

Converts a Responses request into a Chat Completions request and translates
the Chat Completions response (including tool calls and streaming) back into
a Responses-compatible shape.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..errors import AdapterError, UnsupportedFeatureError
from ..i18n import tr
from ..models import ResolvedModel
from .base import (
    Adapter,
    ResponsesStreamBuilder,
    aparse_sse_lines,
    text_from_content,
)

log = logging.getLogger("zumg.adapter.chat")

_FINISH_TO_STATUS = {
    None: "completed",
    "stop": "completed",
    "tool_calls": "completed",
    "function_call": "completed",
    "length": "incomplete",
    "content_filter": "incomplete",
}

# Responses -> Chat field mapping (straight pass-through keys).
_DIRECT_FIELDS = (
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "user",
    "stop",
    "metadata",
)


def _chat_content(content: Any) -> Any:
    """Convert Responses message content into Chat content (str or array)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in {"input_text", "output_text", "text", "summary_text"}:
            blocks.append({"type": "text", "text": str(part.get("text", ""))})
        elif ptype == "refusal":
            blocks.append({"type": "text", "text": str(part.get("refusal", ""))})
        elif ptype in {"input_image", "image_url"}:
            url = part.get("image_url")
            detail = part.get("detail")
            if isinstance(url, dict):
                detail = url.get("detail", detail)
                url = url.get("url")
            if not url:
                raise UnsupportedFeatureError(tr("chat.image_no_url"))
            block = {"type": "image_url", "image_url": {"url": url}}
            if detail:
                block["image_url"]["detail"] = detail
            blocks.append(block)
        else:
            raise UnsupportedFeatureError(
                tr("chat.block_type", type=ptype)
            )

    if not blocks:
        return None
    if all(b["type"] == "text" for b in blocks):
        return "".join(b["text"] for b in blocks)
    return blocks


def _tool_calls_to_chat(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call.get("call_id") or call.get("id") or "",
        "type": "function",
        "function": {
            "name": call.get("name") or "",
            "arguments": call.get("arguments") or "",
        },
    }


def input_to_messages(inp: Any) -> list[dict[str, Any]]:
    """Convert a Responses ``input`` value into Chat messages."""
    messages: list[dict[str, Any]] = []
    if inp is None:
        return messages
    if isinstance(inp, str):
        if inp:
            messages.append({"role": "user", "content": inp})
        return messages
    if not isinstance(inp, list):
        raise AdapterError(tr("adapter.input_not_string_or_array"))

    for item in inp:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            raise AdapterError(tr("adapter.item_not_object"))

        itype = item.get("type")
        if itype == "function_call":
            call = _tool_calls_to_chat(item)
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and messages[-1].get("tool_calls")
            ):
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append(
                    {"role": "assistant", "content": None, "tool_calls": [call]}
                )
            continue
        if itype == "function_call_output":
            output = item.get("output")
            content = output if isinstance(output, str) else json.dumps(output)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "",
                    "content": content,
                }
            )
            continue
        if itype == "reasoning":
            # Chain-of-thought cannot be replayed to Chat Completions.
            continue

        role = item.get("role")
        if role in {"user", "assistant", "system", "developer", "tool"}:
            if role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.get("tool_call_id")
                        or item.get("call_id")
                        or "",
                        "content": text_from_content(item.get("content")),
                    }
                )
                continue
            chat_role = "system" if role == "developer" else role
            messages.append(
                {"role": chat_role, "content": _chat_content(item.get("content"))}
            )
            continue

        raise UnsupportedFeatureError(
            tr("chat.item_type", type=itype)
        )
    return messages


def _tools_to_chat(tools: Any) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise AdapterError(tr("adapter.tool_not_object"))
        ttype = tool.get("type", "function")
        if ttype != "function":
            raise UnsupportedFeatureError(
                tr("chat.tool_type", type=ttype)
            )
        if isinstance(tool.get("function"), dict):
            out.append({"type": "function", "function": tool["function"]})
            continue
        fn: dict[str, Any] = {
            "name": tool.get("name"),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        if tool.get("description") is not None:
            fn["description"] = tool["description"]
        if tool.get("strict") is not None:
            fn["strict"] = tool["strict"]
        out.append({"type": "function", "function": fn})
    return out or None


def _tool_choice_to_chat(choice: Any) -> Any:
    if choice is None or isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = choice.get("name") or (choice.get("function") or {}).get("name")
            if not name:
                raise AdapterError(tr("adapter.tool_choice_no_name"))
            return {"type": "function", "function": {"name": name}}
        raise UnsupportedFeatureError(
            tr("chat.tool_choice_type", type=choice.get("type"))
        )
    raise AdapterError(tr("adapter.invalid_tool_choice"))


def _text_format_to_response_format(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype in {None, "text"}:
        return None
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        json_schema: dict[str, Any] = {
            "name": fmt.get("name") or "response",
            "schema": fmt.get("schema") or {"type": "object"},
        }
        if fmt.get("strict") is not None:
            json_schema["strict"] = fmt["strict"]
        return {"type": "json_schema", "json_schema": json_schema}
    raise UnsupportedFeatureError(
        tr("chat.text_format_type", type=ftype)
    )


def chat_usage_to_responses(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens", prompt + completion) or 0
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": prompt,
        "input_tokens_details": {
            "cached_tokens": (prompt_details or {}).get("cached_tokens", 0)
        },
        "output_tokens": completion,
        "output_tokens_details": {
            "reasoning_tokens": (completion_details or {}).get("reasoning_tokens", 0)
        },
        "total_tokens": total,
    }


class OpenAIChatAdapter(Adapter):
    protocol = "openai_chat"

    # -- request ---------------------------------------------------------

    def convert_request(
        self, resolved: ResolvedModel, responses_body: dict[str, Any]
    ) -> dict[str, Any]:
        body = responses_body
        self.reject(body, "previous_response_id")

        chat: dict[str, Any] = {}
        messages: list[dict[str, Any]] = []
        instructions = body.get("instructions")
        if isinstance(instructions, str) and instructions:
            messages.append({"role": "system", "content": instructions})
        messages.extend(input_to_messages(body.get("input")))
        # Tolerate a client that already speaks Chat.
        if isinstance(body.get("messages"), list):
            messages.extend(body["messages"])
        if not messages:
            raise AdapterError(tr("adapter.no_input_messages"))
        chat["messages"] = messages

        for key in _DIRECT_FIELDS:
            if body.get(key) is not None:
                chat[key] = body[key]

        if body.get("max_output_tokens") is not None:
            chat["max_tokens"] = body["max_output_tokens"]
        if body.get("max_completion_tokens") is not None:
            chat["max_completion_tokens"] = body["max_completion_tokens"]

        tools = _tools_to_chat(body.get("tools"))
        if tools:
            chat["tools"] = tools
        choice = _tool_choice_to_chat(body.get("tool_choice"))
        if choice is not None:
            chat["tool_choice"] = choice
        if body.get("parallel_tool_calls") is not None:
            chat["parallel_tool_calls"] = body["parallel_tool_calls"]

        response_format = _text_format_to_response_format(body.get("text"))
        if response_format:
            chat["response_format"] = response_format

        if body.get("stream"):
            chat["stream"] = True
            if isinstance(body.get("stream_options"), dict):
                chat["stream_options"] = body["stream_options"]

        return chat

    # -- response --------------------------------------------------------

    def convert_response(
        self, resolved: ResolvedModel, upstream: dict[str, Any], status_code: int
    ) -> dict[str, Any]:
        if not isinstance(upstream, dict):
            raise AdapterError(tr("adapter.upstream_not_json_object"))
        if "error" in upstream and "choices" not in upstream:
            return upstream

        choice = (upstream.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")

        output: list[dict[str, Any]] = []
        reasoning_text = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning_text, str) and reasoning_text:
            output.append(
                {
                    "type": "reasoning",
                    "id": f"rs_{abs(hash(reasoning_text)) & 0xFFFFFFFF:x}",
                    "summary": [{"type": "summary_text", "text": reasoning_text}],
                }
            )

        content = message.get("content")
        text = text_from_content(content)
        if text:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{abs(hash(text)) & 0xFFFFFFFF:x}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            )

        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            output.append(
                {
                    "type": "function_call",
                    "id": call.get("id") or "",
                    "call_id": call.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "",
                    "status": "completed",
                }
            )

        status = _FINISH_TO_STATUS.get(finish_reason, "completed")
        response: dict[str, Any] = {
            "id": upstream.get("id") or "",
            "object": "response",
            "created_at": upstream.get("created") or 0,
            "status": status,
            "model": resolved.requested_model,
            "output": output,
            "output_text": text,
            "usage": chat_usage_to_responses(upstream.get("usage")),
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return response

    # -- streaming -------------------------------------------------------

    async def stream_events(
        self, resolved: ResolvedModel, upstream: Any
    ) -> AsyncIterator[bytes]:
        builder = ResponsesStreamBuilder(resolved.requested_model)
        finish_reason: str | None = None
        emitted_any = False
        try:
            async for _event, data in aparse_sse_lines(upstream.aiter_lines()):
                if data is None:
                    continue
                data = data.strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    message = (
                        err.get("message") if isinstance(err, dict) else str(err)
                    )
                    for ev in builder.fail(message or tr("adapter.stream_error")):
                        yield ev
                    return
                if not isinstance(chunk, dict):
                    continue

                usage = chunk.get("usage")
                if usage:
                    builder.set_usage(chat_usage_to_responses(usage))

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}

                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    emitted_any = True
                    for ev in builder.reasoning_delta(text_from_content(reasoning)):
                        yield ev

                text = text_from_content(delta.get("content"))
                if text:
                    emitted_any = True
                    for ev in builder.text_delta(text):
                        yield ev

                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    fn = call.get("function") or {}
                    if call.get("id") or fn.get("name"):
                        emitted_any = True
                        for ev in builder.tool_start(
                            index, call.get("id"), fn.get("name")
                        ):
                            yield ev
                    if fn.get("arguments"):
                        emitted_any = True
                        for ev in builder.tool_args_delta(index, fn["arguments"]):
                            yield ev

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

            if not emitted_any:
                # Still emit a well-formed (empty) response sequence.
                for ev in builder.ensure_created():
                    yield ev
            for ev in builder.finish(finish_reason):
                yield ev
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("chat stream translation failed: %s", exc)
            for ev in builder.fail(tr("adapter.stream_translate_failed", error=exc)):
                yield ev
