# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Gemini Vault — one self-contained executable.

Build it via the helper (recommended):   python build/build_windows.py
…or directly:                            pyinstaller build/gemini_vault.spec

The core app is stdlib-only, so the bundle is small and has no third-party
runtime deps. We ship the static viewer (HTML/JS), the Python backend modules and
the demo export as data; writable state (DB/config/logs) is created at runtime in
a per-user directory by desktop_app.py — never inside the read-only bundle.
"""

import os
from pathlib import Path

# SPECPATH is the dir containing this spec (…/Gemini_Vault/build) -> repo root is its parent.
ROOT = Path(SPECPATH).resolve().parent

def _exists(*parts):
    return (ROOT.joinpath(*parts)).exists()

# (source, destination-dir-inside-bundle) — only include what is actually present.
datas = [(str(ROOT / "viewer" / "index.html"), "viewer")]
if _exists("viewer", "lib"):
    datas.append((str(ROOT / "viewer" / "lib"), "viewer/lib"))
if _exists("Source_Accounts", "demo_export.json"):
    datas.append((str(ROOT / "Source_Accounts" / "demo_export.json"), "Source_Accounts"))

# Backend modules are imported via a runtime sys.path.insert, so name them
# explicitly to be safe (pathex also lets PyInstaller resolve them statically).
hiddenimports = [
    "serve", "llm_gateway", "llm_client",
    "parse_and_index", "librarian", "parsers", "importers",
]

a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT), str(ROOT / "processor"), str(ROOT / "viewer")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "playwright"],  # not needed by the viewer; keeps it lean
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GeminiVault",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,            # windowed desktop app; flip to True to see logs
    icon=str(ROOT / "build" / "icon.ico") if _exists("build", "icon.ico") else None,
)
