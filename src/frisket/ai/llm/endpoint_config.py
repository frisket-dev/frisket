"""The local model server's endpoint identity: one atomic bundle.

Pure llm-layer module -- no import of ``frisket.server`` (the server layer
imports this, never the reverse), so ``ModelRouter`` and every other llm-layer
consumer can depend on it without a layering cycle.

``LocalModelEndpointConfig`` is the ONE thing every seam that talks to the
"ollama" slot (adapter, probes, diagnostics, the pull worker, the transcribe
borrower) is threaded with, instead of a bare URL string or scattered
token/flag kwargs. Resolution into this
shape (env bundle plus the persisted collection) lives in
``frisket.server.provider_config.resolve_local_endpoints`` -- a server-layer
concern (it reads the workspace file) -- this module only defines the shape,
strict identity grammar, and tokenless-bearer rule.
"""

from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

# The dummy bearer a tokenless local-server adapter sends. Ollama's own
# OpenAI-compatible surface ignores the Authorization header, while adapters
# still use one uniform authenticated request shape.
DEFAULT_OLLAMA_BEARER = "ollama"

# Origins that count as "loopback" for the token-bearing-origin HTTPS rule:
# a token is meaningless to intercept on a connection that never leaves the
# host.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class LocalModelEndpointConfig:
    """One atomic bundle describing an OpenAI-compatible local model server.

    ``origin`` is ALWAYS a `normalize_ollama_url`-validated origin (never a
    raw/unvalidated string) -- every construction path in this codebase
    validates before building one of these, so callers can rely on it.
    ``source`` is diagnostics-only provenance (``env`` or ``local_file``),
    never itself a trust decision.
    """

    endpoint_id: str
    display_name: str
    origin: str
    source: Literal["env", "local_file"]
    inference_token: str | None = None
    provisioning_token: str | None = None
    edge_auth: bool = False
    pull_enabled: bool = False

    def bearer_for_inference(self) -> str:
        """The Authorization bearer the adapter/probes send for inference
        calls: the configured token, or the tokenless adapter sentinel."""
        return self.inference_token or DEFAULT_OLLAMA_BEARER

    def bearer_for_provisioning(self) -> str | None:
        """The bearer the pull worker sends for BOTH its calls (tags re-list
        and pull) when a provisioning token is configured; None means
        tokenless. It never falls back to the inference token."""
        return self.provisioning_token


def normalize_ollama_url(url: str) -> str:
    """Validate + normalize a local-server base URL to an ORIGIN:
    ``http(s)://host[:port]`` only -- no userinfo, path, query, or fragment
    because the value feeds server-side GETs and
    later carries prompt content, so it must not smuggle credentials or a
    path to an arbitrary endpoint. The router appends ``/v1`` verbatim.

    Lives in the LLM layer (not ``frisket.server``) so every composition root
    validates endpoint origins through the same authority.
    """
    clean = (url or "").strip()
    if len(clean) > 200:
        raise ValueError("ollama url is too long")
    parsed = urlparse(clean)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.strip("/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "ollama url must be an origin — http(s)://host[:port] with no "
            "credentials, path, or query"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def origin_is_https_or_loopback(origin: str) -> bool:
    """A token-bearing bundle whose origin is plain HTTP and NOT loopback is
    rejected as a
    unit. Loopback http (the default local dev shape) is fine -- the token
    never leaves the host."""
    parsed = urlparse(origin)
    if parsed.scheme == "https":
        return True
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# env-bundle resolution
#
# Pure env parsing + `normalize_ollama_url` -- no filesystem/workspace
# access -- so it lives in the llm layer, not `frisket.server`, and every
# composition root can share one implementation.
# ---------------------------------------------------------------------------

_TRUTHY = ("1", "true", "yes", "on")

# The env bundle's field names. Any of these being set at all is what makes
# the env bundle "the candidate" (even if it turns out invalid and gets
# rejected) — a lone OLLAMA_URL with no tokens is still an env-sourced origin.
ENV_OLLAMA_URL = "OLLAMA_URL"
ENV_LLM_TOKEN = "FRISKET_LLM_TOKEN"
ENV_LLM_PROVISIONING_TOKEN = "FRISKET_LLM_PROVISIONING_TOKEN"
ENV_LLM_EDGE_AUTH = "FRISKET_LLM_EDGE_AUTH"
ENV_MODEL_PULL_ENABLED = "FRISKET_ENABLE_MODEL_PULL"


def resolve_env_local_endpoint(
    env: dict[str, str] | None = None,
) -> tuple[LocalModelEndpointConfig | None, list[str]]:
    """The env bundle candidate ONLY (no file/default fallback) — shared by
    ``frisket.server.provider_config.resolve_local_endpoints``, the org-run
    env-bundle carve-out in
    ``jobs/runs.py`` (org runs read ONLY the operator's env bundle, never the
    workspace file), and the team tier's org routes.

    Returns ``(None, notes)`` when no env field is set at all (env is not a
    candidate) OR when the candidate is rejected as a unit — an invalid/
    missing origin while tokens are set, or a token-bearing origin that is
    plain http and not loopback. Rejection always drops the WHOLE bundle
    (including the origin) so an env token can never ride a non-env origin.
    """
    env = os.environ if env is None else env
    notes: list[str] = []
    fields = (
        ENV_OLLAMA_URL,
        ENV_LLM_TOKEN,
        ENV_LLM_PROVISIONING_TOKEN,
        ENV_LLM_EDGE_AUTH,
        ENV_MODEL_PULL_ENABLED,
    )
    if not any((env.get(name) or "").strip() for name in fields):
        return None, notes

    raw_origin = env.get(ENV_OLLAMA_URL)
    inference_token = env.get(ENV_LLM_TOKEN) or None
    provisioning_token = env.get(ENV_LLM_PROVISIONING_TOKEN) or None
    edge_auth = (env.get(ENV_LLM_EDGE_AUTH) or "").strip().lower() in _TRUTHY
    pull_enabled = (env.get(ENV_MODEL_PULL_ENABLED) or "").strip().lower() in _TRUTHY
    has_tokens = bool(inference_token or provisioning_token)

    try:
        origin = normalize_ollama_url(raw_origin) if raw_origin else None
    except ValueError:
        origin = None

    if origin is None:
        notes.append(
            "env local-model bundle rejected: OLLAMA_URL is missing or "
            "invalid, so FRISKET_LLM_TOKEN/FRISKET_LLM_PROVISIONING_TOKEN "
            "cannot be used — an env token may never ride a non-env origin"
        )
        return None, notes

    if has_tokens and not origin_is_https_or_loopback(origin):
        notes.append(
            "env local-model bundle rejected: a token-bearing origin must "
            f"be https (or loopback), got {origin!r}"
        )
        return None, notes

    return (
        LocalModelEndpointConfig(
            endpoint_id=f"env-{hashlib.sha256(origin.encode()).hexdigest()[:12]}",
            display_name="Environment server",
            origin=origin,
            inference_token=inference_token,
            provisioning_token=provisioning_token,
            edge_auth=edge_auth,
            pull_enabled=pull_enabled,
            source="env",
        ),
        notes,
    )
