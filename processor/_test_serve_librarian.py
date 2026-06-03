"""Functional test for the Smart Librarian endpoints in viewer/serve.py.

Spins up the real VaultHandler on an ephemeral localhost port and exercises
the routing without spawning the librarian subprocess (uses the disabled-scope
400 path), so it's deterministic, offline and touches no real data (DB_PATH is
redirected to a temp file). Run:  python processor/_test_serve_librarian.py
"""

import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

import serve  # noqa: E402


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req)


def main():
    tmp = tempfile.TemporaryDirectory()
    serve.DB_PATH = Path(tmp.name) / "t.db"               # don't touch real data
    serve._librarian_log_path = str(Path(tmp.name) / ".librarian_log.txt")

    # Hermetic: inject the "AI off" gateway so /api/ask exercises the disabled
    # path deterministically — no env/config fiddling needed thanks to DI.
    import llm_gateway
    serve.build_gateway = lambda *a, **k: llm_gateway.NullGateway()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.VaultHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    try:
        # status before any run: not running, empty log
        d = json.loads(urllib.request.urlopen(base + "/api/librarian/status").read())
        assert d["running"] is False and d["log"] == "", d
        print("  OK  status (idle)")

        # run with both scopes off -> 400 (no subprocess spawned)
        try:
            _post(base, "/api/librarian/run", {"tag": False, "summarize": False})
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
        print("  OK  run rejects empty scope (400)")

        # stop when nothing is running
        d = json.loads(_post(base, "/api/librarian/stop", {}).read())
        assert d["status"] == "not_running", d
        print("  OK  stop when idle -> not_running")

        # ask: empty question -> 400
        try:
            _post(base, "/api/ask", {"question": "   "})
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
        print("  OK  ask rejects empty question (400)")

        # ask: with no key configured -> enabled:false, graceful (no crash on bare DB)
        d = json.loads(_post(base, "/api/ask", {"question": "what did we discuss?"}).read())
        assert d["enabled"] is False and d["answer"] is None, d
        print("  OK  ask degrades gracefully when LLM disabled")
    finally:
        srv.shutdown()
        tmp.cleanup()

    print("\nserve librarian endpoints: ALL OK")


if __name__ == "__main__":
    main()
