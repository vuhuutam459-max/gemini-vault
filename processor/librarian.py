"""Smart Librarian — auto-tagging and summarization for the Gemini Vault DB.

Walks conversations that still need processing and, via the FreeLLMAPI
gateway (see ``llm_client.py``), assigns concise topical tags and a short
summary. Results are written back into the SQLite DB:

  conversations.summary / summary_model / summarized_at   (summarization)
  tags(name) + conversation_tags(conversation_id, tag_id) (tagging)

Design notes
------------
* **Idempotent / cheap.** Rows already carrying ``tagged_at`` / ``summarized_at``
  are skipped, so re-running never re-spends tokens. Each conversation is
  committed individually — a crash or stop loses at most one chat's work.
* **Resilient.** A failure on one conversation is logged and skipped; the
  batch keeps going (free-tier providers throttle, and retries live in the
  client).
* **Opt-in.** If no API key is configured the client is disabled and this
  module does nothing — content never leaves the machine until the user
  wires up a key.

CLI:  python processor/librarian.py [--tag] [--summarize] [--limit N] [--log FILE]
(no --tag/--summarize flags  ->  do both)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # package vs. script execution
    from processor.parse_and_index import init_db, DB_PATH
    from processor.llm_client import LLMClient, LLMError
except ImportError:  # pragma: no cover
    from parse_and_index import init_db, DB_PATH
    from llm_client import LLMClient, LLMError

# Keep requests cheap: cap the transcript we send (head + tail of the chat).
MAX_TRANSCRIPT_CHARS = 6000

TAG_SYSTEM = (
    "You are a librarian that files chat transcripts. Assign concise topical "
    "tags. Reply with ONLY a JSON object of the form {\"tags\": [\"...\"]} "
    "containing 3 to 6 short tags (one or two words each), lowercase, written "
    "in the same language as the chat. No commentary."
)

SUMMARY_SYSTEM = (
    "You are a librarian. Summarize the chat in 2 to 4 sentences, in the same "
    "language as the chat. Capture the topic and outcome. No preamble, no "
    "markdown, no bullet points."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_transcript(conn: sqlite3.Connection, conv_id: str,
                     max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Flatten a conversation to text, truncated to a token budget (head+tail)."""
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY seq",
        (conv_id,),
    ).fetchall()
    text = "\n\n".join(f"{role}: {content}" for role, content in rows)
    if len(text) <= max_chars:
        return text
    head = text[: max_chars * 2 // 3]
    tail = text[-max_chars // 3:]
    return f"{head}\n\n[... transcript truncated ...]\n\n{tail}"


def upsert_tags(conn: sqlite3.Connection, conv_id: str, names) -> list[str]:
    """Normalize, de-dup and link tags to a conversation. Returns stored names."""
    stored: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO conversation_tags(conversation_id, tag_id) VALUES (?, ?)",
            (conv_id, tag_id),
        )
        stored.append(name)
    return stored


def tag_conversation(conn: sqlite3.Connection, client: LLMClient,
                     conv_id: str, title: str) -> list[str]:
    transcript = build_transcript(conn, conv_id)
    user = (
        f"Chat title: {title or '(untitled)'}\n\n"
        f"Transcript:\n{transcript}\n\n"
        'Return JSON: {"tags": ["...", "..."]}'
    )
    obj = client.complete_json(user, system=TAG_SYSTEM)
    names = obj.get("tags", []) if isinstance(obj, dict) else obj
    stored = upsert_tags(conn, conv_id, names)
    conn.execute("UPDATE conversations SET tagged_at = ? WHERE id = ?",
                 (_now_iso(), conv_id))
    conn.commit()
    return stored


def summarize_conversation(conn: sqlite3.Connection, client: LLMClient,
                           conv_id: str, title: str) -> str:
    transcript = build_transcript(conn, conv_id)
    user = f"Chat title: {title or '(untitled)'}\n\nTranscript:\n{transcript}"
    summary = client.complete(user, system=SUMMARY_SYSTEM).strip()
    conn.execute(
        "UPDATE conversations SET summary = ?, summary_model = ?, summarized_at = ? "
        "WHERE id = ?",
        (summary, client.model, _now_iso(), conv_id),
    )
    conn.commit()
    return summary


def run(conn: sqlite3.Connection, client: LLMClient, *,
        do_tag: bool = True, do_summarize: bool = True,
        limit: int | None = None, log=print) -> dict:
    """Process conversations needing tags and/or a summary. Returns counts."""
    if not client.enabled:
        log("[librarian] FreeLLMAPI key not configured — nothing to do. "
            "Set FREELLMAPI_KEY or fill librarian_config.json.")
        return {"tagged": 0, "summarized": 0, "errors": 0, "skipped_disabled": True}

    clauses = []
    if do_tag:
        clauses.append("tagged_at IS NULL")
    if do_summarize:
        clauses.append("summarized_at IS NULL")
    if not clauses:
        return {"tagged": 0, "summarized": 0, "errors": 0}

    sql = (f"SELECT id, title, tagged_at, summarized_at FROM conversations "
           f"WHERE {' OR '.join(clauses)} ORDER BY updated_time ASC")  # oldest first
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    counts = {"tagged": 0, "summarized": 0, "errors": 0}
    total = len(rows)
    log(f"[librarian] {total} conversation(s) to process "
        f"(tag={do_tag}, summarize={do_summarize}).")

    for i, (conv_id, title, tagged_at, summarized_at) in enumerate(rows, 1):
        label = (title or conv_id)[:60]
        try:
            if do_tag and tagged_at is None:
                names = tag_conversation(conn, client, conv_id, title)
                counts["tagged"] += 1
                log(f"[{i}/{total}] tagged   {label!r} -> {names}")
            if do_summarize and summarized_at is None:
                summarize_conversation(conn, client, conv_id, title)
                counts["summarized"] += 1
                log(f"[{i}/{total}] summarized {label!r}")
        except LLMError as exc:
            counts["errors"] += 1
            log(f"[{i}/{total}] ERROR on {label!r}: {exc}")

    log(f"[librarian] done. tagged={counts['tagged']} "
        f"summarized={counts['summarized']} errors={counts['errors']}")
    return counts


def _make_logger(log_path: str | None):
    if not log_path:
        return print
    fh = open(log_path, "w", encoding="utf-8")

    def log(msg):
        print(msg)
        fh.write(f"{msg}\n")
        fh.flush()

    return log


def main(argv=None):
    ap = argparse.ArgumentParser(description="Smart Librarian: auto-tag and summarize the vault.")
    ap.add_argument("--tag", action="store_true", help="generate tags")
    ap.add_argument("--summarize", action="store_true", help="generate summaries")
    ap.add_argument("--limit", type=int, default=None, help="process at most N conversations")
    ap.add_argument("--log", default=None, help="write progress to this file too")
    ap.add_argument("--db", default=None, help="path to gemini_vault.db (default: project root)")
    args = ap.parse_args(argv)

    # No flag given -> do both.
    do_tag, do_summarize = args.tag, args.summarize
    if not do_tag and not do_summarize:
        do_tag = do_summarize = True

    log = _make_logger(args.log)
    client = LLMClient()
    if not client.enabled:
        log("[librarian] No API key configured; the Smart Librarian is opt-in. "
            "Nothing was sent anywhere.")
        return

    conn = init_db(Path(args.db) if args.db else DB_PATH)
    try:
        run(conn, client, do_tag=do_tag, do_summarize=do_summarize,
            limit=args.limit, log=log)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
