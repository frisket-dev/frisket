"""Document conversion using local workers or an admitted remote route.

Remote calls require admission, including previews. Datalab acceptance is
recorded before polling so cancellation cannot erase a potentially paid job.
Actual document reads travel with the returned-row checkpoint; publication
records quote-free source links for nonblank text outputs in the same savepoint.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from frisket.actions.document_types import ConvertedDocument
from frisket.actions.types import ColumnRef, Row, RowError
from frisket.contracts.actions.schemas._engines import (
    TO_MARKDOWN_ENGINE_TABLE,
    engine_ids,
    execution_alias_map,
)
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.provider import enforce_pdf_page_limit
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
from frisket.execution.targets import CAPABILITY_TO_MARKDOWN
from frisket.ops._sidecar import sidecar_post
from frisket.ops.base import RecipeInvocationHalt

LIGHT_ENGINE = "markitdown"
TRAFILATURA_HTML_ENGINE = "trafilatura_html"
# Roster/alias/tier truth lives in TO_MARKDOWN_ENGINE_TABLE.
SIDECAR_ENGINES = frozenset(engine_ids(TO_MARKDOWN_ENGINE_TABLE, tier="sidecar"))
DATALAB_ENGINE = "datalab"
ENGINE_ALIASES = execution_alias_map(TO_MARKDOWN_ENGINE_TABLE)

# The route must carry the SAME capability the retired converter routed on
# (CAPABILITY_TO_MARKDOWN == "document.convert"), and the transport each engine
# is pinned to (execution/definitions.py). A routed call whose snapshot
# disagrees refuses before the document leaves the machine (mirrors
# AdmittedGeocoder).
_EXPECTED_TRANSPORT = {
    LIGHT_ENGINE: "local",
    TRAFILATURA_HTML_ENGINE: "local",
    "docling": "sidecar.convert",
    "chandra": "sidecar.convert",
    DATALAB_ENGINE: "datalab.convert",
}

# Datalab's /convert accepts PDF/Word/PowerPoint/PNG/JPG/WebP — NOT html/text.
# Pre-validated here BEFORE any network call so a local input-type mismatch
# fails loudly instead of surfacing a confusing remote 422.
_DATALAB_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Rough mime→extension hints for blob inputs without filenames. markitdown also
# sniffs bytes (magika), so these only help ambiguous office zips.
_MIME_EXT = {
    **{mime: suffix for suffix, mime in _DATALAB_MIME.items()},
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

# Runs in a sandboxed child process (no provider keys, wall-clock timeout on
# every platform, POSIX-only CPU/memory rlimits) like the ocr/transcribe
# workers. Result JSON goes to a file — converter libraries (pdfminer et al.)
# chatter on stdout/stderr.
CONVERT_WORKER = """
import json, logging, sys, warnings

def main():
    payload = json.load(sys.stdin)
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")
    try:
        from markitdown import MarkItDown
    except ImportError:
        _write(payload, {"error": "markitdown not installed in this "
                         "environment even though it is part of the base "
                         "Frisket install; repair or reinstall Frisket"})
        return
    md = MarkItDown(enable_plugins=False)
    try:
        result = md.convert(payload["path"])
    except Exception as e:  # markitdown raises per-format exceptions
        _write(payload, {"error": f"{type(e).__name__}: {e}"})
        return
    text = getattr(result, "markdown", None)
    if text is None:
        text = getattr(result, "text_content", "")
    _write(payload, {"markdown": text or ""})

def _write(payload, obj):
    with open(payload["out"], "w") as f:
        json.dump(obj, f)

main()
"""


def _engine_tier(engine: str) -> str:
    if engine in SIDECAR_ENGINES:
        return "sidecar"
    if engine == DATALAB_ENGINE:
        return "hosted"
    return "local"


def _looks_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    return (
        head.startswith(("<!doctype", "<html"))
        or "<body" in head
        or ("<" in head and "</" in text)
    )


def _is_media_typed(column_type: str) -> bool:
    """True iff the supplying column renders as media (file/image/audio/video
    or a plugin type presented as media) — the gate on the path-probe below.
    A bare text cell naming a real file is content to convert, not a file
    reference."""
    from frisket.authoring.column_types import get_column_type

    spec = get_column_type(column_type or "")
    return bool(spec and spec.presentation.get("renderer") in ("media", "image"))


class AdmittedDocumentConverter:
    """One conversion per admitted row; the engine + route are pinned.

    ``engine`` is the resolved, canonicalized engine id the action authored
    (``_capability_engine`` off the ``EngineRef`` param).  It is the dispatch
    engine for an UNROUTED call; a ROUTED call reads the authoritative engine
    off the admitted route and validates the route's capability/transport,
    exactly as ``AdmittedGeocoder`` does.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        engine: str | None = None,
        cancelled: Any = None,
    ) -> None:
        self._ctx = ctx
        self._engine = ENGINE_ALIASES.get(engine, engine) if engine else None
        self._cancelled = cancelled
        self._closed = False
        self._called_rows: set[int] = set()
        # Per-row provider accounting (datalab) surfaced to the host writer via
        # the same channel geocode/ocr use; datalab's accepted-job fact is ALSO
        # persisted immediately (persist_datalab_accepted_accounting) so it is
        # durable independent of this envelope.
        self.accounting_by_row: dict[int, dict[str, Any]] = {}
        # Per-row PRIVATE read-facts, keyed by row_id. The host's existing
        # accounting carriage (map_rows_action._with_accounting) copies these
        # onto the PreparedRowPublication cell as ``row_file_calls`` — exactly
        # the file_fetch/row_media pattern — so they are checkpointed and
        # REPLAYED from the durable batch on returned-row recovery (a fresh
        # worker never re-runs convert()). Recorded for EVERY engine that ran
        # (not just datalab) so local/sidecar provenance — which mints no
        # external model call and would otherwise be erased by the shared
        # no-call provider_use fallback — survives. One fact per row:
        #   {
        #     "kind": "document_convert_read",  # row_file_call discriminator
        #     "engine": str,                    # canonical engine id that ran
        #     "tier": "local"|"sidecar"|"hosted",
        #     "external": bool,                 # True only for datalab (egress)
        #     "ocr_used": list[bool],           # per-page (sidecar); [] else
        #     "produced_markdown": bool,        # False for a blank output
        #     "document_read": {                # the ACTUAL read converted
        #        "kind": "blob"|"text"|"path",
        #        "sheet_id": int, "row_id": int,
        #        "column_id": int, "source_column": str,
        #        # kind=="blob" only:
        #        "blob_hash": str, "mime": str|None, "filename": str|None,
        #     },
        #   }
        # A blank INPUT cell does no work and records NO fact at all.
        self.calls_by_row: dict[int, list[dict[str, Any]]] = {}

    def bind_row(
        self, row: Row, *, sheet_id: int, row_id: int, sources: Any, ctx: Any
    ) -> _BoundDocumentConverter:
        self._check_active()
        if type(sheet_id) is not int or type(row_id) is not int:
            raise RowError(
                "invalid_input_ref", "Document converter requires its admitted row."
            )
        import copy

        return _BoundDocumentConverter(
            self, row, sheet_id, row_id, copy.deepcopy(dict(sources or {})), ctx
        )

    async def aclose(self) -> None:
        self._closed = True

    def _check_active(self) -> None:
        if self._closed:
            raise RuntimeError("document converter is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError


class _BoundDocumentConverter:
    def __init__(self, owner, row, sheet_id, row_id, sources, ctx) -> None:
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources
        self._ctx = ctx

    async def convert(self, row: Row, source: ColumnRef[Any]) -> ConvertedDocument:
        owner = self._owner
        owner._check_active()
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref", "Document converter requires its admitted source."
            )
        if self._row_id in owner._called_rows:
            raise RuntimeError("document converter permits one conversion per row")
        owner._called_rows.add(self._row_id)
        doc, column_type, column_id = self._source(source)
        # A blank input cell does no work and records no document link.
        if doc is None or (isinstance(doc, str) and not doc.strip()) or doc == {}:
            return ConvertedDocument(markdown="")

        engine = self._selected_engine()
        media_typed = _is_media_typed(column_type)
        ocr_used: list[Any] = []
        cost_meta: dict[str, Any] | None = None
        with tempfile.TemporaryDirectory(prefix="frisket-convert-") as td:
            scratch = Path(td)
            path, read = self._resolve_input(doc, scratch, media_typed=media_typed)
            self._enforce_pdf_limit(path, doc)
            if engine == LIGHT_ENGINE:
                markdown = await self._convert_markitdown(path, scratch)
            elif engine == TRAFILATURA_HTML_ENGINE:
                markdown = self._convert_trafilatura_html(path)
            elif engine in SIDECAR_ENGINES:
                markdown, ocr_used = await self._convert_sidecar(engine, path)
            elif engine == DATALAB_ENGINE:
                markdown, cost_meta = await self._convert_datalab(path)
            else:
                raise RowError(
                    "invalid_engine",
                    f"unknown convert engine {engine!r} (use "
                    f"{LIGHT_ENGINE!r}, {TRAFILATURA_HTML_ENGINE!r}, "
                    f"{DATALAB_ENGINE!r}, or one of {sorted(SIDECAR_ENGINES)!r})",
                )
        if cost_meta is not None:
            self._store_datalab_accounting(cost_meta)
        markdown = markdown.strip()
        # Record the ACTUAL read for EVERY engine that ran, naming the engine so
        # local/sidecar provenance survives; the fact rides the publication cell
        # (row_file_calls) into the checkpoint. The source→output link is gated
        # on non-blank markdown (produced_markdown) downstream.
        read["sheet_id"] = self._sheet_id
        read["row_id"] = self._row_id
        read["column_id"] = column_id
        read["source_column"] = source.name
        owner.calls_by_row.setdefault(self._row_id, []).append(
            {
                "kind": "document_convert_read",
                "engine": engine,
                "tier": _engine_tier(engine),
                "external": engine == DATALAB_ENGINE,
                "ocr_used": list(ocr_used),
                "produced_markdown": bool(markdown),
                "document_read": read,
            }
        )
        return ConvertedDocument(markdown=markdown, ocr_used=list(ocr_used))

    # -- source resolution -------------------------------------------------

    def _source(self, source: ColumnRef[Any]) -> tuple[Any, str, int]:
        """Validate the ACTUAL source column against its admitted cell and
        return ``(value, column_type, column_id)``.  Selection is by
        ``source.name`` — the actual (possibly renamed) column argument — not
        any Params field name."""
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in self._row.values
            or canonical_json_hash(source.read(self._row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError(
                "stale_input", "Document source differs from its admitted cell."
            )
        column = self._ctx.project.db.execute(
            "SELECT sheet_id,type,hidden FROM columns WHERE id=?",
            (captured["column_id"],),
        ).fetchone()
        if (
            column is None
            or column["sheet_id"] != self._sheet_id
            or column["hidden"]
            or column["type"] not in ("file", "text")
            or column["type"] != captured.get("column_type")
        ):
            raise RowError(
                "invalid_input_ref",
                "Document source column is unavailable or incompatible.",
            )
        return captured["value"], column["type"], int(captured["column_id"])

    def _resolve_input(
        self, doc: Any, scratch: Path, *, media_typed: bool
    ) -> tuple[Path, dict[str, Any]]:
        """Cell values: an ingested blob ({blob, mime}), a trusted local path
        (media-typed columns only), or raw document content held inline in a
        text cell (scraped HTML) written to a scratch file to be sniffed."""
        ctx = self._ctx
        if isinstance(doc, dict) and doc.get("blob"):
            ext = _MIME_EXT.get((doc.get("mime") or "").split(";")[0].strip())
            target = scratch / f"doc{ext or '.bin'}"
            try:
                with ctx.project.materialize_blob(str(doc["blob"])) as path:
                    target.write_bytes(Path(path).read_bytes())
            except BlobNotFoundError:
                raise RowError(
                    "missing_blob", "Document source blob bytes are missing."
                ) from None
            return target, {
                "kind": "blob",
                "blob_hash": str(doc["blob"]),
                "mime": doc.get("mime"),
                "filename": doc.get("filename"),
            }
        if isinstance(doc, str):
            if doc.startswith(("http://", "https://")):
                raise RowError(
                    "invalid_input_ref",
                    "to_markdown expects a local file, ingested blob, or inline "
                    "document content; run the 'download media' step first for "
                    "URL columns",
                )
            from frisket.ops.base import resolve_media_path

            # Path-probe, gated on media-typed input columns: a text cell
            # holding "/etc/passwd" is content to convert, not a file reference.
            if media_typed and "\n" not in doc and len(doc) < 4096:
                try:
                    candidate = resolve_media_path(doc, ctx, op="to_markdown")
                    if Path(candidate).exists():
                        return Path(candidate), {"kind": "path"}
                except ValueError:
                    pass  # untrusted deploy: fall through to inline content
            ext = ".html" if _looks_html(doc) else ".txt"
            inline = scratch / f"doc{ext}"
            inline.write_text(doc, encoding="utf-8")
            return inline, {"kind": "text"}
        raise RowError(
            "invalid_input_ref", f"to_markdown can't read a {type(doc).__name__} cell"
        )

    def _enforce_pdf_limit(self, path: Path, doc: Any) -> None:
        limits = self._ctx.execution_limits
        if limits is None or limits.max_pdf_pages is None:
            return
        try:
            with path.open("rb") as stream:
                is_pdf = stream.read(5) == b"%PDF-"
        except OSError:
            is_pdf = False
        if not is_pdf:
            return
        digest = doc.get("blob") if isinstance(doc, dict) else None
        probe = (
            MediaBlobStore(self._ctx.project).probe_metadata(str(digest))
            if digest and self._ctx.project is not None
            else {}
        )
        enforce_pdf_page_limit(limits.max_pdf_pages, probe.get("pages"))

    # -- engine + route selection -----------------------------------------

    def _selected_engine(self) -> str:
        admission = routed_admission_in_scope(self._ctx.extras)
        if admission is not None:
            engine = admission.route.engine
            snapshot = admission.route.target_snapshot
            expected = _EXPECTED_TRANSPORT.get(engine)
            if (
                snapshot.get("capability") != CAPABILITY_TO_MARKDOWN
                or expected is None
                or snapshot.get("transport") != expected
            ):
                raise RecipeInvocationHalt(
                    "promise_violation",
                    "Document converter requires its admitted convert transport",
                )
            return engine
        if self._owner._engine is None:
            raise RuntimeError("document converter requires an admitted engine")
        if self._owner._engine not in {LIGHT_ENGINE, TRAFILATURA_HTML_ENGINE}:
            raise RecipeInvocationHalt(
                "promise_violation", "Document provider requires its admitted route"
            )
        return self._owner._engine

    def _route_connection(self) -> Any:
        """Use only the gateway connection admitted for this invocation."""
        admission = routed_admission_in_scope(self._ctx.extras)
        if admission is not None:
            return admission.binding.connection
        raise RecipeInvocationHalt(
            "promise_violation", "Document gateway requires its admitted route"
        )

    # -- engines -----------------------------------------------------------

    async def _convert_markitdown(self, path: Path, scratch: Path) -> str:
        if not path.exists():
            raise RowError("invalid_input_ref", f"convert input not found: {path}")
        out = scratch / "convert-result.json"
        result = await run_sandboxed(
            [sys.executable, "-c", CONVERT_WORKER],
            policy=SandboxPolicy(
                cpu_seconds=600,
                wall_seconds=900,
                memory_mb=4096,
                env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
                # The document is attacker-supplied and markitdown parses it
                # with pdfminer/magika/ONNX Runtime; read one document + the
                # static /etc/mime.types, write one result file.
                confine=fence.Confinement(
                    op="to_markdown (markitdown)",
                    read=(str(path), "/etc/mime.types"),
                    write=(str(scratch),),
                ),
            ),
            stdin_data=json.dumps({"path": str(path), "out": str(out)}).encode(),
            should_cancel=lambda: (
                self._owner._closed
                or bool(self._owner._cancelled and self._owner._cancelled())
            ),
        )
        if getattr(result, "cancelled", False):
            raise asyncio.CancelledError
        if not result.ok or not out.exists():
            raise RowError(
                "to_markdown_run_failed",
                f"convert worker failed: {(result.stderr or result.stdout)[:300]}",
            )
        data = json.loads(out.read_text())
        if data.get("error"):
            raise RowError("to_markdown_run_failed", str(data["error"]))
        return data["markdown"]

    def _convert_trafilatura_html(self, path: Path) -> str:
        if not path.exists():
            raise RowError("invalid_input_ref", f"convert input not found: {path}")
        from frisket.ops.capture.url import extract_markdown

        html = path.read_bytes().decode("utf-8", errors="replace")
        markdown, _warnings = extract_markdown(html)
        return markdown

    async def _convert_sidecar(self, engine: str, path: Path) -> tuple[str, list]:
        """POST /to-markdown on the frisket-models sidecar: multipart blob
        bytes (the sidecar never reads our disk), bearer-token auth; returns
        (markdown, per-page ocr_used).  The connection is the ADMITTED binding
        for a routed call, never a fresh ephemeral dereference."""
        if not path.exists():
            raise RowError("invalid_input_ref", f"convert input not found: {path}")
        files = [("files", (path.name, path.read_bytes(), "application/octet-stream"))]
        body = await sidecar_post(
            self._ctx,
            "/to-markdown",
            files=files,
            data={"engine": engine},
            op="convert",
            light_engine=LIGHT_ENGINE,
            connection=self._route_connection(),
        )
        docs = body.get("documents") or body.get("results")
        doc = docs[0] if docs else body
        return doc.get("markdown", ""), doc.get("ocr_used") or []

    async def _convert_datalab(self, path: Path) -> tuple[str, dict[str, Any]]:
        """Datalab's hosted Marker /convert (pay-per-call).  Credential-gated
        (DATALAB_API_KEY), whole document uploaded in one call (Datalab
        paginates PDFs server-side).  An accepted-job cost fact is persisted
        immediately and bound to the route; the SAME call identity is
        terminally enriched on completion (metered vs unknown-cost)."""
        if not path.exists():
            raise RowError("invalid_input_ref", f"convert input not found: {path}")
        ctx = self._ctx
        from frisket.credentials import resolve_credential_for_use
        from frisket.ai.external_pricing import (
            DATALAB_CONVERT_PAGE,
            external_unit_price_usd,
        )
        from frisket.ops.integrations.datalab import DatalabEngineError, datalab_convert

        credential = resolve_credential_for_use(
            ctx.project, "DATALAB_API_KEY", context=ctx.credential_use_context
        )
        if credential is None:
            raise DatalabEngineError(
                code="auth",
                message="DATALAB_API_KEY is not configured (Settings → Secrets).",
            )
        # The §3.5 pre-effect credential-USE fence: the class the consent named
        # vs the class this dispatch actually selected, checked BEFORE the
        # document leaves the machine.  A credential the user did not consent to
        # is a direct authorization refusal — the run halts before effect.
        admission = routed_admission_in_scope(ctx.extras)
        if admission is not None:
            self._halt_unless_consented_credential(
                admission.route,
                credential.source,
                "the hosted Datalab convert call",
                selected_owner=credential.owner,
            )
        suffix = path.suffix.lower()
        mime = _DATALAB_MIME.get(suffix)
        if mime is None:
            supported = ", ".join(sorted(_DATALAB_MIME))
            raise DatalabEngineError(
                code="bad_request",
                message=(
                    f"Datalab conversion does not support "
                    f"'{suffix or '(no extension)'}' input (supported: "
                    f"{supported}). Use 'markitdown' for HTML/text/office "
                    "documents, or convert this file to a supported format first."
                ),
            )
        should_cancel = (ctx.extras or {}).get("cancelled")
        if should_cancel is not None and should_cancel():
            raise DatalabEngineError(
                code="cancelled",
                message="Datalab conversion stopped: the run was cancelled.",
            )
        accepted_accounting: dict[str, Any] | None = None

        def persist_accepted(accounting: dict[str, Any]) -> None:
            nonlocal accepted_accounting
            from frisket.sdk.ops._datalab_accounting import (
                persist_datalab_accepted_accounting,
            )

            accepted_accounting = accounting
            persist_datalab_accepted_accounting(ctx, accounting)

        body, cost_usd = await datalab_convert(
            ctx.http,
            credential.value,
            path.read_bytes(),
            path.name,
            mime,
            should_cancel=should_cancel,
            credential_source=credential.source,
            on_accepted=persist_accepted,
        )
        if accepted_accounting is None:
            raise RuntimeError("Datalab returned without its accepted-job accounting")
        meta = accepted_accounting
        provider_reported_cost = cost_usd
        page_count = body.get("page_count")
        pages = page_count if isinstance(page_count, int) and page_count > 0 else 1
        if cost_usd is None:
            # One /convert call can cover a multi-page document — scale the
            # per-page estimate by the response's own page_count.
            unit_price = external_unit_price_usd(DATALAB_CONVERT_PAGE)
            meta["cost"] = unit_price * pages if unit_price is not None else None
            if meta["cost"] is not None:
                meta["cost_source"] = "estimated"
        else:
            meta["cost"] = cost_usd
        [fact] = meta["model_calls"]
        fact["provider_reported_cost_usd"] = provider_reported_cost
        fact["provider_cost_usd"] = meta["cost"]
        fact["cost_source"] = (
            "provider_reported"
            if provider_reported_cost is not None
            else str(meta.get("cost_source") or "unknown")
        )
        fact["units"] = {"pages": pages, "requests": 1}
        return str(body.get("markdown") or ""), meta

    def _halt_unless_consented_credential(
        self, route: Any, selected_source: Any, effect: str, *, selected_owner: Any
    ) -> None:
        from frisket.execution.credential_use import (
            CredentialUseRefusal,
            require_consented_credential,
        )

        try:
            require_consented_credential(
                cost_posture=route.cost_posture,
                selected_source=selected_source,
                selected_owner=selected_owner,
                context=self._ctx.credential_use_context,
                effect=effect,
            )
        except CredentialUseRefusal as exc:
            raise RecipeInvocationHalt(
                "promise_violation",
                f"{exc}; select an authorized credential before resuming",
            ) from exc

    def _store_datalab_accounting(self, meta: dict[str, Any]) -> None:
        """Bind the terminal fact to the pinned route (§6 epoch invariant) and
        surface it to the host writer.  Re-binding the enriched terminal fact
        is what makes its durable cost_source agree with what the route
        requires, rather than the provider-vocabulary string the completion
        callback wrote with no route in scope."""
        admission = routed_admission_in_scope(self._ctx.extras)
        if admission is not None:
            calls = meta.get("model_calls")
            if isinstance(calls, list) and calls:
                calls = [bind_fact_to_route(admission.route, call) for call in calls]
                meta["model_calls"] = calls
                for call in calls:
                    if not call.get(ROUTE_OBSERVATION_KEY):
                        raise RuntimeError(
                            "to_markdown fact built under a route carries no "
                            "binding observation required by the route-epoch "
                            "invariant"
                        )
        self._owner.accounting_by_row[self._row_id] = meta


READ_FACT_KIND = "document_convert_read"


def document_convert_reads(batch: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The per-row document-read fact recovered from the DURABLE batch (the
    checkpointed ``row_file_calls`` carriage), keyed by row_id. Recovery-safe:
    a fresh worker that never re-ran ``convert()`` still finds these here.
    Root's receipt/provider-use projection consumes the same source."""
    reads: dict[int, dict[str, Any]] = {}
    for item in batch:
        for call in item.get("row_file_calls") or []:
            if isinstance(call, dict) and call.get("kind") == READ_FACT_KIND:
                reads[int(item["row_id"])] = call
    return reads


def write_document_convert_evidence(
    project,
    spec,
    *,
    batch,
    run_id,
    op_id,
    output_columns,
    writer_attempt_id,
    claim_token,
    **kwargs,
):
    """Retain actual reads and quote-free dependencies at the result savepoint."""
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.evidence import (
        record_evidence_link,
        record_source_artifact,
        record_source_span,
    )

    store = ReceiptStore(project)
    reads = document_convert_reads(batch)
    for row_id, read in reads.items():
        store._record_writer_evidence(
            {**read, "row_id": row_id},
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )
    for item in batch:
        value = item.get("value")
        if (
            item.get("error") is not None
            or not isinstance(value, str)
            or not value.strip()
        ):
            continue
        row_id = int(item["row_id"])
        read = reads.get(row_id)
        if read is None:
            continue
        column_id = int(item["column_id"])
        input_ref = read["document_read"]
        engine = read["engine"]
        if (
            project.db.execute(
                "SELECT 1 FROM evidence_links WHERE run_id=? AND row_id=? AND column_id=? "
                "AND link_role='source_provenance' LIMIT 1",
                (run_id, row_id, column_id),
            ).fetchone()
            is not None
        ):
            continue
        blob = input_ref.get("kind") == "blob"
        artifact = record_source_artifact(
            project,
            artifact_kind="file" if blob else "text",
            media_type=str(input_ref.get("mime") or "application/octet-stream")
            if blob
            else "text/plain",
            blob_hash=input_ref.get("blob_hash") if blob else None,
            filename=input_ref.get("filename") if blob else None,
            source_sheet_id=int(input_ref["sheet_id"]),
            source_row_id=row_id,
            source_column_id=int(input_ref["column_id"]),
            metadata={"engine": engine},
        )
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="whole",
            metadata={
                "granularity": "whole_document",
                "evidence_semantics": "source_provenance",
            },
        )
        receipt_id = project.db.execute(
            "SELECT DISTINCT receipt_id FROM output_column_claims "
            "WHERE run_id=? AND claim_token=? AND status='active'",
            (run_id, claim_token),
        ).fetchone()[0]
        link = record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref={
                "kind": "run_result",
                "op_id": op_id,
                "row_id": row_id,
                "column_id": column_id,
                "run_id": run_id,
            },
            spans=[{"span_id": span["id"], "rank": 0}],
            sheet_id=int(input_ref["sheet_id"]),
            row_id=row_id,
            column_id=column_id,
            run_id=run_id,
            op_id=op_id,
            receipt_id=receipt_id,
            link_role="source_provenance",
            producer={"action_kind": spec["action_kind"], "engine": engine},
            metadata={
                "provenance_for": "row_execution",
                "grounding_granularity": "whole_document",
            },
        )
        store._record_writer_evidence(
            {
                "kind": "document_source_provenance",
                "row_id": row_id,
                "column_id": column_id,
                "artifact_id": artifact["id"],
                "artifact_stable_id": artifact["stable_id"],
                "evidence_link_id": link["id"],
                "stable_id": link["stable_id"],
            },
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )
