"""Error-CLASS remediation for the model layer.

Fix-the-genre seam: this module translates the two named, recoverable LLM
failure classes at the ONE place they are typed (the router/adapters, via
:class:`frisket.llm.types.LLMError`), not by matching
raw provider/stderr text string-by-string inside UI components or ad hoc
`except Exception` blocks scattered through the executor.

- ``missing_provider_key``: the router raised its own, fixed
  ``"no adapter for provider '<name>'"`` message (router.py:_call_with_retry)
  because no API key is configured. We control that exact string, so matching
  it is matching OUR typed error, not a third party's.
- ``ollama_unreachable``: the router's closed ``LLMError.transport_kind``
  records a connection failure without preserving the raw httpx exception; a
  connection failure while calling the always-registered
  ``ollama`` adapter (models/metadata.py:PROVIDER_KIND) means the local Ollama server
  isn't running, not a real "no key" problem.
- ``model_not_installed``: the local server answered 404 with a
  machine-readable not-found reason (``LLMError.provider_code``, parsed from
  the OpenAI-compat error body by adapters._raise_for_status — Ollama emits
  ``type: "not_found_error"``) — the server is up but the model isn't pulled.
  Local-server-only: a cloud provider's 404 is a different genre (typo'd
  model id; no install remediation exists) and stays ``model_error``.

Everything else stays ``model_error``/``invalid_output`` with the original
message untouched — no guessing at causes we haven't named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frisket.local_model_ids import bare_model_name, parse_local_model_id

from .cache import CacheMiss
from .types import LLMError, SchemaViolation

MISSING_PROVIDER_KEY = "missing_provider_key"
OLLAMA_UNREACHABLE = "ollama_unreachable"
MODEL_NOT_INSTALLED = "model_not_installed"
MODEL_ERROR = "model_error"
INVALID_OUTPUT = "invalid_output"
PROVIDER_RATE_LIMITED = "provider_rate_limited"
PROVIDER_KEY_EXHAUSTED = "provider_key_exhausted"
INVALID_PROVIDER_KEY = "invalid_provider_key"
LOCAL_TRANSCRIBE_UNSUPPORTED = "local_transcribe_unsupported"

# Machine-readable provider reasons (LLMError.provider_code, parsed from the
# provider's error body by adapters._raise_for_status) that mean the key's
# quota/credits are spent: an immediate retry cannot succeed the way it can
# for a momentary rate limit — the fix is funding the key, then re-running.
KEY_EXHAUSTED_PROVIDER_CODES = frozenset({"insufficient_quota"})

# Machine-readable not-found reasons on a local-server 404 that mean "the
# model isn't installed". Ollama's OpenAI-compat surface emits
# `type: "not_found_error"` with `code: null`; `model_not_found` is OpenAI's
# equivalent code, accepted in case a local server adopts it. Structured
# codes only — an HTML 404 or bare-text body parses no provider_code and
# stays ambiguous.
MODEL_NOT_FOUND_PROVIDER_CODES = frozenset({"not_found_error", "model_not_found"})

# The typed resumable provider-failure vocabulary and its honest retry/resume
# metadata. Every code here has live producers — classify_resumable_provider_error
# below, called from
# runner/map_runner.py's per-row seam and executor/action_families/reduces.py's
# per-group seam — and three consumers reuse this SAME mapping so surfaces
# cannot disagree about retryability: jobs/runs.py promotes the row code to
# the run-level ActionError, executor/action_families/reduces.py promotes the
# group code to the receipt ActionError, and store/runs.py stamps the details
# onto row-error summary groups (public_status.row_errors).
RESUMABLE_PROVIDER_ERROR_DETAILS: dict[str, dict[str, bool]] = {
    PROVIDER_RATE_LIMITED: {"retryable": True, "resumable": True},
    PROVIDER_KEY_EXHAUSTED: {"retryable": False, "resumable": True},
    INVALID_PROVIDER_KEY: {"retryable": False, "resumable": True},
}

_NO_ADAPTER_PREFIX = "no adapter for provider"


def missing_provider_key_message(provider: str) -> str:
    """Canonical actionable copy for a keyless provider (manifest
    `llm-model-key-request-gate-v1`). The ONE place this string is built —
    both the request-time gates (MapRunner._validate_spec's MissingProviderKey
    pre-flight, server/action_enqueue.py's generic request-time model-key
    gate, and the three existing MissingProviderKey exception handlers in
    server/action_enqueue.py, executor/action_lifecycle.py, and
    server/services/action_preview_runs.py) and this module's own row-level
    classify_llm_error branch below build the message from, so a request-time
    400 and a row-level failure never drift apart."""
    return (
        f"No API key is configured for '{provider}'. Add one in "
        "Settings → AI Providers, or pick a different model."
    )


@dataclass
class RemediatedError:
    """``message`` is the polished, user-facing string — safe to store as a
    result cell's ``error`` or an ``ActionError.message`` verbatim. ``details``
    carries structured extras (provider name, endpoint identity and origin)
    for callers that
    want to render a settings deep link or "open Diagnose" affordance rather
    than just the string."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def classify_llm_error(
    exc: Exception,
    *,
    provider: str | None = None,
    endpoint_origin: str | None = None,
    model: str | None = None,
) -> RemediatedError:
    """Classify an exception caught from the map-runner's model-call seam.

    ``provider`` is the model's provider id (``spec["model"].split("/", 1)[0]``);
    ``endpoint_origin`` is the selected local endpoint address, when known.
    ``model`` is the full requested
    model id (``ollama/@studio/qwen3:0.6b``), when known — it lets the
    ``model_not_installed`` copy name the model and carry a copyable pull
    command; without it the class is still typed, just less specific.
    """
    raw = str(exc)
    # Local import: `frisket.sdk.ops.transcribe` depends on the llm layer
    # (pricing) already, so importing it at module level HERE would create
    # the reverse edge this layer must never have (llm never imports ops) —
    # a deferred, function-scope import avoids the cycle at import time,
    # the same pattern this codebase already uses to break layer cycles
    # (e.g. router.py's `.structured` import). The split-host Caddy front
    # door's method/path allowlist doesn't include the transcription route,
    # so the borrower's typed "unsupported in this deployment shape" error
    # (``LocalTranscribeUnsupportedError``, already a ``RuntimeError``
    # subclass so it already reaches this classifier via map_runner's
    # existing catch tuple) deserves its own actionable code and copy
    # instead of falling through to the generic ``model_error`` bucket with
    # its raw message.
    from frisket.sdk.ops.transcribe_engines import LocalTranscribeUnsupportedError

    if isinstance(exc, LocalTranscribeUnsupportedError):
        return RemediatedError(
            code=LOCAL_TRANSCRIBE_UNSUPPORTED,
            message=(
                "Local-server transcription isn't supported through an "
                "authenticated front door in this deployment. Use a cloud "
                "provider for transcription instead, or reach the local "
                "server directly (without edge auth) if that's an option."
            ),
        )
    if isinstance(exc, LLMError) and raw.startswith(_NO_ADAPTER_PREFIX):
        message = (
            missing_provider_key_message(provider)
            if provider
            else (
                "No API key is configured for this provider. Add one in "
                "Settings → AI Providers, or pick a different model."
            )
        )
        return RemediatedError(
            code=MISSING_PROVIDER_KEY,
            message=message,
            details={"provider": provider} if provider else {},
        )
    if isinstance(exc, CacheMiss):
        # replay_strict miss: CacheMiss's own text is developer copy telling
        # the reader to re-run pytest with FRISKET_CACHE_REFRESH — never show
        # that to a real user (replay_strict is an operator-settable
        # FRISKET_HOSTED_CACHE_MODE value). The raw text still reaches the
        # model-call trace via the caller; the cell gets this instead.
        return RemediatedError(
            code=MODEL_ERROR,
            message=(
                "No cached result for this row and live AI calls are off "
                "(replay mode). Enable live calls or configure a provider, "
                "then re-run."
            ),
        )
    if (
        provider == "ollama"
        and isinstance(exc, LLMError)
        and exc.status == 404
        and getattr(exc, "provider_code", None) in MODEL_NOT_FOUND_PROVIDER_CODES
    ):
        # The server is up (it answered) but the model isn't installed. Class,
        # not brand, in the guidance; the brand appears only inside the one
        # concrete command a user can copy.
        url = endpoint_origin or "the configured local server URL"
        details: dict[str, Any] = {}
        if endpoint_origin:
            details["endpoint_origin"] = endpoint_origin
        bare = bare_model_name(model) if model else None
        if bare:
            endpoint_id, _ = parse_local_model_id(model)
            pull_command = f"ollama pull {bare}"
            details["model"] = model
            details["endpoint_id"] = endpoint_id
            details["pull_command"] = pull_command
            message = (
                f"The model '{bare}' isn't installed on the local server at "
                f"{url}. Install it there — for Ollama: `{pull_command}` — or "
                "pick another model."
            )
        else:
            message = (
                f"The requested model isn't installed on the local server at "
                f"{url}. Install it there, or pick another model."
            )
        return RemediatedError(
            code=MODEL_NOT_INSTALLED, message=message, details=details
        )
    if provider == "ollama" and getattr(exc, "transport_kind", None) == "connect":
        url = endpoint_origin or "the configured local server URL"
        details = {"endpoint_origin": endpoint_origin} if endpoint_origin else {}
        if model:
            try:
                endpoint_id, _bare_model = parse_local_model_id(model)
            except ValueError:
                pass
            else:
                details["model"] = model
                details["endpoint_id"] = endpoint_id
        return RemediatedError(
            code=OLLAMA_UNREACHABLE,
            # Class, not brand: the "ollama"
            # slot serves any OpenAI-compatible local server.
            message=(
                f"No local AI server is responding at {url} — start Ollama or "
                "LM Studio, or pick another model."
            ),
            details=details,
        )
    return RemediatedError(
        code=INVALID_OUTPUT if isinstance(exc, SchemaViolation) else MODEL_ERROR,
        message=raw,
    )


def classify_resumable_provider_error(
    exc: Exception, *, provider: str | None = None
) -> RemediatedError | None:
    """Opt-in normalization for provider credential/capacity failures.

    Generic LLM remediation deliberately keeps provider HTTP failures as
    ``model_error`` for compatibility. Runtime families that expose a
    resumable provider contract (map rows, reduce group summaries) call this
    first, so the typed status is not guessed from text and unrelated error
    taxonomy does not change. Three classes are typed:

    - ``invalid_provider_key``: HTTP 401/403 — the configured key is present
      but rejected (typo'd, revoked, or unauthorized).
    - ``provider_key_exhausted``: HTTP 429 whose ``provider_code`` is a
      KEY_EXHAUSTED_PROVIDER_CODES member — spend is gone, retrying now
      cannot help; fund the key and re-run.
    - ``provider_rate_limited``: any other retryable HTTP 429.

    Messages here are CANONICAL copy, never ``str(exc)``: a 401/429 provider
    body (or an adapter echo of the request) may contain the credential
    itself, and these messages persist into results, receipts, and queue
    payloads (redaction pinned by tests/test_split_byok_error_taxonomy.py).
    """
    if not isinstance(exc, LLMError):
        return None
    if exc.status in (401, 403):
        details = dict(RESUMABLE_PROVIDER_ERROR_DETAILS[INVALID_PROVIDER_KEY])
        if provider:
            details["provider"] = provider
        named = f"'{provider}'" if provider else "this provider"
        return RemediatedError(
            code=INVALID_PROVIDER_KEY,
            message=(
                f"The provider rejected the API key configured for {named} "
                f"(HTTP {exc.status}: invalid or revoked). Update the key in "
                "Settings → AI Providers, then re-run."
            ),
            details=details,
        )
    if exc.status == 429 and exc.retryable:
        provider_code = getattr(exc, "provider_code", None)
        if provider_code in KEY_EXHAUSTED_PROVIDER_CODES:
            details = dict(RESUMABLE_PROVIDER_ERROR_DETAILS[PROVIDER_KEY_EXHAUSTED])
            if provider:
                details["provider"] = provider
            named = f"'{provider}'" if provider else "this provider"
            return RemediatedError(
                code=PROVIDER_KEY_EXHAUSTED,
                message=(
                    f"The API key configured for {named} is out of quota or "
                    f"credits (provider reported {provider_code}). Add credits "
                    "or raise the limit with the provider, then re-run to "
                    "resume."
                ),
                details=details,
            )
        return RemediatedError(
            code=PROVIDER_RATE_LIMITED,
            message=(
                "The provider rate limited this request (HTTP 429). Wait a "
                "moment, then re-run to resume from completed rows."
            ),
            # Exactly retryable/resumable, no provider key: the open-team
            # receipt contract pins this details dict verbatim.
            details=dict(RESUMABLE_PROVIDER_ERROR_DETAILS[PROVIDER_RATE_LIMITED]),
        )
    return None
