"""Typed gateway errors with OpenAI-style JSON payloads.

Every error the gateway returns to a client is represented by a
:class:`GatewayError`. Upstream HTTP failures carry their original status,
body and content type so we never flatten a 429 into a 500.
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """Base class for all client-visible gateway errors."""

    error_type: str = "gateway_error"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        status_code: int | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if error_type is not None:
            self.error_type = error_type
        if status_code is not None:
            self.status_code = status_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.error_type,
            "message": self.message,
        }
        if self.details is not None:
            payload["details"] = self.details
        return {"error": payload}


class ConfigError(GatewayError):
    error_type = "config_error"
    status_code = 500


class UnknownModelError(GatewayError):
    error_type = "unknown_model"
    status_code = 404


class UnknownReasoningLevelError(GatewayError):
    error_type = "unknown_reasoning_level"
    status_code = 400


class ProviderDisabledError(GatewayError):
    error_type = "provider_disabled"
    status_code = 409


class ModelDisabledError(GatewayError):
    error_type = "model_disabled"
    status_code = 409


class ProviderAuthError(GatewayError):
    error_type = "provider_auth_error"
    status_code = 401


class UpstreamTimeoutError(GatewayError):
    error_type = "upstream_timeout"
    status_code = 504


class UpstreamError(GatewayError):
    """An upstream returned a non-2xx response.

    ``body`` and ``content_type`` are preserved verbatim when present.
    """

    error_type = "upstream_error"
    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        body: bytes | None = None,
        content_type: str | None = None,
        details: Any | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(
            message,
            error_type=error_type,
            status_code=status_code,
            details=details,
        )
        self.body = body
        self.content_type = content_type


class AdapterError(GatewayError):
    error_type = "adapter_error"
    status_code = 500


class UnsupportedFeatureError(GatewayError):
    error_type = "unsupported_feature"
    status_code = 400
