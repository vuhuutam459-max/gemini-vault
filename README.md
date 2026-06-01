<p align="center">
  <img src="banner.jpg" alt="Gemini Vault" width="100%">
</p>

# Gemini Vault

> Full local backup and offline viewer for your Google Gemini chats.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Deps](https://img.shields.io/badge/core%20deps-zero-brightgreen)

Keep all your Gemini conversations locally, search them in milliseconds,
read them offline (with LaTeX, code highlighting and Canvas), and export
them anywhere.
**All data is processed 100% on your own machine — no external servers.**

## Quick start

The fastest way (Windows): double-click **`Open Gemini Vault.bat`**. It loads the
bundled demo data on first run and opens the viewer in your browser. Nothing to install.

Or from the command line:

```bash
git clone https://github.com/vuhuutam459-max/gemini-vault.git
cd gemini-vault/Gemini_Vault

# 1. Try it on the bundled demo data
python processor/parse_and_index.py Source_Accounts/demo_export.json

# 2. Start the viewer (opens a browser at http://127.0.0.1:8642/)
python viewer/serve.py
```

The core needs no dependencies — just the Python 3.10+ standard library.

## Features

- 📥 **Import** from Google Takeout (ZIP, no unpacking) or from a browser export
- 🔎 **Full-text search** on SQLite FTS5 (Unicode-aware)
- 🖥 **Offline viewer**: LaTeX (KaTeX), code highlighting (highlight.js), Canvas
- 📊 **Dashboard**: activity by month, by account, document types, top chats
- 🔀 **Filters & sorting**: by account, year, Canvas presence, length, title
- ⤓ **Export**: a single chat (`.md` / HTML / print→PDF) or the whole vault (`.md` / ZIP / **portable JSON archive**)
- ⏰ **Scheduled auto-backup** (generates a Windows Task Scheduler task)
- 👥 **Multiple accounts** in one database
- ♻️ **Incremental**: SHA256 deduplication — only new/changed chats are pulled
- 🌐 **Bilingual UI** (English ⇄ Russian) with a one-click toggle
- 🧩 **Multi-source import**: also reads **ChatGPT** and **Claude** exports — one searchable vault for all your AI chats
- 🧠 **Smart Librarian** (optional, **opt-in**): AI auto-tagging, per-chat summaries and an "ask the archive" search, powered by a self-hosted [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) gateway

### With your own data

**Option 1 — Google Takeout (recommended):**
```bash
python processor/import_takeout.py /path/to/takeout-001.zip
python viewer/serve.py
```

**Option 2 — from the browser (needs Playwright):**

On Windows, the easiest way to install the scraper dependencies is the launcher:
double-click **`Open Gemini Vault.bat`** and choose **[2] Set up / update the live
scraper** — it installs the Python packages and downloads Chromium for you. Or do
it manually:

```bash
pip install -r requirements.txt
playwright install chromium
python processor/scrape_gemini_url.py --list-all --account you@gmail.com
python viewer/serve.py
```

You can also use the browser extractor scripts in `extractor/` (Tampermonkey or
a one-off console paste) — see `extractor/README_install.md`.

### Importing from ChatGPT & Claude

Gemini Vault isn't only for Gemini — it can ingest your **ChatGPT** and
**Claude** history too, so all your AI conversations live in one searchable,
offline vault.

**1. Get your export**

- **ChatGPT:** Settings → **Data controls** → **Export data** → confirm. You'll
  receive an email with a `.zip`; inside it is `conversations.json`.
- **Claude:** Settings → **Privacy** (Account) → **Export data** → confirm. You'll
  receive an email with an archive containing `conversations.json`.

**2. Import it** (the format is auto-detected — no flags needed):

```bash
python processor/parse_and_index.py path/to/conversations.json
python viewer/serve.py
```

ChatGPT chats are grouped under the account **`ChatGPT`** and Claude chats under
**`Claude`**, so you can filter by source in the viewer. Re-importing a newer
export only adds what changed (same SHA256 deduplication as Gemini).

> Notes: only the **active** message thread of each ChatGPT chat is imported
> (regenerated/branch variants are skipped), and full-text search works across
> all sources automatically.

## 🧠 Smart Librarian (optional, opt-in)

The **Smart Librarian** can auto-**tag** your chats, write a short **summary** for
each one, and answer plain-language questions about your whole archive
(**"Ask the archive"**). It is powered by **[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi)** —
a self-hosted gateway that aggregates free-tier LLM providers behind one
OpenAI-compatible endpoint (~1.7B tokens/month across providers).

> ⚠️ **PRIVACY — READ THIS FIRST. The Smart Librarian is OPT-IN and DISABLED by
> default. The rest of Gemini Vault runs 100% on your machine — but when you enable
> the Librarian, the content of your chats is sent to your local FreeLLMAPI gateway
> and on to the external AI providers you configured there (Google, Groq, Mistral,
> …) for processing. Only turn it on for an archive you are comfortable sending to
> those providers. With no key configured, nothing is ever sent.**

Gemini Vault's core stays **zero-dependency**: the client is plain `urllib`, so you
do **not** need the `openai` package.

### 1. Run the FreeLLMAPI gateway locally

FreeLLMAPI is a separate project you run yourself (it is **not** bundled here):

1. Clone and start it (Docker Compose or Node.js) — follow its README:
   <https://github.com/tashfeenahmed/freellmapi>
2. Open its dashboard at <http://localhost:3001> and add your free provider API
   keys on the **Keys** page.
3. Copy the unified key from the dashboard header — it looks like `freellmapi-…`.

### 2. Give the key to Gemini Vault

Either set environment variables:

```bash
# Windows (PowerShell)
$env:FREELLMAPI_KEY = "freellmapi-your-unified-key"
$env:FREELLMAPI_BASE_URL = "http://localhost:3001/v1"   # optional (this is the default)
$env:FREELLMAPI_MODEL = "auto"                          # optional

# macOS / Linux
export FREELLMAPI_KEY="freellmapi-your-unified-key"
```

…**or** create **`librarian_config.json`** in the project root (it is gitignored,
so the key never lands in the repo):

```json
{
  "base_url": "http://localhost:3001/v1",
  "api_key": "freellmapi-your-unified-key",
  "model": "auto"
}
```

### 3. Run the Librarian

```bash
python processor/librarian.py                  # tag + summarize everything new
python processor/librarian.py --tag            # tags only
python processor/librarian.py --summarize --limit 50
```

…or trigger it from the viewer (background job with live progress in
`.librarian_log.txt`). Processing is **idempotent** — already-tagged/summarized
chats are skipped, so it never re-spends tokens, and it is safe to stop and resume.

Afterwards the viewer shows **tag chips** and an **AI summary** on each chat, a
**tag filter** in the sidebar, and the **Ask the archive** bar (full-text search
finds the relevant chats, then the model answers with citations).

## Commands

```bash
# Import
python processor/import_takeout.py takeout.zip          # from a Takeout ZIP
python processor/parse_and_index.py export.json         # Gemini JSON export
python processor/parse_and_index.py conversations.json  # ChatGPT / Claude export (auto-detected)
python processor/parse_and_index.py --stats             # database statistics

# Smart Librarian (opt-in; needs a FreeLLMAPI key — see the section above)
python processor/librarian.py                   # auto-tag + summarize new chats
python processor/librarian.py --tag             # tags only

# Viewer
python viewer/serve.py                  # port 8642
python viewer/serve.py --port 9000 --no-open

# Export to Obsidian (each chat = one .md note)
python processor/export_obsidian.py --dest ~/my-vault
```

Export to `.md` / HTML / ZIP / JSON archive is also available right from the
interface (the "⤓ Export" button).

## Moving to another account

Click **⤓ Export → 📦 JSON archive (transfer)**. The resulting file can be
imported into another Gemini Vault with `parse_and_index.py archive.json` — all
chats, messages, Canvas artifacts and dates are carried over 1-to-1, with no risk
to the account.

> The old `migrate_uploader.py` module (re-sending prompts through Gemini's
> internal API) is **deprecated**: it does not preserve the original answers,
> burns quota, and violates the ToS.

## Structure

```
Gemini_Vault/
├── Open Gemini Vault.bat   One-click launcher (import demo + start viewer)
├── extractor/           Tampermonkey / console scripts for the browser
├── processor/
│   ├── parse_and_index.py   JSON  -> SQLite + FTS5 (SHA256 dedup)
│   ├── import_takeout.py    Takeout ZIP -> SQLite
│   ├── export_obsidian.py   SQLite -> Obsidian Vault
│   ├── parsers.py           Gemini JSON/HTML -> Markdown
│   ├── scrape_gemini_url.py Scraper (Playwright)
│   ├── llm_client.py        FreeLLMAPI client (stdlib urllib; opt-in)
│   └── librarian.py         Smart Librarian: auto-tag / summarize
├── viewer/
│   ├── index.html           SPA viewer
│   ├── serve.py             HTTP server + JSON API
│   └── lib/                 Offline copies of the CDN libs (auto-fallback)
├── Source_Accounts/     JSON dumps (+ demo_export.json)
├── backup.bat           Backup entry point for Task Scheduler
└── gemini_vault.db      SQLite (created on first import; in .gitignore)
```

## Keyboard shortcuts

- `Ctrl+K` — focus the search box
- `↑ / ↓` — navigate between chats
- `Escape` — close Canvas / modal

## Privacy & limitations

- Everything runs locally; the database, logs and personal dumps are excluded via `.gitignore`.
- The **Smart Librarian is opt-in and off by default.** Enabling it (configuring a FreeLLMAPI key) sends chat content to your local gateway and the external AI providers behind it. With no key configured, nothing leaves your machine.
- The viewer is designed for **trusted, self-owned data**: rendered Markdown is
  not sanitized (by original design). Do not open someone else's archives with it.
- `migrate_uploader.py` violates Google's ToS and is kept only as a deprecated reference.

## Disclaimer & your responsibility

This tool is provided **as-is, with no warranty of any kind** (see the MIT
[LICENSE](LICENSE)). It works, but how you use it is **entirely your own
responsibility**:

- The exported files contain **your private conversations**. Treat them as
  confidential. **You** decide where they are stored, backed up, or uploaded.
- If you **share, send, or hand these files (or this tool) to anyone else**, that
  is **your decision and your responsibility** — including any personal data the
  archives may contain and any consequences that follow.
- You are responsible for complying with Google's Terms of Service and any
  applicable laws in your jurisdiction when extracting and storing your data.
- The authors and contributors are **not liable** for any data loss, account
  issues, privacy incidents, or damages arising from the use, misuse, or
  distribution of this software or the data it produces.

In short: the tool does its job — but the data, and what you do with it, are on you.
See the full [DISCLAIMER](DISCLAIMER.md).

## Credits

- **AI engine (optional):** the Smart Librarian is powered by
  **[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi)** by Tashfeen Ahmed,
  licensed **MIT** (© 2026 Tashfeen Ahmed). Gemini Vault does **not** bundle or
  redistribute FreeLLMAPI — you run your own local instance and Gemini Vault only
  talks to its OpenAI-compatible HTTP endpoint.

## License

MIT — see [LICENSE](LICENSE).
