"""PyInstaller entry point.

Exists because ``gateway/__main__.py`` uses relative imports and therefore
cannot be used directly as a frozen entry script. Running this file works the
same as ``python -m gateway``:

    python run_gateway.py
"""

from gateway.__main__ import main

if __name__ == "__main__":
    main()
