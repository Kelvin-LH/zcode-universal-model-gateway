# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build the gateway as a single portable Windows exe.

Usage (from the project root):

    pyinstaller --clean --noconfirm zumg.spec

The result is dist/ZUMG.exe. It bundles the Web UI and the example config; the
user's own config.yaml and secrets.local.json are created next to the exe on
first run, never inside the bundle.
"""

from pathlib import Path

ROOT = Path(SPECPATH)

# uvicorn imports its protocol/loop implementations dynamically, so PyInstaller
# cannot see them by scanning imports.
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

# Keep the bundle small: none of these are used at runtime.
EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "pytest",
    "respx",
    "PIL",
    "numpy",
    "pandas",
    "matplotlib",
]

a = Analysis(
    [str(ROOT / "run_gateway.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "gateway" / "static"), "gateway/static"),
        (str(ROOT / "config.example.yaml"), "."),
    ],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ZUMG",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
