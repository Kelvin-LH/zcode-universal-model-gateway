"""Model resolution: logical model + reasoning alias -> upstream target.

This module is where a ZCode request model like ``deepseek-flash@max`` is
turned into a concrete provider, upstream model, URL and reasoning mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config, ModelConfig, ProviderConfig
from .errors import (
    ModelDisabledError,
    ProviderDisabledError,
    UnknownModelError,
    UnknownReasoningLevelError,
)
from .merge import deep_merge, remove_path

SEPARATOR = "@"

# Reasoning controls a client (e.g. an IDE) may send. Removed before the alias
# mapping is applied when a model sets ``ignore_client_reasoning``.
#
# The list covers every shape an OpenAI-compatible / Anthropic client may use:
# the OpenAI Responses field (``reasoning``), the flattened Chat Completions
# field (``reasoning_effort``), its camelCase spelling (``reasoningEffort``,
# used by some provider catalogs), the Anthropic field (``thinking``), generic
# thinking-config shapes (``thinkingConfig`` / ``thinking_budget``), the Qwen /
# DashScope switch (``enable_thinking``), and the Anthropic ``output_config``
# block used to carry an effort level.
CLIENT_REASONING_FIELDS = (
    "reasoning",
    "reasoning_effort",
    "reasoningEffort",
    "reasoning_summary",
    "thinking",
    "thinkingConfig",
    "thinking_budget",
    "enable_thinking",
    "output_config",
)


@dataclass
class ResolvedModel:
    """Everything the router needs to build and send an upstream request."""

    requested_model: str
    logical_id: str
    display_name: str
    reasoning_level: str | None
    reasoning_default: str | None
    provider_id: str
    provider: ProviderConfig
    upstream_model: str
    protocol: str
    base_url: str
    endpoint_kind: str
    mapping: dict[str, Any] = field(default_factory=dict)
    request_overrides: dict[str, Any] = field(default_factory=dict)
    remove_fields: list[str] = field(default_factory=list)
    ignore_client_reasoning: bool = False
    model: ModelConfig | None = None

    @property
    def target_url(self) -> str:
        return self.provider.url_for(self.endpoint_kind)

    def effective_body(
        self, client_body: dict[str, Any], precedence: str = "alias"
    ) -> dict[str, Any]:
        """Build the upstream body from a (translated) client body.

        With ``precedence="alias"`` (default) the reasoning-level mapping is
        applied last so the alias always wins over reasoning parameters the
        client sent. With ``precedence="client"`` the client body is applied
        last instead.

        ``request_overrides`` always win over the client body; the model name
        is always forced to the upstream model; ``remove_fields`` runs last.

        When the model sets ``ignore_client_reasoning`` and an alias level
        mapping is in force, reasoning controls sent by the client are removed
        before the mapping is applied. This matters when the client and the
        mapping use different field names (e.g. the client sends
        ``reasoning.effort`` while the mapping sets ``reasoning_effort``):
        otherwise both would reach the upstream and the client's value could
        still win.
        """
        body: dict[str, Any] = dict(client_body or {})
        if (
            precedence == "alias"
            and self.ignore_client_reasoning
            and self.mapping
        ):
            for field in CLIENT_REASONING_FIELDS:
                body.pop(field, None)
        if precedence == "client":
            body = deep_merge(self.mapping, body)
            body = deep_merge(body, self.request_overrides)
        else:
            body = deep_merge(body, self.request_overrides)
            body = deep_merge(body, self.mapping)
        body["model"] = self.upstream_model
        for path in self.remove_fields:
            remove_path(body, path)
        return body


def split_alias(requested: str) -> tuple[str, str | None]:
    """Split ``model@level`` into ``("model", "level")``.

    A trailing '@' is treated as part of the model id only if the base is not
    a known model, which :func:`resolve_model` decides. Here we always split
    on the last '@'.
    """
    if SEPARATOR in requested:
        base, _, level = requested.rpartition(SEPARATOR)
        if base:
            return base, (level or None)
    return requested, None


def resolve_model(config: Config, requested: str) -> ResolvedModel:
    """Resolve a client-supplied model id against the active config."""
    if not requested:
        raise UnknownModelError("请求中没有提供 model 参数")

    base, level = split_alias(requested)
    model = config.models.get(base)
    if model is None:
        raise UnknownModelError(f"未知模型 {requested!r}")

    if not model.enabled:
        raise ModelDisabledError(f"模型 {base!r} 已被禁用")

    reasoning = model.reasoning
    if level is not None:
        if not reasoning or not reasoning.enabled:
            raise UnknownReasoningLevelError(
                f"模型 {base!r} 不支持思考档位"
            )
        if level not in reasoning.supported:
            raise UnknownReasoningLevelError(
                f"模型 {base!r} 不支持思考档位 {level!r}；"
                f"支持的档位：{', '.join(reasoning.supported)}"
            )
    else:
        level = reasoning.default if reasoning and reasoning.enabled else None

    provider = config.providers.get(model.provider)
    if provider is None:
        raise UnknownModelError(
            f"模型 {base!r} 引用了不存在的服务商 {model.provider!r}"
        )
    if not provider.enabled:
        raise ProviderDisabledError(f"服务商 {model.provider!r} 已被禁用")

    endpoint_kind = {
        "openai_responses": "responses",
        "openai_chat": "chat_completions",
        "anthropic_messages": "messages",
    }[provider.protocol]

    return ResolvedModel(
        requested_model=requested,
        logical_id=base,
        display_name=model.display_name,
        reasoning_level=level,
        reasoning_default=(
            reasoning.default if reasoning and reasoning.enabled else None
        ),
        provider_id=model.provider,
        provider=provider,
        upstream_model=model.upstream_model,
        protocol=provider.protocol,
        base_url=provider.base_url,
        endpoint_kind=endpoint_kind,
        mapping=model.mapping_for(level),
        request_overrides=dict(model.request_overrides or {}),
        remove_fields=list(model.remove_fields or []),
        ignore_client_reasoning=bool(model.ignore_client_reasoning),
        model=model,
    )


def list_models(config: Config) -> list[dict[str, Any]]:
    """OpenAI-compatible model list entries for ``GET /v1/models``."""
    created = 1700000000
    entries: list[dict[str, Any]] = []
    for model_id, model in config.models.items():
        if not model.enabled:
            continue
        entries.append(
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": model.provider,
            }
        )
        if model.reasoning and model.reasoning.enabled:
            for level in model.reasoning.supported:
                entries.append(
                    {
                        "id": f"{model_id}{SEPARATOR}{level}",
                        "object": "model",
                        "created": created,
                        "owned_by": model.provider,
                    }
                )
    return entries
