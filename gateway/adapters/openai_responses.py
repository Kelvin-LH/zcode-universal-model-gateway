"""OpenAI Responses -> OpenAI Responses passthrough adapter.

This is the highest-priority protocol and must stay as close to a transparent
proxy as possible: only ``model``, the reasoning mapping, overrides and
removed fields are touched. Streaming is a raw byte passthrough.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .base import Adapter


class OpenAIResponsesAdapter(Adapter):
    protocol = "openai_responses"

    def convert_request(
        self, resolved, responses_body: dict[str, Any]
    ) -> dict[str, Any]:
        # Structure is preserved verbatim; the router only rewrites the model
        # and merges the configured mapping/overrides.
        return dict(responses_body)

    def convert_response(self, resolved, upstream: dict[str, Any], status_code: int):
        # Preserve the upstream payload as-is.
        return upstream

    async def _stream_passthrough(self, upstream: Any) -> AsyncIterator[bytes]:
        async for chunk in upstream.aiter_bytes():
            yield chunk
