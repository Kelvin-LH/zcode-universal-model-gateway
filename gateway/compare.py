"""Reasoning-level comparison engine.

Sends one request body once per reasoning level of a model and reports, per
level, what actually came back: the reasoning text length, the reasoning token
count when the upstream reports one, timings, and the mapping the gateway
injected into the upstream body. This is the data behind the Web UI's
"档位对比" view — a way to check that each ``model@level`` virtual id really
changes upstream behaviour, instead of trusting the configuration.

Results are yielded as each level completes so the UI can render them
progressively; one failing level never cancels the others.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

from .errors import GatewayError, UnknownModelError, UnknownReasoningLevelError
from .router import RequestPlan, Router

#: Reasoning text is handed to the UI for inspection, but a pathological
#: upstream must not turn one comparison into a multi-megabyte payload.
MAX_TEXT_CHARS = 20000

#: Levels of one model run concurrently, capped: providers commonly
#: rate-limit bursts of simultaneous requests.
DEFAULT_CONCURRENCY = 4

_REASONING_DELTA_EVENTS = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
}


def select_levels(
    router: Router, model_id: str, levels: Iterable[Any] | None
) -> list[str]:
    """Validate the requested levels against the model's configured ones.

    ``levels=None`` means "every configured level". Raises a client-visible
    :class:`GatewayError` for unknown models, unknown levels, or a model with
    no reasoning levels at all.
    """
    model = router.config_manager.config.models.get(model_id)
    if model is None:
        raise UnknownModelError(f"未知模型 {model_id!r}")

    reasoning = model.reasoning
    supported = list(reasoning.supported) if reasoning and reasoning.enabled else []

    if levels is None:
        if not supported:
            raise GatewayError(
                f"模型 {model_id!r} 没有配置思考档位，无法对比",
                error_type="no_reasoning_levels",
                status_code=409,
            )
        return supported

    selected: list[str] = []
    for raw in levels:
        name = str(raw).strip()
        if not name or name in selected:
            continue
        if name not in supported:
            raise UnknownReasoningLevelError(
                f"模型 {model_id!r} 不支持思考档位 {name!r}；"
                f"支持的档位：{', '.join(supported) if supported else '（无）'}"
            )
        selected.append(name)
    if not selected:
        raise GatewayError(
            "没有选择任何思考档位",
            error_type="no_reasoning_levels",
            status_code=400,
        )
    return selected


def _empty_metrics() -> dict[str, Any]:
    return {
        "first_reasoning_ms": None,
        "reasoning_tokens": None,
        "reasoning_chars": 0,
        "reasoning_text": "",
        "reasoning_truncated": False,
        "output_chars": 0,
        "output_text": "",
        "output_truncated": False,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "usage_reported": False,
    }


def _apply_usage(metrics: dict[str, Any], usage: Any) -> None:
    """Copy token counts from a Responses ``usage`` object.

    ``reasoning_tokens`` stays ``None`` unless the upstream reports a positive
    value: adapters surface ``0`` both for "thinking was off" and for "this
    upstream does not break reasoning tokens out", and the UI should say
    "未上报" rather than imply a measurement.
    """
    if not isinstance(usage, dict) or not usage:
        return
    metrics["usage_reported"] = True
    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        tokens = details.get("reasoning_tokens")
        if isinstance(tokens, (int, float)) and tokens > 0:
            metrics["reasoning_tokens"] = int(tokens)
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            metrics[key] = int(value)


def _text_from_output_items(payload: dict[str, Any]) -> tuple[str, str]:
    """Return ``(reasoning_text, answer_text)`` from a Responses payload."""
    reasoning: list[str] = []
    answer: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            for summary in item.get("summary") or []:
                if isinstance(summary, dict) and isinstance(summary.get("text"), str):
                    reasoning.append(summary["text"])
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    answer.append(part["text"])
    return "".join(reasoning), "".join(answer)


def _finalise_texts(metrics: dict[str, Any], reasoning: str, answer: str) -> None:
    metrics["reasoning_chars"] = len(reasoning)
    metrics["reasoning_text"] = reasoning[:MAX_TEXT_CHARS]
    metrics["reasoning_truncated"] = len(reasoning) > MAX_TEXT_CHARS
    metrics["output_chars"] = len(answer)
    metrics["output_text"] = answer[:MAX_TEXT_CHARS]
    metrics["output_truncated"] = len(answer) > MAX_TEXT_CHARS


def _collect_payload(payload: Any, status: int) -> tuple[dict[str, Any], bool, str | None]:
    """Collect metrics from a non-streaming converted Responses payload."""
    metrics = _empty_metrics()
    ok = status < 400
    error: str | None = None

    if not isinstance(payload, dict):
        return metrics, ok, error

    err = payload.get("error")
    if isinstance(err, (dict, str)):
        ok = False
        error = err.get("message") if isinstance(err, dict) else str(err)
        return metrics, ok, error

    reasoning, answer = _text_from_output_items(payload)
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        answer = answer or output_text
    _finalise_texts(metrics, reasoning, answer)
    _apply_usage(metrics, payload.get("usage"))
    return metrics, ok, error


class _StreamCollector:
    """Assemble metrics from the gateway's Responses SSE event stream."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.buffer = ""
        self.metrics = _empty_metrics()
        self._reasoning: list[str] = []
        self._answer: list[str] = []
        self.error: str | None = None

    def feed(self, text: str) -> None:
        self.buffer += text.replace("\r\n", "\n")
        while "\n\n" in self.buffer:
            block, self.buffer = self.buffer.split("\n\n", 1)
            self._handle_block(block)

    def _handle_block(self, block: str) -> None:
        data_lines = [
            line[5:].lstrip(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            return
        try:
            event = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return

        etype = event.get("type")
        if etype == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._answer.append(delta)
        elif etype in _REASONING_DELTA_EVENTS:
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                if self.metrics["first_reasoning_ms"] is None:
                    self.metrics["first_reasoning_ms"] = round(
                        (time.perf_counter() - self.started) * 1000, 1
                    )
                self._reasoning.append(delta)
        elif etype in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if not isinstance(response, dict):
                return
            if etype == "response.failed":
                err = response.get("error")
                message = err.get("message") if isinstance(err, dict) else None
                self.error = message or "上游返回 response.failed"
            # A relay that omits deltas may still send the finished text.
            if not self._reasoning or not self._answer:
                reasoning, answer = _text_from_output_items(response)
                if not self._reasoning and reasoning:
                    self._reasoning.append(reasoning)
                if not self._answer and answer:
                    self._answer.append(answer)
            _apply_usage(self.metrics, response.get("usage"))

    def finish(self) -> tuple[dict[str, Any], bool, str | None]:
        _finalise_texts(self.metrics, "".join(self._reasoning), "".join(self._answer))
        error = self.error
        return self.metrics, error is None, error


async def _run_level(
    router: Router,
    model_id: str,
    level: str,
    body: dict[str, Any],
    stream: bool,
) -> dict[str, Any]:
    virtual_model = f"{model_id}@{level}"
    request_body = dict(body or {})
    request_body["model"] = virtual_model
    request_body["stream"] = bool(stream)

    started = time.perf_counter()
    result: dict[str, Any] = {
        "level": level,
        "virtual_model": virtual_model,
        "ok": False,
        "status": 0,
        "error": None,
        "error_type": None,
        "provider": None,
        "protocol": None,
        "upstream_model": None,
        "mapping": {},
        "request_overrides": {},
        "is_default": False,
    }

    try:
        plan = router.plan(virtual_model, request_body)
        resolved = plan.resolved
        result.update(
            provider=resolved.provider_id,
            protocol=resolved.protocol,
            upstream_model=resolved.upstream_model,
            mapping=resolved.mapping,
            request_overrides=resolved.request_overrides,
            is_default=resolved.reasoning_level == resolved.reasoning_default,
        )
        if stream:
            metrics, ok, error = await _execute_stream(router, plan, started)
            result["status"] = 200 if ok else 502
        else:
            payload, status = await router.execute_plan(plan)
            if status:
                result["status"] = status
            metrics, ok, error = _collect_payload(payload, status)
        result.update(metrics)
        result["ok"] = ok
        result["error"] = error
    except GatewayError as exc:
        result["status"] = exc.status_code
        result["error"] = exc.message
        result["error_type"] = exc.error_type
    except Exception as exc:  # pragma: no cover - defensive
        result["status"] = 500
        result["error"] = str(exc)
        result["error_type"] = "internal_error"

    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


async def _execute_stream(
    router: Router, plan: RequestPlan, started: float
) -> tuple[dict[str, Any], bool, str | None]:
    # An OpenAI-compatible upstream only reports token usage mid-stream when
    # asked; the adapter passes this through for openai_chat.
    if plan.resolved.protocol == "openai_chat" and not plan.body.get("stream_options"):
        plan.body["stream_options"] = {"include_usage": True}

    collector = _StreamCollector(started)
    async for chunk in router.stream_plan(plan):
        if isinstance(chunk, (bytes, bytearray)):
            collector.feed(chunk.decode("utf-8", "replace"))
        else:  # pragma: no cover - adapters yield bytes
            collector.feed(str(chunk))
    return collector.finish()


async def run_comparison(
    router: Router,
    model_id: str,
    levels: list[str],
    body: dict[str, Any],
    *,
    stream: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one result dict per level, in completion order."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_level(level: str) -> dict[str, Any]:
        async with semaphore:
            return await _run_level(router, model_id, level, body, stream)

    tasks = [asyncio.create_task(run_level(level)) for level in levels]
    try:
        for completed in asyncio.as_completed(tasks):
            yield await completed
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
