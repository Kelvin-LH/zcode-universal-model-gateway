"""Provider API key resolution.

Keys are resolved from three places, in priority order:

1. a *temporary* key held in process memory (set via the Web UI, gone on restart);
2. a *local* key file (``secrets.local.json``), written by the Web UI when you
   ask to remember a key — this is what lets you enter a key once instead of
   exporting an environment variable every time;
3. the environment variable named by the provider's ``api_key_env``.

Key values are never written to ``config.yaml``, never logged, and never
returned to a client. The local file is git-ignored and, on POSIX systems,
created with ``0600`` permissions.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger("zumg.secrets")

DEFAULT_SECRETS_FILENAME = "secrets.local.json"
SECRETS_PATH_ENV = "GATEWAY_SECRETS"


class SecretStore:
    """Resolve provider API keys without ever exposing their values."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._env: Mapping[str, str] = env if env is not None else os.environ
        # provider_id -> secret value (process memory only)
        self._temporary: dict[str, str] = {}
        # provider_id -> secret value (persisted to `path`)
        self._persistent: dict[str, str] = {}
        self.path: Path | None = Path(path) if path is not None else None
        if self.path is not None:
            self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read local key file %s: %s", self.path, exc)
            return
        if isinstance(raw, dict):
            self._persistent = {
                str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v
            }

    def _save(self) -> None:
        if self.path is None:
            return
        from .config import atomic_write

        atomic_write(
            self.path,
            json.dumps(self._persistent, indent=2, ensure_ascii=False) + "\n",
        )
        _restrict_permissions(self.path)

    # -- resolution -----------------------------------------------------

    def get(self, provider_id: str, api_key_env: str | None) -> str | None:
        """Return the effective key for a provider, or ``None`` if missing."""
        temp = self._temporary.get(provider_id)
        if temp:
            return temp
        local = self._persistent.get(provider_id)
        if local:
            return local
        if api_key_env:
            value = self._env.get(api_key_env)
            if value:
                return value
        return None

    def status(self, provider_id: str, api_key_env: str | None) -> dict:
        """Return a *safe* description of key availability (never the value)."""
        if self._temporary.get(provider_id):
            return {
                "api_key_env": api_key_env,
                "available": True,
                "source": "temporary",
                "temporary_key_active": True,
                "local_key_saved": bool(self._persistent.get(provider_id)),
            }
        if self._persistent.get(provider_id):
            return {
                "api_key_env": api_key_env,
                "available": True,
                "source": "local",
                "temporary_key_active": False,
                "local_key_saved": True,
            }
        if api_key_env and self._env.get(api_key_env):
            return {
                "api_key_env": api_key_env,
                "available": True,
                "source": "environment",
                "temporary_key_active": False,
                "local_key_saved": False,
            }
        return {
            "api_key_env": api_key_env,
            "available": False,
            "source": None,
            "temporary_key_active": False,
            "local_key_saved": False,
        }

    # -- temporary keys -------------------------------------------------

    def set_temporary(self, provider_id: str, key: str) -> None:
        if not key:
            self._temporary.pop(provider_id, None)
        else:
            self._temporary[provider_id] = key

    def clear_temporary(self, provider_id: str) -> None:
        self._temporary.pop(provider_id, None)

    def has_temporary(self, provider_id: str) -> bool:
        return bool(self._temporary.get(provider_id))

    # -- local (persisted) keys -----------------------------------------

    def set_persistent(self, provider_id: str, key: str) -> None:
        """Save a key to the local file so it survives restarts."""
        if key:
            self._persistent[provider_id] = key
        else:
            self._persistent.pop(provider_id, None)
        self._save()

    def clear_persistent(self, provider_id: str) -> None:
        if provider_id in self._persistent:
            del self._persistent[provider_id]
            self._save()

    def has_persistent(self, provider_id: str) -> bool:
        return bool(self._persistent.get(provider_id))

    def export_keys(self, only_providers: set[str] | None = None) -> dict[str, str]:
        """All saved local keys, for a full backup.

        Only the persisted keys are returned: temporary ones live in memory and
        are deliberately excluded. Callers must not expose the result through a
        normal API response.

        ``only_providers`` limits the export to providers that still exist in
        the configuration, so a backup does not carry stale keys.
        """
        if only_providers is None:
            return dict(self._persistent)
        return {
            pid: key
            for pid, key in self._persistent.items()
            if pid in only_providers
        }

    def import_keys(self, keys: Mapping[str, str]) -> list[str]:
        """Merge saved keys from a backup.

        Existing keys for providers not present in ``keys`` are kept, so
        restoring a backup on a machine that already has other providers does
        not wipe them. Returns the provider ids that were written.
        """
        written: list[str] = []
        for provider_id, key in (keys or {}).items():
            if isinstance(key, str) and key:
                self._persistent[str(provider_id)] = key
                written.append(str(provider_id))
        if written:
            self._save()
        return written

    def replace_keys(self, keys: Mapping[str, str]) -> list[str]:
        """Replace every saved key with the backup's contents."""
        self._persistent = {
            str(pid): str(key)
            for pid, key in (keys or {}).items()
            if isinstance(key, str) and key
        }
        self._save()
        return sorted(self._persistent)

    def clear(self, provider_id: str) -> None:
        """Forget a provider's key everywhere (temporary and local)."""
        self.clear_temporary(provider_id)
        self.clear_persistent(provider_id)

    def reset(self) -> None:
        self._temporary.clear()


def _restrict_permissions(path: Path) -> None:
    """Best-effort ``chmod 600``; a no-op on platforms without POSIX modes."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows / restricted filesystems
        pass


def default_secrets_path(config_path: str | os.PathLike[str]) -> str:
    """Where the local key file lives, next to the config file."""
    override = os.environ.get(SECRETS_PATH_ENV)
    if override:
        return override
    return str(Path(config_path).resolve().parent / DEFAULT_SECRETS_FILENAME)
