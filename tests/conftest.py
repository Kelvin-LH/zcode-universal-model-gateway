"""Shared pytest fixtures.

Upstream providers are always mocked with ``httpx.MockTransport`` — the test
suite never performs a real (paid) API call.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from gateway.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_CONFIG = """
settings:
  reasoning_precedence: alias

providers:
  responses_provider:
    display_name: Responses Provider
    protocol: openai_responses
    base_url: https://responses.example/v1
    api_key_env: RESPONSES_KEY
    headers:
      x-client: zumg
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
  foo:
    display_name: Foo
    provider: responses_provider
    upstream_model: foo-real
    enabled: true
    reasoning:
      supported: [off, low, high, max]
      default: high
      mapping:
        off:
          reasoning:
            effort: none
        low:
          reasoning:
            effort: low
        high:
          reasoning:
            effort: high
        max:
          reasoning:
            effort: max
  bar:
    display_name: Bar
    provider: chat_provider
    upstream_model: bar-real
    enabled: true
  baz:
    display_name: Baz
    provider: anthropic_provider
    upstream_model: claude-real
    enabled: true
  disabled-model:
    display_name: Disabled
    provider: responses_provider
    upstream_model: nope
    enabled: false
"""

ENV = {
    "RESPONSES_KEY": "sk-responses-secret",
    "CHAT_KEY": "sk-chat-secret",
    "ANTHROPIC_KEY": "sk-anthropic-secret",
    "OPENAI_API_KEY": "sk-openai-secret",
}


@pytest.fixture
def base_config() -> str:
    return BASE_CONFIG


@pytest.fixture
def env() -> dict[str, str]:
    return dict(ENV)


class AppHarness:
    def __init__(self, app, path: Path) -> None:
        self.app = app
        self.path = path

    def write_config(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")
        # mtime resolution can be coarse on some filesystems; bump explicitly.
        import os
        import time

        stat = self.path.stat()
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        del stat

    def write_raw(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")


@pytest.fixture
def build_app(tmp_path):
    """Return a factory building an app wired to a MockTransport handler."""
    created = []

    def _build(config_yaml: str = BASE_CONFIG, handler=None, env_map=None):
        path = tmp_path / "config.yaml"
        path.write_text(config_yaml, encoding="utf-8")

        if handler is None:

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"echo": True})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = create_app(str(path), client=upstream, env=env_map if env_map is not None else dict(ENV))
        app.state.config_manager.load_or_default()
        harness = AppHarness(app, path)
        created.append(upstream)
        return harness

    yield _build


@pytest_asyncio.fixture
async def client_factory():
    """Create httpx clients bound to an ASGI app (no lifespan side effects)."""
    clients = []

    def _make(app):
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()


def json_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


def sse_response(chunks, status_code: int = 200) -> httpx.Response:
    """Build a streaming httpx.Response from raw SSE text chunks."""

    async def iterator():
        for chunk in chunks:
            yield chunk.encode("utf-8")

    return httpx.Response(
        status_code,
        headers={"content-type": "text/event-stream"},
        content=iterator(),
    )
