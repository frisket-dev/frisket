"""Minimal Anthropic Messages API client for the action-flight tool.

Raw HTTP via ``httpx`` rather than the ``anthropic`` SDK: this repo's
pyproject.toml does not depend on the SDK today (``src/frisket/ai/llm/adapters.py``
talks to the Messages API directly over httpx, using the same wire shape this
module reuses). Keeping this local tool on the owned wire shape avoids adding
an SDK dependency solely for a developer utility. See
src/frisket/ai/llm/router.py for how the app resolves provider
keys in production; this script is intentionally simpler since it never runs
inside the sandboxed op broker.

Reads ``ANTHROPIC_API_KEY`` from the environment only. NEVER hardcode a key
here, log one, or write one to disk. Export it in the invoking shell before
running; ``scripts/dev/action_flight.py`` shows the local-env usage.
"""

from __future__ import annotations

import os

import httpx

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Dated Haiku snapshot keeps the two calls per action inexpensive.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicKeyMissing(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AnthropicKeyMissing(
            "ANTHROPIC_API_KEY is not set. Source .secrets/frisket.env first, e.g.:\n"
            "  set -a; . .secrets/frisket.env; set +a"
        )
    return key


def complete(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 700,
    system: str | None = None,
    timeout: float = 60.0,
) -> str:
    """One-shot text completion. Returns the concatenated text of the response."""
    body: dict[str, object] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    resp = httpx.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": _api_key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "".join(parts).strip()
