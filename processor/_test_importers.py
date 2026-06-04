"""Unit test for the multi-source importers (ChatGPT / Claude -> canonical)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importers import parse_chatgpt, parse_claude, detect_source, normalize

print("== ChatGPT ==")

# Minimal ChatGPT export: a branching tree. current_node points at the
# regenerated answer (msg-2b), so msg-2a must NOT appear in the linear thread.
chatgpt_raw = [{
    "title": "Greptile",
    "conversation_id": "abc-123",
    "create_time": 1716620400.0,        # epoch seconds
    "update_time": 1716620500.0,
    "current_node": "msg-2b",
    "mapping": {
        "root": {"id": "root", "message": None, "parent": None, "children": ["msg-1"]},
        "msg-1": {
            "id": "msg-1", "parent": "root", "children": ["msg-2a", "msg-2b"],
            "message": {
                "author": {"role": "user"},
                "create_time": 1716620400.0,
                "content": {"content_type": "text", "parts": ["Hello there"]},
            },
        },
        "msg-2a": {
            "id": "msg-2a", "parent": "msg-1", "children": [],
            "message": {
                "author": {"role": "assistant"},
                "create_time": 1716620410.0,
                "content": {"content_type": "text", "parts": ["First (discarded) reply"]},
            },
        },
        "msg-2b": {
            "id": "msg-2b", "parent": "msg-1", "children": [],
            "message": {
                "author": {"role": "assistant"},
                "create_time": 1716620420.0,
                "content": {"content_type": "text", "parts": ["Regenerated reply"]},
            },
        },
    },
}]

cg = parse_chatgpt(chatgpt_raw)
conv = cg["conversations"][0]
print(f"  id={conv['id']} title={conv['title']} msgs={len(conv['messages'])}")
assert detect_source(chatgpt_raw) == "chatgpt", "detect failed"
assert conv["id"] == "chatgpt_abc-123"
assert conv["source"] == "chatgpt"
assert len(conv["messages"]) == 2, "should linearize active thread only"
assert conv["messages"][0]["role"] == "user"
assert conv["messages"][1]["role"] == "model", "assistant must map to model"
assert conv["messages"][1]["content"] == "Regenerated reply", "must follow current_node branch"
assert conv["messages"][0]["timestamp"] == 1716620400000, "sec->ms conversion"
assert conv["created_time"] == 1716620400000
print("  ChatGPT: OK")

print("\n== Claude ==")

# Claude export: text lives directly on .text for one message and inside
# content[] blocks for another (exercises the fallback).
claude_raw = [{
    "uuid": "xyz-789",
    "name": "Quantum chat",
    "created_at": "2024-05-25T07:00:00Z",
    "updated_at": "2024-05-25T07:05:00Z",
    "chat_messages": [
        {"sender": "human", "text": "Explain entanglement",
         "created_at": "2024-05-25T07:00:00Z", "content": []},
        {"sender": "assistant", "text": "",
         "created_at": "2024-05-25T07:01:00Z",
         "content": [{"type": "text", "text": "Two particles share a state."}]},
    ],
}]

cl = parse_claude(claude_raw)
cconv = cl["conversations"][0]
print(f"  id={cconv['id']} title={cconv['title']} msgs={len(cconv['messages'])}")
assert detect_source(claude_raw) == "claude", "detect failed"
assert cconv["id"] == "claude_xyz-789"
assert cconv["source"] == "claude"
assert len(cconv["messages"]) == 2
assert cconv["messages"][0]["role"] == "user", "human must map to user"
assert cconv["messages"][1]["role"] == "model"
assert cconv["messages"][1]["content"] == "Two particles share a state.", "content[] fallback"
# 2024-05-25T07:00:00Z == 1716620400000 ms
assert cconv["messages"][0]["timestamp"] == 1716620400000, "ISO->ms conversion"
assert cconv["created_time"] == 1716620400000
print("  Claude: OK")

print("\n== Dispatcher ==")
# A canonical Gemini file must pass through normalize() untouched.
canonical = {"export_metadata": {"source": "gemini"}, "conversations": [{"id": "g1"}]}
assert detect_source(canonical) == "gemini"
assert normalize(canonical) is canonical, "canonical data must be returned unchanged"
# Auto-routing
assert normalize(chatgpt_raw)["export_metadata"]["source"] == "chatgpt"
assert normalize(claude_raw)["export_metadata"]["source"] == "claude"
print("  Dispatcher: OK")

print("\nALL OK")
