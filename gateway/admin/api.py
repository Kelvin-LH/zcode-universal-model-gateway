"""Admin API backing the Web UI.

All ``/api/admin/*`` endpoints optionally require a token: when the
``GATEWAY_ADMIN_TOKEN`` environment variable is set, every request must carry
it. Secrets (API keys, Authorization headers, temporary keys) are never
returned — only availability status.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from .. import __version__
from ..adapters import supported_protocols
from ..config import (
    Config,
    ConfigError,
    ConfigManager,
    ModelConfig,
    ProviderConfig,
    load_config_text,
)
from ..errors import GatewayError
from ..metrics import Metrics
from ..router import Router, test_connection
from ..secrets import SecretStore

ADMIN_TOKEN_ENV = "GATEWAY_ADMIN_TOKEN"


def require_admin(request: Request) -> None:
    """Enforce ``GATEWAY_ADMIN_TOKEN`` when it is configured."""
    expected = os.environ.get(ADMIN_TOKEN_ENV)
    if not expected:
        return
    supplied = request.headers.get("x-admin-token")
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied or supplied != expected:
        raise HTTPException(status_code=401, detail="需要有效的管理 Token，或 Token 不正确")


def _provider_status(provider: ProviderConfig, secrets: SecretStore, provider_id: str) -> dict:
    return secrets.status(provider_id, provider.api_key_env)


def _provider_dict(provider_id: str, provider: ProviderConfig, secrets: SecretStore) -> dict:
    data = provider.model_dump(mode="json")
    data["id"] = provider_id
    data["key_status"] = _provider_status(provider, secrets, provider_id)
    return data


def _model_dict(model_id: str, model: ModelConfig, config: Config) -> dict:
    data = model.model_dump(mode="json")
    data["id"] = model_id
    data["provider_enabled"] = bool(
        config.providers.get(model.provider)
        and config.providers[model.provider].enabled
    )
    virtual = [model_id]
    if model.reasoning and model.reasoning.supported:
        virtual = [model_id] + [f"{model_id}@{lvl}" for lvl in model.reasoning.supported]
    data["virtual_models"] = virtual if model.enabled else []
    return data


def _config_diff(old: Config, new: Config) -> dict[str, Any]:
    def delta(attr: str) -> dict[str, list[str]]:
        o = set(getattr(old, attr))
        n = set(getattr(new, attr))
        return {
            "added": sorted(n - o),
            "removed": sorted(o - n),
            "changed": sorted(
                k
                for k in (o & n)
                if getattr(old, attr)[k].model_dump() != getattr(new, attr)[k].model_dump()
            ),
        }

    return {
        "providers": delta("providers"),
        "models": delta("models"),
        "settings_changed": old.settings.model_dump() != new.settings.model_dump(),
    }


def build_admin_router(
    manager: ConfigManager,
    secrets: SecretStore,
    metrics: Metrics,
    router: Router,
) -> APIRouter:
    api = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

    # -- status / meta ---------------------------------------------------

    @api.get("/status")
    async def status() -> dict[str, Any]:
        config = manager.config
        available_keys = sum(
            1
            for pid, provider in config.providers.items()
            if secrets.status(pid, provider.api_key_env)["available"]
        )
        return {
            "version": __version__,
            "config_path": str(manager.path),
            "config_valid": manager.load_error is None,
            "config_error": manager.load_error,
            "loaded_at": manager.loaded_at,
            "providers": len(config.providers),
            "models": len(config.models),
            "virtual_models": len(config.virtual_model_ids()),
            "providers_with_keys": available_keys,
            "metrics": metrics.snapshot(),
            "recent_requests": metrics.recent_requests(5),
            "recent_errors": metrics.recent_errors(5),
        }

    @api.get("/meta")
    async def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "protocols": supported_protocols(),
            "default_base_url": "http://127.0.0.1:8787/v1",
        }

    @api.get("/logs")
    async def logs(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        return {"logs": metrics.logs(limit)}

    @api.get("/metrics")
    async def metrics_endpoint() -> dict[str, Any]:
        return metrics.snapshot()

    # -- providers -------------------------------------------------------

    @api.get("/providers")
    async def list_providers() -> dict[str, Any]:
        config = manager.config
        return {
            "providers": [
                _provider_dict(pid, provider, secrets)
                for pid, provider in config.providers.items()
            ]
        }

    @api.post("/providers")
    async def create_provider(payload: dict = Body(...)) -> dict[str, Any]:
        provider_id = str(payload.pop("id", "") or "").strip()
        if not provider_id:
            raise HTTPException(status_code=400, detail="服务商 ID 不能为空")
        if "@" in provider_id:
            raise HTTPException(status_code=400, detail="服务商 ID 不能包含 '@'")

        def mutate(config: Config) -> None:
            if provider_id in config.providers:
                raise HTTPException(status_code=409, detail="该服务商已存在")
            config.providers[provider_id] = _validate_provider(payload)

        manager.update(mutate)
        provider = manager.config.providers[provider_id]
        return {"provider": _provider_dict(provider_id, provider, secrets)}

    @api.put("/providers/{provider_id}")
    async def update_provider(provider_id: str, payload: dict = Body(...)) -> dict[str, Any]:
        def mutate(config: Config) -> None:
            existing = config.providers.get(provider_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="找不到该服务商")
            merged = existing.model_dump(mode="json")
            merged.update({k: v for k, v in payload.items() if k != "id"})
            config.providers[provider_id] = _validate_provider(merged)

        manager.update(mutate)
        provider = manager.config.providers[provider_id]
        return {"provider": _provider_dict(provider_id, provider, secrets)}

    @api.delete("/providers/{provider_id}")
    async def delete_provider(
        provider_id: str, cascade: bool = Query(default=False)
    ) -> dict[str, Any]:
        """Delete a provider.

        By default a provider that is still referenced by models is refused,
        so a dangling reference can never be written. With ``?cascade=true``
        the calling models are deleted together with the provider.
        """
        config = manager.config
        if provider_id not in config.providers:
            raise HTTPException(status_code=404, detail="找不到该服务商")
        used_by = [
            mid for mid, model in config.models.items() if model.provider == provider_id
        ]
        if used_by and not cascade:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"该服务商仍被以下模型引用：{', '.join(used_by)}。"
                    "请先删除这些模型，或选择连同它们一起删除。"
                ),
            )

        def mutate(cfg: Config) -> None:
            for model_id in used_by:
                cfg.models.pop(model_id, None)
            cfg.providers.pop(provider_id, None)

        manager.update(mutate)
        secrets.clear(provider_id)
        return {"deleted": provider_id, "deleted_models": used_by}

    @api.post("/providers/{provider_id}/duplicate")
    async def duplicate_provider(
        provider_id: str, payload: dict = Body(default_factory=dict)
    ) -> dict[str, Any]:
        config = manager.config
        source = config.providers.get(provider_id)
        if source is None:
            raise HTTPException(status_code=404, detail="找不到该服务商")
        new_id = str(payload.get("id") or f"{provider_id}-copy").strip()
        if new_id in config.providers:
            raise HTTPException(status_code=409, detail="目标服务商 ID 已存在")
        data = source.model_dump(mode="json")
        data["display_name"] = f"{source.display_name} (copy)"

        def mutate(cfg: Config) -> None:
            cfg.providers[new_id] = _validate_provider(data)

        manager.update(mutate)
        provider = manager.config.providers[new_id]
        return {"provider": _provider_dict(new_id, provider, secrets)}

    @api.post("/providers/{provider_id}/test")
    async def test_provider(provider_id: str) -> dict[str, Any]:
        config = manager.config
        provider = config.providers.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="找不到该服务商")
        await router.startup()
        return await test_connection(provider, secrets, provider_id, router.client)

    @api.put("/providers/{provider_id}/key")
    async def set_provider_key(
        provider_id: str, payload: dict = Body(...)
    ) -> dict[str, Any]:
        """Set a provider's API key.

        ``persist=true`` saves it to the local key file so it survives restarts
        (this is the "enter it once" option). ``persist=false`` keeps it only in
        process memory for the current session.
        """
        provider = manager.config.providers.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="找不到该服务商")
        key = str(payload.get("api_key") or "")
        persist = bool(payload.get("persist"))
        if persist:
            secrets.set_persistent(provider_id, key)
            # A saved local key is the effective one now; drop any session key.
            secrets.clear_temporary(provider_id)
        else:
            secrets.set_temporary(provider_id, key)
        return {
            "provider": provider_id,
            "key_status": secrets.status(provider_id, provider.api_key_env),
        }

    @api.delete("/providers/{provider_id}/key")
    async def clear_provider_key(provider_id: str) -> dict[str, Any]:
        """Forget a provider's key (both the session key and the saved local one)."""
        secrets.clear(provider_id)
        provider = manager.config.providers.get(provider_id)
        api_key_env = provider.api_key_env if provider else None
        return {
            "provider": provider_id,
            "key_status": secrets.status(provider_id, api_key_env),
        }

    # Backwards-compatible aliases for the original temporary-only endpoints.
    @api.put("/providers/{provider_id}/temporary-key")
    async def set_temporary_key(provider_id: str, payload: dict = Body(...)) -> dict[str, Any]:
        if provider_id not in manager.config.providers:
            raise HTTPException(status_code=404, detail="找不到该服务商")
        key = payload.get("api_key") or ""
        secrets.set_temporary(provider_id, str(key))
        return {
            "provider": provider_id,
            "temporary_key_active": secrets.has_temporary(provider_id),
        }

    @api.delete("/providers/{provider_id}/temporary-key")
    async def clear_temporary_key(provider_id: str) -> dict[str, Any]:
        secrets.clear_temporary(provider_id)
        return {"provider": provider_id, "temporary_key_active": False}

    # -- models ----------------------------------------------------------

    @api.get("/models")
    async def list_models_endpoint() -> dict[str, Any]:
        config = manager.config
        return {
            "models": [
                _model_dict(mid, model, config) for mid, model in config.models.items()
            ],
            "virtual_models": config.virtual_model_ids(),
        }

    @api.post("/models")
    async def create_model(payload: dict = Body(...)) -> dict[str, Any]:
        model_id = str(payload.pop("id", "") or "").strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="模型 ID 不能为空")
        if "@" in model_id:
            raise HTTPException(status_code=400, detail="模型 ID 不能包含 '@'")

        def mutate(config: Config) -> None:
            if model_id in config.models:
                raise HTTPException(status_code=409, detail="该模型已存在")
            config.models[model_id] = _validate_model(payload, config)

        manager.update(mutate)
        return {
            "model": _model_dict(model_id, manager.config.models[model_id], manager.config)
        }

    @api.put("/models/{model_id}")
    async def update_model(model_id: str, payload: dict = Body(...)) -> dict[str, Any]:
        def mutate(config: Config) -> None:
            existing = config.models.get(model_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="找不到该模型")
            merged = existing.model_dump(mode="json")
            merged.update({k: v for k, v in payload.items() if k != "id"})
            config.models[model_id] = _validate_model(merged, config)

        manager.update(mutate)
        return {
            "model": _model_dict(model_id, manager.config.models[model_id], manager.config)
        }

    @api.delete("/models/{model_id}")
    async def delete_model(model_id: str) -> dict[str, Any]:
        def mutate(config: Config) -> None:
            if model_id not in config.models:
                raise HTTPException(status_code=404, detail="找不到该模型")
            del config.models[model_id]

        manager.update(mutate)
        return {"deleted": model_id}

    @api.post("/models/{model_id}/duplicate")
    async def duplicate_model(
        model_id: str, payload: dict = Body(default_factory=dict)
    ) -> dict[str, Any]:
        config = manager.config
        source = config.models.get(model_id)
        if source is None:
            raise HTTPException(status_code=404, detail="找不到该模型")
        new_id = str(payload.get("id") or f"{model_id}-copy").strip()
        if new_id in config.models:
            raise HTTPException(status_code=409, detail="目标模型 ID 已存在")
        data = source.model_dump(mode="json")
        data["display_name"] = f"{source.display_name} (copy)"

        def mutate(cfg: Config) -> None:
            cfg.models[new_id] = _validate_model(data, cfg)

        manager.update(mutate)
        return {
            "model": _model_dict(new_id, manager.config.models[new_id], manager.config)
        }

    @api.post("/models/{model_id}/test")
    async def test_model(model_id: str, payload: dict = Body(default_factory=dict)) -> dict[str, Any]:
        manager.maybe_reload()
        model = manager.config.models.get(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="找不到该模型")
        provider = manager.config.providers.get(model.provider)
        if provider is None:
            raise HTTPException(status_code=409, detail="找不到该模型所属的服务商")
        await router.startup()
        result = await test_connection(provider, secrets, model.provider, router.client)
        result["model"] = model_id
        result["upstream_model"] = model.upstream_model
        return result

    @api.get("/models/{model_id}/virtual")
    async def virtual_models(model_id: str) -> dict[str, Any]:
        config = manager.config
        model = config.models.get(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="找不到该模型")
        virtual = [model_id]
        if model.reasoning and model.reasoning.supported:
            virtual += [f"{model_id}@{lvl}" for lvl in model.reasoning.supported]
        return {"virtual_models": virtual}

    # -- request preview -------------------------------------------------

    @api.post("/preview")
    async def preview(payload: dict = Body(...)) -> dict[str, Any]:
        model = payload.get("model")
        body = payload.get("body") or payload.get("input")
        if not model:
            raise HTTPException(status_code=400, detail="必须提供 model")
        if isinstance(body, str):
            body = {"model": model, "input": body}
        elif body is None:
            body = {"model": model}
        else:
            body = dict(body)
            body.setdefault("model", model)
        try:
            return router.preview(model, body)
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc

    # -- configuration ---------------------------------------------------

    @api.get("/config")
    async def get_config() -> dict[str, Any]:
        return {
            "yaml": manager.config.to_yaml(),
            "path": str(manager.path),
            "valid": manager.load_error is None,
            "error": manager.load_error,
        }

    @api.post("/config/validate")
    async def validate_config(payload: dict = Body(...)) -> dict[str, Any]:
        text = payload.get("yaml")
        if text is None:
            raise HTTPException(status_code=400, detail="必须提供 'yaml' 字段")
        try:
            new_config = load_config_text(text)
        except ConfigError as exc:
            return {"valid": False, "error": str(exc)}
        return {
            "valid": True,
            "diff": _config_diff(manager.config, new_config),
            "config": new_config.to_dict(),
        }

    @api.put("/config")
    async def save_config(payload: dict = Body(...)) -> dict[str, Any]:
        text = payload.get("yaml")
        if text is None:
            raise HTTPException(status_code=400, detail="必须提供 'yaml' 字段")
        try:
            new_config = load_config_text(text)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Only validated configs are written (atomic replace).
        manager.save_config(new_config)
        return {"saved": True, "providers": len(new_config.providers), "models": len(new_config.models)}

    @api.post("/config/reload")
    async def reload_config() -> dict[str, Any]:
        try:
            manager.reload()
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"reloaded": True, "loaded_at": manager.loaded_at}

    @api.get("/config/download")
    async def download_config() -> Response:
        return PlainTextResponse(
            manager.config.to_yaml(),
            media_type="application/x-yaml",
            headers={"content-disposition": 'attachment; filename="config.yaml"'},
        )

    @api.post("/config/upload")
    async def upload_config(payload: dict = Body(...)) -> dict[str, Any]:
        text = payload.get("yaml")
        if text is None:
            raise HTTPException(status_code=400, detail="必须提供 'yaml' 字段")
        try:
            new_config = load_config_text(text)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "valid": True,
            "diff": _config_diff(manager.config, new_config),
            "yaml": new_config.to_yaml(),
        }

    @api.post("/config/reset-example")
    async def reset_example() -> dict[str, Any]:
        example = Path(manager.path).parent / "config.example.yaml"
        if not example.exists():
            raise HTTPException(status_code=404, detail="找不到 config.example.yaml")
        manager.save_text(example.read_text(encoding="utf-8"))
        return {"reset": True}

    return api


# -- validation helpers --------------------------------------------------


def _validate_provider(data: dict) -> ProviderConfig:
    try:
        return ProviderConfig.model_validate(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"服务商配置无效：{exc}") from exc


def _validate_model(data: dict, config: Config) -> ModelConfig:
    provider = data.get("provider")
    if provider and provider not in config.providers:
        raise HTTPException(
            status_code=400, detail=f"未知的服务商 {provider!r}"
        )
    try:
        return ModelConfig.model_validate(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"模型配置无效：{exc}") from exc
