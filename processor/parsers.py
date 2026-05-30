"""
parsers.py — Gemini Vault edition
==================================
Adapted parsers from the Takeout Smart Extractor.
All functions work in-memory (strings/dicts) and write no files.

Capabilities:
  - convert_gemini_json(data) -> Markdown
  - convert_gemini_html(html_str) -> Markdown
  - convert_api_response(messages_list) -> Markdown (new: Gemini API format)
  - clean_text(s) -> str (strips HTML)
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


# ── JSON (Takeout format) ──

def convert_gemini_json(data: Any) -> str:
    parts: list[str] = ["# Gemini — log", ""]

    if isinstance(data, list):
        for item in data:
            parts.append(_render_activity_item(item))
        return "\n".join(parts)

    if isinstance(data, dict):
        if "conversations" in data:
            for conv in data["conversations"]:
                parts.append("---")
                parts.append(_render_activity_item(conv))
            return "\n".join(parts)
        if "messages" in data:
            for m in data["messages"]:
                parts.append(_render_message(m))
            return "\n".join(parts)
        parts.append("```json")
        parts.append(json.dumps(data, ensure_ascii=False, indent=2))
        parts.append("```")
        return "\n".join(parts)

    parts.append("```")
    parts.append(repr(data))
    parts.append("```")
    return "\n".join(parts)


# ── JSON (Gemini API format — extractor output) ──

def convert_api_response(messages: list[dict]) -> str:
    """Converts a list of messages from the extractor's API format into Markdown."""
    parts: list[str] = []

    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        ts = msg.get("timestamp")

        if role == "user":
            parts.append("### **You:**")
        else:
            parts.append("### **Gemini:**")

        if ts:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                parts.append(f"*{dt.strftime('%Y-%m-%d %H:%M')}*")
            except (OSError, ValueError):
                pass

        parts.append("")
        parts.append(clean_text(content))
        parts.append("")

    return "\n".join(parts)


# ── HTML (Takeout My Activity format) ──

def convert_gemini_html(raw: str) -> str:
    if _HAS_BS4:
        return _html_to_md_bs4(raw)
    return _html_to_md_naive(raw)


def _html_to_md_bs4(raw: str) -> str:
    soup = BeautifulSoup(raw, "lxml")
    cells = soup.select("div.outer-cell") or soup.select("div.mdl-grid")
    parts = ["# Gemini — My Activity", ""]

    if cells:
        for cell in cells:
            inner = cell.select("div.content-cell")
            if not inner:
                text = cell.get_text("\n").strip()
                if text:
                    parts.append("---")
                    parts.append(text)
                continue
            parts.append("---")
            for i, c in enumerate(inner):
                text = c.get_text("\n").strip()
                if not text:
                    continue
                if i == 0:
                    parts.append(f"### {text.splitlines()[0]}")
                    rest = "\n".join(text.splitlines()[1:]).strip()
                    if rest:
                        parts.append(rest)
                else:
                    parts.append(text)
            parts.append("")
        return "\n".join(parts)

    text = soup.get_text("\n").strip()
    parts.append(text)
    return "\n".join(parts)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n\s*\n+")


def _html_to_md_naive(raw: str) -> str:
    raw = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    raw = raw.replace("</p>", "\n\n").replace("</div>", "\n")
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    text = _WS_RE.sub("\n\n", text).strip()
    return f"# Gemini — My Activity\n\n{text}\n"


# ── Helpers ──

def clean_text(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    if "<" in s and ">" in s and _HAS_BS4:
        try:
            s = BeautifulSoup(s, "lxml").get_text("\n")
        except Exception:
            pass
    return html.unescape(s).strip()


def _render_activity_item(item: Any) -> str:
    if not isinstance(item, dict):
        return f"\n```\n{item}\n```\n"
    lines = []
    title = item.get("title") or item.get("header") or "Entry"
    lines.append(f"\n## {title}")
    if ts := (item.get("time") or item.get("timestamp")):
        lines.append(f"*Time:* {ts}")
    if prompt := (item.get("prompt") or item.get("user_query") or item.get("query")):
        lines.append("\n**Query:**\n")
        lines.append(clean_text(prompt))
    if resp := (item.get("response") or item.get("answer") or item.get("model_response")):
        lines.append("\n**Response:**\n")
        lines.append(clean_text(resp))
    if "messages" in item and isinstance(item["messages"], list):
        for m in item["messages"]:
            lines.append(_render_message(m))
    if details := item.get("details"):
        lines.append(f"\n*Details:* {details}")
    return "\n".join(lines)


def _render_message(m: Any) -> str:
    if not isinstance(m, dict):
        return f"\n{m}"
    role = m.get("role") or m.get("author") or "?"
    text = m.get("text") or m.get("content") or m.get("message") or ""
    if isinstance(text, list):
        text = "\n".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in text)
    return f"\n**{role}:**\n{clean_text(text)}\n"
