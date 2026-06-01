"""Offline tests for processor.librarian — no network, no tokens spent.

A FakeClient stands in for the FreeLLMAPI gateway and counts its calls, so we
can assert both correctness and idempotency (a second run must not call the
LLM again). Run:  python processor/_test_librarian.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor import parse_and_index as P
from processor import librarian as Lib


class FakeClient:
    """Duck-typed stand-in for LLMClient. Records how many calls it received."""

    def __init__(self, enabled=True):
        self._enabled = enabled
        self.model = "fake-model"
        self.tag_calls = 0
        self.summary_calls = 0

    @property
    def enabled(self):
        return self._enabled

    def complete_json(self, user, **kwargs):
        self.tag_calls += 1
        return {"tags": ["Python", "  python ", "AsyncIO", ""]}  # dups/blanks on purpose

    def complete(self, user, **kwargs):
        self.summary_calls += 1
        return "  A short summary of the chat.  "


def _seed(conn):
    conn.execute(
        "INSERT INTO accounts(email, first_seen, last_export) VALUES (?, ?, ?)",
        ("demo@example.com", "2026-01-01", "2026-01-01"),
    )
    acc = conn.execute("SELECT id FROM accounts").fetchone()[0]
    conn.execute(
        "INSERT INTO conversations(id, account_id, title, updated_time) VALUES (?, ?, ?, ?)",
        ("c1", acc, "Async tutorial", "2026-01-02T00:00:00+00:00"),
    )
    for seq, (role, content) in enumerate([
        ("user", "Explain async/await"),
        ("model", "Asyncio lets you write concurrent code."),
    ]):
        conn.execute(
            "INSERT INTO messages(conversation_id, seq, role, content) VALUES (?, ?, ?, ?)",
            ("c1", seq, role, content),
        )
    conn.commit()


def test_migration_adds_columns_and_tables():
    with tempfile.TemporaryDirectory() as tmp:
        conn = P.init_db(Path(tmp) / "v.db")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
        for c in ("summary", "summary_model", "summarized_at", "tagged_at"):
            assert c in cols, f"missing column {c}"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tags", "conversation_tags"} <= tables
        conn.close()


def test_tag_and_summarize_then_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        conn = P.init_db(Path(tmp) / "v.db")
        _seed(conn)
        client = FakeClient()

        counts = Lib.run(conn, client, do_tag=True, do_summarize=True, log=lambda *_: None)
        assert counts["tagged"] == 1 and counts["summarized"] == 1

        # tags normalized + de-duped: "Python"/" python " collapse to one
        names = sorted(r[0] for r in conn.execute(
            "SELECT t.name FROM tags t "
            "JOIN conversation_tags ct ON ct.tag_id = t.id WHERE ct.conversation_id='c1'"))
        assert names == ["asyncio", "python"], names

        row = conn.execute(
            "SELECT summary, summary_model, summarized_at, tagged_at "
            "FROM conversations WHERE id='c1'").fetchone()
        assert row[0] == "A short summary of the chat."   # trimmed
        assert row[1] == "fake-model"
        assert row[2] is not None and row[3] is not None    # timestamps set

        # Second run must skip everything — no further LLM calls.
        before = (client.tag_calls, client.summary_calls)
        counts2 = Lib.run(conn, client, do_tag=True, do_summarize=True, log=lambda *_: None)
        assert counts2["tagged"] == 0 and counts2["summarized"] == 0
        assert (client.tag_calls, client.summary_calls) == before, "idempotency broken"
        conn.close()


def test_disabled_client_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        conn = P.init_db(Path(tmp) / "v.db")
        _seed(conn)
        counts = Lib.run(conn, FakeClient(enabled=False), log=lambda *_: None)
        assert counts.get("skipped_disabled") is True
        assert counts["tagged"] == 0 and counts["summarized"] == 0
        conn.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK  {t.__name__}")
    print(f"\nAll {len(tests)} librarian tests passed.")


if __name__ == "__main__":
    main()
