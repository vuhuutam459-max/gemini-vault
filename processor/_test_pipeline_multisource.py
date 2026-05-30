"""End-to-end: a ChatGPT export flows through parse_and_index into SQLite."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor.parse_and_index import init_db, process_export_file

chatgpt_raw = [{
    "title": "Recipe chat",
    "conversation_id": "cg-1",
    "create_time": 1716620400.0,
    "update_time": 1716620500.0,
    "current_node": "m2",
    "mapping": {
        "root": {"id": "root", "message": None, "parent": None, "children": ["m1"]},
        "m1": {"id": "m1", "parent": "root", "children": ["m2"],
               "message": {"author": {"role": "user"}, "create_time": 1716620400.0,
                           "content": {"content_type": "text", "parts": ["How do I bake sourdough?"]}}},
        "m2": {"id": "m2", "parent": "m1", "children": [],
               "message": {"author": {"role": "assistant"}, "create_time": 1716620420.0,
                           "content": {"content_type": "text", "parts": ["Mix flour, water and starter."]}}},
    },
}]

tmp = tempfile.mkdtemp()
try:
    db = Path(tmp) / "ms.db"
    src = Path(tmp) / "chatgpt_export.json"
    src.write_text(json.dumps(chatgpt_raw, ensure_ascii=False), encoding="utf-8")

    conn = init_db(db)
    stats = process_export_file(conn, src)
    print(f"  import stats: {stats}")
    assert stats["new"] == 1, stats
    assert stats["errors"] == 0

    # Account grouped under the ChatGPT label
    acct = conn.execute("SELECT email FROM accounts").fetchone()[0]
    print(f"  account: {acct}")
    assert acct == "ChatGPT"

    # Conversation stored with provenance
    row = conn.execute("SELECT id, title, source FROM conversations").fetchone()
    print(f"  conversation: {row}")
    assert row[0] == "chatgpt_cg-1"
    assert row[2] == "chatgpt", "source column must record provenance"

    # Roles mapped correctly
    roles = [r[0] for r in conn.execute(
        "SELECT role FROM messages ORDER BY seq")]
    print(f"  roles: {roles}")
    assert roles == ["user", "model"], roles

    # FTS search reaches imported ChatGPT content
    hits = conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'sourdough'"
    ).fetchone()[0]
    print(f"  FTS 'sourdough': {hits}")
    assert hits == 1

    # Re-import is deduplicated
    stats2 = process_export_file(conn, src)
    assert stats2["skipped"] == 1, stats2
    print("  dedup on re-import: OK")

    conn.close()
    print("\nALL OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
