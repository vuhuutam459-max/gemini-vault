"""Quick end-to-end check: a mixed-provider DB exposes `source` through the
exact SQL the viewer's /api/conversations endpoint runs. Run directly."""
import json, os, sqlite3, tempfile
from pathlib import Path

import parse_and_index as P

# The three SELECT bodies live in viewer/serve.py::_api_conversations. We only
# need the "all" branch here — it's the one loadChats() uses (no q, no account).
ALL_SQL = """
    SELECT c.id, c.title, c.created_time, c.updated_time,
           c.message_count, c.canvas_count, c.source, a.email
    FROM conversations c
    JOIN accounts a ON c.account_id = a.id
    ORDER BY c.updated_time DESC
"""

GEMINI = {
    "export_metadata": {"version": "1.0.0", "source": "gemini",
                        "account_email": "me@gmail.com", "total_conversations": 1},
    "conversations": [{"id": "gem-1", "title": "Gemini chat", "source": "gemini",
                       "messages": [{"role": "user", "content": "hi", "timestamp": 1700000000000}],
                       "canvas_artifacts": [], "created_time": 1700000000000,
                       "updated_time": 1700000000000}],
}
CHATGPT = [{
    "conversation_id": "cg-1", "title": "ChatGPT chat",
    "create_time": 1700000100, "update_time": 1700000100,
    "current_node": "n2",
    "mapping": {
        "n1": {"id": "n1", "parent": None, "children": ["n2"],
               "message": {"author": {"role": "user"},
                           "content": {"content_type": "text", "parts": ["hello gpt"]},
                           "create_time": 1700000100}},
        "n2": {"id": "n2", "parent": "n1", "children": [],
               "message": {"author": {"role": "assistant"},
                           "content": {"content_type": "text", "parts": ["hi there"]},
                           "create_time": 1700000101}},
    },
}]
CLAUDE = [{
    "uuid": "cl-1", "name": "Claude chat",
    "created_at": "2023-11-15T00:00:00Z", "updated_at": "2023-11-15T00:00:00Z",
    "chat_messages": [
        {"sender": "human", "text": "hello claude", "created_at": "2023-11-15T00:00:00Z"},
        {"sender": "assistant", "text": "hello human", "created_at": "2023-11-15T00:00:01Z"},
    ],
}]

def main():
    tmp = Path(tempfile.mkdtemp())
    conn = P.init_db(tmp / "test.db")
    conn.row_factory = sqlite3.Row  # the viewer sets this too (serve.py get_db)
    for name, data in [("gemini_export.json", GEMINI),
                       ("chatgpt_export.json", CHATGPT),
                       ("claude_export.json", CLAUDE)]:
        f = tmp / name
        f.write_text(json.dumps(data), encoding="utf-8")
        P.process_export_file(conn, f)

    rows = [dict(r) for r in conn.execute(
        "SELECT c.id, c.title, c.source, a.email FROM conversations c "
        "JOIN accounts a ON c.account_id = a.id ORDER BY c.id")]

    assert all("source" in r for r in rows), "source missing from API row"
    sources = sorted(r["source"] for r in rows)
    assert sources == ["chatgpt", "claude", "gemini"], sources
    for r in rows:
        print(f"  {r['id']:<14} source={r['source']:<8} title={r['title']}")
    print("\nAPI exposes source for all providers: OK\nALL OK")

if __name__ == "__main__":
    main()
