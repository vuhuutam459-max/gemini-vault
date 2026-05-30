"""
import_takeout.py
=================
Bridge: Takeout ZIP -> Gemini Vault SQLite.

Scans Google Takeout ZIP archives, finds Gemini data
(JSON logs, My Activity HTML, Canvas documents) and imports
them into gemini_vault.db WITHOUT fully extracting to disk.

Usage:
  python import_takeout.py path/to/takeout-001.zip
  python import_takeout.py path/to/folder/with/zips/
  python import_takeout.py   # looks for ZIPs in the project's parent folder

Reuses TakeoutArchive from takeout_extractor for streaming.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "gemini_vault.db"
CANVAS_DIR = BASE_DIR / "Canvas_Files"

# Add takeout_extractor to the path to reuse its parsers
TAKEOUT_DIR = BASE_DIR.parent / "takeout_extractor"
if TAKEOUT_DIR.exists():
    sys.path.insert(0, str(TAKEOUT_DIR))

from parse_and_index import init_db, get_or_create_account, generate_manifest, print_stats

try:
    from parsers import convert_gemini_json, convert_gemini_html, is_gemini_path, kind_of
except ImportError:
    from processor.parsers import convert_gemini_json, convert_gemini_html
    def is_gemini_path(name: str) -> bool:
        n = name.lower().replace("\\", "/")
        return any(seg in n for seg in ("/gemini/", "/my activity/gemini", "/myactivity/gemini", "bard"))
    def kind_of(path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext == ".json": return "json"
        if ext in (".html", ".htm"): return "html"
        return "other"


# Pattern for Gemini files inside Takeout
GEMINI_PATTERNS = [
    re.compile(r"gemini", re.I),
    re.compile(r"my\s*activity.*gemini", re.I),
    re.compile(r"bard", re.I),
]

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB max per file


def find_takeout_zips(search_path: Path) -> list[Path]:
    """Finds all Takeout ZIPs in the given folder."""
    if search_path.is_file() and search_path.suffix.lower() == ".zip":
        return [search_path]

    zips = sorted(search_path.glob("takeout-*.zip"))
    if not zips:
        zips = sorted(search_path.glob("*.zip"))
    return zips


def is_gemini_file(name: str) -> bool:
    """Checks whether a file belongs to Gemini data."""
    n = name.lower().replace("\\", "/")
    return any(p.search(n) for p in GEMINI_PATTERNS)


def extract_email_from_zip(zf: zipfile.ZipFile) -> str:
    """Tries to detect the account email from the archive contents."""
    for info in zf.infolist():
        name = info.filename.lower()
        if "account" in name and name.endswith(".json"):
            try:
                data = json.loads(zf.read(info.filename).decode("utf-8", "replace"))
                if isinstance(data, dict):
                    for key in ("email", "account_email", "id"):
                        if key in data and "@" in str(data[key]):
                            return data[key]
            except Exception:
                pass
    return "takeout_import"


def parse_gemini_content(raw: bytes, filename: str) -> dict | None:
    """Parses the contents of a Gemini file into an import structure."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None

    ext = Path(filename).suffix.lower()
    basename = Path(filename).stem

    conversation = {
        "id": f"takeout_{hashlib.md5(filename.encode()).hexdigest()[:16]}",
        "title": basename,
        "messages": [],
        "canvas_artifacts": [],
        "created_time": None,
        "updated_time": None,
        "source": "takeout",
        "original_path": filename,
    }

    if ext == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None

        # TAKEOUT GEMINI FORMAT: a list of chats with chat_messages
        # [{"uuid": ..., "name": ..., "chat_messages": [{"sender": "human", "text": ...}]}]
        if isinstance(data, list) and data and isinstance(data[0], dict) and "chat_messages" in data[0]:
            return _parse_takeout_conversations(data, filename)

        # Format 1: {"messages": [...]}
        if isinstance(data, dict) and "messages" in data:
            for msg in data["messages"]:
                conversation["messages"].append({
                    "role": _normalize_role(msg.get("role") or msg.get("sender", "model")),
                    "content": msg.get("text") or msg.get("content") or msg.get("message") or "",
                    "timestamp": _parse_ts(msg.get("timestamp") or msg.get("created_at")),
                })
            return conversation

        # Format 2: [{"title": ..., "prompt": ..., "response": ...}] (My Activity)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("header") or item.get("name")
                if title:
                    conversation["title"] = title

                prompt = item.get("prompt") or item.get("user_query") or item.get("query")
                response = item.get("response") or item.get("answer") or item.get("model_response")

                if prompt:
                    conversation["messages"].append({
                        "role": "user",
                        "content": str(prompt),
                        "timestamp": _parse_ts(item.get("time") or item.get("timestamp") or item.get("created_at")),
                    })
                if response:
                    conversation["messages"].append({
                        "role": "model",
                        "content": str(response),
                        "timestamp": _parse_ts(item.get("time") or item.get("timestamp") or item.get("created_at")),
                    })

                if "messages" in item and isinstance(item["messages"], list):
                    for msg in item["messages"]:
                        if isinstance(msg, dict):
                            conversation["messages"].append({
                                "role": _normalize_role(msg.get("role") or msg.get("sender", "model")),
                                "content": msg.get("text") or msg.get("content") or "",
                                "timestamp": _parse_ts(msg.get("timestamp") or msg.get("created_at")),
                            })

            if conversation["messages"]:
                return conversation
            return None

        # Format 3: {"conversations": [...]}
        if isinstance(data, dict) and "conversations" in data:
            convs = data["conversations"]
            if isinstance(convs, list) and convs and isinstance(convs[0], dict) and "chat_messages" in convs[0]:
                return _parse_takeout_conversations(convs, filename)
            for conv in convs:
                if isinstance(conv, dict) and "messages" in conv:
                    for msg in conv["messages"]:
                        conversation["messages"].append({
                            "role": _normalize_role(msg.get("role") or msg.get("sender", "model")),
                            "content": msg.get("text") or msg.get("content") or "",
                            "timestamp": _parse_ts(msg.get("timestamp") or msg.get("created_at")),
                        })
            if conversation["messages"]:
                return conversation

        return None

    elif ext in (".html", ".htm"):
        # HTML file — convert via the parser
        md_text = convert_gemini_html(text)
        if md_text and len(md_text) > 50:
            conversation["messages"].append({
                "role": "model",
                "content": md_text,
                "timestamp": None,
            })
            return conversation

    return None


def _normalize_role(role: str) -> str:
    """human/assistant/user/model -> user/model."""
    r = (role or "").lower().strip()
    if r in ("human", "user"):
        return "user"
    return "model"


def _parse_takeout_conversations(items: list[dict], filename: str) -> dict | None:
    """Parses the Takeout Gemini format: an array of chats with chat_messages.

    Each element is a separate chat. This format:
    {"uuid": ..., "name": ..., "chat_messages": [{"sender": "human", "text": ...}]}

    Returns the FIRST chat (the rest are handled by
    parse_gemini_content_multi, which creates one conversation per chat).
    """
    # Return None — actual handling happens in parse_gemini_content_multi
    return None


def parse_gemini_content_multi(raw: bytes, filename: str) -> list[dict]:
    """Like parse_gemini_content, but for files with multiple chats.
    Returns a list of conversations."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return []

    ext = Path(filename).suffix.lower()
    if ext != ".json":
        # For HTML — a single conversation
        single = parse_gemini_content(raw, filename)
        return [single] if single and single["messages"] else []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    # Takeout Gemini format: an array of chats
    if isinstance(data, list) and data and isinstance(data[0], dict) and "chat_messages" in data[0]:
        results = []
        for item in data:
            conv = _takeout_item_to_conversation(item)
            if conv and conv["messages"]:
                results.append(conv)
        return results

    # Other formats — a single conversation
    single = parse_gemini_content(raw, filename)
    return [single] if single and single.get("messages") else []


def _takeout_item_to_conversation(item: dict) -> dict | None:
    """Converts a single chat from the Takeout format into the import format."""
    conv_uuid = item.get("uuid", "")
    if not conv_uuid:
        return None

    conv = {
        "id": conv_uuid,
        "title": item.get("name") or item.get("summary") or f"Chat {conv_uuid[:8]}",
        "messages": [],
        "canvas_artifacts": [],
        "created_time": _parse_ts(item.get("created_at")),
        "updated_time": _parse_ts(item.get("updated_at")),
        "source": "takeout",
    }

    for msg in item.get("chat_messages", []):
        if not isinstance(msg, dict):
            continue

        sender = msg.get("sender", "")
        role = _normalize_role(sender)

        # Main text
        text = msg.get("text", "")

        # If text is empty, build it from content[]
        if not text and isinstance(msg.get("content"), list):
            parts = []
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            text = "\n".join(parts)

        if not text:
            continue

        conv["messages"].append({
            "role": role,
            "content": text,
            "timestamp": _parse_ts(msg.get("created_at")),
        })

        # Files/attachments as Canvas artifacts
        for f in msg.get("files", []):
            if isinstance(f, dict) and f.get("name"):
                conv["canvas_artifacts"].append({
                    "id": f.get("uuid", f"file_{conv_uuid[:8]}_{len(conv['canvas_artifacts'])}"),
                    "type": "file",
                    "content": f.get("name", ""),
                    "raw_html": json.dumps(f, ensure_ascii=False),
                    "parent_message_index": len(conv["messages"]) - 1,
                })

    return conv


def _parse_ts(val: Any) -> int | None:
    """Tries to parse a timestamp into milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val > 1e12:
            return int(val)
        if val > 1e9:
            return int(val * 1000)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
    return None


CANVAS_EXTENSIONS = {".docx", ".doc", ".pdf", ".txt", ".pptx", ".xlsx", ".xls"}


def extract_docx_text(raw: bytes) -> str:
    """Extracts text from a .docx (a ZIP with XML inside). No external deps."""
    try:
        import io
        inner_zip = zipfile.ZipFile(io.BytesIO(raw), "r")
        if "word/document.xml" not in inner_zip.namelist():
            return ""
        xml = inner_zip.read("word/document.xml").decode("utf-8", "replace")
        inner_zip.close()
        paragraphs = []
        for p_match in re.finditer(r'<w:p[^>]*>(.*?)</w:p>', xml, re.DOTALL):
            p_xml = p_match.group(1)
            texts = re.findall(r'<w:t[^>]*>([^<]*)</w:t>', p_xml)
            line = "".join(texts)
            paragraphs.append(line)
        text = "\n".join(p for p in paragraphs if p)
        if not text:
            text = "\n".join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', xml))
        return text.strip()
    except Exception:
        return ""


def extract_txt_text(raw: bytes) -> str:
    """Extracts text from .txt/.doc."""
    for enc in ("utf-8", "cp1251", "cp866", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_takeout_zip(
    zip_path: Path, conn: sqlite3.Connection, account_id: int
) -> dict:
    """Imports Gemini data from a single ZIP."""
    stats = {"zip": zip_path.name, "scanned": 0, "gemini_files": 0,
             "imported": 0, "skipped": 0, "errors": 0, "canvas": 0}

    try:
        zf = zipfile.ZipFile(str(zip_path), "r", allowZip64=True)
    except (zipfile.BadZipFile, OSError) as e:
        print(f"  [!] Cannot open ZIP: {e}")
        stats["errors"] += 1
        return stats

    for info in zf.infolist():
        if info.is_dir():
            continue
        stats["scanned"] += 1

        if not is_gemini_file(info.filename):
            continue
        stats["gemini_files"] += 1

        if info.file_size > MAX_FILE_SIZE:
            ext = Path(info.filename).suffix.lower()
            if ext not in (".mp4", ".mov", ".avi", ".wav", ".mp3"):
                print(f"  [!] Skipping (too large: {info.file_size / 1024 / 1024:.0f}MB): {info.filename}")
            stats["skipped"] += 1
            continue

        ext = Path(info.filename).suffix.lower()

        # Canvas documents: .docx, .pdf, .txt, .pptx, .xlsx
        if ext in CANVAS_EXTENSIONS:
            try:
                raw = zf.read(info.filename)
                saved = _save_canvas_from_zip(conn, account_id, info, raw, ext)
                if saved:
                    stats["canvas"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                print(f"  [!] Canvas error: {info.filename}: {e}")
                stats["errors"] += 1
            continue

        # Media files — skip silently
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov",
                    ".avi", ".wav", ".mp3", ".m4a", ".webm", ".heic"):
            stats["skipped"] += 1
            continue

        # JSON/HTML — chats
        if ext not in (".json", ".html", ".htm"):
            stats["skipped"] += 1
            continue

        try:
            raw = zf.read(info.filename)
        except Exception as e:
            print(f"  [!] Read error: {info.filename}: {e}")
            stats["errors"] += 1
            continue

        conversations = parse_gemini_content_multi(raw, info.filename)
        if not conversations:
            stats["skipped"] += 1
            continue

        for conv in conversations:
            try:
                from parse_and_index import import_conversation
                is_new, _ = import_conversation(conn, account_id, conv)
                if is_new:
                    stats["imported"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                print(f"  [!] DB error: {info.filename}: {e}")
                stats["errors"] += 1

    zf.close()
    return stats


def _save_canvas_from_zip(
    conn: sqlite3.Connection, account_id: int,
    info: zipfile.ZipInfo, raw: bytes, ext: str,
) -> bool:
    """Saves a Canvas document from the ZIP to a file + indexes it in the DB."""
    filename = info.filename
    basename = Path(filename).stem
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', basename)[:120]

    # Extract the text content
    text_content = ""
    if ext == ".docx":
        text_content = extract_docx_text(raw)
    elif ext in (".txt", ".doc"):
        text_content = extract_txt_text(raw)
    elif ext == ".pdf":
        text_content = f"[PDF document: {basename}, {len(raw) // 1024} KB]"
    elif ext in (".xlsx", ".xls"):
        text_content = f"[Spreadsheet: {basename}, {len(raw) // 1024} KB]"
    elif ext == ".pptx":
        text_content = f"[Presentation: {basename}, {len(raw) // 1024} KB]"

    if not text_content:
        text_content = f"[Document: {basename}{ext}, {len(raw) // 1024} KB]"

    # File hash for the ID
    art_id = f"canvas_{hashlib.md5(filename.encode()).hexdigest()[:16]}"

    # Find which chat it belongs to — by the hash in the file name
    # Format: "document name-HASHHASHHASH.docx"
    hash_match = re.search(r'-([a-f0-9]{16})\.[a-z]+$', filename.lower())
    conv_id = None
    if hash_match:
        file_hash = hash_match.group(1)
        # Try to find the conversation by a match
        row = conn.execute(
            "SELECT id FROM conversations WHERE id LIKE ? LIMIT 1",
            (f"%{file_hash}%",)
        ).fetchone()
        if row:
            conv_id = row[0]

    # If there is no link — create a "virtual" chat for Canvas documents
    if not conv_id:
        conv_id = f"canvas_collection_{account_id}"
        existing = conn.execute(
            "SELECT id FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO conversations (id, account_id, title, message_count, canvas_count, import_hash)
                VALUES (?, ?, 'Canvas Documents', 0, 0, 'canvas_collection')
            """, (conv_id, account_id))

    # Insert the Canvas artifact
    conn.execute("""
        INSERT OR REPLACE INTO canvas_artifacts (id, conversation_id, type, content, raw_html, parent_message_seq)
        VALUES (?, ?, ?, ?, ?, NULL)
    """, (art_id, conv_id, ext.lstrip('.'), text_content, filename))

    # Update the conversation's canvas counter
    conn.execute("""
        UPDATE conversations SET canvas_count = (
            SELECT count(*) FROM canvas_artifacts WHERE conversation_id = ?
        ) WHERE id = ?
    """, (conv_id, conv_id))

    conn.commit()
    return True


def main():
    args = sys.argv[1:]

    # Decide where to look for ZIPs
    if args and not args[0].startswith("--"):
        search_path = Path(args[0])
    else:
        search_path = BASE_DIR.parent  # folder with Takeout ZIPs

    zips = find_takeout_zips(search_path)
    if not zips:
        print(f"No ZIP files found in: {search_path}")
        return

    print(f"Gemini Vault -- Import from Takeout")
    print(f"Found {len(zips)} ZIP(s) in {search_path}")
    print()

    conn = init_db(DB_PATH)

    # Try to detect the email from the first archive
    email = "takeout_import"
    try:
        with zipfile.ZipFile(str(zips[0]), "r") as zf:
            email = extract_email_from_zip(zf)
    except Exception:
        pass

    account_id = get_or_create_account(conn, email)
    print(f"Account: {email}\n")

    all_stats = []
    for z in zips:
        print(f"* {z.name}")
        st = import_takeout_zip(z, conn, account_id)
        all_stats.append(st)
        print(f"  scanned: {st['scanned']}, gemini: {st['gemini_files']}, "
              f"imported: {st['imported']}, canvas: {st.get('canvas', 0)}, "
              f"skipped: {st['skipped']}, errors: {st['errors']}")

    conn.commit()

    total_imported = sum(s["imported"] for s in all_stats)
    total_canvas = sum(s.get("canvas", 0) for s in all_stats)
    total_gemini = sum(s["gemini_files"] for s in all_stats)
    total_errors = sum(s["errors"] for s in all_stats)

    print(f"\n{'=' * 50}")
    print(f"  Gemini files found:   {total_gemini}")
    print(f"  Chats imported to DB: {total_imported}")
    print(f"  Canvas extracted:     {total_canvas}")
    print(f"  Errors:               {total_errors}")
    print(f"{'=' * 50}")

    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
