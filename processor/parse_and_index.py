"""
parse_and_index.py
==================
Import Gemini Vault JSON dumps → SQLite + FTS5.

Usage:
  python parse_and_index.py                    # all .json/.zip from Source_Accounts/
  python parse_and_index.py path/to/export.json  # a specific file
  python parse_and_index.py path/to/chatgpt.zip  # ChatGPT/Claude export ZIP (as-is)
  python parse_and_index.py --stats            # database statistics
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Multi-source adapters (ChatGPT/Claude → canonical). Works whether this file
# is run as a script (processor/ on sys.path) or imported as a package.
try:
    from importers import normalize as _normalize_export
except ImportError:  # pragma: no cover
    from processor.importers import normalize as _normalize_export

# ── Paths ──
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "gemini_vault.db"
SOURCE_DIR = BASE_DIR / "Source_Accounts"
CANVAS_DIR = BASE_DIR / "Canvas_Files"
MANIFEST_PATH = BASE_DIR / "export_manifest.json"

# ── DB schema ──
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE NOT NULL,
    first_seen TEXT NOT NULL,
    last_export TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    title       TEXT NOT NULL DEFAULT '',
    created_time TEXT,
    updated_time TEXT,
    message_count INTEGER DEFAULT 0,
    canvas_count  INTEGER DEFAULT 0,
    import_hash  TEXT,
    source       TEXT DEFAULT 'gemini',
    UNIQUE(id, account_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL CHECK(role IN ('user', 'model', 'system')),
    content         TEXT NOT NULL,
    timestamp_ms    INTEGER,
    UNIQUE(conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS canvas_artifacts (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    type            TEXT NOT NULL DEFAULT 'text',
    content         TEXT NOT NULL,
    raw_html        TEXT,
    parent_message_seq INTEGER,
    UNIQUE(id, conversation_id)
);

-- FTS5 for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS canvas_fts USING fts5(
    content,
    content=canvas_artifacts,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS canvas_ai AFTER INSERT ON canvas_artifacts BEGIN
    INSERT INTO canvas_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS canvas_ad AFTER DELETE ON canvas_artifacts BEGIN
    INSERT INTO canvas_fts(canvas_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
END;
"""

# ── Utilities ──

def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ts_to_iso(ts_ms: int | None) -> str | None:
    if ts_ms is None or not isinstance(ts_ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return None


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    # Backward-compat migration: add 'source' to databases created before
    # multi-source support (CREATE TABLE IF NOT EXISTS won't add columns).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
    if "source" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN source TEXT DEFAULT 'gemini'")
    conn.commit()
    return conn


# ── Import ──

def get_or_create_account(conn: sqlite3.Connection, email: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
    if row:
        conn.execute("UPDATE accounts SET last_export = ? WHERE id = ?", (now, row[0]))
        return row[0]
    cur = conn.execute(
        "INSERT INTO accounts (email, first_seen, last_export) VALUES (?, ?, ?)",
        (email, now, now),
    )
    return cur.lastrowid


def import_conversation(
    conn: sqlite3.Connection, account_id: int, conv: dict
) -> tuple[bool, str]:
    """Imports a single chat. Returns (is_new_or_updated, conv_id)."""
    conv_id = conv.get("id", "")
    if not conv_id:
        return False, ""

    content_hash = sha256_of(json.dumps(conv, sort_keys=True, ensure_ascii=False))

    existing = conn.execute(
        "SELECT import_hash FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()

    if existing and existing[0] == content_hash:
        return False, conv_id

    title = conv.get("title", "")
    messages = conv.get("messages", [])
    canvas = conv.get("canvas_artifacts", [])

    created = ts_to_iso(conv.get("created_time"))
    updated = ts_to_iso(conv.get("updated_time"))
    source = conv.get("source", "gemini")

    # Upsert conversation
    conn.execute("""
        INSERT INTO conversations (id, account_id, title, created_time, updated_time,
                                   message_count, canvas_count, import_hash, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            updated_time = excluded.updated_time,
            message_count = excluded.message_count,
            canvas_count = excluded.canvas_count,
            import_hash = excluded.import_hash,
            source = excluded.source
    """, (conv_id, account_id, title, created, updated,
          len(messages), len(canvas), content_hash, source))

    # Delete old messages (overwrite)
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    conn.execute("DELETE FROM canvas_artifacts WHERE conversation_id = ?", (conv_id,))

    # Insert messages
    for seq, msg in enumerate(messages):
        role = msg.get("role", "model")
        if role not in ("user", "model", "system"):
            role = "model"
        content = msg.get("content", "")
        ts = msg.get("timestamp")
        conn.execute("""
            INSERT OR REPLACE INTO messages (conversation_id, seq, role, content, timestamp_ms)
            VALUES (?, ?, ?, ?, ?)
        """, (conv_id, seq, role, content, ts))

    # Insert Canvas artifacts
    for art in canvas:
        art_id = art.get("id", f"art_{conv_id}_{len(canvas)}")
        conn.execute("""
            INSERT OR REPLACE INTO canvas_artifacts
                (id, conversation_id, type, content, raw_html, parent_message_seq)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (art_id, conv_id, art.get("type", "text"),
              art.get("content", ""), art.get("raw_html", ""),
              art.get("parent_message_index")))

        # Save Canvas as a .md file
        save_canvas_file(conv_id, art_id, art)

    return True, conv_id


def save_canvas_file(conv_id: str, art_id: str, artifact: dict) -> None:
    CANVAS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r'[^\w\-]', '_', art_id)[:80]
    path = CANVAS_DIR / f"{safe_id}.md"

    content = artifact.get("content", "")
    art_type = artifact.get("type", "text")
    header = f"---\nartifact_id: {art_id}\nconversation_id: {conv_id}\ntype: {art_type}\n---\n\n"

    path.write_text(header + content, encoding="utf-8")


def _load_export_raw(filepath: Path):
    """Load the raw export object from a .json file or a ChatGPT/Claude .zip.

    ChatGPT and Claude "Export data" archives bundle a ``conversations.json``
    alongside other files — we read that member directly so the user can pass
    the .zip as-is. (Google Takeout archives have a different layout and are
    handled by import_takeout.py.)
    """
    if filepath.suffix.lower() == ".zip":
        with zipfile.ZipFile(filepath) as zf:
            member = next(
                (n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "conversations.json"),
                None,
            )
            if member is None:
                raise ValueError(
                    "no conversations.json inside the archive — for Google "
                    "Takeout ZIPs use import_takeout.py instead"
                )
            return json.loads(zf.read(member).decode("utf-8"))
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def process_export_file(conn: sqlite3.Connection, filepath: Path) -> dict:
    """Processes a single export file (.json, or a ChatGPT/Claude .zip)."""
    raw = _load_export_raw(filepath)

    # Normalize ChatGPT/Claude exports to canonical form. A file already in
    # canonical (Gemini Vault) form is returned unchanged.
    data = _normalize_export(raw)

    meta = data.get("export_metadata", {})
    email = meta.get("account_email", "unknown")
    conversations = data.get("conversations", [])

    account_id = get_or_create_account(conn, email)

    stats = {"file": filepath.name, "email": email, "total": len(conversations),
             "new": 0, "updated": 0, "skipped": 0, "errors": 0}

    for conv in conversations:
        try:
            is_new, _ = import_conversation(conn, account_id, conv)
            if is_new:
                stats["new"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            print(f"  [!] Failed to import chat {conv.get('id', '?')}: {e}")
            stats["errors"] += 1

    conn.commit()
    return stats


# ── Manifest ──

def generate_manifest(conn: sqlite3.Connection) -> dict:
    accounts = conn.execute("SELECT id, email, last_export FROM accounts").fetchall()
    total_convs = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
    total_msgs = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    total_canvas = conn.execute("SELECT count(*) FROM canvas_artifacts").fetchone()[0]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(DB_PATH),
        "accounts": [{"id": a[0], "email": a[1], "last_export": a[2]} for a in accounts],
        "totals": {
            "conversations": total_convs,
            "messages": total_msgs,
            "canvas_artifacts": total_canvas,
        },
    }


def print_stats(conn: sqlite3.Connection) -> None:
    m = generate_manifest(conn)
    print("\n═══ Gemini Vault — Database statistics ═══")
    print(f"  DB: {m['db_path']}")
    for a in m["accounts"]:
        print(f"  Account: {a['email']} (last export: {a['last_export']})")
    t = m["totals"]
    print(f"\n  Chats:          {t['conversations']}")
    print(f"  Messages:       {t['messages']}")
    print(f"  Canvas files:   {t['canvas_artifacts']}")
    print("═" * 42)


# ── CLI ──

def main():
    args = sys.argv[1:]

    if "--stats" in args:
        if not DB_PATH.exists():
            print("Database not found. Import some data first.")
            return
        conn = init_db(DB_PATH)
        print_stats(conn)
        conn.close()
        return

    conn = init_db(DB_PATH)

    # Determine which files to import
    if args and not args[0].startswith("--"):
        files = [Path(a) for a in args if Path(a).is_file()]
    else:
        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(SOURCE_DIR.glob("*.json")) + sorted(SOURCE_DIR.glob("*.zip"))

    if not files:
        print("No export files to import.")
        print(f"  Put export files (.json or ChatGPT/Claude .zip) into: {SOURCE_DIR}")
        print(f"  Or pass a path: python parse_and_index.py /path/to/export.zip")
        conn.close()
        return

    print(f"Gemini Vault — Importing into {DB_PATH}")
    print(f"Files to process: {len(files)}\n")

    all_stats = []
    for f in files:
        print(f"* {f.name}")
        try:
            stats = process_export_file(conn, f)
            all_stats.append(stats)
            print(f"   ✓ {stats['email']}: {stats['new']} new, "
                  f"{stats['skipped']} unchanged, {stats['errors']} errors")
        except Exception as e:
            print(f"   [ERR] Error: {e}")

    # Generate the manifest
    manifest = generate_manifest(conn)
    manifest["import_history"] = all_stats
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n[i] Manifest: {MANIFEST_PATH}")

    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
