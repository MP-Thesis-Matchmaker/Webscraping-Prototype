"""LLM abstraction — the *only* place the scraper talks to a language model.

Public surface is deliberately tiny (the plan): one function

    complete(system, prompt) -> str

Everything else — which provider, which model, keys, retries — is config. The
provider is chosen by ``SCRAPER_LLM_PROVIDER`` (default ``openai``). Swapping in
another provider means adding one file ``scraper/llm_<name>.py`` that exposes a
``Provider`` class with ``available()`` and ``complete(system, prompt, **opts)``
— no call site anywhere else changes.

Degrades gracefully: if no provider is configured (e.g. no ``OPENAI_API_KEY``),
``is_available()`` is False and callers keep their deterministic output instead
of crashing.
"""

from __future__ import annotations

import importlib
import os
import time

_ENV_LOADED = False


def _load_env() -> None:
    """Load a project-root .env once, if python-dotenv is available."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    from .registry import ROOT
    load_dotenv(ROOT / ".env")


def provider_name() -> str:
    _load_env()
    return os.environ.get("SCRAPER_LLM_PROVIDER", "openai")


def model_name() -> str:
    _load_env()
    return os.environ.get("SCRAPER_LLM_MODEL", "gpt-5-mini")


def _get_provider():
    name = provider_name()
    if name == "openai":
        return _OpenAIProvider()
    # Any other provider is a pluggable module, imported by name.
    mod = importlib.import_module(f".llm_{name}", __package__)
    return mod.Provider()


def is_available() -> bool:
    try:
        return _get_provider().available()
    except Exception:
        return False


def complete(system: str, prompt: str, **opts) -> str:
    """Run one completion. Raises if the provider is unavailable or errors out
    after its retry budget — callers that want graceful degradation should gate
    on ``is_available()`` first."""
    return _get_provider().complete(system, prompt, **opts)


# --- Built-in OpenAI provider ----------------------------------------------

class _OpenAIProvider:
    """Chat Completions. No temperature is sent (gpt-5.x rejects != 1). Own
    retry loop with a hard per-request timeout so a hung call can't freeze a
    whole run; auth failures are never retried."""

    MAX_ATTEMPTS = 3
    TIMEOUT = 120

    def __init__(self) -> None:
        _load_env()
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.model = model_name()

    def available(self) -> bool:
        return bool(self.api_key)

    def _client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.api_key, timeout=self.TIMEOUT, max_retries=0)

    def complete(self, system: str, prompt: str, **opts) -> str:
        if not self.available():
            raise RuntimeError("OPENAI_API_KEY not set (see .env)")
        from openai import (APIConnectionError, APITimeoutError,
                            AuthenticationError, InternalServerError,
                            RateLimitError)

        client = self._client()
        transient = (APIConnectionError, APITimeoutError, RateLimitError,
                     InternalServerError)
        last = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                )
                return (resp.choices[0].message.content or "").strip()
            except AuthenticationError:
                raise  # bad/expired key — retrying is pointless
            except transient as exc:
                last = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM failed after {self.MAX_ATTEMPTS} attempts: {last}")
