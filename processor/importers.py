"""
importers.py
============
Adapters that normalize chat exports from different AI providers into the
single canonical structure consumed by parse_and_index.py.

Canonical structure (what every adapter must return)::

    {
      "export_metadata": {
          "version": "1.0.0",
          "source": "gemini" | "chatgpt" | "claude",
          "account_email": "<grouping key>",
          "total_conversations": <int>,
      },
      "conversations": [
          {
            "id": "<stable id>",
            "title": "<str>",
            "source": "<provider>",
            "messages": [
                {"role": "user"|"model"|"system", "content": "<str>",
                 "timestamp": <epoch_ms|None>}
            ],
            "canvas_artifacts": [...],
            "created_time": <epoch_ms|None>,
            "updated_time": <epoch_ms|None>,
          },
      ],
    }

The viewer/DB layer is provider-agnostic, so adding a provider only means
adding a parser here — the working core (SHA256 dedup, FTS5, viewer) is
never touched.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Roles allowed by the DB schema (messages.role CHECK constraint).
# Everything an adapter emits must map onto one of these.
_USER, _MODEL, _SYSTEM = "user", "model", "system"


# ── timestamp helpers ──────────────────────────────────────────────────────

def _sec_to_ms(sec) -> int | None:
    """ChatGPT timestamps are epoch *seconds* (float). Convert to ms."""
    if sec is None or not isinstance(sec, (int, float)):
        return None
    return int(sec * 1000)


def _iso_to_ms(iso) -> int | None:
    """Claude timestamps are ISO-8601 strings. Convert to epoch ms (UTC)."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        # Python's fromisoformat dislikes a trailing 'Z' before 3.11.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# ── ChatGPT (OpenAI export: conversations.json) ─────────────────────────────

# OpenAI author roles -> our canonical roles. "tool" messages are internal
# plumbing (function calls) and are dropped.
_CHATGPT_ROLE = {"user": _USER, "assistant": _MODEL, "system": _SYSTEM}


def _chatgpt_text(message: dict) -> str:
    """Extract plain text from a ChatGPT message node."""
    content = message.get("content") or {}
    ctype = content.get("content_type")
    if ctype == "text":
        parts = content.get("parts") or []
        # parts can contain non-string items (image refs); keep strings only.
        return "\n".join(p for p in parts if isinstance(p, str)).strip()
    if ctype == "code":
        return (content.get("text") or "").strip()
    # Fallback: best-effort join of any string parts.
    parts = content.get("parts") or []
    return "\n".join(p for p in parts if isinstance(p, str)).strip()


def _chatgpt_linear_messages(mapping: dict, current_node) -> list[dict]:
    """Walk the active thread of a ChatGPT conversation.

    ChatGPT stores messages as a tree (regenerations create branches). The
    thread the user actually saw is the path from ``current_node`` back to the
    root via ``parent`` pointers — so we follow that chain and reverse it.
    """
    # Build the path leaf -> root.
    path = []
    node_id = current_node
    seen = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        path.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    path.reverse()

    out = []
    for node in path:
        msg = node.get("message")
        if not msg:
            continue
        role = _CHATGPT_ROLE.get((msg.get("author") or {}).get("role"))
        if role is None:  # tool / unknown -> drop
            continue
        text = _chatgpt_text(msg)
        if not text:  # hidden/empty system messages etc.
            continue
        out.append({
            "role": role,
            "content": text,
            "timestamp": _sec_to_ms(msg.get("create_time")),
        })
    return out


def parse_chatgpt(raw, account: str = "ChatGPT") -> dict:
    """Normalize a ChatGPT export (a list of conversation objects)."""
    convos = raw if isinstance(raw, list) else raw.get("conversations", [])
    out_convs = []
    for c in convos:
        mapping = c.get("mapping") or {}
        current = c.get("current_node")
        if current is None and mapping:
            # No explicit current node: fall back to the last node added.
            current = next(reversed(mapping))
        messages = _chatgpt_linear_messages(mapping, current)
        conv_id = c.get("conversation_id") or c.get("id") or ""
        out_convs.append({
            "id": f"chatgpt_{conv_id}" if conv_id else "",
            "title": c.get("title") or "Untitled",
            "source": "chatgpt",
            "messages": messages,
            "canvas_artifacts": [],
            "created_time": _sec_to_ms(c.get("create_time")),
            "updated_time": _sec_to_ms(c.get("update_time")),
        })
    return _wrap(out_convs, "chatgpt", account)


# ── Claude (claude.ai data export: conversations.json) ──────────────────────

_CLAUDE_ROLE = {"human": _USER, "user": _USER, "assistant": _MODEL}


def _claude_text(message: dict) -> str:
    """Extract plain text from a Claude chat_message."""
    text = (message.get("text") or "").strip()
    if text:
        return text
    # Newer exports keep text inside a content[] list of typed blocks.
    blocks = message.get("content") or []
    parts = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            parts.append(b["text"])
    return "\n".join(parts).strip()


def parse_claude(raw, account: str = "Claude") -> dict:
    """Normalize a Claude export (a list of conversation objects)."""
    convos = raw if isinstance(raw, list) else raw.get("conversations", [])
    out_convs = []
    for c in convos:
        messages = []
        for m in c.get("chat_messages") or []:
            role = _CLAUDE_ROLE.get(m.get("sender"))
            if role is None:
                continue
            text = _claude_text(m)
            if not text:
                continue
            messages.append({
                "role": role,
                "content": text,
                "timestamp": _iso_to_ms(m.get("created_at")),
            })
        conv_id = c.get("uuid") or c.get("id") or ""
        out_convs.append({
            "id": f"claude_{conv_id}" if conv_id else "",
            "title": c.get("name") or "Untitled",
            "source": "claude",
            "messages": messages,
            "canvas_artifacts": [],
            "created_time": _iso_to_ms(c.get("created_at")),
            "updated_time": _iso_to_ms(c.get("updated_at")),
        })
    return _wrap(out_convs, "claude", account)


# ── dispatcher ──────────────────────────────────────────────────────────────

def _wrap(conversations: list[dict], source: str, account: str) -> dict:
    return {
        "export_metadata": {
            "version": "1.0.0",
            "source": source,
            "account_email": account,
            "total_conversations": len(conversations),
        },
        "conversations": conversations,
    }


def detect_source(raw) -> str:
    """Best-effort detection of which provider an export came from."""
    # Chatrove canonical files: dict with these two keys.
    if isinstance(raw, dict) and "conversations" in raw and "export_metadata" in raw:
        return raw.get("export_metadata", {}).get("source", "gemini")
    # ChatGPT / Claude exports are top-level lists of conversation objects.
    items = raw if isinstance(raw, list) else raw.get("conversations", []) if isinstance(raw, dict) else []
    sample = items[0] if items else {}
    if isinstance(sample, dict):
        if "mapping" in sample:
            return "chatgpt"
        if "chat_messages" in sample or ("uuid" in sample and "name" in sample):
            return "claude"
    return "unknown"


def normalize(raw, account: str | None = None, source: str | None = None) -> dict:
    """Return canonical export data regardless of the originating provider.

    A file already in Chatrove canonical form is returned unchanged.
    """
    src = source or detect_source(raw)
    if src == "chatgpt":
        return parse_chatgpt(raw, account or "ChatGPT")
    if src == "claude":
        return parse_claude(raw, account or "Claude")
    # 'gemini' / canonical / unknown -> assume already canonical.
    return raw
