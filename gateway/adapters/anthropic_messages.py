"""OpenAI Responses -> Anthropic Messages adapter.

Handles system instructions, user/assistant messages, tool definitions, tool
calls/results, thinking blocks and streaming. Responses-only features that
cannot be translated losslessly (e.g. ``previous_response_id``, ``store``)
raise :class:`UnsupportedFeatureError` rather than silently changing meaning.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..errors import AdapterError, UnsupportedFeatureError
from ..models import ResolvedModel
from .base import (
    Adapter,
    ResponsesStreamBuilder,
    aparse_sse_lines,
    text_from_content,
)

log = logging.getLogger("zumg.adapter.anthropic")

# Responses -> Anthropic direct field mapping.
_DIRECT_FIELDS = ("temperature", "top_p", "top_k", "metadata")


def _anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

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
        elif ptype in {"input_image", "image_url"}:
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url:
                raise UnsupportedFeatureError(
                    "缺少 URL 的图片内容无法转换到 Anthropic Messages"
                )
            if url.startswith("data:"):
                try:
                    header, b64 = url.split(",", 1)
                    media_type = header.split(";", 1)[0].split(":", 1)[1]
                except (ValueError, IndexError) as exc:
                    raise UnsupportedFeatureError(
                        "图片内容中的 data URL 格式不正确"
                    ) from exc
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    }
                )
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        elif ptype == "refusal":
            blocks.append({"type": "text", "text": str(part.get("refusal", ""))})
        else:
            raise UnsupportedFeatureError(
                f"内容块类型 {ptype!r} 无法转换到 Anthropic Messages"
            )
    return blocks


def input_to_messages(inp: Any) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(system_text, messages)`` from a Responses ``input`` value."""
    if inp is None:
        return "", []
    if isinstance(inp, str):
        return "", (
            [{"role": "user", "content": [{"type": "text", "text": inp}]}] if inp else []
        )
    if not isinstance(inp, list):
        raise AdapterError("'input' 必须是字符串或输入项数组")

    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for item in inp:
        if isinstance(item, str):
            flush_tool_results()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            raise AdapterError("输入项必须是对象")
        itype = item.get("type")

        if itype == "function_call":
            flush_tool_results()
            block = {
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or "",
                "name": item.get("name") or "",
                "input": _parse_arguments(item.get("arguments")),
            }
            if (
                messages
                and messages[-1]["role"] == "assistant"
                and isinstance(messages[-1]["content"], list)
            ):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "assistant", "content": [block]})
            continue

        if itype == "function_call_output":
            output = item.get("output")
            content = output if isinstance(output, str) else json.dumps(output)
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": item.get("call_id") or item.get("id") or "",
                    "content": content,
                }
            )
            continue

        if itype == "reasoning":
            continue

        role = item.get("role")
        if role == "system" or role == "developer":
            flush_tool_results()
            system_parts.append(text_from_content(item.get("content")))
            continue
        if role in {"user", "assistant"}:
            flush_tool_results()
            messages.append(
                {
                    "role": role,
                    "content": _anthropic_content_blocks(item.get("content")),
                }
            )
            continue
        raise UnsupportedFeatureError(
            f"输入项类型 {itype!r} / 角色 {role!r} 无法转换到 Anthropic Messages"
        )

    flush_tool_results()
    return "\n\n".join(p for p in system_parts if p), messages


def _parse_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise UnsupportedFeatureError(
                "工具调用参数不是合法 JSON，无法转换到 Anthropic Messages"
            ) from exc
        return parsed
    return arguments


def _tools_to_anthropic(tools: Any) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise AdapterError("每个 tool 必须是对象")
        ttype = tool.get("type", "function")
        if ttype != "function":
            raise UnsupportedFeatureError(
                f"tool 类型 {ttype!r} 无法转换到 Anthropic Messages"
            )
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        entry: dict[str, Any] = {"name": fn.get("name")}
        if fn.get("description") is not None:
            entry["description"] = fn["description"]
        entry["input_schema"] = fn.get("parameters") or {
            "type": "object",
            "properties": {},
        }
        out.append(entry)
    return out or None


def _tool_choice_to_anthropic(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        mapping = {
            "auto": {"type": "auto"},
            "none": {"type": "none"},
            "required": {"type": "any"},
        }
        if choice not in mapping:
            raise AdapterError(f"未知的 tool_choice {choice!r}")
        return mapping[choice]
    if isinstance(choice, dict):
        ctype = choice.get("type")
        if ctype == "function":
            name = choice.get("name") or (choice.get("function") or {}).get("name")
            if not name:
                raise AdapterError("tool_choice 的 function 缺少 name")
            return {"type": "tool", "name": name}
        if ctype in {"auto", "none", "any"}:
            return {"type": ctype}
        raise UnsupportedFeatureError(
            f"tool_choice 类型 {ctype!r} 无法转换到 Anthropic Messages"
        )
    raise AdapterError("无效的 tool_choice")


def anthropic_usage_to_responses(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": usage.get("cache_read_input_tokens", 0) or 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": prompt + completion,
    }


_STOP_REASON_MAP = {
    "end_turn": "completed",
    "stop_sequence": "completed",
    "tool_use": "completed",
    "max_tokens": "incomplete",
    "pause_turn": "completed",
    "refusal": "incomplete",
}


class AnthropicMessagesAdapter(Adapter):
    protocol = "anthropic_messages"

    def build_headers(self, provider, api_key: str | None) -> dict[str, str]:
        headers = dict(provider.resolved_headers())
        headers["content-type"] = "application/json"
        headers["accept"] = "application/json"
        headers.setdefault("anthropic-version", "2023-06-01")
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    # -- request ---------------------------------------------------------

    def convert_request(
        self, resolved: ResolvedModel, responses_body: dict[str, Any]
    ) -> dict[str, Any]:
        body = responses_body
        self.reject(body, "previous_response_id", "store", "include")

        system, messages = input_to_messages(body.get("input"))
        instructions = body.get("instructions")
        if isinstance(instructions, str) and instructions:
            system = f"{instructions}\n\n{system}" if system else instructions
        if not messages:
            raise AdapterError("请求中没有任何输入消息")

        anthropic: dict[str, Any] = {"messages": messages}
        if system:
            anthropic["system"] = system

        for key in _DIRECT_FIELDS:
            if body.get(key) is not None:
                anthropic[key] = body[key]

        if body.get("max_output_tokens") is not None:
            anthropic["max_tokens"] = body["max_output_tokens"]
        else:
            anthropic["max_tokens"] = 4096

        tools = _tools_to_anthropic(body.get("tools"))
        if tools:
            anthropic["tools"] = tools
        choice = _tool_choice_to_anthropic(body.get("tool_choice"))
        if body.get("parallel_tool_calls") is False:
            choice = dict(choice or {"type": "auto"})
            choice["disable_parallel_tool_use"] = True
        if choice is not None:
            anthropic["tool_choice"] = choice

        if body.get("stream"):
            anthropic["stream"] = True

        return anthropic

    # -- response --------------------------------------------------------

    def convert_response(self, resolved, upstream: dict[str, Any], status_code: int):
        if not isinstance(upstream, dict):
            raise AdapterError("上游返回的响应不是 JSON 对象")
        if upstream.get("type") == "error":
            return upstream

        output: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for block in upstream.get("content") or []:
            btype = block.get("type")
            if btype == "thinking":
                output.append(
                    {
                        "type": "reasoning",
                        "id": block.get("id") or "",
                        "summary": [
                            {"type": "summary_text", "text": block.get("thinking", "")}
                        ],
                    }
                )
            elif btype == "text":
                text_parts.append(block.get("text", ""))
                output.append(
                    {
                        "type": "message",
                        "id": upstream.get("id") or "",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": block.get("text", ""),
                                "annotations": [],
                            }
                        ],
                    }
                )
            elif btype == "tool_use":
                output.append(
                    {
                        "type": "function_call",
                        "id": block.get("id") or "",
                        "call_id": block.get("id") or "",
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}),
                        "status": "completed",
                    }
                )

        status = _STOP_REASON_MAP.get(upstream.get("stop_reason"), "completed")
        response: dict[str, Any] = {
            "id": upstream.get("id") or "",
            "object": "response",
            "created_at": 0,
            "status": status,
            "model": resolved.requested_model,
            "output": output,
            "output_text": "".join(text_parts),
            "usage": anthropic_usage_to_responses(upstream.get("usage")),
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return response

    # -- streaming -------------------------------------------------------

    async def stream_events(
        self, resolved: ResolvedModel, upstream: Any
    ) -> AsyncIterator[bytes]:
        builder = ResponsesStreamBuilder(resolved.requested_model)
        blocks: dict[int, dict[str, Any]] = {}
        stop_reason: str | None = None

        try:
            async for _event, data in aparse_sse_lines(upstream.aiter_lines()):
                if not data:
                    continue
                data = data.strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                ctype = chunk.get("type")

                if ctype == "error":
                    err = chunk.get("error") or {}
                    message = err.get("message") if isinstance(err, dict) else str(err)
                    for ev in builder.fail(message or "上游流式响应出错"):
                        yield ev
                    return

                if ctype == "message_start":
                    usage = (chunk.get("message") or {}).get("usage")
                    builder.set_usage(anthropic_usage_to_responses(usage))
                    for ev in builder.ensure_created():
                        yield ev
                elif ctype == "content_block_start":
                    index = chunk.get("index", 0)
                    block = chunk.get("content_block") or {}
                    blocks[index] = block
                    if block.get("type") == "tool_use":
                        for ev in builder.tool_start(
                            index, block.get("id"), block.get("name")
                        ):
                            yield ev
                elif ctype == "content_block_delta":
                    index = chunk.get("index", 0)
                    delta = chunk.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        for ev in builder.text_delta(delta.get("text", "")):
                            yield ev
                    elif dtype == "thinking_delta":
                        for ev in builder.reasoning_delta(delta.get("thinking", "")):
                            yield ev
                    elif dtype == "input_json_delta":
                        for ev in builder.tool_args_delta(
                            index, delta.get("partial_json", "")
                        ):
                            yield ev
                elif ctype == "message_delta":
                    delta = chunk.get("delta") or {}
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    usage = chunk.get("usage")
                    if usage:
                        existing = dict(builder.usage)
                        update = anthropic_usage_to_responses(usage)
                        if not update.get("input_tokens") and existing:
                            update["input_tokens"] = existing.get("input_tokens", 0)
                        if existing.get("input_tokens_details"):
                            update["input_tokens_details"] = existing[
                                "input_tokens_details"
                            ]
                        update["total_tokens"] = update.get(
                            "input_tokens", 0
                        ) + update.get("output_tokens", 0)
                        builder.set_usage(update)
                elif ctype == "message_stop":
                    break

            finish = _STOP_REASON_MAP.get(stop_reason, "completed")
            for ev in builder.finish(finish):
                yield ev
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("anthropic stream translation failed: %s", exc)
            for ev in builder.fail(f"流式转换失败：{exc}"):
                yield ev
