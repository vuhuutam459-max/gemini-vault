"""End-to-end test: serve.py exposes tags + summary, and /api/ask retrieves
FTS sources. Seeds a real schema DB, runs the actual VaultHandler over an
ephemeral port, no network/LLM (no key -> ask degrades to FTS sources only).
Run:  python processor/_test_smart_api.py
"""

import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "processor"))

from processor import parse_and_index as P
from processor import librarian as Lib
import serve  # noqa: E402


def _seed(db_path):
    conn = P.init_db(db_path)
    conn.execute("INSERT INTO accounts(email, first_seen, last_export) VALUES (?,?,?)",
                 ("demo@example.com", "2026-01-01", "2026-01-01"))
    conn.execute("INSERT INTO conversations(id, account_id, title, updated_time, summary) "
                 "VALUES (?,?,?,?,?)",
                 ("c1", 1, "Async tutorial", "2026-01-02T00:00:00+00:00",
                  "A concise chat about Python asyncio."))
    for seq, (role, content) in enumerate([
        ("user", "Explain asyncio coroutines"),
        ("model", "Asyncio uses async/await for concurrency."),
    ]):
        conn.execute("INSERT INTO messages(conversation_id, seq, role, content) VALUES (?,?,?,?)",
                     ("c1", seq, role, content))
    Lib.upsert_tags(conn, "c1", ["python", "asyncio"])
    conn.commit()
    conn.close()


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req)


def main():
    tmp = tempfile.TemporaryDirectory()
    db = Path(tmp.name) / "v.db"
    _seed(db)
    serve.DB_PATH = db
    serve._librarian_log_path = str(Path(tmp.name) / ".lib.txt")

    # Hermetic: ignore any real librarian_config.json / env so the LLM stays
    # disabled and /api/ask degrades to FTS-only as the test expects.
    import os
    import llm_client as LC
    LC.CONFIG_PATH = Path(tmp.name) / "__no_config__.json"
    for _v in ("FREELLMAPI_BASE_URL", "FREELLMAPI_KEY", "FREELLMAPI_MODEL"):
        os.environ.pop(_v, None)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.VaultHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        convs = json.loads(urllib.request.urlopen(base + "/api/conversations").read())
        assert convs and convs[0]["tags"] == ["asyncio", "python"], convs
        assert convs[0]["summary"].startswith("A concise"), convs[0]
        print("  OK  /api/conversations exposes tags + summary")

        detail = json.loads(urllib.request.urlopen(base + "/api/conversation/c1").read())
        assert detail["conversation"]["tags"] == ["asyncio", "python"], detail
        assert detail["conversation"]["summary"].startswith("A concise")
        print("  OK  /api/conversation exposes tags + summary")

        ask = json.loads(_post(base, "/api/ask", {"question": "asyncio coroutines"}).read())
        assert ask["enabled"] is False, ask
        assert any(s["id"] == "c1" for s in ask["sources"]), ask
        print("  OK  /api/ask retrieves FTS sources (LLM disabled)")
    finally:
        srv.shutdown()
        tmp.cleanup()
    print("\nsmart API: ALL OK")


if __name__ == "__main__":
    main()
