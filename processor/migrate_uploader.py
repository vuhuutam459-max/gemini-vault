"""
migrate_uploader.py — DEPRECATED / EXPERIMENTAL
================================================
⚠️ DEPRECATED. Not recommended for use.

The recommended way to "migrate" to another account is the portable
JSON archive: in the viewer click "⤓ Export → 📦 JSON archive (migrate)"
(or GET /api/export-json). Such an archive imports into another
Chatrove 1:1, WITHOUT losing the original texts and WITHOUT the risk
of an account ban.

This module does NOT transfer the conversation; it RE-SENDS your
prompts to Gemini's internal API: the answers will differ, it burns
quota, and it violates Google's ToS. Kept for reference only.

 *** WARNING ***
 This module violates Google's Terms of Service (ToS).
 Using it may get your account banned.
 Google's internal APIs change without notice — the code may
 stop working at any moment.
 Use ONLY at your own risk.
 It is recommended to test on an empty / disposable account.
 *** WARNING ***

Usage:
  python migrate_uploader.py --cookies cookies.txt --source vault.db
  python migrate_uploader.py --cookies cookies.txt --json export.json --dry-run
  python migrate_uploader.py --help

Cookies can be exported via:
  - "EditThisCookie" extension -> Export as Netscape format
  - "Cookie-Editor" extension -> Export -> Netscape
  - DevTools -> Application -> Cookies -> copy manually

Minimum required cookies: __Secure-1PSID, __Secure-1PSIDTS
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "gemini_vault.db"

GEMINI_URL = "https://gemini.google.com"
BATCH_URL = f"{GEMINI_URL}/_/BardChatUi/data/batchexecute"

DELAY_BETWEEN_MESSAGES_S = 3.0
DELAY_BETWEEN_CONVERSATIONS_S = 5.0


def load_cookies(cookie_file: Path) -> dict[str, str]:
    """Loads cookies from Netscape format or JSON."""
    text = cookie_file.read_text(encoding="utf-8")

    # JSON format (Cookie-Editor export)
    if text.strip().startswith("["):
        cookies = {}
        for c in json.loads(text):
            if ".google.com" in c.get("domain", ""):
                cookies[c["name"]] = c["value"]
        return cookies

    # Netscape format
    jar = MozillaCookieJar()
    # MozillaCookieJar requires a file — use it directly
    jar.filename = str(cookie_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        # Manual Netscape format parsing
        cookies = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and ".google.com" in parts[0]:
                cookies[parts[5]] = parts[6]
        return cookies

    cookies = {}
    for cookie in jar:
        if ".google.com" in cookie.domain:
            cookies[cookie.name] = cookie.value
    return cookies


def get_csrf_token(cookies: dict[str, str]) -> str | None:
    """Fetches the CSRF token (SNlM0e) from the Gemini page."""
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    req = Request(GEMINI_URL, headers={
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[ERR] Cannot load Gemini page: {e}")
        return None

    match = re.search(r'SNlM0e":"([^"]+)"', html)
    if match:
        return match.group(1)

    print("[ERR] CSRF token (SNlM0e) not found in page.")
    print("      Possible reasons: cookies expired, account locked, or API changed.")
    return None


def send_message(
    cookies: dict[str, str],
    csrf_token: str,
    message: str,
    conversation_id: str | None = None,
    dry_run: bool = False,
) -> dict | None:
    """Sends one message to Gemini. Returns the response or None."""

    if dry_run:
        preview = message[:80].replace("\n", " ")
        print(f"  [DRY-RUN] Would send: \"{preview}...\"")
        return {"status": "dry_run"}

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # Build the payload for batch execute
    # This structure can change with Google updates
    inner_payload = json.dumps([
        [message],
        None,
        [conversation_id] if conversation_id else None,
    ])

    req_data = json.dumps([[
        ["MkEWBc", inner_payload, None, "generic"]
    ]])

    body = urlencode({
        "f.req": req_data,
        "at": csrf_token,
    }).encode("utf-8")

    req = Request(BATCH_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": GEMINI_URL,
        "Referer": f"{GEMINI_URL}/",
    })

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # Strip the anti-XSRF prefix
        if raw.startswith(")]}'"):
            raw = raw[raw.index("\n") + 1:]

        # Try to find the conversation ID in the response
        conv_match = re.search(r'"(c_[a-zA-Z0-9_-]+)"', raw)
        new_conv_id = conv_match.group(1) if conv_match else conversation_id

        return {"status": "ok", "conversation_id": new_conv_id, "response_length": len(raw)}

    except Exception as e:
        print(f"  [ERR] Send failed: {e}")
        return None


def migrate_from_db(
    db_path: Path,
    cookies: dict[str, str],
    csrf_token: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Migrates chats from SQLite into a new account."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    query = "SELECT id, title FROM conversations ORDER BY created_time ASC"
    if limit:
        query += f" LIMIT {limit}"
    conversations = conn.execute(query).fetchall()

    stats = {"total": len(conversations), "sent": 0, "errors": 0, "skipped": 0}

    print(f"\nMigration: {len(conversations)} conversations")
    if dry_run:
        print("[DRY-RUN MODE — nothing will be sent]\n")
    print()

    for i, conv in enumerate(conversations):
        conv_id = conv["id"]
        title = conv["title"]
        print(f"[{i+1}/{len(conversations)}] {title}")

        messages = conn.execute("""
            SELECT role, content FROM messages
            WHERE conversation_id = ? ORDER BY seq
        """, (conv_id,)).fetchall()

        if not messages:
            print("  (empty, skipping)")
            stats["skipped"] += 1
            continue

        # Send only user messages (Gemini will answer on its own)
        user_messages = [m for m in messages if m["role"] == "user"]
        if not user_messages:
            print("  (no user messages, skipping)")
            stats["skipped"] += 1
            continue

        target_conv_id = None

        for j, msg in enumerate(user_messages):
            content = msg["content"]
            if not content.strip():
                continue

            # First message — without conversation_id (creates a new chat)
            result = send_message(
                cookies, csrf_token,
                content,
                conversation_id=target_conv_id,
                dry_run=dry_run,
            )

            if result and result.get("status") == "ok":
                stats["sent"] += 1
                if result.get("conversation_id"):
                    target_conv_id = result["conversation_id"]
                print(f"  [{j+1}/{len(user_messages)}] sent OK")
            elif result and result.get("status") == "dry_run":
                stats["sent"] += 1
            else:
                stats["errors"] += 1
                print(f"  [{j+1}/{len(user_messages)}] FAILED")

            # Delay between messages
            if j < len(user_messages) - 1:
                time.sleep(DELAY_BETWEEN_MESSAGES_S)

        # Delay between chats
        if i < len(conversations) - 1:
            time.sleep(DELAY_BETWEEN_CONVERSATIONS_S)

    conn.close()
    return stats


def migrate_from_json(
    json_path: Path,
    cookies: dict[str, str],
    csrf_token: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Migrates chats from a JSON export file."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    conversations = data.get("conversations", [])
    if limit:
        conversations = conversations[:limit]

    stats = {"total": len(conversations), "sent": 0, "errors": 0, "skipped": 0}

    print(f"\nMigration from JSON: {len(conversations)} conversations")
    if dry_run:
        print("[DRY-RUN MODE]\n")
    print()

    for i, conv in enumerate(conversations):
        title = conv.get("title", f"Chat {i+1}")
        print(f"[{i+1}/{len(conversations)}] {title}")

        messages = conv.get("messages", [])
        user_messages = [m for m in messages if m.get("role") == "user"]

        if not user_messages:
            print("  (no user messages, skipping)")
            stats["skipped"] += 1
            continue

        target_conv_id = None

        for j, msg in enumerate(user_messages):
            content = msg.get("content", "")
            if not content.strip():
                continue

            result = send_message(
                cookies, csrf_token,
                content,
                conversation_id=target_conv_id,
                dry_run=dry_run,
            )

            if result and result.get("status") in ("ok", "dry_run"):
                stats["sent"] += 1
                if result.get("conversation_id"):
                    target_conv_id = result["conversation_id"]
            else:
                stats["errors"] += 1

            if j < len(user_messages) - 1:
                time.sleep(DELAY_BETWEEN_MESSAGES_S)

        if i < len(conversations) - 1:
            time.sleep(DELAY_BETWEEN_CONVERSATIONS_S)

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Chatrove — Migrate chats to another account (EXPERIMENTAL)",
        epilog="WARNING: This violates Google ToS and may result in account ban.",
    )
    parser.add_argument("--cookies", required=True, type=Path,
                        help="Path to cookies file (Netscape or JSON format)")
    parser.add_argument("--source", type=Path, default=None,
                        help="Path to gemini_vault.db (default: auto-detect)")
    parser.add_argument("--json", type=Path, default=None,
                        help="Path to JSON export file (alternative to --source)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without actually sending messages")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of conversations to migrate")

    args = parser.parse_args()

    print("=" * 60)
    print("  CHATROVE — ACCOUNT MIGRATION (EXPERIMENTAL)")
    print("=" * 60)
    print()
    print("  [!] This feature violates Google Terms of Service.")
    print("  [!] Your account may be suspended or banned.")
    print("  [!] Internal APIs may change at any time.")
    print("  [!] Use at your own risk.")
    print()

    if not args.dry_run:
        print("  This is NOT a dry run. Messages WILL be sent.")
        try:
            answer = input("  Type 'YES' to continue: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return
        if answer.strip() != "YES":
            print("  Cancelled.")
            return

    # Load cookies
    print("\nLoading cookies...")
    cookies = load_cookies(args.cookies)
    required = {"__Secure-1PSID", "__Secure-1PSIDTS"}
    missing = required - set(cookies.keys())
    if missing:
        print(f"[ERR] Missing required cookies: {missing}")
        print("      Export cookies from your browser (EditThisCookie / Cookie-Editor)")
        sys.exit(1)
    print(f"  Loaded {len(cookies)} cookies")

    # Get CSRF token
    print("Fetching CSRF token...")
    csrf = get_csrf_token(cookies)
    if not csrf:
        sys.exit(1)
    print(f"  Token: {csrf[:20]}...")

    # Migrate
    if args.json:
        stats = migrate_from_json(args.json, cookies, csrf,
                                   dry_run=args.dry_run, limit=args.limit)
    else:
        db = args.source or DB_PATH
        if not db.exists():
            print(f"[ERR] Database not found: {db}")
            sys.exit(1)
        stats = migrate_from_db(db, cookies, csrf,
                                 dry_run=args.dry_run, limit=args.limit)

    print(f"\n{'=' * 40}")
    print(f"  Total: {stats['total']}")
    print(f"  Sent:  {stats['sent']}")
    print(f"  Errors: {stats['errors']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"{'=' * 40}")


if __name__ == "__main__":
    main()
