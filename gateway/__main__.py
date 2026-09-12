"""``python -m gateway`` entry point."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import uvicorn

from .app import HOST_ENV, PORT_ENV, create_app
from .paths import app_dir, example_config_path, is_frozen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CONFIG = "config.yaml"
EXAMPLE_CONFIG = "config.example.yaml"


def load_dotenv(path: str | None = None) -> None:
    """Minimal .env loader (no third-party dependency).

    Existing environment variables are never overwritten.
    """
    env_file = Path(path) if path is not None else (app_dir() / ".env")
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def ensure_config(path: str) -> None:
    """Create a working config from the example on first run.

    The example is looked up next to the target first (so a portable exe picks
    up a user-provided example), then in the bundled resources.
    """
    target = Path(path)
    if target.exists():
        return
    candidates = [target.parent / EXAMPLE_CONFIG, example_config_path()]
    for example in candidates:
        if example.is_file():
            shutil.copyfile(example, target)
            logging.getLogger("zumg").info(
                "Generated %s from %s", target.name, example.name
            )
            return


def print_banner(host: str, port: int) -> None:
    """Print the startup banner.

    Printed from Python (not the shell launchers) so the text is correct on
    any Windows locale and does not depend on the console code page.
    """
    line = "=" * 58
    print()
    print(line)
    print("  ZCode Universal Model Gateway")
    print("-" * 58)
    print(f"  Admin UI  : http://{host}:{port}/")
    print(f"  ZCode URL : http://{host}:{port}/v1")
    print("-" * 58)
    print("  Press Ctrl+C to stop.")
    print(line)
    print(flush=True)


def resolve_config_path() -> str:
    """Where config.yaml lives.

    ``GATEWAY_CONFIG`` wins. Otherwise the file lives next to the executable
    when frozen (so a portable exe stays self-contained), or in the working
    directory when run from source.
    """
    explicit = os.environ.get("GATEWAY_CONFIG")
    if explicit:
        return explicit
    if is_frozen():
        return str(app_dir() / DEFAULT_CONFIG)
    return DEFAULT_CONFIG


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    config_path = resolve_config_path()
    ensure_config(config_path)

    host = os.environ.get(HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))

    print_banner(host, port)
    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
