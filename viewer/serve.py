"""
serve.py
========
Minimal HTTP server for the Chatrove Viewer.

Serves static files (HTML/JS/CSS) + a JSON API backed by SQLite.
Usage:
  python serve.py              # port 8642
  python serve.py --port 9000  # custom port
  python serve.py --no-open    # don't open the browser
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
import zipfile
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

__version__ = "1.1.4"

VIEWER_DIR = Path(__file__).resolve().parent
DB_PATH = VIEWER_DIR.parent / "gemini_vault.db"
DEFAULT_PORT = 8642

# Global scraper state
_scraper_lock = threading.Lock()
_scraper_proc = None
_scraper_log_path = str(VIEWER_DIR.parent / ".scraper_log.txt")

# Global Smart Librarian state (mirrors the scraper pattern above)
_librarian_lock = threading.Lock()
_librarian_proc = None
_librarian_log_path = str(VIEWER_DIR.parent / ".librarian_log.txt")

# Smart Librarian LLM gateway (stdlib-only; safe to import even without a provider).
# The viewer depends only on the gateway *interface* — it never touches a concrete
# client or provider, so AI is a clean optional plugin.
sys.path.insert(0, str(VIEWER_DIR.parent / "processor"))
from llm_gateway import build_gateway, LLMError, LLMUnavailable  # noqa: E402


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class VaultHandler(SimpleHTTPRequestHandler):
    """Serves both static files and /api/* requests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEWER_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/export-zip":
            self._handle_export_zip(parse_qs(parsed.query))
        elif parsed.path == "/api/export-json":
            self._handle_export_json(parse_qs(parsed.query))
        elif parsed.path.startswith("/api/"):
            self._handle_api(parsed.path, parse_qs(parsed.query))
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/scrape":
            self._handle_scrape()
        elif parsed.path == "/api/scrape-all":
            self._handle_scrape_all()
        elif parsed.path == "/api/scrape-stop":
            self._handle_scrape_stop()
        elif parsed.path == "/api/librarian/run":
            self._handle_librarian_run()
        elif parsed.path == "/api/librarian/stop":
            self._handle_librarian_stop()
        elif parsed.path == "/api/ask":
            self._handle_ask()
        else:
            self._json_response(404, {"error": "Not found"})

    def _handle_scrape(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        url = body.get("url", "")

        if not url or "gemini.google.com" not in url:
            self._json_response(400, {"error": "Invalid Gemini URL"})
            return

        # Same pre-flight as scrape-all: report a missing browser plainly up front.
        missing = self._playwright_missing()
        if missing:
            self._json_response(503, {"error": missing})
            return

        try:
            script = str(VIEWER_DIR.parent / "processor" / "scrape_gemini_url.py")
            result = subprocess.run(
                [sys.executable, script, url],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
            if result.returncode == 0:
                self._json_response(200, {"status": "ok", "output": result.stdout[-500:]})
            else:
                self._json_response(500, {"error": result.stderr[-300:] or "Scraper failed"})
        except FileNotFoundError:
            self._json_response(500, {"error": "Playwright not installed. Run: pip install playwright && playwright install chromium"})
        except subprocess.TimeoutExpired:
            self._json_response(500, {"error": "Timeout -- run manually: python processor/scrape_gemini_url.py URL"})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    @staticmethod
    def _playwright_missing():
        """Return a human-readable reason if the scraper can't run (Playwright
        module absent), else ''. Cheap import-spec probe in the server's own
        interpreter — the scraper child is spawned with the same sys.executable,
        so whatever we can import here is what it can import too."""
        import importlib.util
        if importlib.util.find_spec("playwright") is None:
            return ("Playwright is not installed or configured. "
                    "Install it from a terminal:\n"
                    "    pip install playwright\n"
                    "    playwright install chromium")
        return ""

    def _handle_scrape_all(self):
        global _scraper_proc
        with _scraper_lock:
            # Self-heal: only block when a PREVIOUS run is genuinely still alive.
            # A crashed/finished child (poll() is not None) must never wedge the
            # button on a stale "already running" — clear the slot and continue.
            if _scraper_proc is not None and _scraper_proc.poll() is None:
                self._json_response(409, {"error": "Scraper already running"})
                return
            _scraper_proc = None

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            account = body.get("account") or self._first_account_email() or "default@gmail.com"

            # Pre-flight: fail LOUDLY here if Playwright is missing instead of
            # spawning a child that dies silently into a log nobody can read.
            missing = self._playwright_missing()
            if missing:
                with open(_scraper_log_path, "w", encoding="utf-8") as f:
                    f.write("[ERROR] " + missing + "\n")
                self._json_response(503, {"error": missing})
                return

            try:
                with open(_scraper_log_path, "w", encoding="utf-8") as f:
                    f.write("[STATUS] Starting scraper...\n")

                script = str(VIEWER_DIR.parent / "processor" / "scrape_gemini_url.py")
                _scraper_proc = subprocess.Popen(
                    [sys.executable, script, "--list-all",
                     "--account", account, "--log", _scraper_log_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
            except Exception as e:
                # Spawn itself failed (bad interpreter path, OS error, ...).
                # GUARANTEE: reset state + answer with JSON so the button frees up.
                _scraper_proc = None
                with open(_scraper_log_path, "w", encoding="utf-8") as f:
                    f.write(f"[ERROR] Could not start scraper: {e}\n")
                self._json_response(500, {"error": f"Could not start scraper: {e}"})
                return

        self._json_response(200, {"status": "started", "pid": _scraper_proc.pid})

    def _handle_scrape_stop(self):
        global _scraper_proc
        with _scraper_lock:
            if _scraper_proc is not None and _scraper_proc.poll() is None:
                _scraper_proc.terminate()
                self._json_response(200, {"status": "stopped"})
            else:
                self._json_response(200, {"status": "not_running"})
            # Either way, drop the reference so the slot is free for the next run.
            _scraper_proc = None

    # ── Smart Librarian (auto-tag / summarize) — same subprocess pattern ──

    def _handle_librarian_run(self):
        global _librarian_proc
        with _librarian_lock:
            # Self-heal, mirroring the scraper: a finished/crashed child must
            # never leave a stale "already running" wedged on the button.
            if _librarian_proc is not None and _librarian_proc.poll() is None:
                self._json_response(409, {"error": "Librarian already running"})
                return
            _librarian_proc = None

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            # Default to doing both unless the caller narrows the scope.
            do_tag = bool(body.get("tag", True))
            do_summarize = bool(body.get("summarize", True))
            if not do_tag and not do_summarize:
                self._json_response(400, {"error": "Enable at least one of tag/summarize"})
                return

            try:
                with open(_librarian_log_path, "w", encoding="utf-8") as f:
                    f.write("[STATUS] Starting Smart Librarian...\n")

                script = str(VIEWER_DIR.parent / "processor" / "librarian.py")
                cmd = [sys.executable, script, "--log", _librarian_log_path]
                if do_tag:
                    cmd.append("--tag")
                if do_summarize:
                    cmd.append("--summarize")
                _librarian_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
            except Exception as e:
                # GUARANTEE: reset state + JSON error so the button frees up.
                _librarian_proc = None
                with open(_librarian_log_path, "w", encoding="utf-8") as f:
                    f.write(f"[ERROR] Could not start Librarian: {e}\n")
                self._json_response(500, {"error": f"Could not start Librarian: {e}"})
                return

        self._json_response(200, {"status": "started", "pid": _librarian_proc.pid,
                                  "tag": do_tag, "summarize": do_summarize})

    def _handle_librarian_stop(self):
        global _librarian_proc
        with _librarian_lock:
            if _librarian_proc and _librarian_proc.poll() is None:
                _librarian_proc.terminate()
                self._json_response(200, {"status": "stopped"})
            else:
                self._json_response(200, {"status": "not_running"})

    def _api_librarian_status(self):
        with _librarian_lock:
            running = _librarian_proc is not None and _librarian_proc.poll() is None
        log_text = ""
        try:
            with open(_librarian_log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            log_text = "".join(lines[-200:])  # last lines only
        except FileNotFoundError:
            pass
        return {"running": running, "log": log_text}

    # ── Smart search: "ask the archive" (RAG-lite over FTS) ──

    @staticmethod
    def _fts_query(text: str) -> str:
        """Build a safe FTS5 MATCH query: OR of quoted word tokens."""
        import re
        terms = re.findall(r"\w+", text, flags=re.UNICODE)
        return " OR ".join(f'"{t}"' for t in terms)

    def _handle_ask(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        question = (body.get("question") or "").strip()
        if not question:
            self._json_response(400, {"error": "Empty question"})
            return

        # 1) Retrieve candidate chats via FTS (phase-1 RAG-lite).
        sources, seen = [], set()
        fts_q = self._fts_query(question)
        if fts_q:
            conn = get_db()
            try:
                rows = conn.execute("""
                    SELECT m.conversation_id AS id, c.title AS title,
                           snippet(messages_fts, 0, '[', ']', '…', 18) AS snippet
                    FROM messages_fts
                    JOIN messages m ON messages_fts.rowid = m.id
                    JOIN conversations c ON m.conversation_id = c.id
                    WHERE messages_fts MATCH ?
                    ORDER BY rank LIMIT 12
                """, (fts_q,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                sources.append({"id": r["id"], "title": r["title"], "snippet": r["snippet"]})
                if len(sources) >= 6:
                    break

        # 2) Ask the LLM to answer over the excerpts — opt-in, fail fast, degrade clearly.
        gateway = build_gateway()
        if not gateway.available:
            # Not configured at all → offer FTS results only.
            self._json_response(200, {"enabled": False, "answer": None, "sources": sources})
            return
        if not sources:
            self._json_response(200, {"enabled": True, "answer": None, "sources": []})
            return
        # Configured but the daemon isn't responding: fail fast (a short pre-flight,
        # not minutes of retries) with an honest message — NOT a fake "API key" note.
        if not gateway.reachable():
            self._json_response(200, {
                "enabled": True, "answer": None, "sources": sources,
                "error": "model offline — is Ollama running? Start it, then ask again."})
            return

        context = "\n\n".join(
            f"[{i + 1}] {s['title']}: {s['snippet']}" for i, s in enumerate(sources))
        try:
            answer = gateway.generate_text(
                f"Question: {question}\n\nArchive excerpts:\n{context}\n\n"
                "Answer the question using ONLY these excerpts and cite sources as [n]. "
                "If the excerpts do not contain the answer, say so plainly.",
                system="You are a librarian answering questions about the user's own chat archive.",
            )
            self._json_response(200, {"enabled": True, "answer": answer, "sources": sources})
        except (LLMError, LLMUnavailable) as exc:
            self._json_response(200, {"enabled": True, "answer": None,
                                      "error": str(exc), "sources": sources})

    def _handle_api(self, path: str, params: dict):
        try:
            conn = get_db()
            result = self._route(conn, path, params)
            conn.close()
            self._json_response(200, result)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _route(self, conn: sqlite3.Connection, path: str, params: dict):
        if path == "/api/health":
            return {"status": "ok", "version": __version__}
        if path == "/api/system-info":
            return {
                "base_dir": str(VIEWER_DIR.parent),
                "python": sys.executable,
                "default_account": self._first_account_email() or "",
                "platform": sys.platform,
            }
        if path == "/api/scrape-status":
            return self._api_scrape_status()
        if path == "/api/librarian/status":
            return self._api_librarian_status()
        if path == "/api/stats":
            return self._api_stats(conn)
        if path == "/api/dashboard":
            return self._api_dashboard(conn, params)
        if path == "/api/accounts":
            return self._api_accounts(conn)
        if path == "/api/conversations":
            return self._api_conversations(conn, params)
        if path.startswith("/api/conversation/"):
            conv_id = path.split("/api/conversation/")[1]
            return self._api_conversation(conn, conv_id)
        if path == "/api/search":
            return self._api_search(conn, params)
        if path == "/api/canvas-list":
            return self._api_canvas_list(conn, params)
        if path.startswith("/api/canvas/"):
            art_id = path.split("/api/canvas/")[1]
            return self._api_canvas(conn, art_id)
        return {"error": "Unknown endpoint"}

    def _first_account_email(self):
        """Email of the first account in the DB — default for the scraper."""
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT email FROM accounts ORDER BY id LIMIT 1"
            ).fetchone()
            conn.close()
            return row["email"] if row else None
        except Exception:
            return None

    def _api_scrape_status(self):
        running = False
        with _scraper_lock:
            running = _scraper_proc is not None and _scraper_proc.poll() is None
        log_text = ""
        try:
            with open(_scraper_log_path, "r", encoding="utf-8") as f:
                log_text = f.read()
        except FileNotFoundError:
            pass
        return {"running": running, "log": log_text}

    def _api_stats(self, conn):
        convs = conn.execute("SELECT count(*) as c FROM conversations").fetchone()["c"]
        msgs = conn.execute("SELECT count(*) as c FROM messages").fetchone()["c"]
        canvas = conn.execute("SELECT count(*) as c FROM canvas_artifacts").fetchone()["c"]
        accs = conn.execute("SELECT count(*) as c FROM accounts").fetchone()["c"]
        return {"conversations": convs, "messages": msgs,
                "canvas_artifacts": canvas, "accounts": accs}

    def _api_dashboard(self, conn, params):
        """Aggregated statistics for the dashboard.

        Optional ?account_id=N restricts the selection to a single account.
        """
        account_id = params.get("account_id", [None])[0]
        where = ""
        args: tuple = ()
        if account_id:
            where = "WHERE c.account_id = ?"
            args = (int(account_id),)

        # Activity by month (by created date, else updated)
        by_month = conn.execute(f"""
            SELECT substr(COALESCE(c.created_time, c.updated_time), 1, 7) AS ym,
                   count(*) AS n
            FROM conversations c
            {where}
            GROUP BY ym
            HAVING ym IS NOT NULL
            ORDER BY ym
        """, args).fetchall()

        # Chats and messages per account
        by_account = conn.execute("""
            SELECT a.email,
                   count(DISTINCT c.id) AS chats,
                   COALESCE(sum(c.message_count), 0) AS messages,
                   COALESCE(sum(c.canvas_count), 0) AS canvas
            FROM accounts a
            LEFT JOIN conversations c ON c.account_id = a.id
            GROUP BY a.id
            ORDER BY chats DESC
        """).fetchall()

        # Chats per provider (Gemini / ChatGPT / Claude)
        by_source = conn.execute(f"""
            SELECT COALESCE(c.source, 'gemini') AS source, count(*) AS n
            FROM conversations c
            {where}
            GROUP BY COALESCE(c.source, 'gemini')
            ORDER BY n DESC
        """, args).fetchall()

        # Message distribution by role
        role_join = "JOIN conversations c ON m.conversation_id = c.id"
        role_rows = conn.execute(f"""
            SELECT m.role, count(*) AS n
            FROM messages m {role_join}
            {where}
            GROUP BY m.role
        """, args).fetchall()

        # Canvas by type: count and total size
        canvas_by_type = conn.execute(f"""
            SELECT ca.type, count(*) AS n, COALESCE(sum(length(ca.content)), 0) AS chars
            FROM canvas_artifacts ca
            JOIN conversations c ON ca.conversation_id = c.id
            {where}
            GROUP BY ca.type
            ORDER BY n DESC
        """, args).fetchall()

        # Top chats by message count and by Canvas
        top_messages = conn.execute(f"""
            SELECT c.id, c.title, c.message_count
            FROM conversations c
            {where}
            ORDER BY c.message_count DESC
            LIMIT 10
        """, args).fetchall()

        top_canvas = conn.execute(f"""
            SELECT c.id, c.title, c.canvas_count
            FROM conversations c
            {where}
            {'AND' if where else 'WHERE'} c.canvas_count > 0
            ORDER BY c.canvas_count DESC
            LIMIT 10
        """, args).fetchall()

        return {
            "by_month": [dict(r) for r in by_month],
            "by_account": [dict(r) for r in by_account],
            "by_source": [dict(r) for r in by_source],
            "role_split": [dict(r) for r in role_rows],
            "canvas_by_type": [dict(r) for r in canvas_by_type],
            "top_messages": [dict(r) for r in top_messages],
            "top_canvas": [dict(r) for r in top_canvas],
        }

    def _api_accounts(self, conn):
        rows = conn.execute(
            "SELECT id, email, first_seen, last_export FROM accounts ORDER BY email"
        ).fetchall()
        return [dict(r) for r in rows]

    def _api_conversations(self, conn, params):
        account_id = params.get("account_id", [None])[0]
        limit = int(params.get("limit", ["100"])[0])
        offset = int(params.get("offset", ["0"])[0])
        q = params.get("q", [None])[0]

        if q:
            # Search via FTS5
            rows = conn.execute("""
                SELECT c.id, c.title, c.created_time, c.updated_time,
                       c.message_count, c.canvas_count, c.source, c.summary, a.email
                FROM conversations c
                JOIN accounts a ON c.account_id = a.id
                WHERE c.id IN (
                    SELECT conversation_id FROM messages
                    WHERE id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)
                )
                ORDER BY c.updated_time DESC
                LIMIT ? OFFSET ?
            """, (q, limit, offset)).fetchall()
        elif account_id:
            rows = conn.execute("""
                SELECT c.id, c.title, c.created_time, c.updated_time,
                       c.message_count, c.canvas_count, c.source, c.summary, a.email
                FROM conversations c
                JOIN accounts a ON c.account_id = a.id
                WHERE c.account_id = ?
                ORDER BY c.updated_time DESC
                LIMIT ? OFFSET ?
            """, (int(account_id), limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT c.id, c.title, c.created_time, c.updated_time,
                       c.message_count, c.canvas_count, c.source, c.summary, a.email
                FROM conversations c
                JOIN accounts a ON c.account_id = a.id
                ORDER BY c.updated_time DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        result = [dict(r) for r in rows]
        self._attach_tags(conn, result)
        return result

    def _attach_tags(self, conn, rows):
        """Attach a `tags` list (sorted) to each conversation dict in `rows`."""
        if not rows:
            return
        ids = [r["id"] for r in rows]
        qmarks = ",".join("?" * len(ids))
        tagmap: dict = {}
        for r in conn.execute(
            f"SELECT ct.conversation_id AS cid, t.name AS name "
            f"FROM conversation_tags ct JOIN tags t ON t.id = ct.tag_id "
            f"WHERE ct.conversation_id IN ({qmarks}) ORDER BY t.name", ids):
            tagmap.setdefault(r["cid"], []).append(r["name"])
        for r in rows:
            r["tags"] = tagmap.get(r["id"], [])

    def _api_conversation(self, conn, conv_id: str):
        conv = conn.execute("""
            SELECT c.*, a.email FROM conversations c
            JOIN accounts a ON c.account_id = a.id
            WHERE c.id = ?
        """, (conv_id,)).fetchone()
        if not conv:
            return {"error": "Not found"}

        messages = conn.execute("""
            SELECT seq, role, content, timestamp_ms
            FROM messages WHERE conversation_id = ?
            ORDER BY seq
        """, (conv_id,)).fetchall()

        canvas = conn.execute("""
            SELECT id, type, content, raw_html, parent_message_seq
            FROM canvas_artifacts WHERE conversation_id = ?
        """, (conv_id,)).fetchall()

        conv_d = dict(conv)  # includes summary/summary_model/... via c.*
        conv_d["tags"] = [r["name"] for r in conn.execute(
            "SELECT t.name FROM conversation_tags ct JOIN tags t ON t.id = ct.tag_id "
            "WHERE ct.conversation_id = ? ORDER BY t.name", (conv_id,))]

        return {
            "conversation": conv_d,
            "messages": [dict(m) for m in messages],
            "canvas_artifacts": [dict(c) for c in canvas],
        }

    def _api_canvas_list(self, conn, params):
        q = params.get("q", [None])[0]
        limit = int(params.get("limit", ["5000"])[0])

        if q:
            rows = conn.execute("""
                SELECT ca.id, ca.type, ca.conversation_id,
                       substr(ca.content, 1, 200) as preview,
                       ca.raw_html as source_path,
                       c.title as conv_title, a.email
                FROM canvas_fts
                JOIN canvas_artifacts ca ON canvas_fts.rowid = ca.rowid
                JOIN conversations c ON ca.conversation_id = c.id
                JOIN accounts a ON c.account_id = a.id
                WHERE canvas_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (q, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT ca.id, ca.type, ca.conversation_id,
                       substr(ca.content, 1, 200) as preview,
                       ca.raw_html as source_path,
                       c.title as conv_title, a.email
                FROM canvas_artifacts ca
                JOIN conversations c ON ca.conversation_id = c.id
                JOIN accounts a ON c.account_id = a.id
                ORDER BY ca.id LIMIT ?
            """, (limit,)).fetchall()

        return [dict(r) for r in rows]

    def _api_search(self, conn, params):
        q = params.get("q", [""])[0]
        scope = params.get("scope", ["all"])[0]
        limit = int(params.get("limit", ["50"])[0])

        if not q:
            return {"results": []}

        results = []

        if scope in ("all", "messages"):
            rows = conn.execute("""
                SELECT m.conversation_id, m.role, m.content, m.seq,
                       c.title, a.email,
                       snippet(messages_fts, 0, '<mark>', '</mark>', '…', 40) as snippet
                FROM messages_fts
                JOIN messages m ON messages_fts.rowid = m.id
                JOIN conversations c ON m.conversation_id = c.id
                JOIN accounts a ON c.account_id = a.id
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (q, limit)).fetchall()
            for r in rows:
                results.append({
                    "type": "message", **dict(r)
                })

        if scope in ("all", "canvas"):
            rows = conn.execute("""
                SELECT ca.id as artifact_id, ca.conversation_id, ca.type,
                       ca.content, c.title, a.email,
                       snippet(canvas_fts, 0, '<mark>', '</mark>', '…', 40) as snippet
                FROM canvas_fts
                JOIN canvas_artifacts ca ON canvas_fts.rowid = ca.rowid
                JOIN conversations c ON ca.conversation_id = c.id
                JOIN accounts a ON c.account_id = a.id
                WHERE canvas_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (q, limit)).fetchall()
            for r in rows:
                results.append({
                    "type": "canvas", **dict(r)
                })

        return {"query": q, "total": len(results), "results": results}

    def _api_canvas(self, conn, art_id: str):
        row = conn.execute("""
            SELECT ca.*, c.title as conv_title, a.email
            FROM canvas_artifacts ca
            JOIN conversations c ON ca.conversation_id = c.id
            JOIN accounts a ON c.account_id = a.id
            WHERE ca.id = ?
        """, (art_id,)).fetchone()
        if not row:
            return {"error": "Not found"}
        return dict(row)

    # ── Export the whole vault as ZIP (server-side, no JS deps) ──

    @staticmethod
    def _safe_name(text: str, limit: int = 80) -> str:
        text = (text or "untitled").strip()
        text = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", text)
        text = re.sub(r"\s+", " ", text).strip(" .")
        return (text[:limit] or "untitled")

    def _build_chat_markdown(self, conn, conv: dict) -> str:
        """Builds a single chat as Markdown with frontmatter."""
        conv_id = conv["id"]
        messages = conn.execute(
            "SELECT seq, role, content FROM messages WHERE conversation_id = ? ORDER BY seq",
            (conv_id,),
        ).fetchall()
        canvas = conn.execute(
            "SELECT type, content FROM canvas_artifacts WHERE conversation_id = ?",
            (conv_id,),
        ).fetchall()

        lines = [
            "---",
            f"title: {conv.get('title') or 'Untitled'}",
            f"account: {conv.get('email') or ''}",
            f"created: {conv.get('created_time') or ''}",
            f"messages: {conv.get('message_count') or 0}",
            f"canvas: {conv.get('canvas_count') or 0}",
            "tags: [gemini-vault]",
            "---",
            "",
            f"# {conv.get('title') or 'Untitled'}",
            "",
        ]
        for m in messages:
            who = "👤 You" if m["role"] == "user" else "✦ Gemini"
            lines.append(f"### {who}")
            lines.append("")
            lines.append((m["content"] or "").strip())
            lines.append("")

        if canvas:
            lines.append("---")
            lines.append("")
            lines.append("## Canvas / documents")
            lines.append("")
            for art in canvas:
                lines.append(f"### {art['type'] or 'document'}")
                lines.append("")
                lines.append((art["content"] or "").strip())
                lines.append("")

        return "\n".join(lines)

    def _handle_export_zip(self, params):
        account_id = params.get("account_id", [None])[0]
        try:
            conn = get_db()
            args: tuple = (int(account_id),) if account_id else ()
            sql = """
                SELECT c.id, c.title, c.created_time, c.updated_time,
                       c.message_count, c.canvas_count, a.email
                FROM conversations c
                JOIN accounts a ON c.account_id = a.id
            """
            if account_id:
                sql += " WHERE c.account_id = ?"
            sql += " ORDER BY a.email, c.created_time"
            convs = [dict(r) for r in conn.execute(sql, args).fetchall()]

            buf = io.BytesIO()
            index = [
                "# Chatrove — export",
                "",
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                f"Chats: {len(convs)}",
                "",
                "| # | Chat | Account | Messages | Canvas |",
                "|---|-----|---------|-----------|--------|",
            ]
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                used: dict[str, int] = {}
                for i, conv in enumerate(convs, 1):
                    acc = self._safe_name(conv.get("email") or "unknown", 60)
                    base = f"{i:04d}_{self._safe_name(conv.get('title') or 'chat')}"
                    # In case of duplicate names
                    key = f"{acc}/{base}"
                    used[key] = used.get(key, 0) + 1
                    if used[key] > 1:
                        base = f"{base}_{used[key]}"
                    arcname = f"{acc}/{base}.md"
                    zf.writestr(arcname, self._build_chat_markdown(conn, conv))
                    index.append(
                        f"| {i} | {conv.get('title') or 'Untitled'} | "
                        f"{conv.get('email') or ''} | {conv.get('message_count') or 0} | "
                        f"{conv.get('canvas_count') or 0} |"
                    )
                zf.writestr("INDEX.md", "\n".join(index))
            conn.close()

            data = buf.getvalue()
            stamp = datetime.now().strftime("%Y%m%d")
            fname = f"gemini_vault_export_{stamp}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── Portable JSON archive (round-trips through parse_and_index) ──

    @staticmethod
    def _iso_to_ms(iso: str | None):
        """ISO string -> epoch ms (the format the importer expects). None -> None."""
        if not iso:
            return None
        try:
            s = iso.replace("Z", "+00:00")
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except (ValueError, TypeError):
            return None

    def _handle_export_json(self, params):
        """Exports data in the same JSON format that parse_and_index accepts.

        Lets you migrate the archive into another Chatrove WITHOUT data loss.
        If ?account_id=N is given, a single account is exported.
        """
        account_id = params.get("account_id", [None])[0]
        try:
            conn = get_db()
            if account_id:
                email_row = conn.execute(
                    "SELECT email FROM accounts WHERE id = ?", (int(account_id),)
                ).fetchone()
                email = email_row["email"] if email_row else "unknown"
                conv_rows = conn.execute(
                    "SELECT * FROM conversations WHERE account_id = ? ORDER BY created_time",
                    (int(account_id),),
                ).fetchall()
            else:
                email = "multiple@vault"
                conv_rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY account_id, created_time"
                ).fetchall()

            conversations = []
            total_msgs = total_canvas = 0
            for c in conv_rows:
                msgs = conn.execute(
                    "SELECT role, content, timestamp_ms FROM messages "
                    "WHERE conversation_id = ? ORDER BY seq",
                    (c["id"],),
                ).fetchall()
                canvas = conn.execute(
                    "SELECT id, type, content, raw_html, parent_message_seq "
                    "FROM canvas_artifacts WHERE conversation_id = ?",
                    (c["id"],),
                ).fetchall()
                total_msgs += len(msgs)
                total_canvas += len(canvas)
                conversations.append({
                    "id": c["id"],
                    "title": c["title"],
                    "messages": [
                        {"role": m["role"], "content": m["content"],
                         "timestamp": m["timestamp_ms"]}
                        for m in msgs
                    ],
                    "canvas_artifacts": [
                        {"id": a["id"], "type": a["type"], "content": a["content"],
                         "raw_html": a["raw_html"],
                         "parent_message_index": a["parent_message_seq"]}
                        for a in canvas
                    ],
                    "created_time": self._iso_to_ms(c["created_time"]),
                    "updated_time": self._iso_to_ms(c["updated_time"]),
                })
            conn.close()

            payload = {
                "export_metadata": {
                    "version": "1.0.0",
                    "export_date": datetime.now().isoformat(),
                    "account_email": email,
                    "total_conversations": len(conversations),
                    "total_messages": total_msgs,
                    "total_canvas_artifacts": total_canvas,
                    "errors": 0,
                    "source": "gemini_vault_export",
                },
                "conversations": conversations,
            }
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            stamp = datetime.now().strftime("%Y%m%d")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="gemini_vault_archive_{stamp}.json"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _json_response(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Local viewer: never cache, so file edits show up on refresh
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):
        first = str(args[0]) if args else ""
        if "/api/" in first or "favicon" in first:
            return
        super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(
        description="Chatrove Viewer — local viewer for Gemini chats",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP server port (default {DEFAULT_PORT})")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open the browser automatically")
    parser.add_argument("--version", action="version",
                        version=f"Chatrove Viewer {__version__}")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[!] Database not found: {DB_PATH}")
        print("    Import some data first:")
        print("      python processor/parse_and_index.py")
        print("      python processor/import_takeout.py /path/to/takeout.zip")
        sys.exit(1)

    url = f"http://{args.host}:{args.port}/"

    try:
        server = HTTPServer((args.host, args.port), VaultHandler)
    except OSError as e:
        # Port is busy: the server is probably already running in another window.
        print(f"[!] Could not bind port {args.port}: {e}")
        print(f"    Chatrove may already be running at {url}")
        print(f"    If the UI behaves oddly, close the old server window")
        print(f"    and restart, or pick another port: --port {args.port + 1}")
        if not args.no_open:
            webbrowser.open(url)
        return

    print(f"Chatrove Viewer {__version__}: {url}")
    print(f"DB: {DB_PATH}")
    print("Ctrl+C to stop.\n")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
