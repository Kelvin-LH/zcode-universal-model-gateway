"""Request routing: config -> provider -> upstream HTTP -> Responses.

The router is transport-oriented and adapter-agnostic. It resolves a logical
model, delegates request/response translation to the protocol adapter,
applies reasoning mappings/overrides, performs the upstream call (streaming
or not) and records metrics. Upstream HTTP errors keep their original status,
body and content type.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .adapters import get_adapter
from .adapters.base import require_api_key
from .config import ConfigManager, ProviderConfig
from .errors import GatewayError, UpstreamError, UpstreamTimeoutError
from .metrics import Metrics, RequestRecord
from .models import ResolvedModel, resolve_model
from .secrets import SecretStore

log = logging.getLogger("zumg.router")


def _redact(value: Any) -> Any:
    """Recursively blank anything that looks like a credential."""
    sensitive = {
        "authorization",
        "x-api-key",
        "api_key",
        "apikey",
        "api-key",
        "key",
        "secret",
        "token",
        "password",
        "access_token",
        "refresh_token",
        "temporary_key",
    }
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower().replace("-", "_") in sensitive:
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


@dataclass
class RequestPlan:
    """Everything needed to execute (and preview) an upstream request."""

    resolved: ResolvedModel
    url: str
    body: dict[str, Any]
    headers: dict[str, Any]
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def preview(self) -> dict[str, Any]:
        """A redacted, client-safe description for the Test Console."""
        return {
            "request_id": self.request_id,
            "logical_model": self.resolved.logical_id,
            "requested_model": self.resolved.requested_model,
            "reasoning_level": self.resolved.reasoning_level,
            "reasoning_default": self.resolved.reasoning_default,
            "provider": self.resolved.provider_id,
            "protocol": self.resolved.protocol,
            "upstream_url": self.url,
            "upstream_model": self.resolved.upstream_model,
            "headers": _redact(self.headers),
            "body": _redact(self.body),
        }


class Router:
    """Executes gateway requests against upstream providers."""

    def __init__(
        self,
        config_manager: ConfigManager,
        secrets: SecretStore,
        metrics: Metrics,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.secrets = secrets
        self.metrics = metrics
        self._client = client
        self._owns_client = client is None

    async def startup(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True)

    async def shutdown(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("router not started")
        return self._client

    # -- planning --------------------------------------------------------

    def plan(self, requested_model: str, body: dict[str, Any]) -> RequestPlan:
        """Resolve a request into a concrete upstream plan."""
        self.config_manager.maybe_reload()
        config = self.config_manager.config
        resolved = resolve_model(config, requested_model)
        adapter = get_adapter(resolved.protocol)

        api_key = self.secrets.get(resolved.provider_id, resolved.provider.api_key_env)
        headers = adapter.build_headers(resolved.provider, api_key)

        translated = adapter.convert_request(resolved, body)
        precedence = config.settings.reasoning_precedence
        effective = resolved.effective_body(translated, precedence)

        return RequestPlan(
            resolved=resolved,
            url=resolved.target_url,
            body=effective,
            headers=headers,
        )

    def preview(self, requested_model: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.plan(requested_model, body).preview()

    def require_key(self, plan: RequestPlan) -> str:
        """Ensure the provider has a usable API key before any I/O starts."""
        resolved = plan.resolved
        api_key = self.secrets.get(resolved.provider_id, resolved.provider.api_key_env)
        return require_api_key(resolved.provider, api_key)

    # -- execution -------------------------------------------------------

    async def execute(
        self, requested_model: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], RequestPlan, int]:
        """Run a non-streaming request. Returns (responses_body, plan, status)."""
        plan = self.plan(requested_model, body)
        payload, status = await self.execute_plan(plan)
        return payload, plan, status

    async def execute_plan(
        self, plan: RequestPlan
    ) -> tuple[dict[str, Any], int]:
        resolved = plan.resolved
        adapter = get_adapter(resolved.protocol)
        self.require_key(plan)

        started = time.perf_counter()
        status = 0
        error_type: str | None = None
        try:
            response = await self._send(plan)
            status = response.status_code
            if response.status_code >= 400:
                self._raise_upstream_error(response)
            payload = response.json()
            converted = adapter.convert_response(resolved, payload, status)
            return converted, status
        except GatewayError as exc:
            error_type = exc.error_type
            raise
        except httpx.TimeoutException as exc:
            error_type = "upstream_timeout"
            raise UpstreamTimeoutError(
                f"upstream provider {resolved.provider_id!r} timed out"
            ) from exc
        except httpx.HTTPError as exc:
            error_type = "upstream_error"
            raise UpstreamError(f"upstream request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            error_type = "adapter_error"
            raise UpstreamError(
                "upstream response body is not valid JSON"
            ) from exc
        finally:
            latency = (time.perf_counter() - started) * 1000
            self._record(plan, status, latency, error_type)

    async def stream(
        self, requested_model: str, body: dict[str, Any]
    ) -> AsyncIterator[bytes]:
        """Run a streaming request, yielding Responses SSE bytes."""
        plan = self.plan(requested_model, body)
        self.require_key(plan)
        async for chunk in self.stream_plan(plan):
            yield chunk

    async def stream_plan(self, plan: RequestPlan) -> AsyncIterator[bytes]:
        """Stream a pre-built plan. Key/resolution errors were already raised."""
        resolved = plan.resolved
        adapter = get_adapter(resolved.protocol)
        self.require_key(plan)

        started = time.perf_counter()
        status = 0
        error_type: str | None = None
        try:
            async with self.client.stream(
                "POST",
                plan.url,
                json=plan.body,
                headers=plan.headers,
                timeout=resolved.provider.timeout,
            ) as response:
                status = response.status_code
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_upstream_error(response)
                async for chunk in adapter.stream_events(resolved, response):
                    yield chunk
        except GatewayError as exc:
            error_type = exc.error_type
            raise
        except httpx.TimeoutException as exc:
            error_type = "upstream_timeout"
            raise UpstreamTimeoutError(
                f"upstream provider {resolved.provider_id!r} timed out"
            ) from exc
        except httpx.HTTPError as exc:
            error_type = "upstream_error"
            raise UpstreamError(f"upstream request failed: {exc}") from exc
        finally:
            latency = (time.perf_counter() - started) * 1000
            self._record(plan, status, latency, error_type)

    # -- helpers ---------------------------------------------------------

    async def _send(self, plan: RequestPlan) -> httpx.Response:
        return await self.client.post(
            plan.url,
            json=plan.body,
            headers=plan.headers,
            timeout=plan.resolved.provider.timeout,
        )

    @staticmethod
    def _raise_upstream_error(response: httpx.Response) -> None:
        body = response.content
        content_type = response.headers.get("content-type")
        message = f"upstream returned HTTP {response.status_code}"
        try:
            parsed = json.loads(body.decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict) and err.get("message"):
                    message = str(err["message"])
                elif isinstance(err, str):
                    message = err
                elif parsed.get("message"):
                    message = str(parsed["message"])
        except (ValueError, UnicodeDecodeError):
            text = body.decode("utf-8", "replace").strip()
            if text:
                message = text[:500]
        raise UpstreamError(
            message,
            status_code=response.status_code,
            body=body,
            content_type=content_type,
            details={
                "upstream_status": response.status_code,
                "upstream_content_type": content_type,
                "upstream_body": _safe_body(body),
            },
        )

    def _record(
        self,
        plan: RequestPlan,
        status: int,
        latency_ms: float,
        error_type: str | None,
    ) -> None:
        resolved = plan.resolved
        record = RequestRecord(
            time=time.time(),
            request_id=plan.request_id,
            logical_model=resolved.logical_id,
            reasoning_level=resolved.reasoning_level,
            provider=resolved.provider_id,
            upstream_model=resolved.upstream_model,
            protocol=resolved.protocol,
            stream=bool(plan.body.get("stream")),
            status=status,
            latency_ms=latency_ms,
            error_type=error_type,
        )
        self.metrics.record(record)


def _safe_body(body: bytes, limit: int = 2000) -> str:
    try:
        text = body.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode with 'replace' cannot fail
        return ""
    return text[:limit]


async def test_connection(
    provider: ProviderConfig,
    secrets: SecretStore,
    provider_id: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Cheaply probe a provider's ``/models`` endpoint (never a paid call)."""
    api_key = secrets.get(provider_id, provider.api_key_env)
    headers = provider.resolved_headers()
    headers["accept"] = "application/json"
    if provider.protocol == "anthropic_messages":
        headers.setdefault("anthropic-version", "2023-06-01")
        if api_key:
            headers["x-api-key"] = api_key
    elif api_key:
        headers["authorization"] = f"Bearer {api_key}"

    result: dict[str, Any] = {
        "provider": provider_id,
        "url": provider.url_for("models"),
        "api_key_available": bool(api_key),
    }
    started = time.perf_counter()
    try:
        response = await client.get(
            provider.url_for("models"),
            headers=headers,
            timeout=min(provider.timeout, 30.0),
        )
        latency = (time.perf_counter() - started) * 1000
        result.update(
            {
                "ok": response.status_code < 400,
                "status": response.status_code,
                "latency_ms": round(latency, 1),
            }
        )
        if response.status_code >= 400:
            result["error"] = response.text[:500]
        else:
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    result["model_count"] = len(payload["data"])
            except ValueError:
                pass
        return result
    except httpx.TimeoutException:
        result.update({"ok": False, "status": 0, "error": "request timed out"})
        return result
    except httpx.HTTPError as exc:
        result.update({"ok": False, "status": 0, "error": str(exc)})
        return result
