"""Smoke test: verifies the full pipeline parse -> FTS5 search -> obsidian export."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor.parse_and_index import init_db, process_export_file, generate_manifest
from processor.export_obsidian import export_vault, safe_filename

print("== parse_and_index ==")

tmp = tempfile.mkdtemp()
try:
    db_path = Path(tmp) / "test_vault.db"

    # Build a fake JSON dump
    fake_export = {
        "export_metadata": {
            "version": "1.0.0",
            "export_date": "2026-05-25T12:00:00Z",
            "account_email": "test@gmail.com",
            "total_conversations": 2,
            "total_messages": 5,
            "total_canvas_artifacts": 1,
            "errors": 0,
            "source": "smoke_test",
        },
        "conversations": [
            {
                "id": "conv-001-test",
                "title": "Hello, Gemini",
                "messages": [
                    {"role": "user", "content": "Hi! Write some Python code.", "timestamp": 1716620400000},
                    {"role": "model", "content": "Sure! Here is an example:\n```python\nprint('hello')\n```", "timestamp": 1716620401000},
                    {"role": "user", "content": "Thanks — I had a café and wrote my résumé.", "timestamp": 1716620402000},
                ],
                "canvas_artifacts": [
                    {
                        "id": "canvas-test-001",
                        "type": "code",
                        "content": "```python\ndef hello():\n    print('world')\n```",
                        "raw_html": "<pre><code>def hello():\n    print('world')</code></pre>",
                        "parent_message_index": 1,
                    }
                ],
                "created_time": 1716620400000,
                "updated_time": 1716620402000,
            },
            {
                "id": "conv-002-test",
                "title": "Math and quantum physics",
                "messages": [
                    {"role": "user", "content": "Explain the Schrodinger equation", "timestamp": 1716700000000},
                    {"role": "model", "content": "Equation: $$i\\hbar\\frac{\\partial}{\\partial t}\\Psi = \\hat{H}\\Psi$$", "timestamp": 1716700001000},
                ],
                "canvas_artifacts": [],
                "created_time": 1716700000000,
                "updated_time": 1716700001000,
            },
        ],
    }

    json_path = Path(tmp) / "test_export.json"
    json_path.write_text(json.dumps(fake_export, ensure_ascii=False), encoding="utf-8")

    # Import
    conn = init_db(db_path)
    stats = process_export_file(conn, json_path)
    print(f"  Import: {stats}")
    assert stats["new"] == 2, f"Expected 2 new, got {stats['new']}"
    assert stats["errors"] == 0

    # Check the data
    conv_count = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
    msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    canvas_count = conn.execute("SELECT count(*) FROM canvas_artifacts").fetchone()[0]
    print(f"  DB: {conv_count} chats, {msg_count} messages, {canvas_count} artifacts")
    assert conv_count == 2
    assert msg_count == 5
    assert canvas_count == 1

    # Re-import — deduplication
    stats2 = process_export_file(conn, json_path)
    print(f"  Re-import: {stats2}")
    assert stats2["skipped"] == 2, "Deduplication did not work"

    # FTS5 search
    print("\n== FTS5 search ==")

    # Search over messages
    fts_results = conn.execute("""
        SELECT m.conversation_id, snippet(messages_fts, 0, '[', ']', '...', 20) as snip
        FROM messages_fts
        JOIN messages m ON messages_fts.rowid = m.id
        WHERE messages_fts MATCH 'Python'
    """).fetchall()
    print(f"  'Python': {len(fts_results)} results")
    assert len(fts_results) >= 1, "FTS5 did not find 'Python'"

    # Unicode / diacritics search (tokenizer uses remove_diacritics)
    fts_uni = conn.execute("""
        SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'cafe'
    """).fetchone()[0]
    print(f"  'cafe' (diacritics): {fts_uni} results")
    assert fts_uni >= 1, "FTS5 did not handle diacritics"

    # Canvas search
    fts_canvas = conn.execute("""
        SELECT count(*) FROM canvas_fts WHERE canvas_fts MATCH 'hello'
    """).fetchone()[0]
    print(f"  Canvas 'hello': {fts_canvas} results")
    assert fts_canvas >= 1, "FTS5 did not find it in canvas"

    # Manifest
    manifest = generate_manifest(conn)
    print(f"\n== Manifest ==")
    print(f"  accounts: {len(manifest['accounts'])}")
    print(f"  totals: {manifest['totals']}")
    assert manifest["totals"]["conversations"] == 2

    conn.close()

    # Obsidian export
    print("\n== Obsidian export ==")
    obsidian_dest = Path(tmp) / "obsidian_out"
    export_vault(db_path, obsidian_dest)

    # Check the files
    md_files = list(obsidian_dest.rglob("*.md"))
    print(f"  .md files: {len(md_files)}")
    assert len(md_files) >= 3, f"Too few files: {md_files}"

    # Check the contents of one chat
    # safe_filename("test@gmail.com") -> "test@gmail.com" (@ is allowed in names)
    acc_folder = safe_filename("test@gmail.com")
    chat_files = list((obsidian_dest / acc_folder / "Chats").glob("*.md"))
    assert len(chat_files) >= 2, f"Too few chats: {chat_files}"

    sample = chat_files[0].read_text(encoding="utf-8")
    assert "---" in sample, "No frontmatter"
    assert "tags:" in sample, "No tags"
    print(f"  Sample: {chat_files[0].name} ({len(sample)} chars)")

    # Check safe_filename
    assert safe_filename('test/file:name*?') == 'test_file_name'
    assert safe_filename('') == 'untitled'
    print("  safe_filename: OK")

    print("\nALL OK")

finally:
    # Cleanup: on Windows SQLite may keep the file locked,
    # so ignore deletion errors.
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
