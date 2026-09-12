"""``python -m gateway`` entry point."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import uvicorn

from .app import HOST_ENV, PORT_ENV, create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CONFIG = "config.yaml"
EXAMPLE_CONFIG = "config.example.yaml"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no third-party dependency).

    Existing environment variables are never overwritten.
    """
    env_file = Path(path)
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


def ensure_config(path: str = DEFAULT_CONFIG) -> None:
    """Create a working config from the example on first run.

    A fresh clone has no ``config.yaml`` (it is git-ignored). Rather than
    starting empty, copy the example so the user has providers/models to edit
    in the Web UI immediately.
    """
    target = Path(path)
    if target.exists():
        return
    example = target.parent / EXAMPLE_CONFIG
    if example.is_file():
        shutil.copyfile(example, target)
        logging.getLogger("zumg").info(
            "已从 %s 生成 %s", example.name, target.name
        )


def print_banner(host: str, port: int) -> None:
    """Print a Chinese startup banner.

    Printed from Python (not the shell launchers) so the text is correct on
    any Windows locale and does not depend on the console code page.
    """
    line = "=" * 58
    print()
    print(line)
    print("  ZCode Universal Model Gateway")
    print("-" * 58)
    print(f"  管理界面  : http://{host}:{port}/")
    print(f"  ZCode 地址: http://{host}:{port}/v1")
    print("-" * 58)
    print("  按 Ctrl+C 停止服务。")
    print(line)
    print(flush=True)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    ensure_config(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG))

    host = os.environ.get(HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))

    print_banner(host, port)
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
