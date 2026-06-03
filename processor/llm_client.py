"""Thin OpenAI-compatible client for the self-hosted FreeLLMAPI gateway.

FreeLLMAPI (https://github.com/tashfeenahmed/freellmapi) runs locally and
exposes an OpenAI-compatible endpoint (default ``http://localhost:3001/v1``)
behind a single ``freellmapi-...`` key. Because that endpoint is just
HTTP+JSON, this client is implemented with the **standard library only**
(``urllib``) to preserve Gemini Vault's zero-dependency core — no ``openai``
package is required.

Configuration is read, in priority order:
  1. environment variables   FREELLMAPI_BASE_URL / FREELLMAPI_KEY / FREELLMAPI_MODEL
  2. librarian_config.json    (in the project root; gitignored — holds the key)

When no API key is configured the client reports ``enabled == False`` and the
Smart Librarian features stay dormant (opt-in privacy: nothing leaves the
machine until the user wires up a key).
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "librarian_config.json"

DEFAULT_BASE_URL = "http://localhost:3001/v1"
DEFAULT_MODEL = "auto"

# HTTP statuses worth retrying: rate-limit + transient server errors.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Raised when the gateway cannot satisfy a request after retries."""


def _load_config() -> dict:
    """Merge file config (if present) with environment overrides."""
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    # Environment wins over file.
    if os.getenv("FREELLMAPI_BASE_URL"):
        cfg["base_url"] = os.environ["FREELLMAPI_BASE_URL"]
    if os.getenv("FREELLMAPI_KEY"):
        cfg["api_key"] = os.environ["FREELLMAPI_KEY"]
    if os.getenv("FREELLMAPI_MODEL"):
        cfg["model"] = os.environ["FREELLMAPI_MODEL"]
    return cfg


class LLMClient:
    """Minimal chat-completions client for an OpenAI-compatible gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 180.0,
    ):
        cfg = _load_config()
        self.base_url = (base_url or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or cfg.get("api_key")
        self.model = model or cfg.get("model") or DEFAULT_MODEL
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """True when an API key is configured (feature is opt-in)."""
        return bool(self.api_key)

    # ── public API ──

    def complete(
        self,
        user: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_retries: int = 4,
    ) -> str:
        """Return the assistant's reply text for a single-turn prompt.

        ``json_mode`` asks the gateway for a JSON object via ``response_format``.
        Retries 429/5xx with exponential backoff + jitter.
        """
        if not self.enabled:
            raise LLMError("FreeLLMAPI key not configured (client disabled)")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post_with_retries("/chat/completions", payload, max_retries)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {data!r}") from exc

    def complete_json(self, user: str, **kwargs):
        """Like :meth:`complete` but parses the reply as JSON.

        Tolerates models that wrap JSON in ``` fences.
        """
        kwargs.setdefault("json_mode", True)
        text = self.complete(user, **kwargs).strip()
        if text.startswith("```"):
            # strip a leading ```json / ``` fence and the trailing fence
            text = text.split("\n", 1)[-1] if "\n" in text else text
            text = text.rsplit("```", 1)[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"reply was not valid JSON: {text!r}") from exc

    # ── transport (the seam tests monkeypatch) ──

    def _post_with_retries(self, path: str, payload: dict, max_retries: int) -> dict:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return self._http_post(self.base_url + path, payload)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code not in _RETRY_STATUSES or attempt == max_retries:
                    raise LLMError(f"HTTP {exc.code} from gateway: {exc}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                # TimeoutError (== socket.timeout in 3.10+) is an OSError, not a
                # URLError, so it must be caught explicitly or a slow generation
                # would crash the whole run instead of being retried.
                last_exc = exc
                if attempt == max_retries:
                    raise LLMError(f"cannot reach gateway at {self.base_url}: {exc}") from exc
            # exponential backoff with jitter: 0.5, 1, 2, 4 ... (+/- 25%)
            delay = (0.5 * (2 ** attempt)) * (1 + random.uniform(-0.25, 0.25))
            time.sleep(delay)
        raise LLMError(f"exhausted retries: {last_exc}")  # pragma: no cover

    def _http_post(self, url: str, payload: dict) -> dict:
        """Single POST returning the parsed JSON body. Patched in tests."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
