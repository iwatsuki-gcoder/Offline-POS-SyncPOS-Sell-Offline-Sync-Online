"""Real LLM backend for the hybrid chatbot (stdlib only).

An OpenAI-compatible ``/chat/completions`` client over ``urllib`` — no
third-party SDK needed, which keeps the engine dependency-free. It works
with:
- OpenAI:            base_url=https://api.openai.com/v1 (default)
- Any OpenAI-compatible provider (Groq, Together, OpenRouter, ...)
- A local model:     base_url=http://localhost:11434/v1  (Ollama)

Configuration (environment):
    OFFLINEPOS_LLM_BASE_URL  default https://api.openai.com/v1
    OFFLINEPOS_LLM_API_KEY   required; without it the chatbot stays
                             offline-only (local intents)
    OFFLINEPOS_LLM_MODEL     default gpt-4o-mini
    OFFLINEPOS_LLM_TIMEOUT   seconds, default 20

Design contract: every failure mode — no key, DNS/timeout, HTTP error,
malformed JSON, empty choices — raises ``LLMError``. The chatbot catches
it and degrades to offline intents, so a dead network or a bad key can
never break billing or leave the cashier staring at an error.
"""
from __future__ import annotations

import json
import os
import urllib.request


class LLMError(Exception):
    """Anything that stopped us getting an LLM answer."""


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, system: str, user: str) -> str:
        """One chat-completion round trip. Raises LLMError on any failure."""
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": 300,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # timeout, DNS, refused, HTTPError, bad JSON
            raise LLMError(f"LLM request failed: {e}") from e
        try:
            text = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"unexpected LLM response shape: {body!r}") from e
        if not text:
            raise LLMError("LLM returned an empty answer")
        return text


def client_from_env() -> LLMClient | None:
    """Build a client from env, or None when no API key is configured."""
    api_key = os.environ.get("OFFLINEPOS_LLM_API_KEY", "").strip()
    if not api_key:
        return None
    return LLMClient(
        base_url=os.environ.get("OFFLINEPOS_LLM_BASE_URL",
                                "https://api.openai.com/v1"),
        api_key=api_key,
        model=os.environ.get("OFFLINEPOS_LLM_MODEL", "gpt-4o-mini"),
        timeout=float(os.environ.get("OFFLINEPOS_LLM_TIMEOUT", "20")),
    )
