"""Desktop entry point for the packaged (PyInstaller) Gemini Vault app.

Running from source this is just a convenience launcher; frozen into an .exe it
is the program's `main`. The tricky part of bundling a local web app is paths:

* **Read-only bundle** (HTML/JS, demo data) lives in a temp dir PyInstaller
  unpacks at runtime — `sys._MEIPASS`. We read static assets from there.
* **Writable user data** (the SQLite DB, the optional librarian_config.json,
  logs) must persist between runs and survive being installed under
  Program Files, so it goes in a per-user data directory, NOT next to the .exe.

We import the existing `serve` module unchanged and just repoint its module-level
path globals (the same seam the tests use), then run its HTTP server.
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


RES = resource_dir()
DATA = data_dir()

# Make the bundled backend importable both from source and when frozen.
sys.path.insert(0, str(RES / "processor"))
sys.path.insert(0, str(RES / "viewer"))

import serve          # noqa: E402  (the unchanged viewer server)
import llm_gateway    # noqa: E402  (gateway: we only move its config path)

# Repoint every writable path into the per-user data dir; keep static assets in
# the bundle. These are the same globals the test-suite overrides.
serve.VIEWER_DIR = RES / "viewer"
serve.DB_PATH = DATA / "gemini_vault.db"
serve._scraper_log_path = str(DATA / ".scraper_log.txt")
serve._librarian_log_path = str(DATA / ".librarian_log.txt")
llm_gateway.CONFIG_PATH = DATA / "librarian_config.json"


def seed_first_run() -> None:
    """On first launch, create the DB. If the bundled demo export is present,
    import it so the app opens with something to look at; otherwise create an
    empty (valid) schema so the viewer starts cleanly."""
    if serve.DB_PATH.exists():
        return
    import parse_and_index as P
    P.DB_PATH = serve.DB_PATH
    demo = RES / "Source_Accounts" / "demo_export.json"
    conn = P.init_db(serve.DB_PATH)
    try:
        if demo.exists():
            try:
                P.process_export_file(conn, demo)
            except Exception as exc:  # demo is optional — never block startup
                print(f"[gemini-vault] demo import skipped: {exc}")
    finally:
        conn.close()


def main() -> None:
    seed_first_run()

    from http.server import ThreadingHTTPServer
    port = int(os.environ.get("GEMINI_VAULT_PORT", DEFAULT_PORT))
    url = f"http://127.0.0.1:{port}/"

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), serve.VaultHandler)
    except OSError:
        # Port busy: an instance is probably already running — just open it.
        webbrowser.open(url)
        return

    # Open the browser a beat after the server starts listening.
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Gemini Vault running at {url}  (data: {DATA})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
