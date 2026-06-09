"""
scrape_gemini_url.py
====================
Scraping chats and Canvas from gemini.google.com via Playwright.

Uses the installed Chrome (not bundled Chromium) to avoid "browser too old"
errors on Google's login page. Persistent profile remembers the session.

Usage:
  python scrape_gemini_url.py "https://gemini.google.com/app/CHAT_ID"
  python scrape_gemini_url.py --list-all
  python scrape_gemini_url.py --list-all --account user@gmail.com
  python scrape_gemini_url.py --list-all --log /path/to/log.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

PROCESSOR_DIR = Path(__file__).resolve().parent
BASE_DIR = PROCESSOR_DIR.parent
DB_PATH = BASE_DIR / "gemini_vault.db"
CANVAS_DIR = BASE_DIR / "Canvas_Files"
PROFILE_DIR = BASE_DIR / ".browser_profile"

if str(PROCESSOR_DIR) not in sys.path:
    sys.path.insert(0, str(PROCESSOR_DIR))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


class Logger:
    """Prints to stdout and optionally writes to a log file."""

    def __init__(self, log_file=None):
        self.log_file = log_file

    def __call__(self, msg):
        print(msg, flush=True)
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except Exception:
                pass


def ensure_playwright():
    if not HAS_PLAYWRIGHT:
        print("[ERROR] Playwright not installed.")
        print("    pip install playwright")
        print("    playwright install chromium")
        sys.exit(1)


def get_browser(pw, headed=True):
    """Opens browser with persistent profile. Prefers installed Chrome."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        headless=not headed,
        viewport={"width": 1400, "height": 900},
        locale="ru-RU",
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        return pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", **kwargs
        )
    except Exception:
        return pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), **kwargs
        )


def wait_for_gemini_load(page, timeout=15000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    time.sleep(2)


def wait_for_messages(page, timeout=12000):
    """Waits for Angular to render conversation turns."""
    try:
        page.wait_for_selector("user-query, model-response", timeout=timeout)
    except Exception:
        pass
    time.sleep(1)


def wait_for_login(page, log, timeout=600):
    """Waits for user to log in via the browser window (up to 10 min)."""
    log("[STATUS] Login required -- log in in the browser window (10 min timeout)")
    start = time.time()
    last_url = ""
    while time.time() - start < timeout:
        try:
            url = page.url
        except Exception:
            time.sleep(2)
            continue

        if url != last_url:
            elapsed = int(time.time() - start)
            log(f"[LOGIN] {elapsed}s -- {url[:100]}")
            last_url = url

        # Success: we're on Gemini
        if "gemini.google.com" in url and "accounts.google.com" not in url:
            log("[STATUS] Login successful! On Gemini page.")
            time.sleep(3)
            return True

        # User finished login but landed on some other Google page
        # (consent, account chooser done, myaccount, etc.)
        if ("accounts.google.com" not in url
                and "signin" not in url.lower()
                and "google.com" in url):
            log(f"[STATUS] Login done, redirecting to Gemini... (was: {url[:80]})")
            try:
                page.goto("https://gemini.google.com/app")
                wait_for_gemini_load(page)
                if "gemini.google.com" in page.url:
                    log("[STATUS] Login successful! Redirected to Gemini.")
                    return True
            except Exception:
                pass

        time.sleep(3)

    log("[ERROR] Login timeout (10 min). Close the browser and try again.")
    return False


def extract_chat_from_page(page) -> dict:
    """Extracts chat data from current Gemini page."""
    url = page.url
    title = page.title().replace(" - Gemini", "").replace(" - Google", "").strip()

    conv_id_match = re.search(r'/(?:app|c)/([a-f0-9-]+)', url)
    conv_id = conv_id_match.group(1) if conv_id_match else f"web_{hashlib.md5(url.encode()).hexdigest()[:16]}"

    messages = page.evaluate(r"""() => {
        const msgs = [];
        // Gemini (Angular) uses custom elements: <user-query>, <model-response>.
        // querySelectorAll returns them in DOM order = conversation order.
        const turns = document.querySelectorAll('user-query, model-response');
        for (const el of turns) {
            const tag = el.tagName.toLowerCase();
            let role, contentEl;
            if (tag === 'user-query') {
                role = 'user';
                contentEl = el.querySelector('user-query-content') || el;
            } else {
                role = 'model';
                contentEl = el.querySelector('message-content') || el;
            }
            let text = (contentEl.innerText || '').trim();
            // Strip screen-reader label prefixes
            text = text.replace(/^Ваш запрос\s*/, '')
                       .replace(/^Ответ Gemini\s*/, '')
                       .replace(/^Your query\s*/, '')
                       .replace(/^Gemini'?s? response\s*/i, '')
                       .trim();
            if (text) msgs.push({ role, content: text });
        }
        return msgs;
    }""")

    # Canvas artifacts are extracted separately via extract_canvas_artifacts()
    # (they require clicking immersive-entry-chip to mount the editor panel).
    return {
        "id": conv_id,
        "title": title or f"Chat from {conv_id[:12]}",
        "url": url,
        "messages": messages,
        "canvas_artifacts": [],
        "source": "web_scrape",
    }


def extract_canvas_artifacts(page, conv_id, log) -> list[dict]:
    """Opens each Canvas (immersive) chip and extracts the document content.

    Gemini mounts the <immersive-editor> only after clicking a chip's "Open"
    button, so passive DOM reads miss Canvas entirely. We scroll to wake up
    lazy-rendered chips, click each, read the editor, then close the panel.
    """
    # Quick exit: no Canvas chips at all in this chat
    chip_count = page.evaluate("() => document.querySelectorAll('immersive-entry-chip').length")
    if not chip_count:
        return []

    # Scroll conversation to mount lazy chip cards (down then back up)
    for _ in range(6):
        page.evaluate("""() => {
            const sc = document.querySelector('[class*="conversation-container"]') ||
                       document.querySelector('main [class*="scroll"]') ||
                       document.querySelector('main');
            if (sc) sc.scrollTop = sc.scrollHeight;
        }""")
        time.sleep(0.3)
    page.evaluate("""() => {
        const sc = document.querySelector('[class*="conversation-container"]') ||
                   document.querySelector('main [class*="scroll"]') ||
                   document.querySelector('main');
        if (sc) sc.scrollTop = 0;
    }""")
    time.sleep(1)

    chip_count = page.evaluate("() => document.querySelectorAll('immersive-entry-chip').length")
    artifacts = []
    seen = set()

    for i in range(chip_count):
        try:
            clicked = page.evaluate("""(idx) => {
                const chips = document.querySelectorAll('immersive-entry-chip');
                if (idx >= chips.length) return false;
                const btn = chips[idx].querySelector('button, [role="button"]');
                if (btn) { btn.click(); return true; }
                return false;
            }""", i)
            if not clicked:
                continue

            try:
                page.wait_for_selector("immersive-editor", timeout=8000)
            except Exception:
                continue
            time.sleep(1.5)

            data = page.evaluate(r"""() => {
                const editor = document.querySelector('immersive-editor');
                const content = editor ? (editor.innerText || '').trim() : '';
                const titleEl = document.querySelector('immersive-panel .title-text') ||
                                document.querySelector('immersive-panel [class*="title-text"]') ||
                                document.querySelector('immersive-panel [class*="overflow-title"]');
                const title = titleEl ? titleEl.innerText.trim() : '';
                const isCode = !!(editor && editor.querySelector('.cm-content, code, pre'));
                return { title, content, type: isCode ? 'code' : 'document' };
            }""")

            content = (data.get("content") or "").strip()
            title = (data.get("title") or "").strip()

            if content and len(content) > 20:
                ch = hashlib.md5(content.encode("utf-8")).hexdigest()
                if ch not in seen:
                    seen.add(ch)
                    full = (f"# {title}\n\n" if title else "") + content
                    artifacts.append({
                        "id": f"canvas_{conv_id}_{len(artifacts)}",
                        "type": data.get("type", "document"),
                        "content": full,
                        "raw_html": "",  # empty => real Canvas (not Takeout upload)
                        "parent_message_index": None,
                    })
                    log(f"    Canvas[{i + 1}/{chip_count}]: '{title[:40]}' ({len(content)} ch)")

            # Close panel before next chip
            page.evaluate("""() => {
                const b = document.querySelector('immersive-panel button[aria-label*="Закрыть"]') ||
                          document.querySelector('button[aria-label*="Close"]');
                if (b) b.click();
            }""")
            time.sleep(0.5)
        except Exception as e:
            log(f"    Canvas[{i + 1}] error: {e}")

    return artifacts


def extract_canvas_from_page(page) -> dict:
    """Extracts Canvas document from a gem/... page."""
    url = page.url
    title = page.title().replace(" - Gemini", "").strip()

    content = page.evaluate("""() => {
        const main = document.querySelector(
            '[class*="canvas-content"], [class*="document-content"], ' +
            '[class*="gem-content"], main, article'
        );
        return main ? main.innerText : document.body.innerText;
    }""")

    return {
        "id": f"gem_{hashlib.md5(url.encode()).hexdigest()[:16]}",
        "title": title,
        "url": url,
        "content": content or "",
        "type": "document",
        "source": "web_scrape",
    }


def get_all_chat_urls(page, log=None) -> list[dict]:
    """Opens sidebar, waits for history to load, scrolls, collects chat URLs."""
    if log is None:
        log = Logger()

    # Open the sidebar if collapsed. The aria-label matchers below intentionally
    # include localized Gemini UI strings (RU "открыть бок" + EN "open side") so the
    # scraper works regardless of the account's interface language.
    opened = page.evaluate("""() => {
        const btns = document.querySelectorAll('button');
        for (const btn of btns) {
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            const txt = (btn.innerText || '').toLowerCase();
            if (label.includes('открыть бок') || label.includes('open side') ||
                label.includes('expand') || txt.includes('side_nav_expand')) {
                if (btn.offsetParent !== null) { btn.click(); return true; }
            }
        }
        return false;
    }""")
    log(f"[STATUS] Sidebar toggle clicked: {opened}")
    time.sleep(2)

    # Wait for chat history to load (links appear after async load)
    for _ in range(30):
        count = page.evaluate("""() => document.querySelectorAll('a[href*="/app/"]').length""")
        if count > 0:
            break
        time.sleep(1)

    # Scroll history container to lazy-load all chats
    prev_count = 0
    for attempt in range(80):
        page.evaluate("""() => {
            const c = document.querySelector('.chat-history-scroll-container') ||
                      document.querySelector('[class*="history-scroll"]') ||
                      document.querySelector('.chat-history-list') ||
                      document.querySelector('[role="navigation"]');
            if (c) c.scrollTop = c.scrollHeight;
        }""")
        time.sleep(0.5)

        cur_count = page.evaluate("""() => document.querySelectorAll('a[href*="/app/"]').length""")
        if cur_count == prev_count and attempt > 4:
            break
        prev_count = cur_count

    urls = page.evaluate(r"""() => {
        const items = [];
        const seen = new Set();
        const links = document.querySelectorAll('a[href*="/app/"]');
        for (const a of links) {
            const href = a.getAttribute('href');
            if (!href) continue;
            const full = new URL(href, window.location.origin).href;
            const path = new URL(full).pathname;
            // Valid chat URLs: /app/HASH (hash is 8+ hex chars)
            const match = path.match(/\/(?:app|c)\/([a-f0-9-]{8,})/);
            if (!match) continue;
            if (seen.has(full)) continue;
            seen.add(full);
            const title = (a.innerText || '').trim().substring(0, 200);
            items.push({ url: full, title });
        }
        return items;
    }""")

    log(f"[STATUS] Sidebar scan: {len(urls)} chat URLs found")
    return urls


def save_to_db(data: dict, account_email: str):
    """Saves scraped data to SQLite."""
    from parse_and_index import init_db, get_or_create_account, import_conversation

    conn = init_db(DB_PATH)
    account_id = get_or_create_account(conn, account_email)

    conv = {
        "id": data["id"],
        "title": data.get("title", ""),
        "messages": [
            {"role": m["role"], "content": m["content"], "timestamp": None}
            for m in data.get("messages", [])
        ],
        "canvas_artifacts": data.get("canvas_artifacts", []),
        "created_time": None,
        "updated_time": None,
    }

    is_new, conv_id = import_conversation(conn, account_id, conv)
    conn.commit()
    conn.close()

    return is_new, conv_id


def scrape_url(url: str, account_email: str, headed: bool = True, log_file: str = None):
    """Scrapes a single Gemini URL."""
    ensure_playwright()
    log = Logger(log_file)

    with sync_playwright() as pw:
        browser = get_browser(pw, headed=headed)
        page = browser.pages[0] if browser.pages else browser.new_page()

        log(f"[STATUS] Opening: {url}")
        page.goto(url)
        wait_for_gemini_load(page)

        if "accounts.google.com" in page.url or "signin" in page.url.lower():
            if not wait_for_login(page, log):
                browser.close()
                return
            page.goto(url)
            wait_for_gemini_load(page)

        if "/gem/" in url:
            log("[STATUS] Detected: Canvas/Gem document")
            data = extract_canvas_from_page(page)
            log(f"  Title: {data['title']}")
            log(f"  Content: {len(data['content'])} chars")

            CANVAS_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', data['title'])[:80]
            md_path = CANVAS_DIR / f"{safe_name}.md"
            md_path.write_text(
                f"---\nurl: {url}\ntitle: {data['title']}\n---\n\n{data['content']}",
                encoding="utf-8"
            )
            log(f"  Saved: {md_path}")

        else:
            log("[STATUS] Detected: Chat conversation")
            data = extract_chat_from_page(page)
            if data['messages']:
                data['canvas_artifacts'] = extract_canvas_artifacts(page, data['id'], log)
            log(f"  Title: {data['title']}")
            log(f"  Messages: {len(data['messages'])}")
            log(f"  Canvas artifacts: {len(data['canvas_artifacts'])}")

            if data['messages']:
                is_new, conv_id = save_to_db(data, account_email)
                status = "NEW" if is_new else "updated"
                log(f"  DB: {status} (id: {conv_id})")
            else:
                log("[STATUS] No messages found -- page may not have loaded fully")

        browser.close()
        log("[DONE] Scrape complete")


def scrape_all(account_email: str, headed: bool = True, log_file: str = None):
    """Scrapes all chats from the Gemini sidebar."""
    ensure_playwright()
    log = Logger(log_file)

    with sync_playwright() as pw:
        browser = get_browser(pw, headed=headed)
        page = browser.pages[0] if browser.pages else browser.new_page()

        log("[STATUS] Opening Gemini...")
        # Try /u/1/ first (multi-account), fall back to root
        page.goto("https://gemini.google.com/u/1/app")
        wait_for_gemini_load(page)

        # If redirected to login — wait for user to log in
        if "accounts.google.com" in page.url or "signin" in page.url.lower():
            if not wait_for_login(page, log):
                log("[ERROR] Could not log in. Closing browser.")
                browser.close()
                return
            page.goto("https://gemini.google.com/u/1/app")
            wait_for_gemini_load(page)

        log(f"[STATUS] Logged in. Current URL: {page.url}")

        log("[STATUS] Scanning sidebar for chats...")
        chat_urls = get_all_chat_urls(page, log)
        total = len(chat_urls)
        log(f"[STATUS] Found {total} chat URLs to scrape")

        if total == 0:
            log("[STATUS] No chats found in sidebar. Possible causes:")
            log("  - Sidebar may be collapsed")
            log("  - Chat history may be empty for this account")
            log("  - Gemini HTML structure may have changed")
            log(f"  - Current URL: {page.url}")
            browser.close()
            log("[DONE] Finished (0 chats)")
            return

        new_count = 0
        err_count = 0
        for i, item in enumerate(chat_urls):
            log(f"[PROGRESS] {i + 1}/{total} {item['title'][:60]}")
            try:
                page.goto(item['url'])
                wait_for_messages(page)
                data = extract_chat_from_page(page)
                if data['messages']:
                    data['canvas_artifacts'] = extract_canvas_artifacts(page, data['id'], log)
                    is_new, _ = save_to_db(data, account_email)
                    if is_new:
                        new_count += 1
                    nc = len(data['canvas_artifacts'])
                    canvas_note = f", {nc} canvas" if nc else ""
                    log(f"  {len(data['messages'])} msgs{canvas_note}, {'NEW' if is_new else 'exists'}")
                else:
                    log("  (no messages)")
            except Exception as e:
                log(f"  [ERROR] {e}")
                err_count += 1
            time.sleep(1.5)

        browser.close()
        log(f"[DONE] Scraped {total} chats, {new_count} new, {err_count} errors")


def main():
    parser = argparse.ArgumentParser(description="Scrape Gemini chats/Canvas")
    parser.add_argument("url", nargs="?", help="Gemini URL to scrape")
    parser.add_argument("--account", required=True,
                        help="Account email to tag the scraped chats with in the DB "
                             "(e.g. you@gmail.com)")
    parser.add_argument("--list-all", action="store_true",
                        help="Scrape all chats from sidebar")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser without UI")
    parser.add_argument("--log", help="Path to log file for progress output")

    args = parser.parse_args()
    log = Logger(args.log)

    # Surface a missing browser as a real, log-visible [ERROR] (the bare
    # ensure_playwright() only prints to stdout, which the viewer discards).
    if not HAS_PLAYWRIGHT:
        log("[ERROR] Playwright is not installed or configured.")
        log("    pip install playwright")
        log("    playwright install chromium")
        sys.exit(1)

    # Anti-freeze guarantee: ANY uncaught failure (browser crash, locked
    # profile, login timeout, etc.) must leave a terminal [ERROR] marker in the
    # log so the viewer's poll loop ends with "failed" instead of hanging on a
    # process that never wrote [DONE]/[ERROR].
    try:
        if args.list_all:
            scrape_all(args.account, headed=not args.headless, log_file=args.log)
        elif args.url:
            scrape_url(args.url, args.account, headed=not args.headless, log_file=args.log)
        else:
            print("Usage:")
            print('  python scrape_gemini_url.py "https://gemini.google.com/app/CHAT_ID"')
            print('  python scrape_gemini_url.py --list-all')
            print('  python scrape_gemini_url.py --list-all --log progress.txt')
    except KeyboardInterrupt:
        log("[ERROR] Cancelled.")
        sys.exit(130)
    except Exception as e:
        # Playwright is installed but the Chromium *binary* was never downloaded
        # (a separate ~170 MB step). Its raw error is cryptic, so translate the
        # known signatures into one actionable line.
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            log("[ERROR] Chromium browser is not installed (Playwright module is "
                "present, but the browser was never downloaded).")
            log("    Run once in a terminal:  playwright install chromium")
        else:
            log(f"[ERROR] {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
