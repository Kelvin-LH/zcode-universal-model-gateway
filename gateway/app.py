"""FastAPI application: ZCode-facing API, admin API and the local Web UI."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from . import __version__
from .admin.api import build_admin_router
from .config import ConfigManager
from .errors import GatewayError, UnknownModelError, UpstreamError
from .metrics import Metrics
from .models import list_models, resolve_model
from .paths import static_dir
from .router import Router, test_connection
from .secrets import SecretStore, default_secrets_path

log = logging.getLogger("zumg.app")

STATIC_DIR = static_dir()
DEFAULT_CONFIG_PATH = "config.yaml"
HOST_ENV = "GATEWAY_HOST"
PORT_ENV = "GATEWAY_PORT"

# Revalidate on every load so UI updates take effect without a hard refresh.
NO_CACHE = "no-cache, must-revalidate"


def _default_config_path() -> str:
    return os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)


def create_app(
    config_path: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
) -> FastAPI:
    """Build the FastAPI app. ``client``/``env`` are injection points for tests."""
    path = config_path or _default_config_path()
    manager = ConfigManager(path)
    secret_store = SecretStore(env, path=default_secrets_path(path))
    metrics = Metrics()
    router = Router(manager, secret_store, metrics, client=client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager.load_or_default()
        await router.startup()
        log.info(
            "ZUMG %s started; config=%s providers=%d models=%d",
            __version__,
            path,
            len(manager.config.providers),
            len(manager.config.models),
        )
        try:
            yield
        finally:
            await router.shutdown()

    app = FastAPI(
        title="ZCode Universal Model Gateway",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.config_manager = manager
    app.state.secrets = secret_store
    app.state.metrics = metrics
    app.state.router = router
    app.state.config_path = str(path)

    # -- error handling --------------------------------------------------

    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError):
        if (
            isinstance(exc, UpstreamError)
            and exc.body
            and exc.content_type
            and "json" in exc.content_type.lower()
        ):
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.content_type,
            )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):  # pragma: no cover
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "internal_error",
                    "message": "An unexpected internal error occurred",
                }
            },
        )

    # -- ZCode-facing API ------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        manager.maybe_reload()
        return {
            "status": "ok",
            "version": __version__,
            "config": "invalid" if manager.load_error else "valid",
            "config_error": manager.load_error,
            "providers": len(manager.config.providers),
            "models": len(manager.config.models),
        }

    @app.get("/v1/models")
    async def v1_models() -> dict[str, Any]:
        manager.maybe_reload()
        return {"object": "list", "data": list_models(manager.config)}

    @app.get("/v1/models/{model_id:path}")
    async def v1_model(model_id: str) -> dict[str, Any]:
        manager.maybe_reload()
        resolve_model(manager.config, model_id)
        return {
            "id": model_id,
            "object": "model",
            "created": 1700000000,
            "owned_by": "zumg",
        }

    @app.post("/v1/responses")
    async def v1_responses(request: Request):
        manager.maybe_reload()
        body = await _json_body(request)
        model = body.get("model")
        if not model:
            raise UnknownModelError("no model parameter in request")

        if body.get("stream"):
            # Resolve and validate before the response starts so auth/model
            # errors are returned as real HTTP errors, not a broken stream.
            plan = router.plan(model, body)
            router.require_key(plan)
            return StreamingResponse(
                router.stream_plan(plan),
                media_type="text/event-stream",
                headers={
                    "cache-control": "no-cache",
                    "connection": "keep-alive",
                    "x-accel-buffering": "no",
                },
            )

        payload, _plan, _status = await router.execute(model, body)
        return JSONResponse(content=payload)

    @app.post("/v1/chat/completions")
    async def v1_chat_completions(request: Request):
        """Convenience endpoint: same pipeline, returns Responses shape.

        ZCode's primary entry point is ``/v1/responses``; this exists so a
        client that posts a Chat-shaped body can still be served when a
        ``model`` is resolvable.
        """
        manager.maybe_reload()
        body = await _json_body(request)
        model = body.get("model")
        if not model:
            raise UnknownModelError("no model parameter in request")
        # Present the body as a Responses request so adapters can consume it.
        responses_body = dict(body)
        if "input" not in responses_body and body.get("messages") is not None:
            responses_body["input"] = body["messages"]
        if body.get("stream"):
            plan = router.plan(model, responses_body)
            router.require_key(plan)
            return StreamingResponse(
                router.stream_plan(plan),
                media_type="text/event-stream",
                headers={
                    "cache-control": "no-cache",
                    "connection": "keep-alive",
                    "x-accel-buffering": "no",
                },
            )
        payload, _plan, _status = await router.execute(model, responses_body)
        return JSONResponse(content=payload)

    # -- Admin UI + API --------------------------------------------------

    app.include_router(build_admin_router(manager, secret_store, metrics, router))

    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():  # pragma: no cover - shipped with package
            return HTMLResponse("<h1>ZUMG</h1><p>UI assets missing.</p>")
        return HTMLResponse(
            index_file.read_text(encoding="utf-8"),
            headers={"cache-control": NO_CACHE},
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/static/{asset_path:path}")
    async def static_asset(asset_path: str):
        target = (STATIC_DIR / asset_path).resolve()
        static_root = STATIC_DIR.resolve()
        if static_root not in target.parents and target != static_root:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        if not target.is_file():
            return JSONResponse(status_code=404, content={"detail": "not found"})
        # The UI is served from a local, frequently-updated package: always let
        # the browser revalidate so an updated app.js/css is picked up without
        # a manual hard refresh. ETag keeps this cheap (304 when unchanged).
        return FileResponse(target, headers={"cache-control": NO_CACHE})

    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        from .errors import AdapterError

        raise AdapterError(f"request body must be valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        from .errors import AdapterError

        raise AdapterError("request body must be a JSON object")
    return body


# Module-level app for `uvicorn gateway.app:app`.
app = create_app()
