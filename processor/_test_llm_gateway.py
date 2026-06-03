"""Offline tests for processor.llm_gateway — no network, no tokens spent.

Covers the dependency-injection seam: the factory picks the right gateway from
config, the Null gateway stays safely unavailable, and the OpenAI-compatible
gateway adapts the transport to the universal generate_json/generate_text
interface. Run:  python processor/_test_llm_gateway.py
"""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor import llm_gateway as G
from processor.llm_gateway import (
    build_gateway, load_provider_config, NullGateway, OpenAICompatGateway,
    LLMUnavailable, DEFAULT_PROVIDER,
)


def _reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeConn:
    """Stand-in for a successful socket connection (a context manager)."""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_default_config_is_local_ollama():
    # Hermetic: no config file, no env — must fall back to the free local default.
    G.CONFIG_PATH = Path(__file__).resolve().parent / "__no_such_config__.json"
    for v in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
              "FREELLMAPI_BASE_URL", "FREELLMAPI_KEY", "FREELLMAPI_MODEL"):
        os.environ.pop(v, None)
    cfg = load_provider_config()
    assert cfg["base_url"] == "http://localhost:11434/v1", cfg
    assert cfg["model"] == "gemma3:4b", cfg
    assert cfg == DEFAULT_PROVIDER


def test_env_overrides_config():
    G.CONFIG_PATH = Path(__file__).resolve().parent / "__no_such_config__.json"
    os.environ["LLM_MODEL"] = "llama3.2"
    try:
        cfg = load_provider_config()
        assert cfg["model"] == "llama3.2", cfg
    finally:
        os.environ.pop("LLM_MODEL", None)


def test_no_key_yields_null_gateway():
    gw = build_gateway({"base_url": "http://x/v1", "api_key": "", "model": "m"})
    assert isinstance(gw, NullGateway)
    assert gw.available is False
    for call in (lambda: gw.generate_json("hi"), lambda: gw.generate_text("hi")):
        try:
            call()
        except LLMUnavailable:
            pass
        else:
            raise AssertionError("NullGateway must refuse to generate")


def test_available_is_config_based_not_a_live_ping():
    # available must reflect "configured", never a live socket — a transient probe
    # failure must not silently disable a provider that can actually answer.
    gw = build_gateway({"base_url": "http://localhost:11434/v1",
                        "api_key": "ollama", "model": "gemma3:4b"})
    orig = G.socket.create_connection
    try:
        def _down(*a, **k):
            raise OSError("connection refused")
        G.socket.create_connection = _down            # daemon unreachable...
        assert gw.available is True                    # ...yet still "available" (configured)
    finally:
        G.socket.create_connection = orig


def test_reachable_is_an_advisory_ping():
    gw = build_gateway({"base_url": "http://localhost:11434/v1",
                        "api_key": "ollama", "model": "gemma3:4b"})
    orig = G.socket.create_connection
    try:
        G.socket.create_connection = lambda *a, **k: _FakeConn()      # daemon up
        assert gw.reachable() is True
        def _down(*a, **k):
            raise OSError("connection refused")
        G.socket.create_connection = _down                            # daemon down
        assert gw.reachable() is False                                # no crash
    finally:
        G.socket.create_connection = orig


def test_openai_compat_gateway_adapts_transport():
    gw = build_gateway({"base_url": "http://x/v1", "api_key": "k", "model": "gemma3:4b"})
    assert isinstance(gw, OpenAICompatGateway)
    assert gw.available is True and gw.model == "gemma3:4b"

    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["json_mode"] = "response_format" in payload
        return _reply('{"tags": ["a", "b"]}' if payload.get("response_format")
                      else "plain answer")

    # Reach through to the transport seam — consumers never see this.
    gw._client._http_post = fake_post

    assert gw.generate_json("give tags") == {"tags": ["a", "b"]}
    assert captured["json_mode"] is True
    assert gw.generate_text("answer me") == "plain answer"
    assert captured["url"] == "http://x/v1/chat/completions"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK  {t.__name__}")
    print(f"\nAll {len(tests)} llm_gateway tests passed.")


if __name__ == "__main__":
    main()
