"""Adapter registry."""

from __future__ import annotations

from .anthropic_messages import AnthropicMessagesAdapter
from .base import Adapter
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter

_ADAPTERS: dict[str, type[Adapter]] = {
    OpenAIResponsesAdapter.protocol: OpenAIResponsesAdapter,
    OpenAIChatAdapter.protocol: OpenAIChatAdapter,
    AnthropicMessagesAdapter.protocol: AnthropicMessagesAdapter,
}


def get_adapter(protocol: str) -> Adapter:
    try:
        return _ADAPTERS[protocol]()
    except KeyError as exc:  # pragma: no cover - validated at config time
        raise ValueError(f"no adapter registered for protocol {protocol!r}") from exc


def supported_protocols() -> list[str]:
    return list(_ADAPTERS)


__all__ = [
    "Adapter",
    "AnthropicMessagesAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "get_adapter",
    "supported_protocols",
]
