"""Offline tests for processor.librarian — no network, no tokens spent.

A FakeGateway stands in for the LLMGateway interface and counts its calls, so we
can assert both correctness and idempotency (a second run must not call the LLM
again). Because the Librarian depends only on the gateway *interface*, the fake
needs no real client at all — that's the payoff of dependency injection.
Run:  python processor/_test_librarian.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor import parse_and_index as P
from processor import librarian as Lib


class FakeGateway:
    """Duck-typed stand-in for the LLMGateway interface. Counts its calls."""

    def __init__(self, available=True):
        self._available = available
        self.model = "fake-model"
        self.tag_calls = 0
        self.summary_calls = 0

    @property
    def available(self):
        return self._available

    def generate_json(self, prompt, *, system=None):
        # Combined tag+summary call returns both keys; dups/blanks on purpose.
        self.tag_calls += 1
        return {"tags": ["Python", "  python ", "AsyncIO", ""],
                "summary": "  A short summary of the chat.  "}

    def generate_text(self, prompt, *, system=None):
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
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
            for c in ("summary", "summary_model", "summarized_at", "tagged_at"):
                assert c in cols, f"missing column {c}"
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"tags", "conversation_tags"} <= tables
        finally:
            conn.close()  # Windows: release the file so TemporaryDirectory can delete it


def test_tag_and_summarize_then_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        conn = P.init_db(Path(tmp) / "v.db")
        try:
            _seed(conn)
            gateway = FakeGateway()

            counts = Lib.run(conn, gateway, do_tag=True, do_summarize=True, log=lambda *_: None)
            assert counts["tagged"] == 1 and counts["summarized"] == 1

            # Combined path: one LLM call covers both tags and summary.
            assert gateway.tag_calls == 1 and gateway.summary_calls == 0, \
                (gateway.tag_calls, gateway.summary_calls)

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
            before = (gateway.tag_calls, gateway.summary_calls)
            counts2 = Lib.run(conn, gateway, do_tag=True, do_summarize=True, log=lambda *_: None)
            assert counts2["tagged"] == 0 and counts2["summarized"] == 0
            assert (gateway.tag_calls, gateway.summary_calls) == before, "idempotency broken"
        finally:
            conn.close()


def test_disabled_client_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        conn = P.init_db(Path(tmp) / "v.db")
        try:
            _seed(conn)
            counts = Lib.run(conn, FakeGateway(available=False), log=lambda *_: None)
            assert counts.get("skipped_disabled") is True
            assert counts["tagged"] == 0 and counts["summarized"] == 0
        finally:
            conn.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK  {t.__name__}")
    print(f"\nAll {len(tests)} librarian tests passed.")


if __name__ == "__main__":
    main()
