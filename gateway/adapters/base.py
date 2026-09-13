"""Adapter base class, shared HTTP/SSE helpers and the Responses stream builder.

Adapters translate an OpenAI *Responses* request into one upstream protocol
and translate the upstream response back into a Responses-compatible shape.
The reasoning mapping is merged *after* translation so that mapping payloads
land verbatim in the upstream body regardless of protocol.
"""

from __future__ import annotations

import abc
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..config import ProviderConfig
from ..errors import UnsupportedFeatureError
from ..i18n import tr
from ..models import ResolvedModel

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def sse(event: str, data: dict[str, Any]) -> bytes:
    """Encode one Server-Sent Event."""
    payload = {"type": event}
    payload.update(data)
    return (
        f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode()


def sse_raw(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


async def aparse_sse_lines(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str | None, str]]:
    """Async version of :func:`parse_sse_lines` for streaming responses."""
    event: str | None = None
    data_lines: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip(" "))
    if data_lines:
        yield event, "\n".join(data_lines)


def text_from_content(content: Any) -> str:
    """Flatten Responses/Chat message content into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if "text" in part and isinstance(part["text"], str):
                    parts.append(part["text"])
                elif part.get("type") in {"input_text", "output_text", "text"}:
                    parts.append(str(part.get("text", "")))
        return "".join(parts)
    return str(content)


def messages_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalise content into a list of content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    return [{"type": "text", "text": str(content)}]


# --------------------------------------------------------------------------
# Responses stream builder (shared by the Chat and Anthropic adapters)
# --------------------------------------------------------------------------

_REASONING_SUMMARY = "response.reasoning_summary_text"


class ResponsesStreamBuilder:
    """Turn a stream of protocol-agnostic deltas into Responses SSE events."""

    def __init__(self, model: str, created_at: int | None = None) -> None:
        self.model = model
        self.response_id = new_id("resp_")
        self.created_at = created_at or int(time.time())
        self.output_index = 0
        self._order: list[str] = []
        self._text_item: dict[str, Any] | None = None
        self._text_buf: list[str] = []
        self._reasoning_item: dict[str, Any] | None = None
        self._reasoning_buf: list[str] = []
        self._tools: dict[int, dict[str, Any]] = {}
        self._created = False
        self._done = False
        self._usage: dict[str, Any] = {}
        self._status = "completed"

    # -- response object -------------------------------------------------

    def _output_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key in self._order:
            if key == "reasoning" and self._reasoning_item is not None:
                items.append(self._reasoning_item)
            elif key == "text" and self._text_item is not None:
                items.append(self._text_item)
            elif key.startswith("tool:") and int(key.split(":", 1)[1]) in self._tools:
                items.append(self._tools[int(key.split(":", 1)[1])])
        return items

    def _response_obj(self, status: str) -> dict[str, Any]:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": self._output_items() if status != "in_progress" else [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": self._usage or None,
        }

    def set_usage(self, usage: dict[str, Any] | None) -> None:
        if usage:
            self._usage = usage

    @property
    def usage(self) -> dict[str, Any]:
        return self._usage

    def ensure_created(self) -> list[bytes]:
        if self._created:
            return []
        self._created = True
        return [
            sse(
                "response.created",
                {"response": self._response_obj("in_progress")},
            ),
            sse(
                "response.in_progress",
                {"response": self._response_obj("in_progress")},
            ),
        ]

    # -- deltas ----------------------------------------------------------

    def reasoning_delta(self, text: str) -> list[bytes]:
        if not text:
            return []
        out = self.ensure_created()
        if self._reasoning_item is None:
            self._close_text()
            item_id = new_id("rs_")
            self._reasoning_item = {
                "type": "reasoning",
                "id": item_id,
                "summary": [],
            }
            self.output_index = self._next_index()
            self._order.append("reasoning")
            out.append(
                sse(
                    "response.output_item.added",
                    {
                        "output_index": self.output_index,
                        "item": {
                            "type": "reasoning",
                            "id": item_id,
                            "summary": [],
                        },
                    },
                )
            )
        self._reasoning_buf.append(text)
        item = self._reasoning_item
        out.append(
            sse(
                f"{_REASONING_SUMMARY}.delta",
                {
                    "item_id": item["id"],
                    "output_index": self.output_index,
                    "summary_index": 0,
                    "delta": text,
                },
            )
        )
        return out

    def text_delta(self, text: str) -> list[bytes]:
        if not text:
            return []
        out = self.ensure_created()
        if self._text_item is None:
            self._close_reasoning()
            item_id = new_id("msg_")
            self._text_item = {
                "type": "message",
                "id": item_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            self.output_index = self._next_index()
            self._order.append("text")
            out.append(
                sse(
                    "response.output_item.added",
                    {
                        "output_index": self.output_index,
                        "item": {
                            "type": "message",
                            "id": item_id,
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
            )
            out.append(
                sse(
                    "response.content_part.added",
                    {
                        "item_id": item_id,
                        "output_index": self.output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            )
        self._text_buf.append(text)
        item = self._text_item
        out.append(
            sse(
                "response.output_text.delta",
                {
                    "item_id": item["id"],
                    "output_index": self.output_index,
                    "content_index": 0,
                    "delta": text,
                },
            )
        )
        return out

    def tool_start(
        self, index: int, call_id: str | None, name: str | None
    ) -> list[bytes]:
        out = self.ensure_created()
        if index not in self._tools:
            self._close_text()
            self._close_reasoning()
            item_id = new_id("fc_")
            item = {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id or new_id("call_"),
                "name": name or "",
                "arguments": "",
                "status": "in_progress",
            }
            self._tools[index] = item
            self.output_index = self._next_index()
            self._order.append(f"tool:{index}")
            out.append(
                sse(
                    "response.output_item.added",
                    {
                        "output_index": self.output_index,
                        "item": dict(item),
                    },
                )
            )
        else:
            item = self._tools[index]
            if call_id and not item.get("call_id"):
                item["call_id"] = call_id
            if name:
                item["name"] = name
        return out

    def tool_args_delta(self, index: int, text: str) -> list[bytes]:
        if not text:
            return []
        out = self.tool_start(index, None, None)
        item = self._tools[index]
        item["arguments"] = (item.get("arguments") or "") + text
        out.append(
            sse(
                "response.function_call_arguments.delta",
                {
                    "item_id": item["id"],
                    "output_index": self._index_of(f"tool:{index}"),
                    "delta": text,
                },
            )
        )
        return out

    # -- closing ---------------------------------------------------------

    def _next_index(self) -> int:
        # output_index assigned in insertion order
        return len(self._order)

    def _index_of(self, key: str) -> int:
        try:
            return self._order.index(key)
        except ValueError:
            return 0

    def _close_text(self) -> list[bytes]:
        if self._text_item is None:
            return []
        item = self._text_item
        text = "".join(self._text_buf)
        item["status"] = "completed"
        item["content"] = [{"type": "output_text", "text": text, "annotations": []}]
        idx = self._index_of("text")
        out = [
            sse(
                "response.output_text.done",
                {
                    "item_id": item["id"],
                    "output_index": idx,
                    "content_index": 0,
                    "text": text,
                },
            ),
            sse(
                "response.content_part.done",
                {
                    "item_id": item["id"],
                    "output_index": idx,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text, "annotations": []},
                },
            ),
            sse(
                "response.output_item.done",
                {"output_index": idx, "item": dict(item)},
            ),
        ]
        self._text_item = None
        return out

    def _close_reasoning(self) -> list[bytes]:
        if self._reasoning_item is None:
            return []
        item = self._reasoning_item
        text = "".join(self._reasoning_buf)
        item["summary"] = [{"type": "summary_text", "text": text}]
        idx = self._index_of("reasoning")
        out = [
            sse(
                f"{_REASONING_SUMMARY}.done",
                {
                    "item_id": item["id"],
                    "output_index": idx,
                    "summary_index": 0,
                    "text": text,
                },
            ),
            sse(
                "response.output_item.done",
                {"output_index": idx, "item": dict(item)},
            ),
        ]
        self._reasoning_item = None
        return out

    def _close_tools(self) -> list[bytes]:
        out: list[bytes] = []
        for index in sorted(self._tools):
            item = self._tools[index]
            if item.get("status") == "completed":
                continue
            item["status"] = "completed"
            idx = self._index_of(f"tool:{index}")
            out.append(
                sse(
                    "response.function_call_arguments.done",
                    {
                        "item_id": item["id"],
                        "output_index": idx,
                        "arguments": item.get("arguments") or "",
                    },
                )
            )
            out.append(
                sse(
                    "response.output_item.done",
                    {"output_index": idx, "item": dict(item)},
                )
            )
        return out

    def finish(
        self,
        finish_reason: str | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> list[bytes]:
        if self._done:
            return []
        self._done = True
        out = self.ensure_created()
        if usage:
            self._usage = usage

        if error:
            self._status = "failed"
            out.append(
                sse(
                    "response.failed",
                    {
                        "response": {
                            **self._response_obj("failed"),
                            "error": {"code": "upstream_error", "message": error},
                        }
                    },
                )
            )
            return out

        status = "completed"
        if finish_reason in {"length", "max_tokens"}:
            status = "incomplete"
        self._status = status
        out.extend(self._close_text())
        out.extend(self._close_reasoning())
        out.extend(self._close_tools())
        response = self._response_obj(status)
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        event = "response.incomplete" if status == "incomplete" else "response.completed"
        out.append(sse(event, {"response": response}))
        return out

    def fail(self, message: str) -> list[bytes]:
        return self.finish(error=message)


# --------------------------------------------------------------------------
# Adapter base
# --------------------------------------------------------------------------


class Adapter(abc.ABC):
    """Base class for protocol adapters."""

    protocol: str = ""

    # -- request ---------------------------------------------------------

    def build_headers(
        self, provider: ProviderConfig, api_key: str | None
    ) -> dict[str, str]:
        headers = dict(provider.resolved_headers())
        headers["content-type"] = "application/json"
        headers["accept"] = "application/json"
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return headers

    def convert_request(
        self, resolved: ResolvedModel, responses_body: dict[str, Any]
    ) -> dict[str, Any]:
        """Translate a Responses body into the upstream protocol body.

        The mapping/overrides merge happens afterwards in the router, so the
        base body here only reflects the client request.
        """
        return dict(responses_body)

    # -- response --------------------------------------------------------

    def convert_response(
        self,
        resolved: ResolvedModel,
        upstream: dict[str, Any],
        status_code: int,
    ) -> dict[str, Any]:
        """Translate a non-streaming upstream response into Responses shape."""
        return upstream

    def stream_events(
        self,
        resolved: ResolvedModel,
        upstream: Any,
    ) -> AsyncIterator[bytes]:
        """Wrap an upstream stream (``aiter_bytes``/``aiter_lines``) as Responses SSE."""
        return self._stream_passthrough(upstream)

    async def _stream_passthrough(self, upstream: Any) -> AsyncIterator[bytes]:
        async for chunk in upstream.aiter_bytes():
            yield chunk

    # -- shared validation ----------------------------------------------

    @staticmethod
    def reject(requested_body: dict[str, Any], *fields: str) -> None:
        for field in fields:
            if requested_body.get(field) is not None:
                raise UnsupportedFeatureError(
                    tr("adapter.unsupported_param", field=field)
                )


def require_api_key(provider: ProviderConfig, api_key: str | None) -> str:
    from ..errors import ProviderAuthError

    if api_key:
        return api_key
    env_name = provider.api_key_env
    if env_name:
        raise ProviderAuthError(
            tr("auth.env_missing", env=env_name)
        )
    raise ProviderAuthError(
        tr("auth.no_key", provider=provider.display_name)
    )
