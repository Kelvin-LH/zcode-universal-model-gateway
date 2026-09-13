"""Filesystem locations that work both from source and from a frozen exe.

Two different roots matter:

``resource_root``
    Read-only files shipped with the program (``gateway/static`` and
    ``config.example.yaml``). When frozen by PyInstaller these live in the
    temporary bundle directory (``sys._MEIPASS``); from source they sit at the
    project root.

``app_dir``
    Writable location for user data (``config.yaml``, ``secrets.local.json``,
    ``.env``). When frozen this is the folder containing the executable, so a
    portable exe keeps its configuration next to itself instead of writing into
    the temporary bundle. From source it is the current working directory,
    which matches the previous behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) bundle."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Root of bundled read-only resources."""
    meipass = getattr(sys, "_MEIPASS", None)
    if is_frozen() and meipass:
        return Path(meipass)
    # gateway/paths.py -> project root
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    """Directory holding the Web UI assets."""
    return resource_root() / "gateway" / "static"


def example_config_path() -> Path:
    """Bundled ``config.example.yaml``."""
    return resource_root() / "config.example.yaml"


def app_dir() -> Path:
    """Writable directory for user data."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()
