"""LLM Gateway — the *only* module that knows HOW we reach a language model.

This is the dependency-injection seam for Gemini Vault's optional AI features
(the "Smart Librarian" and "Ask the archive"). Everything else — the Librarian,
the viewer — depends on the small :class:`LLMGateway` interface, never on a
concrete HTTP client or a particular provider. Swap Ollama for a cloud API by
editing config here; no consumer changes.

Why a gateway?
--------------
* **Provider config lives in one place** (see :data:`DEFAULT_PROVIDER` +
  :func:`load_provider_config`). The default is a *free, fully local* setup:
  Ollama serving ``gemma3:4b`` on ``http://localhost:11434/v1``. Anyone can
  ``ollama pull gemma3:4b`` and the feature lights up — no keys, no cloud.
* **Optional by construction.** When nothing is configured (or the user opts
  out), :func:`build_gateway` returns a :class:`NullGateway` whose
  ``available`` is ``False``. The viewer keeps working; it simply doesn't show
  AI extras. Nothing ever leaves the machine unless a provider is wired up.
* **Graceful when the provider is down.** Even with a provider configured, if
  Ollama isn't running the call raises :class:`LLMError`/:class:`LLMUnavailable`
  which consumers already catch — so a missing daemon degrades, never crashes.

Public surface (import these):
    LLMGateway          – the interface consumers depend on
    build_gateway()     – the factory that assembles a gateway from config
    LLMError            – request failed after retries (re-exported transport error)
    LLMUnavailable      – an AI feature was used but no provider is available
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

# The transport (stdlib-only urllib client) stays a dumb pipe; the gateway owns
# the policy (which provider, what config) and adapts it to a clean interface.
try:  # package vs. flat-script execution (serve.py adds processor/ to sys.path)
    from processor.llm_client import LLMClient, LLMError
except ImportError:  # pragma: no cover
    from llm_client import LLMClient, LLMError

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "librarian_config.json"

# ── Default provider: free + fully local. The whole point of the open-source
#    release is that this works out of the box after `ollama pull gemma3:4b`. ──
DEFAULT_PROVIDER: dict[str, str] = {
    "base_url": "http://localhost:11434/v1",
    # Ollama ignores the key, but the OpenAI-compatible client needs a non-empty
    # string to consider itself "configured". A literal placeholder keeps the
    # local path zero-friction (no real secret to manage).
    "api_key": "ollama",
    "model": "gemma3:4b",
}


class LLMUnavailable(RuntimeError):
    """Raised when an AI feature is requested but no provider is available."""


@runtime_checkable
class LLMGateway(Protocol):
    """The contract the Librarian and viewer depend on — nothing more.

    Any object with these members is a valid gateway (structural typing), which
    is exactly why tests can inject a tiny fake without importing this module.
    """

    model: str

    @property
    def available(self) -> bool:
        """True when AI calls can be attempted (a provider is configured)."""
        ...

    def generate_json(self, prompt: str, *, system: str | None = None) -> Any:
        """Run a single-turn prompt and parse the reply as JSON."""
        ...

    def generate_text(self, prompt: str, *, system: str | None = None) -> str:
        """Run a single-turn prompt and return the reply text."""
        ...


class OpenAICompatGateway:
    """Gateway over any OpenAI-compatible endpoint (Ollama by default).

    Adapts the low-level :class:`LLMClient` transport to the universal
    :class:`LLMGateway` interface. Consumers see only ``generate_json`` /
    ``generate_text`` / ``available`` / ``model`` — never the HTTP details.
    """

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 timeout: float = 180.0):
        self._client = LLMClient(base_url=base_url, api_key=api_key,
                                 model=model, timeout=timeout)
        self.model = self._client.model

    # Health-ping budget: short enough to feel instant in the UI, long enough to
    # survive a loaded local daemon. A closed/missing port fails far quicker.
    PING_TIMEOUT = 1.0

    def _ping(self) -> bool:
        """Fast TCP probe: is anything actually listening at host:port?"""
        parts = urlsplit(self._client.base_url)
        host = parts.hostname or "localhost"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=self.PING_TIMEOUT):
                return True
        except OSError:
            return False

    @property
    def available(self) -> bool:
        # Configured AND reachable: a quick TCP ping means a missing/stopped
        # Ollama daemon reports False up front (UI hides AI), instead of failing
        # only mid-request. The probe is bounded by PING_TIMEOUT, never hangs.
        return self._client.enabled and self._ping()

    def generate_json(self, prompt: str, *, system: str | None = None) -> Any:
        return self._client.complete_json(prompt, system=system)

    def generate_text(self, prompt: str, *, system: str | None = None) -> str:
        return self._client.complete(prompt, system=system)


class NullGateway:
    """The "AI is off" gateway: always unavailable, never reaches the network.

    Returned by :func:`build_gateway` when no provider is configured, so the
    core app can stay blissfully unaware that AI exists. ``available`` is
    ``False``; calling a generate method is a programming error guarded by it.
    """

    model = "(none)"
    available = False

    def generate_json(self, prompt: str, *, system: str | None = None) -> Any:
        raise LLMUnavailable("no LLM provider configured (Smart Librarian is opt-in)")

    def generate_text(self, prompt: str, *, system: str | None = None) -> str:
        raise LLMUnavailable("no LLM provider configured (Smart Librarian is opt-in)")


def load_provider_config() -> dict[str, str]:
    """Resolve the active provider config: defaults -> file -> environment.

    Precedence (later wins):
      1. :data:`DEFAULT_PROVIDER`              (local Ollama, gemma3:4b)
      2. ``librarian_config.json``             (gitignored; may hold a cloud key)
      3. environment variables                 (LLM_* preferred; FREELLMAPI_* legacy)
    """
    cfg: dict[str, str] = dict(DEFAULT_PROVIDER)

    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Only override with truthy values so a partial file can't blank a key.
            cfg.update({k: v for k, v in file_cfg.items() if v})
        except (json.JSONDecodeError, OSError):
            pass

    # Generic LLM_* names are the documented way; FREELLMAPI_* kept for back-compat.
    env_map = (
        ("LLM_BASE_URL", "base_url"), ("LLM_API_KEY", "api_key"), ("LLM_MODEL", "model"),
        ("FREELLMAPI_BASE_URL", "base_url"), ("FREELLMAPI_KEY", "api_key"),
        ("FREELLMAPI_MODEL", "model"),
    )
    for env_name, key in env_map:
        if os.getenv(env_name):
            cfg[key] = os.environ[env_name]

    return cfg


def build_gateway(config: dict[str, str] | None = None) -> LLMGateway:
    """Assemble a gateway from config. This is the factory consumers call.

    Returns a :class:`NullGateway` (AI off) when no API key is present, otherwise
    an :class:`OpenAICompatGateway` pointed at the configured provider. Passing
    ``config`` explicitly (e.g. in tests) bypasses file/env resolution.
    """
    cfg = config if config is not None else load_provider_config()
    if not cfg.get("api_key"):
        return NullGateway()
    return OpenAICompatGateway(
        base_url=cfg.get("base_url", DEFAULT_PROVIDER["base_url"]),
        api_key=cfg["api_key"],
        model=cfg.get("model", DEFAULT_PROVIDER["model"]),
    )


__all__ = [
    "LLMGateway", "OpenAICompatGateway", "NullGateway",
    "build_gateway", "load_provider_config",
    "LLMError", "LLMUnavailable", "DEFAULT_PROVIDER",
]
