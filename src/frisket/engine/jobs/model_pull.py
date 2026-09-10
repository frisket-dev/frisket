"""In-app model pull: grammar, tolerant stream parser, and the ``model.pull``
job handler.

Registry-less model references only -- ``name[:tag]`` or
``namespace/name[:tag]``, conservative charset, length-capped, no registry
host. Registry-qualified refs (``evil.example/x/y``) are a second-hop egress
primitive (the model host would connect to an attacker-chosen registry) and
are rejected outright, never passed through.

The job payload is ``{"pull_id", "workspace_root"}`` -- NEVER a URL/token; the
handler re-resolves the FULL local-model endpoint bundle from config at claim
time (``provider_config.resolve_local_endpoints`` -- id, origin, provisioning
token, edge_auth), so a payload can't be used to redirect the pull at an
arbitrary host or smuggle a credential.

Idempotent by construction: every attempt lists installed models first and
completes immediately if the canonical ref is already present -- queue
retries and worker restarts converge instead of corrupting or duplicating
work (Ollama may keep downloading server-side after a client disconnect).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from frisket.engine.jobs import model_pull_store
from frisket.engine.jobs.queue import MODEL_PULL_KIND, JobQueue
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.worker import (
    HandlerRegistration,
    HandlerRegistry,
    NonRetryableJobError,
)

LOG = logging.getLogger("frisket.jobs.model_pull")

MODEL_PULL_MAX_ATTEMPTS = 3
MODEL_PULL_HANDLER_ORIGIN = "frisket.model_pull.base"

# Read timeout between chunks of the pull stream: Ollama can go quiet for a
# while between progress lines (a slow layer), but a stall well past this is
# indistinguishable from a hung connection.
_PULL_READ_TIMEOUT_SECONDS = 30.0
_PULL_CONNECT_TIMEOUT_SECONDS = 10.0
_TAGS_TIMEOUT_SECONDS = 10.0

# Durable progress writes are throttled: at most once per this interval,
# unless the phase itself changed (a phase transition is always worth a
# write even if it arrives faster than the throttle window).
_PROGRESS_WRITE_MIN_INTERVAL_SECONDS = 0.5


# ---------------------------------------------------------------------------
# grammar
# ---------------------------------------------------------------------------


class InvalidModelRefError(ValueError):
    """A model reference fails the registry-less grammar."""


MAX_MODEL_REF_LENGTH = 160
# Case-insensitive on input -- canonicalization lowercases name/namespace, so
# the charset is validated before lowering (an uppercase component lowers
# into exactly the same charset; canonical output enforces the true grammar).
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def normalize_model_ref(raw: str) -> str:
    """Validate + canonicalize a registry-less model reference.

    Grammar: ``name[:tag]`` or ``namespace/name[:tag]``. ``name``/
    ``namespace`` each match ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}`` (lowercased
    on output); ``namespace`` may not contain a dot (that is always a
    registry host -- ``docker.io/x``, ``ghcr.io/x/y`` -- and this grammar is
    registry-less by design); at most one ``/``; ``tag`` matches
    ``[A-Za-z0-9._-]{1,64}`` and its case is PRESERVED (Ollama tags are
    case-sensitive on the wire); a bare ref with no ``:tag`` canonicalizes to
    ``:latest`` for dedupe stability. Total length is capped at 160 chars.
    Raises :class:`InvalidModelRefError` naming the specific restriction.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidModelRefError("model reference is required")
    if len(text) > MAX_MODEL_REF_LENGTH:
        raise InvalidModelRefError(
            f"model reference is too long (max {MAX_MODEL_REF_LENGTH} characters)"
        )
    if ":" in text:
        path_part, tag_part = text.rsplit(":", 1)
    else:
        path_part, tag_part = text, None

    segments = path_part.split("/")
    if len(segments) > 2:
        raise InvalidModelRefError(
            "model reference may have at most one namespace/name separator"
        )
    if len(segments) == 2:
        namespace, name = segments
    else:
        namespace, name = None, segments[0]

    if namespace is not None:
        if "." in namespace:
            raise InvalidModelRefError(
                "registry-qualified model references are not supported "
                "(the namespace looks like a registry host)"
            )
        if not _COMPONENT_RE.match(namespace):
            raise InvalidModelRefError(
                "model reference namespace has an invalid format"
            )
    if not _COMPONENT_RE.match(name):
        raise InvalidModelRefError("model reference name has an invalid format")

    if tag_part is not None:
        if not _TAG_RE.match(tag_part):
            raise InvalidModelRefError("model reference tag has an invalid format")
        tag = tag_part
    else:
        tag = "latest"

    canonical_name = name.lower()
    canonical = f"{namespace.lower()}/{canonical_name}" if namespace else canonical_name
    canonical = f"{canonical}:{tag}"
    if len(canonical) > MAX_MODEL_REF_LENGTH:
        raise InvalidModelRefError(
            f"model reference is too long (max {MAX_MODEL_REF_LENGTH} characters)"
        )
    return canonical


# ---------------------------------------------------------------------------
# tolerant JSON-lines stream parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullEvent:
    """One parsed step of an ``/api/pull`` stream.

    ``phase`` is one of ``manifest`` | ``downloading`` | ``verifying`` |
    ``writing`` | ``done`` | ``error``. ``total_bytes``/``completed_bytes``
    are the aggregate across every digest seen so far (None until at least
    one digest has reported a ``total``) -- Ollama reports per-layer-digest
    totals that grow in number as new layers start, so progress is
    indeterminate until the first layer appears.
    """

    phase: str
    total_bytes: int | None
    completed_bytes: int | None
    error_message: str | None = None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def iter_pull_events(lines: Iterable[str | bytes]) -> Iterator[PullEvent]:
    """Parse an Ollama ``/api/pull`` JSON-lines stream tolerantly.

    Unparseable or unrecognized lines are skipped, never raised -- Ollama's
    stream format is proprietary and unversioned (risk noted in the design
    doc); a single unexpected line must not abort an otherwise-good pull.
    Terminates (returns) on a terminal ``error`` or ``success`` line; a
    stream that ends without one simply stops yielding (the caller decides
    what an early EOF means).
    """
    digest_totals: dict[str, int] = {}
    digest_completed: dict[str, int] = {}

    def _aggregate() -> tuple[int | None, int | None]:
        if not digest_totals:
            return None, None
        total = sum(digest_totals.values())
        completed = sum(digest_completed.get(d, 0) for d in digest_totals)
        return total, completed

    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", "replace")
            if isinstance(raw_line, bytes)
            else raw_line
        )
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        error = data.get("error")
        if error:
            total, completed = _aggregate()
            yield PullEvent(
                phase="error",
                total_bytes=total,
                completed_bytes=completed,
                error_message=str(error),
            )
            return

        status = str(data.get("status") or "")
        digest = data.get("digest")
        if isinstance(digest, str) and digest:
            total_val = _int_or_none(data.get("total"))
            if total_val is not None:
                digest_totals[digest] = total_val
            completed_val = _int_or_none(data.get("completed"))
            if completed_val is not None:
                digest_completed[digest] = completed_val
            total, completed = _aggregate()
            yield PullEvent(
                phase="downloading", total_bytes=total, completed_bytes=completed
            )
            continue

        total, completed = _aggregate()
        if status.startswith("pulling manifest"):
            yield PullEvent(
                phase="manifest", total_bytes=total, completed_bytes=completed
            )
        elif status.startswith("verifying"):
            yield PullEvent(
                phase="verifying", total_bytes=total, completed_bytes=completed
            )
        elif status.startswith("writing"):
            yield PullEvent(
                phase="writing", total_bytes=total, completed_bytes=completed
            )
        elif status == "success":
            yield PullEvent(phase="done", total_bytes=total, completed_bytes=completed)
            return
        # else: an unrecognized status text -- tolerant skip, no event.


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


class ModelPullTerminalError(RuntimeError):
    """Raised by ``_fail()`` for a RETRYABLE failure class that this attempt
    already recorded on the ``model_pulls`` row (either finalized to
    'failed', if this was the job's LAST attempt, or just an attempt-error
    note while the row stays active, if attempts remain -- see ``_fail``).
    Either way the row bookkeeping is already done; the caller (``handle``'s
    boundary) re-raises this as-is rather than re-wrapping it.

    Contrast with :class:`frisket.jobs.worker.NonRetryableJobError`, raised
    for a TERMINAL failure class (capability/auth failure, an invalid model,
    a structured upstream error, an endpoint change, a reconciliation
    failure) -- those always finalize the row to 'failed' immediately and
    stop the job from retrying at all, regardless of attempts remaining.
    """


def _fetch_tags(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None
) -> tuple[int | None, list[dict[str, Any]] | None]:
    """Returns (status_code, models) -- models is None when the server did
    not answer with Ollama's native ``{"models": [...]}`` shape (empty list
    included as valid, per the reachability probe's own contract)."""
    try:
        resp = client.get(
            f"{url}/api/tags", headers=headers or {}, timeout=_TAGS_TIMEOUT_SECONDS
        )
    except httpx.HTTPError:
        return None, None
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        body = resp.json()
    except ValueError:
        return resp.status_code, None
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return resp.status_code, None
    return resp.status_code, [m for m in body["models"] if isinstance(m, dict)]


def _split_ref(ref: str) -> tuple[str, str]:
    """Split ``namespace/name:tag`` (or ``name:tag``) into ``(path, tag)``,
    defaulting a missing tag to ``latest`` -- mirrors ``normalize_model_ref``'s
    own default so an entry with no explicit tag compares correctly."""
    if ":" in ref:
        path, tag = ref.rsplit(":", 1)
    else:
        path, tag = ref, "latest"
    return path, tag


def _installed_entry(
    models: list[dict[str, Any]], canonical_ref: str
) -> dict[str, Any] | None:
    """Match an ``/api/tags`` entry against our canonical ref.

    The previous version lowercased the WHOLE ref including the tag before
    comparing, but ``normalize_model_ref`` deliberately PRESERVES tag case
    (Ollama tags are case-sensitive on the wire, docstring above) -- so a
    lowercase compare here could false-positive-match a differently-cased
    tag that is actually a DIFFERENT model on the server. name/namespace
    compare case-insensitively (matches normalize_model_ref's own
    lowercasing of those parts); the tag compares case-SENSITIVELY.
    """
    target_path, target_tag = _split_ref(canonical_ref)
    target_path_lower = target_path.lower()
    for entry in models:
        name = str(entry.get("name") or entry.get("model") or "")
        entry_path, entry_tag = _split_ref(name)
        if entry_path.lower() == target_path_lower and entry_tag == target_tag:
            return entry
    return None


def _require_provenance(entry: dict[str, Any]) -> tuple[str, int] | None:
    """Require a resolved digest and size on every completed pull.

    Both the
    already-installed fast path and post-pull reconciliation match a tags
    entry by name only, and Ollama's own ``/api/tags`` schema does not
    guarantee either field is present or well-formed. Returns ``None`` when
    the digest is missing/blank or the size is missing/not-a-positive-
    integer, so the caller fails closed (``reconciliation_failed``) instead
    of ever calling ``mark_done`` with a null/zero digest or size."""
    digest = entry.get("digest")
    size = _int_or_none(entry.get("size"))
    if not isinstance(digest, str) or not digest:
        return None
    if size is None or size <= 0:
        return None
    return digest, size


def _make_cancel_checker(
    queue: JobQueue, job_id: int, engine, pull_id: int
) -> Callable[[], bool]:
    """Cooperative cancellation: cancel is best-effort (closing frisket's
    connection may not stop shared server-side work -- Ollama shares pull
    progress across callers), polled between stream chunks. Either signal
    (the job row flipped to cancelled by ``JobQueue.cancel`` while running,
    or the pull row's own ``cancel_requested_at``) is enough."""

    def check() -> bool:
        job = queue.get(job_id)
        if job is not None and job.status == "cancelled":
            return True
        row = model_pull_store.get(engine, pull_id)
        return row is not None and row.cancel_requested_at is not None

    return check


def _redact(text: str, secrets: Iterable[str]) -> str:
    """Scrub any configured token VALUE out of upstream-sourced text before
    it becomes durable. This is a belt-and-suspenders net over
    the one upstream-controlled field that already rides through ``_fail``
    verbatim by design (Ollama's own structured pull-stream ``error``
    message): if a misconfigured front door or daemon ever echoed a bearer
    value back in its own error text, it must not leak into the durable
    ``model_pulls`` row regardless."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def _fail(
    engine,
    pull_id: int,
    *,
    error_code: str,
    message: str,
    secrets: Iterable[str] = (),
    terminal: bool,
    is_final_attempt: bool,
) -> Exception:
    """Record this attempt's outcome and return the exception the caller
    should raise.

    ``terminal=True`` (a non-retryable failure class -- capability/auth
    failure, invalid model, a structured upstream error, endpoint change,
    reconciliation failure) ALWAYS finalizes the row to 'failed' and returns
    a :class:`~frisket.jobs.worker.NonRetryableJobError`, regardless of
    remaining attempts -- this class of error will never succeed on retry.

    ``terminal=False`` is a retryable class (connect error, read timeout, an
    unclassified transport/parse bug). If ``is_final_attempt`` is also True,
    the row is finalized to 'failed' too (this really is the last chance --
    the queue's own ``fail()`` will terminal-fail the JOB regardless once it
    sees attempts >= max_attempts, so the row must agree). Otherwise the row
    stays ACTIVE (``record_attempt_error`` -- status untouched) so the
    requeued retry resumes the SAME row instead of a duplicate active row
    getting created for the same ref while this one still reads 'failed'
    (exactly the review's reproduced defect).
    """
    safe_message = _redact(message, secrets)
    if terminal or is_final_attempt:
        model_pull_store.mark_failed(
            engine, pull_id, error_code=error_code, error_message=safe_message
        )
    else:
        model_pull_store.record_attempt_error(
            engine, pull_id, error_code=error_code, error_message=safe_message
        )
    if terminal:
        return NonRetryableJobError(f"{error_code}: {safe_message}")
    return ModelPullTerminalError(f"{error_code}: {safe_message}")


# The one known curated upstream shape: Ollama's
# manifest-not-found error always starts with this exact prefix.
_MODEL_NOT_FOUND_PREFIX = "pull model manifest"


def _classify_upstream_error(raw_message: str) -> tuple[str, str]:
    """Classify an Ollama pull-stream ``error`` line into
    ``(error_code, canonical_copy)``. The RAW upstream text is deliberately
    NOT part of
    either return value: only the caller's ``logger.warning`` (redacted) ever
    sees it; the durable ``model_pulls`` row gets canonical, templated copy
    only, so an upstream error body can never smuggle arbitrary text (a
    token, a URL, an injection attempt) into durable/queryable state."""
    if raw_message.strip().lower().startswith(_MODEL_NOT_FOUND_PREFIX):
        return "model_not_found", "the model was not found by the model server"
    return "pull_failed", "the model server reported an error during the pull"


def register_model_pull_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    client_factory: Callable[[], httpx.Client] | None = None,
    artifact_cache_root: Path | None = None,
    artifact_manifest_lookup: Callable[[str], Any] | None = None,
) -> HandlerRegistration:
    """Register the ``model.pull`` handler.

    Deliberately NOT part of ``register_production_handlers`` (the one
    function every edition -- including an external managed composition --
    calls): a caller must opt in explicitly. Three known call sites do:
    ``server/workspace.py``'s ``Workspace`` (gated by the same flag that
    gates the provider-config routes), the plain local ``frisket worker``
    CLI command (unconditional), and ``cli.py``'s team/run-queue-backed
    worker branch, gated by ``FRISKET_ENABLE_MODEL_PULL`` via
    ``provider_config.team_model_pull_enabled``. The hosted worker
    (``register_hosted_handlers``, an external ``cloud`` edition) never
    calls this function at all and structurally cannot have ``model.pull``
    in its registry.

    ``client_factory`` is a test seam (mirrors ``provider_config.probe_provider``'s
    injectable ``client``): defaults to a real ``httpx.Client``; tests inject
    one built with ``transport=httpx.MockTransport(...)``.

    ``artifact_cache_root``/``artifact_manifest_lookup`` are test seams for
    the artifact backend (``opus-mt:``/``hf:`` refs): a temp model dir and an
    injected pinned-manifest lookup. Both default to the real
    ``FRISKET_MODEL_CACHE_DIR`` resolution and the in-tree manifest.
    """
    default_root = Path(workspace_root)
    engine = queue.engine  # type: ignore[attr-defined]
    build_client = client_factory or httpx.Client

    def handle(payload: dict, _context: JobHandlerContext) -> dict:
        # The try boundary covers EVERYTHING below -- payload parsing, the
        # store load, endpoint resolution, client construction -- not just the
        # stream itself. `pull_id`/`is_final_attempt` are tracked outside the try so
        # the except clause knows whether (and how conservatively) to record
        # a failure; `is_final_attempt` defaults to the conservative "yes"
        # until we actually learn the job's attempt budget, so an error
        # during setup (before we know better) still finalizes the row
        # rather than silently leaving it active forever.
        pull_id: int | None = None
        secrets: tuple[str, ...] = ()
        is_final_attempt = True
        try:
            pull_id = int(payload["pull_id"])
            job_id = int(payload["job_id"])
            root = Path(payload.get("workspace_root") or default_root)

            row = model_pull_store.get(engine, pull_id)
            if row is None:
                raise ModelPullTerminalError(f"model pull row {pull_id} not found")

            # Is THIS the job's last attempt? Determines whether a
            # retryable failure below finalizes the row or just records the
            # error and leaves it active for the requeued retry to resume.
            job = queue.get(job_id)
            is_final_attempt = job is None or job.attempts >= job.max_attempts

            # mark_running now reports whether the reactivation actually
            # applied. A refusal means this pull row is terminal in a way
            # THIS job_id may not reactivate -- e.g. a migration
            # reconciliation (queue_migrations
            # ._reconcile_multiple_active_pulls_per_workspace) already
            # cancelled this row's job outright, but if that job somehow
            # still runs (a stale claim, an admin retry of a cancelled job),
            # the row itself remains the authority and must fence it: abort
            # immediately, before any upstream server is ever contacted, as a
            # clean no-op rather than a zombie pull reopening a superseded
            # row.
            if not model_pull_store.mark_running(engine, pull_id, job_id=job_id):
                raise NonRetryableJobError(
                    f"row_terminal: pull row {pull_id} refused reactivation by "
                    f"job {job_id} (the row is already terminal under a "
                    "different owner or a permanent cancel)"
                )
            canonical_ref = row.model_ref

            # Route by ref kind. A qualified local-model ref uses the native
            # server flow below; an opus-mt:/hf: artifact ref routes to the HTTP
            # artifact backend, which OWNS the bytes and has no daemon endpoint
            # -- so the endpoint resolution + fingerprint check are skipped for
            # it entirely.
            from frisket.engine.jobs.artifact_ref import normalize_artifact_ref

            art = normalize_artifact_ref(canonical_ref)
            should_cancel = _make_cancel_checker(queue, job_id, engine, pull_id)
            if not art.is_ollama:
                from frisket.engine.jobs.artifact_pull import run_artifact_pull

                unpinned_ack = bool(payload.get("unpinned_acknowledged"))
                client = build_client()
                try:
                    lookup = artifact_manifest_lookup
                    if lookup is not None:
                        return run_artifact_pull(
                            client,
                            engine=engine,
                            pull_id=pull_id,
                            art=art,
                            should_cancel=should_cancel,
                            is_final_attempt=is_final_attempt,
                            cache_root=artifact_cache_root,
                            manifest_lookup=lookup,
                            unpinned_acknowledged=unpinned_ack,
                        )
                    return run_artifact_pull(
                        client,
                        engine=engine,
                        pull_id=pull_id,
                        art=art,
                        should_cancel=should_cancel,
                        is_final_attempt=is_final_attempt,
                        cache_root=artifact_cache_root,
                        unpinned_acknowledged=unpinned_ack,
                    )
                finally:
                    client.close()

            from frisket.server import provider_config

            endpoint_id = payload["endpoint_id"]
            if endpoint_id != art.endpoint_id:
                raise ModelPullTerminalError(
                    "queued model pull endpoint does not match its artifact reference"
                )
            if row.endpoint_id != endpoint_id:
                raise ModelPullTerminalError(
                    "queued model pull endpoint does not match its durable provenance"
                )
            if payload.get("org_id") is not None:
                env_config, _notes = provider_config.resolve_env_local_endpoint()
                configs = (env_config,) if env_config is not None else ()
            else:
                configs, _notes = provider_config.resolve_local_endpoints(root)
            config = next(
                (
                    endpoint
                    for endpoint in configs
                    if endpoint.endpoint_id == endpoint_id
                ),
                None,
            )
            if config is None:
                raise ModelPullTerminalError(
                    f"local model endpoint no longer exists: {endpoint_id}"
                )
            url = config.origin
            # GET /api/tags accepts EITHER credential; the pull
            # worker sends the PROVISIONING token for BOTH of its calls (the
            # idempotency tags re-list AND the pull itself) -- NEVER falling
            # back to the inference token when no provisioning token is
            # configured (tokenless stays byte-identical to before this stage).
            bearer = config.bearer_for_provisioning()
            headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
            # Every configured token VALUE (whichever ones exist) is scrubbed
            # from any upstream-sourced text before it becomes durable -- see
            # `_redact`'s docstring. Deliberately independent of which bearer
            # this handler actually SENT: if an operator has both an inference
            # and a provisioning token configured, neither may leak even though
            # only the provisioning one rides the wire here.
            secrets = tuple(
                t for t in (config.inference_token, config.provisioning_token) if t
            )

            # The enqueue-frozen origin is mandatory and every claim/retry
            # fails closed before egress if the endpoint identity moved.
            if not row.endpoint_origin or row.endpoint_origin != url:
                raise _fail(
                    engine,
                    pull_id,
                    error_code="endpoint_changed",
                    message=(
                        "the local model endpoint changed after this pull "
                        "was queued (queued against one server, claimed "
                        "while configured to point at another)"
                    ),
                    secrets=secrets,
                    terminal=True,
                    is_final_attempt=is_final_attempt,
                )
            client = build_client()
            try:
                return _run_pull(
                    client,
                    engine=engine,
                    pull_id=pull_id,
                    job_id=job_id,
                    url=url,
                    headers=headers,
                    canonical_ref=art.ollama_ref,
                    should_cancel=should_cancel,
                    secrets=secrets,
                    is_final_attempt=is_final_attempt,
                )
            finally:
                client.close()
        except (ModelPullTerminalError, NonRetryableJobError):
            # Already sanitized and already recorded by `_fail()` at its
            # specific raise site (known, enumerated failure classes) --
            # re-raise as-is.
            raise
        except Exception as exc:  # noqa: BLE001 -- total worker error boundary
            # ANY OTHER exception -- an unexpected transport error escaping
            # `client.stream()`, a parsing bug, anything not already funneled
            # through `_fail()` above -- must become an enumerated safe code
            # with a message built ONLY from the exception's CLASS NAME,
            # never `str(exc)`/`repr(exc)`/a raw traceback fragment. Those
            # could embed the bearer token, a provider response body, or a
            # URL — and the worker's own retry path
            # (`Worker._execute`) persists `traceback.format_exc()`
            # VERBATIM to the queue job's `error` column, so an unsanitized
            # message here would leak straight into durable, queryable
            # state. `from None` suppresses the original exception from the
            # chained traceback text that call site formats. No row to
            # record against if we failed before ever parsing `pull_id`.
            if pull_id is None:
                raise
            raise _fail(
                engine,
                pull_id,
                error_code="pull_worker_error",
                message=f"unexpected worker error ({type(exc).__name__})",
                secrets=secrets,
                terminal=False,
                is_final_attempt=is_final_attempt,
            ) from None

    return registry.add(
        MODEL_PULL_KIND,
        handle,
        origin=MODEL_PULL_HANDLER_ORIGIN,
    )


def _run_pull(
    client: httpx.Client,
    *,
    engine,
    pull_id: int,
    job_id: int,
    url: str,
    headers: dict[str, str],
    canonical_ref: str,
    should_cancel: Callable[[], bool],
    secrets: Iterable[str] = (),
    is_final_attempt: bool,
) -> dict:
    """The actual pull flow (idempotency check, stream, re-verify) -- split
    out from ``handle()`` so the worker error boundary above wraps it
    cleanly in one try/except/finally without duplicating the client
    lifecycle.

    Terminal vs. retryable failure classes: capability/auth failures and the
    local server not
    speaking the expected protocol are TERMINAL (``terminal=True`` --
    retrying with the SAME misconfiguration cannot succeed); an unreachable
    server (a connect error/timeout -- ``status is None``) and an
    unclassified/dropped stream are RETRYABLE (``terminal=False`` -- the
    server or network may recover).
    """
    status, models = _fetch_tags(client, url, headers=headers)
    if models is None:
        if status in (401, 403):
            raise _fail(
                engine,
                pull_id,
                error_code="local_server_unauthorized",
                message=f"local server rejected /api/tags (HTTP {status})",
                secrets=secrets,
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        if status is None:
            raise _fail(
                engine,
                pull_id,
                error_code="local_server_unreachable",
                message=f"no local server answered at {url}",
                secrets=secrets,
                terminal=False,
                is_final_attempt=is_final_attempt,
            )
        raise _fail(
            engine,
            pull_id,
            error_code="pull_unsupported",
            message=(
                "the local server does not provide a native Ollama "
                f"model listing (HTTP {status})"
            ),
            secrets=secrets,
            terminal=True,
            is_final_attempt=is_final_attempt,
        )

    existing = _installed_entry(models, canonical_ref)
    if existing is not None:
        provenance = _require_provenance(existing)
        if provenance is None:
            raise _fail(
                engine,
                pull_id,
                error_code="reconciliation_failed",
                message=(
                    "the model appears already installed but the model "
                    "server's listing did not include a usable digest and "
                    "size for it"
                ),
                secrets=secrets,
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        digest, size = provenance
        model_pull_store.mark_done(
            engine, pull_id, resolved_digest=digest, resolved_size=size
        )
        return {"status": "done", "already_installed": True}

    if should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}

    last_write = 0.0
    last_phase: str | None = None
    with client.stream(
        "POST",
        f"{url}/api/pull",
        json={"model": canonical_ref},
        headers=headers,
        timeout=httpx.Timeout(
            _PULL_READ_TIMEOUT_SECONDS, connect=_PULL_CONNECT_TIMEOUT_SECONDS
        ),
    ) as response:
        if response.status_code in (401, 403):
            response.close()
            raise _fail(
                engine,
                pull_id,
                error_code="local_server_unauthorized",
                message=f"local server rejected /api/pull (HTTP {response.status_code})",
                secrets=secrets,
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        if response.status_code in (404, 405):
            response.close()
            raise _fail(
                engine,
                pull_id,
                error_code="pull_unsupported",
                message=(
                    "the local server lists models but does not accept "
                    f"downloads (HTTP {response.status_code} on /api/pull)"
                ),
                secrets=secrets,
                terminal=True,
                is_final_attempt=is_final_attempt,
            )
        if response.status_code != 200:
            response.close()
            raise _fail(
                engine,
                pull_id,
                error_code="pull_upstream_http_error",
                message=f"/api/pull returned HTTP {response.status_code}",
                secrets=secrets,
                terminal=False,
                is_final_attempt=is_final_attempt,
            )

        terminal_phase: str | None = None
        for event in iter_pull_events(response.iter_lines()):
            if should_cancel():
                model_pull_store.mark_cancelled(engine, pull_id)
                response.close()
                return {"status": "cancelled"}

            now_m = time.monotonic()
            phase_changed = event.phase != last_phase
            if phase_changed or (now_m - last_write) >= (
                _PROGRESS_WRITE_MIN_INTERVAL_SECONDS
            ):
                model_pull_store.update_progress(
                    engine,
                    pull_id,
                    phase=event.phase,
                    total_bytes=event.total_bytes,
                    completed_bytes=event.completed_bytes,
                )
                last_write = now_m
                last_phase = event.phase

            if event.phase == "error":
                # Raw upstream bodies must never reach logs, full stop --
                # `_redact` only scrubs
                # CONFIGURED token VALUES, so it is not a substitute for
                # never logging the raw line (an unconfigured secret, a
                # path, or arbitrary injected text would still ride
                # straight through it). The raw text is therefore never
                # logged, redacted or not, and never persisted -- only the
                # classification, correlation id, and the line's LENGTH
                # (never its content) are recorded for support/ops; only a
                # stable code + canonical, correlation-id-bearing copy
                # reaches the durable row. A structured upstream error is a
                # TERMINAL class: retrying the identical ref
                # against the identical error will not change the outcome.
                raw = event.error_message or ""
                code, canonical = _classify_upstream_error(raw)
                LOG.warning(
                    "upstream pull error (%s), %d chars, correlation %s",
                    code,
                    len(raw),
                    job_id,
                    extra={
                        "event": "model_pull_upstream_error",
                        "pull_id": pull_id,
                        "job_id": job_id,
                        "classification": code,
                        "upstream_message_length": len(raw),
                    },
                )
                if code != "model_not_found":
                    canonical = f"{canonical}; correlation id {job_id}"
                raise _fail(
                    engine,
                    pull_id,
                    error_code=code,
                    message=canonical,
                    secrets=secrets,
                    terminal=True,
                    is_final_attempt=is_final_attempt,
                )
            if event.phase == "done":
                terminal_phase = "done"
                break

        if terminal_phase != "done":
            raise _fail(
                engine,
                pull_id,
                error_code="pull_stream_incomplete",
                message="the pull stream ended without a success or error line",
                secrets=secrets,
                terminal=False,
                is_final_attempt=is_final_attempt,
            )

    # Reconciliation. A success line alone is not "done" -- the post-pull
    # re-list must actually FIND the canonical ref, and digest/size come from THAT
    # listing, never assumed. A failed re-list, an absent model, or one
    # with an unusable digest/size (resolved provenance is required on
    # every completed pull) is a terminal failure, not a
    # silent "done".
    status_after, models_after = _fetch_tags(client, url, headers=headers)
    resolved = (
        _installed_entry(models_after, canonical_ref)
        if models_after is not None
        else None
    )
    provenance = _require_provenance(resolved) if resolved is not None else None
    if provenance is None:
        raise _fail(
            engine,
            pull_id,
            error_code="reconciliation_failed",
            message=(
                "the pull stream reported success but the model could not "
                "be confirmed installed (with a usable digest and size) on "
                "a post-pull listing"
            ),
            secrets=secrets,
            terminal=True,
            is_final_attempt=is_final_attempt,
        )
    digest, size = provenance
    model_pull_store.mark_done(
        engine, pull_id, resolved_digest=digest, resolved_size=size
    )
    return {"status": "done"}
