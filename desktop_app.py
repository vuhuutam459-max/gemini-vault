"""Desktop entry point for the packaged (PyInstaller) Gemini Vault app.

Running from source this is just a convenience launcher; frozen into an .exe it
is the program's `main`. The tricky part of bundling a local web app is paths:

* **Read-only bundle** (HTML/JS, demo data) lives in a temp dir PyInstaller
  unpacks at runtime — `sys._MEIPASS`. We read static assets from there.
* **Writable user data** (the SQLite DB, the optional librarian_config.json,
  logs) must persist between runs and survive being installed under
  Program Files, so it goes in a per-user data directory, NOT next to the .exe.

We import the existing `serve` module unchanged and just repoint its module-level
path globals (the same seam the tests use), then run its HTTP server. Startup is
wrapped so any failure (including a frozen import error) is written to a log file
the user can find, instead of a windowed .exe dying silently.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8642


def resource_dir() -> Path:
    """Read-only assets root (bundle dir when frozen, repo dir otherwise)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Per-user, writable directory for the DB, config and logs."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:  # Linux / *nix
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "GeminiVault"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _NullWriter:
    """No-op stream: last-resort stand-in so writes never raise."""
    def write(self, *_a):
        return 0
    def flush(self):
        pass


def _ensure_streams() -> None:
    """In a windowed (--noconsole) PyInstaller build, sys.stdout/stderr are None,
    so any print()/flush()/write() raises AttributeError: 'NoneType' has no
    attribute 'write'. Point them at a log file (or a no-op) before anything runs.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return  # running with a real console (e.g. from source) — leave as-is
    try:
        stream = open(data_dir() / "app.log", "a", encoding="utf-8", errors="replace")
    except OSError:
        stream = _NullWriter()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _log_fatal(exc: BaseException) -> None:
    """Persist a startup failure so a silent windowed .exe still leaves a trail."""
    import traceback
    msg = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        sys.stderr.write(msg)
    except Exception:
        pass
    for target in (data_dir() / "startup_error.log",
                   Path(sys.executable).resolve().parent / "GeminiVault_error.log"):
        try:
            target.write_text(msg, encoding="utf-8")
        except OSError:
            pass


def _run() -> None:
    """The real startup. Imports happen here so frozen import errors are caught."""
    RES = resource_dir()
    DATA = data_dir()

    # Make the bundled backend importable both from source and when frozen.
    sys.path.insert(0, str(RES / "processor"))
    sys.path.insert(0, str(RES / "viewer"))

    import serve          # the unchanged viewer server
    import llm_gateway    # gateway: we only move its config path

    # Repoint every writable path into the per-user data dir; keep static assets
    # in the bundle. These are the same globals the test-suite overrides.
    serve.VIEWER_DIR = RES / "viewer"
    serve.DB_PATH = DATA / "gemini_vault.db"
    serve._scraper_log_path = str(DATA / ".scraper_log.txt")
    serve._librarian_log_path = str(DATA / ".librarian_log.txt")
    llm_gateway.CONFIG_PATH = DATA / "librarian_config.json"

    # First-run seeding: create the DB, importing the bundled demo if present.
    if not serve.DB_PATH.exists():
        import parse_and_index as P
        P.DB_PATH = serve.DB_PATH
        conn = P.init_db(serve.DB_PATH)
        try:
            demo = RES / "Source_Accounts" / "demo_export.json"
            if demo.exists():
                try:
                    P.process_export_file(conn, demo)
                except Exception as exc:  # demo is optional — never block startup
                    sys.stderr.write(f"[gemini-vault] demo import skipped: {exc}\n")
        finally:
            conn.close()

    from http.server import ThreadingHTTPServer
    port = int(os.environ.get("GEMINI_VAULT_PORT", DEFAULT_PORT))
    url = f"http://127.0.0.1:{port}/"

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), serve.VaultHandler)
    except OSError:
        # Port busy: an instance is probably already running — just open it.
        webbrowser.open(url)
        return

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Gemini Vault running at {url}  (data: {DATA})")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


def main() -> None:
    _ensure_streams()   # FIRST: make stdout/stderr safe under --noconsole
    try:
        _run()
    except Exception as exc:
        _log_fatal(exc)
        raise


if __name__ == "__main__":
    main()
