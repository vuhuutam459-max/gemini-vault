"""Build a standalone Gemini Vault .exe with PyInstaller.

Usage:
    python build/build_windows.py            # build the one-file .exe
    python build/build_windows.py --clean    # remove build/ dist/ artifacts first

Output:  dist/GeminiVault.exe   (a single, self-contained executable)

The app's core is stdlib-only, so the only build-time dependency is PyInstaller
itself. This script checks it's installed, then runs the bundled .spec. The same
.spec is used by the GitHub Actions workflow to build macOS and Linux binaries.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # …/Gemini_Vault
SPEC = ROOT / "build" / "gemini_vault.spec"


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Install it with:")
        print("    pip install pyinstaller")
        sys.exit(1)


def clean() -> None:
    for d in ("build/__pycache__", "dist", "build/build"):
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    # PyInstaller's own work dir at the repo root
    work = ROOT / "build_pyi"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    print("Cleaned previous build artifacts.")


def build() -> None:
    ensure_pyinstaller()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build_pyi"),
        str(SPEC),
    ]
    print("Running:", " ".join(cmd))
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc != 0:
        print(f"\nBuild FAILED (exit {rc}).")
        sys.exit(rc)

    exe_name = "GeminiVault.exe" if sys.platform == "win32" else "GeminiVault"
    exe = ROOT / "dist" / "GeminiVault" / exe_name      # one-DIR layout
    print("\nBuild OK.")
    print(f"  App folder: {ROOT / 'dist' / 'GeminiVault'}")
    print(f"  Executable: {exe}")
    print("  Run the executable — it opens the viewer in your browser. Ship the whole")
    print("  GeminiVault folder (the installer does this); user data lives in your")
    print("  per-user app-data folder, separate from the program files.")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
    build()
