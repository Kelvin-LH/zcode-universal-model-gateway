"""Configuration schema, loading, validation, safe writes and hot reload.

The config file is the single source of truth. Everything the gateway needs
(providers, models, reasoning levels, reasoning mappings) is data — no
provider or reasoning level is hard-coded anywhere in this package.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError


class _Yaml12SafeLoader(yaml.SafeLoader):
    """SafeLoader that uses YAML 1.2 booleans (only true/false).

    PyYAML follows YAML 1.1, where ``off``/``on``/``yes``/``no`` are booleans.
    That would silently turn a reasoning level named ``off`` into ``False``,
    so we drop those implicit resolvers.
    """


# Copy before mutating: the parent's dict is shared between loader subclasses.
_Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: list(value)
    for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _ch, _resolvers in list(_Yaml12SafeLoader.yaml_implicit_resolvers.items()):
    _Yaml12SafeLoader.yaml_implicit_resolvers[_ch] = [
        (tag, regexp)
        for tag, regexp in _resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

# Protocols the gateway can actually speak. Anything else is rejected at
# validation time with a clear message instead of failing at request time.
SUPPORTED_PROTOCOLS = (
    "openai_responses",
    "openai_chat",
    "anthropic_messages",
)

DEFAULT_PATHS: dict[str, dict[str, str]] = {
    "openai_responses": {
        "responses": "/responses",
        "chat_completions": "/chat/completions",
        "messages": "/v1/messages",
        "models": "/models",
    },
    "openai_chat": {
        "responses": "/responses",
        "chat_completions": "/chat/completions",
        "messages": "/v1/messages",
        "models": "/models",
    },
    "anthropic_messages": {
        "responses": "/responses",
        "chat_completions": "/chat/completions",
        "messages": "/v1/messages",
        "models": "/models",
    },
}

DEFAULT_TIMEOUT = 600.0


class PathsConfig(BaseModel):
    """Optional per-provider path overrides. Defaults are protocol-aware."""

    model_config = ConfigDict(extra="allow")

    responses: str | None = None
    chat_completions: str | None = None
    messages: str | None = None
    models: str | None = None

    def filled(self, protocol: str) -> PathsConfig:
        defaults = DEFAULT_PATHS.get(protocol, DEFAULT_PATHS["openai_responses"])
        data = self.model_dump()
        for key, value in defaults.items():
            if not data.get(key):
                data[key] = value
        for key, value in (self.model_extra or {}).items():
            data.setdefault(key, value)
        return PathsConfig(**data)


class ProviderConfig(BaseModel):
    """A single upstream provider."""

    model_config = ConfigDict(extra="allow")

    display_name: str = ""
    protocol: str = "openai_responses"
    base_url: str
    api_key_env: str | None = None
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT
    headers: dict[str, str] = Field(default_factory=dict)
    headers_from_env: dict[str, str] = Field(default_factory=dict)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, value: str) -> str:
        if value not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                f"不支持的协议 {value!r}；支持的协议："
                + ", ".join(SUPPORTED_PROTOCOLS)
            )
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("base_url 不能为空")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _fill_defaults(self) -> ProviderConfig:
        self.paths = self.paths.filled(self.protocol)
        if not self.display_name:
            self.display_name = self.protocol
        return self

    def url_for(self, kind: str) -> str:
        """Resolve a full upstream URL for ``responses``/``chat_completions``/``messages``/``models``."""
        path = getattr(self.paths, kind, None)
        if not path:
            raise ConfigError(f"服务商未配置 {kind!r} 对应的路径")
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def resolved_headers(self, env: dict[str, str] | None = None) -> dict[str, str]:
        """Static headers plus headers sourced from environment variables."""
        env = env if env is not None else os.environ
        headers = dict(self.headers or {})
        for header, env_name in (self.headers_from_env or {}).items():
            value = env.get(env_name)
            if value:
                headers[header] = value
        return headers


class ReasoningConfig(BaseModel):
    """Reasoning levels and their request-body mappings.

    Level names are arbitrary strings — the gateway never assumes any
    particular set of levels.
    """

    model_config = ConfigDict(extra="allow")

    supported: list[str] = Field(default_factory=list)
    default: str | None = None
    mapping: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("supported")
    @classmethod
    def _check_supported(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            level = str(item).strip()
            if not level:
                raise ValueError("思考档位名称不能为空")
            if "@" in level:
                raise ValueError(
                    f"思考档位名称 {level!r} 不能包含 '@'"
                )
            if level not in cleaned:
                cleaned.append(level)
        return cleaned

    @field_validator("mapping")
    @classmethod
    def _check_mapping(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for level, payload in value.items():
            if not isinstance(payload, dict):
                raise ValueError(
                    f"思考档位 {level!r} 的 mapping 必须是对象"
                )
        return value

    @model_validator(mode="after")
    def _normalise(self) -> ReasoningConfig:
        if not self.supported and self.mapping:
            # Be forgiving: derive supported levels from the mapping keys.
            self.supported = list(self.mapping.keys())
        if self.default is None and self.supported:
            self.default = self.supported[0]
        if self.default is not None and self.supported and self.default not in self.supported:
            raise ValueError(
                f"默认思考档位 {self.default!r} 不在支持的档位 {self.supported} 中"
            )
        unknown = [level for level in self.mapping if level not in self.supported]
        if unknown:
            raise ValueError(
                "reasoning mapping 中含有未在 supported 中列出的档位："
                + ", ".join(map(str, unknown))
            )
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.supported)


class ModelConfig(BaseModel):
    """A logical model exposed to ZCode, backed by one provider."""

    model_config = ConfigDict(extra="allow")

    display_name: str = ""
    provider: str
    upstream_model: str
    enabled: bool = True
    reasoning: ReasoningConfig | None = None
    request_overrides: dict[str, Any] = Field(default_factory=dict)
    remove_fields: list[str] = Field(default_factory=list)
    # When true (and the alias level applies), any reasoning controls the
    # client sent are dropped before the level mapping is applied, so the
    # chosen model@level always decides and the client cannot influence it.
    ignore_client_reasoning: bool = False

    @model_validator(mode="after")
    def _fill_display_name(self) -> ModelConfig:
        if not self.display_name:
            self.display_name = self.upstream_model
        return self

    def mapping_for(self, level: str | None) -> dict[str, Any]:
        """Return the (deep-mergeable) mapping body for a reasoning level."""
        if not self.reasoning or not level:
            return {}
        return dict(self.reasoning.mapping.get(level) or {})


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    reasoning_precedence: Literal["alias", "client"] = "alias"
    log_capacity: int = 500
    log_page_size: int = 100


class Config(BaseModel):
    """The whole gateway configuration document."""

    model_config = ConfigDict(extra="allow")

    settings: Settings = Field(default_factory=Settings)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelConfig] = Field(default_factory=dict)

    @field_validator("providers", "models")
    @classmethod
    def _check_ids(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in value:
            if not key or "@" in key:
                raise ValueError(
                    f"标识符 {key!r} 无效：不能为空，且不能包含 '@'"
                )
        return value

    @model_validator(mode="after")
    def _check_references(self) -> Config:
        for model_id, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(
                    f"模型 {model_id!r} 引用了不存在的服务商 {model.provider!r}"
                )
        return self

    # -- helpers --------------------------------------------------------

    def virtual_model_ids(self) -> list[str]:
        """All model IDs advertised through ``GET /v1/models``."""
        ids: list[str] = []
        for model_id, model in self.models.items():
            if not model.enabled:
                continue
            ids.append(model_id)
            if model.reasoning and model.reasoning.enabled:
                for level in model.reasoning.supported:
                    ids.append(f"{model_id}@{level}")
        return ids

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.to_dict(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )


def parse_config(data: Any) -> Config:
    """Validate a raw mapping into a :class:`Config`, raising ConfigError."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("配置文件的根节点必须是映射/对象")
    try:
        return Config.model_validate(data)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"配置无效：{exc}") from exc


def load_config_text(text: str) -> Config:
    try:
        data = yaml.load(text, Loader=_Yaml12SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析错误：{exc}") from exc
    return parse_config(data)


def load_config_file(path: str | os.PathLike[str]) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    return load_config_text(text)


def atomic_write(path: str | os.PathLike[str], text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + fsync + replace)."""
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ConfigManager:
    """Holds the active config and reloads it when the file changes.

    A failed reload never takes the gateway down: the last known-good config
    stays active and the error is exposed for the UI/health endpoints.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._config = Config()
        self._mtime: float | None = None
        self._loaded_at: float | None = None
        self._load_error: str | None = None

    # -- access ---------------------------------------------------------

    @property
    def config(self) -> Config:
        with self._lock:
            return self._config

    @property
    def load_error(self) -> str | None:
        with self._lock:
            return self._load_error

    @property
    def loaded_at(self) -> float | None:
        with self._lock:
            return self._loaded_at

    @property
    def mtime(self) -> float | None:
        with self._lock:
            return self._mtime

    # -- loading --------------------------------------------------------

    def load(self) -> Config:
        """Load from disk, replacing the active config on success."""
        try:
            config = load_config_file(self.path)
        except ConfigError as exc:
            with self._lock:
                self._load_error = str(exc)
            raise
        with self._lock:
            self._config = config
            self._mtime = self._file_mtime()
            self._loaded_at = time.time()
            self._load_error = None
        return config

    def load_or_default(self) -> Config:
        """Best-effort startup load; never raises."""
        try:
            return self.load()
        except ConfigError as exc:
            with self._lock:
                self._load_error = str(exc)
                if self._loaded_at is None:
                    # Keep an empty but valid config so the gateway starts.
                    self._config = Config()
                    self._loaded_at = time.time()
            return self._config

    def maybe_reload(self) -> bool:
        """Reload if the file mtime changed. Returns True when reloaded."""
        mtime = self._file_mtime()
        with self._lock:
            current = self._mtime
        if mtime is None or mtime == current:
            return False
        try:
            self.load()
            return True
        except ConfigError:
            # Keep the previous good config; remember the new mtime so we do
            # not retry on every request, but surface the error.
            with self._lock:
                self._mtime = mtime
            return False

    def reload(self) -> Config:
        """Force a reload, raising on invalid config."""
        return self.load()

    # -- writing --------------------------------------------------------

    def save_text(self, text: str) -> Config:
        """Validate YAML text and, only if valid, atomically persist it."""
        config = load_config_text(text)
        atomic_write(self.path, config.to_yaml())
        with self._lock:
            self._config = config
            self._mtime = self._file_mtime()
            self._loaded_at = time.time()
            self._load_error = None
        return config

    def save_config(self, config: Config) -> Config:
        return self.save_text(config.to_yaml())

    def update(self, mutate) -> Config:
        """Apply ``mutate`` to a copy of the config and persist the result."""
        clone = self.config.model_copy(deep=True)
        mutate(clone)
        return self.save_config(clone)

    def _file_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None
