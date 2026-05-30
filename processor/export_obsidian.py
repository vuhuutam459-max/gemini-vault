"""
export_obsidian.py
==================
Convert Gemini Vault SQLite -> Obsidian Vault.

Each chat -> a separate .md file with YAML frontmatter.
Canvas artifacts -> nested notes with [[wiki-links]].

Usage:
  python export_obsidian.py                    # export to ./Obsidian_Export/
  python export_obsidian.py --dest ~/my-vault  # export to the given folder
  python export_obsidian.py --account user@gmail.com  # a single account only
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "gemini_vault.db"
DEFAULT_DEST = BASE_DIR / "Obsidian_Export"


def safe_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_. ')
    return name[:max_len] or "untitled"


def format_timestamp(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or ""


def export_vault(db_path: Path, dest: Path, account_filter: str | None = None):
    if not db_path.exists():
        print(f"⚠ Database not found: {db_path}")
        print("  Run first: python processor/parse_and_index.py")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Determine the accounts
    if account_filter:
        accounts = conn.execute(
            "SELECT id, email FROM accounts WHERE email = ?", (account_filter,)
        ).fetchall()
        if not accounts:
            print(f"Account '{account_filter}' not found in the database.")
            conn.close()
            return
    else:
        accounts = conn.execute("SELECT id, email FROM accounts").fetchall()

    total_files = 0
    total_canvas = 0

    for acc in accounts:
        acc_dir = dest / safe_filename(acc["email"])
        chats_dir = acc_dir / "Chats"
        canvas_dir = acc_dir / "Canvas"
        chats_dir.mkdir(parents=True, exist_ok=True)
        canvas_dir.mkdir(parents=True, exist_ok=True)

        # Account index file
        write_account_index(conn, acc, acc_dir)

        # Chats
        conversations = conn.execute("""
            SELECT id, title, created_time, updated_time, message_count, canvas_count
            FROM conversations WHERE account_id = ?
            ORDER BY updated_time DESC
        """, (acc["id"],)).fetchall()

        print(f"\n[Account] {acc['email']}: {len(conversations)} chats")

        for conv in conversations:
            filepath = export_conversation(conn, conv, chats_dir, canvas_dir)
            total_files += 1

            canvas_arts = conn.execute(
                "SELECT * FROM canvas_artifacts WHERE conversation_id = ?",
                (conv["id"],)
            ).fetchall()
            for art in canvas_arts:
                export_canvas_artifact(art, canvas_dir, conv["title"])
                total_canvas += 1

    conn.close()

    print(f"\n[OK] Export complete -> {dest}")
    print(f"   Chats: {total_files}")
    print(f"   Canvas files: {total_canvas}")


def write_account_index(conn, acc, acc_dir: Path):
    convs = conn.execute("""
        SELECT title, message_count, updated_time FROM conversations
        WHERE account_id = ? ORDER BY updated_time DESC
    """, (acc["id"],)).fetchall()

    lines = [
        f"# {acc['email']}",
        "",
        f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Chats: {len(convs)}",
        "",
        "## Chats",
        "",
    ]

    for c in convs:
        title = c["title"] or "Untitled"
        fname = safe_filename(title)
        lines.append(f"- [[Chats/{fname}|{title}]] ({c['message_count']} msgs, "
                      f"{format_timestamp(c['updated_time'])})")

    (acc_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def export_conversation(conn, conv, chats_dir: Path, canvas_dir: Path) -> Path:
    title = conv["title"] or "Untitled"
    fname = safe_filename(title)
    filepath = chats_dir / f"{fname}.md"

    # Handle duplicate names
    counter = 1
    while filepath.exists():
        filepath = chats_dir / f"{fname}_{counter}.md"
        counter += 1

    messages = conn.execute("""
        SELECT seq, role, content, timestamp_ms
        FROM messages WHERE conversation_id = ?
        ORDER BY seq
    """, (conv["id"],)).fetchall()

    canvas = conn.execute("""
        SELECT id, type, parent_message_seq
        FROM canvas_artifacts WHERE conversation_id = ?
    """, (conv["id"],)).fetchall()

    # YAML frontmatter
    lines = [
        "---",
        f"title: \"{title.replace(chr(34), chr(39))}\"",
        f"conversation_id: {conv['id']}",
        f"created: {format_timestamp(conv['created_time'])}",
        f"updated: {format_timestamp(conv['updated_time'])}",
        f"messages: {conv['message_count']}",
        f"canvas_artifacts: {conv['canvas_count']}",
        "tags: [gemini-vault]",
        "---",
        "",
        f"# {title}",
        "",
    ]

    # Canvas map: seq -> canvas artifacts
    canvas_map: dict[int, list] = {}
    for art in canvas:
        seq = art["parent_message_seq"]
        if seq is not None:
            canvas_map.setdefault(seq, []).append(art)

    # Messages
    for msg in messages:
        role = msg["role"]
        content = msg["content"] or ""
        role_label = "**You:**" if role == "user" else "**Gemini:**"

        lines.append(f"### {role_label}")
        lines.append("")
        lines.append(content)
        lines.append("")

        # Canvas links after the message
        for art in canvas_map.get(msg["seq"], []):
            art_fname = safe_filename(art["id"])
            lines.append(f"> 📄 Canvas: [[Canvas/{art_fname}|{art['type']} document]]")
            lines.append("")

    # Orphan canvas (not tied to a message)
    orphan = [a for a in canvas if a["parent_message_seq"] is None]
    if orphan:
        lines.append("---")
        lines.append("")
        lines.append("## Canvas artifacts")
        lines.append("")
        for art in orphan:
            art_fname = safe_filename(art["id"])
            lines.append(f"- [[Canvas/{art_fname}|{art['type']} document]]")
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


def export_canvas_artifact(art, canvas_dir: Path, conv_title: str):
    fname = safe_filename(art["id"])
    filepath = canvas_dir / f"{fname}.md"

    content = art["content"] or ""
    art_type = art["type"] or "text"

    lines = [
        "---",
        f"artifact_id: {art['id']}",
        f"conversation_id: {art['conversation_id']}",
        f"type: {art_type}",
        f"parent_chat: \"{conv_title.replace(chr(34), chr(39))}\"",
        "tags: [gemini-canvas]",
        "---",
        "",
        f"# Canvas: {art_type}",
        "",
        f"> From chat: [[Chats/{safe_filename(conv_title)}|{conv_title}]]",
        "",
        content,
    ]

    filepath.write_text("\n".join(lines), encoding="utf-8")


def main():
    dest = DEFAULT_DEST
    account = None

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--dest" and i + 1 < len(args):
            dest = Path(args[i + 1])
        if arg == "--account" and i + 1 < len(args):
            account = args[i + 1]

    export_vault(DB_PATH, dest, account)


if __name__ == "__main__":
    main()
