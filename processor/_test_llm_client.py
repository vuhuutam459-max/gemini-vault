"""Offline tests for processor.llm_client — no network, no tokens spent.

The HTTP seam (LLMClient._http_post) is monkeypatched so every test runs
fully offline and deterministically. Run:  python processor/_test_llm_client.py
"""

import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor import llm_client as L
from processor.llm_client import LLMClient, LLMError


def _client(**kw):
    return LLMClient(base_url="http://x/v1", api_key="freellmapi-test", **kw)


def _reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_disabled_without_key():
    # Hermetic: ignore any real librarian_config.json / env so "no key" holds.
    import os
    L.CONFIG_PATH = Path(__file__).resolve().parent / "__no_such_config__.json"
    for _v in ("FREELLMAPI_BASE_URL", "FREELLMAPI_KEY", "FREELLMAPI_MODEL"):
        os.environ.pop(_v, None)
    c = LLMClient(base_url="http://x/v1", api_key=None)
    assert c.enabled is False
    try:
        c.complete("hi")
    except LLMError:
        pass
    else:
        raise AssertionError("expected LLMError when no key configured")


def test_complete_builds_payload_and_parses():
    c = _client()
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return _reply("hello there")

    c._http_post = fake_post
    out = c.complete("Tag this", system="You are a tagger", temperature=0.1)

    assert out == "hello there"
    assert captured["url"] == "http://x/v1/chat/completions"
    msgs = captured["payload"]["messages"]
    assert msgs[0] == {"role": "system", "content": "You are a tagger"}
    assert msgs[1] == {"role": "user", "content": "Tag this"}
    assert captured["payload"]["temperature"] == 0.1
    assert "response_format" not in captured["payload"]


def test_json_mode_sets_response_format_and_parses():
    c = _client()
    c._http_post = lambda url, payload: (
        _reply('```json\n["python", "asyncio"]\n```')
        if payload.get("response_format")
        else _reply("nope")
    )
    tags = c.complete_json("Give tags")
    assert tags == ["python", "asyncio"]


def test_retry_on_429_then_success(monkeypatch_sleep=True):
    c = _client()
    calls = {"n": 0}

    def flaky_post(url, payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        return _reply("ok after retries")

    L.time.sleep = lambda *_a, **_k: None  # don't actually wait
    c._http_post = flaky_post
    assert c.complete("x") == "ok after retries"
    assert calls["n"] == 3


def test_non_retryable_raises():
    c = _client()

    def bad_post(url, payload):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b""))

    c._http_post = bad_post
    try:
        c.complete("x")
    except LLMError:
        pass
    else:
        raise AssertionError("expected LLMError on HTTP 400")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK  {t.__name__}")
    print(f"\nAll {len(tests)} llm_client tests passed.")


if __name__ == "__main__":
    main()
