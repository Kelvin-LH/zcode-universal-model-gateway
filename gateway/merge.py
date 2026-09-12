"""Deep-merge and safe nested field removal helpers.

These are deliberately provider-agnostic: the gateway never interprets the
``reasoning`` mapping payload, it only deep-merges it into the upstream body.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any


def deep_merge(base: Any, override: Any) -> Any:
    """Recursively merge ``override`` into ``base`` and return a new object.

    - dicts are merged key by key
    - lists and scalars are replaced by the override value
    - ``base`` and ``override`` are never mutated
    """
    if not isinstance(override, dict):
        return copy.deepcopy(override)
    if not isinstance(base, dict):
        base = {}
    result: dict[str, Any] = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def remove_path(obj: Any, path: str) -> bool:
    """Delete a dotted nested path from a dict in place.

    ``path="reasoning.summary"`` removes ``obj["reasoning"]["summary"]``.
    Returns ``True`` when something was removed. Missing paths are not an
    error (providers differ in what they accept).
    """
    if not isinstance(obj, dict) or not path:
        return False
    parts = [p for p in path.split(".") if p != ""]
    if not parts:
        return False
    cursor: Any = obj
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    last = parts[-1]
    if isinstance(cursor, dict) and last in cursor:
        del cursor[last]
        return True
    return False


def apply_remove_fields(obj: Any, fields: Iterable[str]) -> list[str]:
    """Apply :func:`remove_path` for each field; return the removed paths."""
    removed: list[str] = []
    for field in fields or []:
        if remove_path(obj, field):
            removed.append(field)
    return removed
