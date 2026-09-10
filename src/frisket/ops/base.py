"""Recipe contract: a recipe declares typed inputs, builds a
prompt + output schema per row, and the runner executes it as a parallel map.
Multi-field = one call, always.

The spec is declarative JSON — the durable plan format:
{
  "action_kind": "map.classify", "action_version": "1",
  "model": "gemini/gemini-3.5-flash",
  "sheet_id": 1,
  "input_columns": ["transcript"],
  "context": "Each row is a radio transcript from ...",
  "fields": [{"name": "dismantle_agencies", "type": "score",
              "description": "0=not mentioned, 10=central topic"}],
  "include_justification": true,
  "include_confidence": false
}
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from frisket.ai.model_defaults import DEFAULT_MAX_OUTPUT_TOKENS
from frisket.ai.message_content import render_input_block as render_input_block
from frisket.redaction import redact_text
from frisket.authoring.templates import column_template_names

if TYPE_CHECKING:
    from frisket.execution.credential_use import CredentialUseContext
    from frisket.execution.provider import ExecutionLimits


# What an ABSENT estimate means for a recipe (the cost-declaration axis).
#
# Every recipe declares one of these three cost classes:
#
# - "free"        — no money moves. An absent estimate IS $0.00, honestly, and
#                   the run proceeds with no dialog (regex_extract, clean_dates,
#                   local spaCy/GLiNER ner, map.python, ...).
# - "metered"     — a priced meter exists (model tokens, per-page, per-call), so
#                   an absent estimate is a MISSING number, never a free run.
# - "unpriceable" — the meter itself is unknowable before the run (an arbitrary
#                   URL has no published price: api_call, fetch_url,
#                   ytdlp_download). Never free, never guessable — routed through
#                   an explicit UNKNOWN-cost consent instead.
#
# "metered" and "unpriceable" differ in remedy, not in gate: both estimate
# ``cost: None`` when no number was produced, which the cost gate treats exactly
# like an over-budget estimate. The distinction is what the closure test checks
# each recipe's catalog ``CostPolicy`` against.
CostClass = Literal["free", "metered", "unpriceable"]


# Engine-neutral halt vocabulary: this module is shared across every
# local-model engine, so no engine name appears in a code — the engine
# specifics belong in each raise site's free-text detail.
# ``promise_violation`` is reserved for direct pre-effect authorization
# refusals: a non-satisfied fact in the authorized execution promise set,
# changed or malformed authorized work scope, or a selected credential outside
# the consented class. The invocation halts before effect and is surfaced as
# resumable through ``run.backfill``.
RECIPE_INVOCATION_HALT_CODES = frozenset(
    {
        "local_engine_busy",
        "local_artifact_unavailable",
        "local_session_failed",
        "promise_violation",
        "external_effect_reconciliation_required",
    }
)

_RECIPE_INVOCATION_HALT_DEFAULT_DETAILS = {
    "local_engine_busy": (
        "Another local transcription is still finishing; retry after it closes."
    ),
    "local_artifact_unavailable": (
        "The local engine's model artifacts are unavailable; retry after they are ready."
    ),
    "local_session_failed": (
        "The local model session stopped; resume the remaining rows."
    ),
    "promise_violation": (
        "This run's live execution terms, selected rows, source revisions, or "
        "credential no longer match its direct authorization; review and confirm "
        "the current terms before resuming."
    ),
    "external_effect_reconciliation_required": (
        "A prior row may already have reached its external provider, but no "
        "returned response was durably recorded. Reconcile that effect before "
        "retrying; Frisket will not buy the row again automatically."
    ),
}


class RecipeInvocationHalt(Exception):
    """A typed, resumable failure of one recipe invocation, not one row."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def normalize_recipe_invocation_halt(code: Any, detail: Any) -> tuple[str, str]:
    """Return the bounded public halt identity a runner may persist.

    Recipe adapters should raise only parent-owned codes.  This is also the
    fail-closed boundary for a buggy adapter: an unknown code is never copied
    into durable state and instead becomes the v1 session-failure category.
    This module is shared across every local-model engine, so the fail-closed
    default must stay engine-neutral rather than naming any one engine.
    """

    public_code = (
        str(code)
        if isinstance(code, str) and code in RECIPE_INVOCATION_HALT_CODES
        else None
    )
    if public_code is None:
        public_code = "local_session_failed"
        public_detail = _RECIPE_INVOCATION_HALT_DEFAULT_DETAILS[public_code]
    else:
        public_detail = redact_text(detail, max_chars=500) if detail is not None else ""
        if not public_detail:
            public_detail = _RECIPE_INVOCATION_HALT_DEFAULT_DETAILS[public_code]
    return public_code, public_detail


def persisted_recipe_invocation_halt(params: Any) -> tuple[str, str] | None:
    """Read an allowlisted halt from a run's persisted params.

    Unknown or malformed stored codes deliberately mean "not an internal
    invocation halt".  Receipt projection uses this as the sole discriminator
    from operator cancellation; a legacy circuit-breaker ``halted_reason`` by
    itself therefore remains operator-style cancellation.
    """

    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(params, Mapping):
        return None
    code = params.get("halted_code")
    if not isinstance(code, str) or code not in RECIPE_INVOCATION_HALT_CODES:
        return None
    raw_detail = params.get("halted_reason")
    detail = redact_text(raw_detail, max_chars=500) if raw_detail is not None else ""
    if not detail:
        detail = _RECIPE_INVOCATION_HALT_DEFAULT_DETAILS[code]
    return code, detail


#: Container mime -> file suffix, for the two places a media byte-stream has
#: to be given a NAME: the compare preview's scratch file and the multipart
#: part of an upload to a provider that sniffs format by extension. ONE map,
#: because a suffix the preview accepts and the durable path rejects is two
#: surfaces with two answers about the same file.
MEDIA_MIME_SUFFIX = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def media_suffix(filename: str | None, mime: str | None) -> str:
    """The file suffix for a media byte-stream: the declared name's own
    extension when it has a plausible one, else the container mime's, else
    ``.bin`` — never a guess dressed up as a fact."""
    if filename and "." in filename:
        suffix = "." + filename.rsplit(".", 1)[-1].lower()
        if 1 < len(suffix) <= 6:
            return suffix
    if mime and mime.lower() in MEDIA_MIME_SUFFIX:
        return MEDIA_MIME_SUFFIX[mime.lower()]
    return ".bin"


def media_upload_filename(path: Any, media: Any) -> str:
    """The filename to put on a multipart upload part for ``media``.

    Whisper-class endpoints sniff the container format from the part's
    filename extension, and a blob-backed row materializes at its
    CONTENT-ADDRESSED path — ``<root>/e3/e3b0c442…``, a bare 64-hex digest
    with no extension (``store/blob_backend.py``). Posting that basename is
    what made ``whisper-1`` answer "Unrecognized file format" on a perfectly
    standard PCM WAV.

    So the name comes from the media row's OWN ``filename``/``mime``, and the
    suffix is never hardcoded: an mp3 must not be announced as a ``.wav``,
    which trades one unrecognized-format error for a decode error further in.
    Falls back to the path's basename when no media cell is in hand (the
    compare preview writes a real ``scratch<suffix>`` file), and to a bare
    ``audio`` stem when neither offers a name.

    The result is stripped of directory components and of the characters that
    would break out of the multipart ``filename="…"`` header — a media
    filename is user-supplied text.
    """
    from pathlib import PurePosixPath

    cell = media if isinstance(media, dict) else {}
    declared = cell.get("filename")
    mime = cell.get("mime")
    name = str(declared).strip() if declared else ""
    if not name:
        # A digest basename is not a name; only a path that already carries a
        # suffix is worth borrowing (the preview's scratch file).
        candidate = PurePosixPath(str(path).replace("\\", "/")).name
        if "." in candidate:
            name = candidate
    stem = PurePosixPath(name.replace("\\", "/")).name
    stem = "".join(ch for ch in stem if ch.isprintable() and ch not in '"\\\r\n')
    suffix = media_suffix(stem or None, str(mime) if mime else None)
    if stem.lower().endswith(suffix):
        return stem
    root = stem.rsplit(".", 1)[0] if "." in stem else stem
    return (root or "audio") + suffix


def _trusted_local_media_path(media: Any, *, op: str) -> str:
    import os

    if (
        isinstance(media, str)
        and not media.startswith(("http://", "https://"))
        and os.environ.get("FRISKET_ALLOW_CODE_RECIPES", "1") == "1"
    ):
        return media
    raise ValueError(
        f"{op} expects a local file or ingested blob; run the "
        "'download media' step first for URL columns"
    )


@contextmanager
def materialize_media_path(media: Any, ctx: Any, *, op: str = "this op"):
    """Keep a blob-backed path alive for exactly one media consumer call."""

    if isinstance(media, dict) and media.get("blob"):
        with ctx.project.materialize_blob(str(media["blob"])) as path:
            yield Path(path)
        return
    yield Path(_trusted_local_media_path(media, op=op))


def resolve_media_path(media, ctx, *, op: str = "this op") -> str:
    """Resolve only explicitly trusted, persistent host paths.

    Blob envelopes must instead use :func:`materialize_media_path` so remote
    temporary files stay alive for the complete consumer call. A raw string
    resolves as a host path only on trusted local deployments
    (``FRISKET_ALLOW_CODE_RECIPES``); URLs never resolve here.
    """
    if isinstance(media, dict) and media.get("blob"):
        raise ValueError(
            f"{op} must keep blob materialization alive; use materialize_media_path"
        )
    return _trusted_local_media_path(media, op=op)


FIELD_TYPES = {
    "score": {"type": "integer", "column": "integer"},  # 0-10
    "integer": {"type": "integer", "column": "integer"},
    "number": {"type": "number", "column": "number"},
    "boolean": {"type": "boolean", "column": "boolean"},
    "text": {"type": "string", "column": "text"},
    "category": {"type": "string", "column": "category"},
    "date": {"type": "string", "column": "date"},
    "list": {"type": "array", "column": "json"},
    "json": {"type": "object", "column": "json"},
}


@dataclass
class RenderedCall:
    """What the runner sends to the router for one row."""

    messages: list[dict[str, Any]]
    schema: dict[str, Any] | None
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass
class Recipe:
    """Base class. Subclasses implement render(); non-LLM recipes (search,
    transcribe) implement execute() instead and set llm=False."""

    name: str = "base"
    version: str = "1"
    llm: bool = True
    description: str = ""
    # Mechanical/deterministic ops (clean_column) whose rows are auto-verified
    # by MapRunner so they never flood the review queue.
    auto_verify: bool = False
    # Deterministic parsers may define a meaningful result for blank inputs.
    allow_all_empty_input: bool = False
    # Row-local data errors should not trip the provider-failure breaker.
    disable_failure_halt: bool = False
    # Optional per-recipe cap applied in addition to runner configuration.
    max_concurrency: int | None = None
    # Create or exactly reuse the complete declared output set in one
    # transaction. On exact reuse, MapRunner also preserves prior result cells
    # outside a targeted rerun's immutable row scope when it switches pointers.
    atomic_output_columns: bool = False
    # Recipes whose output ownership depends on an action receipt/claim must be
    # launched through the canonical action lifecycle.
    action_lifecycle_only: bool = False
    # Most recipes hide freshly-created columns when every row fails. Some
    # deterministic families retain useful, retryable per-row errors instead.
    keep_zero_success_output_columns: bool = False
    # Private runner-facing error location for the source control. Typed
    # Typed ModelRows uses one canonical ``source`` union instead of legacy columns.
    empty_input_error_field: str = "params.input_columns"
    # REQUIRED DECLARATION — annotated, deliberately NOT defaulted (E-3).
    #
    # Whether this recipe composes with the execution seam: whether its runs
    # resolve a route, compile promises, gate on claims, and get verified at
    # the effect site. Six consumers ask (execution/resolve_for_action.py,
    # engine/jobs/runs.py, engine/runner/map_runner.py, and three sites in
    # engine/runner/validation.py), and every one of them used to ask through
    # ``getattr(recipe, "consumes_resolution", False)`` — six copies of a
    # default that silently answered "no" for any recipe that forgot to
    # declare, which is exactly how a resolution-aware recipe ships with its
    # consent machinery dormant and nothing complains. A ClassVar with no
    # value makes the omission an AttributeError at the first consumer
    # instead: undeclared is unanswerable, not "no".
    #
    # ClassVar (not a dataclass field): this is a per-CLASS declaration about
    # what the recipe IS, never per-instance state, and a required dataclass
    # field could not follow the defaulted ones above anyway. Sibling
    # behavioral booleans keep their defaults on purpose — they tune behavior
    # a caller can observe; this one decides composition.
    consumes_resolution: ClassVar[bool]

    # The execution capability this recipe's runs resolve for — "transcribe",
    # "ocr" (``frisket.execution.targets.EXECUTION_CAPABILITIES``). Required
    # of, and read ONLY from, recipes that declare ``consumes_resolution =
    # True``: it decides which roster canonicalizes the authored engine, which
    # target rows are candidates, which option keys are authored options, and
    # which meter the price book quotes. A default would have made every
    # future capability's runs resolve as transcription — silently, since the
    # transcription roster would simply not know their engine and the run
    # would refuse with a confusing message.
    #
    # ``None`` is the honest value for the (many) recipes that consume no
    # resolution at all, which is why this one CAN carry a default: the
    # question "what capability does this route resolve?" has no answer for a
    # recipe with no route.
    execution_capability: ClassVar[str | None] = None

    # REQUIRED DECLARATION — annotated, deliberately NOT defaulted, for the same
    # reason as ``consumes_resolution`` above: a default here answers a money
    # question for a recipe that never thought about it. The default that used
    # to exist was implicit and lived at the consumer — ``estimate_run`` turned
    # every ``estimate() -> None`` into ``{"cost": 0.0}`` — so a recipe shipped
    # unpriced and priced at $0.00, which is below every gate. Undeclared is now
    # an AttributeError at the first estimate instead of a silent free run.
    #
    # ClassVar, per-CLASS: what the recipe IS. The per-SPEC question (an engine
    # axis where one recipe is local-free or provider-metered depending on the
    # authored engine) is ``cost_class_for`` below, exactly as ``llm`` /
    # ``is_llm(spec)`` split the same way.
    cost_class: ClassVar[CostClass]
    # Host programs may retain their own raw result envelope using an existing
    # fenced store. This changes checkpoint ownership, never paid classification.
    row_effect_checkpoint_owner: ClassVar[str] = "runner"

    def cost_class_for(self, spec: dict) -> "CostClass":
        """Per-run cost class. Defaults to the static ``cost_class``
        declaration; overridden by recipes whose engine axis decides (map.ner
        runs spaCy/GLiNER locally for free and the LLM engine on a meter), so a
        genuinely free run of a metered-capable recipe still runs dialog-free.

        Read by ``engine/runner/validation.py`` when — and only when — the
        recipe produced NO estimate."""
        del spec
        return self.cost_class

    @asynccontextmanager
    async def execution_scope(
        self,
        spec: dict,
        ctx: "OpContext",
        *,
        expected_rows: int,
    ) -> AsyncIterator[None]:
        """Optional resources owned for one row-oriented runner invocation."""

        del spec, ctx, expected_rows
        yield

    def is_llm(self, spec: dict) -> bool:
        """Per-run LLM-ness. Defaults to the static ``llm`` flag; overridden by
        recipes whose engine axis picks per-spec (map.ner's engine="llm"
        dispatches through the same render()/structured-completer path as an
        LLM recipe while spacy/gliner stay the non-LLM execute() path,
        entities-llm-dispatch-v1). The runner (``MapRunner``) asks this
        instead of reading ``.llm`` directly so a single recipe instance can
        answer differently per spec."""
        return self.llm

    def max_row_concurrency(self, spec: dict) -> int | None:
        """Optional cap for this recipe's row-local MapRunner workers.

        ``None`` preserves the runner's configured concurrency. Recipes whose
        selected engine cannot safely execute several rows at once may return
        a positive integer without imposing a global runner limit. Batch
        recipes keep their existing whole-batch execution path.
        """
        return None

    def requires_row_effect_checkpoint(self, spec: dict) -> bool:
        """Whether every row must be durably fenced before execution.

        Most recipes derive this from provider/cost classification. Recipes
        with independently effectful tools may opt in even when model replay
        proves that the model transport itself cannot go live.
        """
        del spec
        return False

    def run_provenance_model(self, spec: dict) -> str | None:
        """The model/engine identity stamped on the run row (shown as the
        cell's ``model`` provenance).

        Defaults to the spec's LLM ``model``. A non-LLM op carries a leftover
        default ``model`` in its spec that it never actually calls, so it
        reports None here (the provenance reads "—") — an engine-driven op
        like transcription/OCR overrides this to name its real engine instead
        of a misleading LLM model."""
        return spec.get("model") if self.is_llm(spec) else None

    def postprocess_value(self, name: str, value: Any, spec: dict) -> Any:
        """Optional per-field transform applied to a successful LLM row's raw
        value before it lands in the cell (e.g. map.ner's llm engine
        canonicalizing entity types the same way the spacy/gliner engines
        do, entities-llm-dispatch-v1). No-op by default."""
        return value

    def normalize_model_output(
        self, data: dict[str, Any], spec: dict, *, row_values: dict[str, Any]
    ) -> Any:
        """Map provider-facing response keys to publication-facing keys."""

        del spec, row_values
        return data

    def params(self) -> list[dict]:
        """Recipe-declared spec parameters beyond fields/prompt —
        [{name, type, choices?, default?, description}]. The v1 action catalog
        projects this metadata into ui_hints so the UI can build selects (e.g.
        the transcribe/ocr 'engine' choice) instead of hardcoding
        provider-specific knowledge."""
        return []

    def estimate(
        self,
        project: Any,
        spec: dict,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Optional non-LLM cost estimate hook.

        LLM recipes are estimated by the runner's token sampler. Non-LLM
        recipes that still call billable providers (for example remote or
        hosted transcription) can return the same estimate envelope here.

        ``None`` means "I produced no estimate" — NOT "free". The runner reads
        this recipe's ``cost_class_for(spec)`` to decide what absence means:
        free declares $0.00, metered/unpriceable declare ``cost: None`` and gate.
        """
        return None

    def source_columns(self, spec: dict) -> list[str]:
        """Column names whose values the runner should load for each row."""
        out: list[str] = []
        for name in spec.get("input_columns") or []:
            if name and str(name) not in out:
                out.append(str(name))
        for name in column_template_names(spec.get("input_template")):
            if name not in out:
                out.append(name)
        return out

    def output_fields(self, spec: dict) -> list[dict]:
        """Returns [{name, type(frisket column type), schema(json-schema)}]."""
        fields = []
        for f in spec.get("fields", []):
            ftype = FIELD_TYPES.get(f.get("type", "text"), FIELD_TYPES["text"])
            fields.append(
                {
                    "name": f["name"],
                    "column_type": ftype["column"],
                    "schema": self._field_schema(f, ftype),
                    "description": f.get("description", ""),
                }
            )
            if spec.get("include_justification"):
                fields.append(
                    {
                        "name": f"{f['name']}_justification",
                        "column_type": "text",
                        "schema": {
                            "type": "string",
                            "description": f"One-sentence reason for {f['name']}",
                        },
                        "description": "",
                    }
                )
        if spec.get("include_confidence"):
            base = (
                (spec.get("fields") or [{}])[0].get("name")
                or spec.get("output_name")
                or "output"
            )
            fields.append(
                {
                    "name": f"{base}_confidence",
                    "column_type": "number",
                    "schema": {
                        "type": "number",
                        "description": "Self-assessed confidence 0.0-1.0",
                    },
                    "description": "",
                }
            )
        return fields

    def retired_output_names(self, spec: dict) -> list[str]:
        """Output-column names a PRIOR run of this recipe's output emitted but
        the current `spec` no longer does. On a
        fresh run MapRunner hides any of these that still exist as ai_generated
        columns (hide-by-default + a provenance note) instead of stranding stale
        values. The recipe owns its "output family" knowledge; MapRunner owns
        the generic hide+provenance hook. Default: nothing to retire."""
        return []

    @staticmethod
    def _field_schema(f: dict, ftype: dict) -> dict:
        s: dict[str, Any] = {"type": ftype["type"]}
        if f.get("description"):
            s["description"] = f["description"]
        if f.get("type") == "score":
            s["minimum"] = 0
            s["maximum"] = 10
            s.setdefault("description", "Score from 0 to 10")
        if f.get("type") == "category" and f.get("labels"):
            s["enum"] = f["labels"]
        if f.get("type") == "list":
            s["items"] = f.get("items", {"type": "string"})
        if f.get("type") == "json" and f.get("properties"):
            s["properties"] = f["properties"]
            s["required"] = list(f["properties"].keys())
        return s

    def response_schema(self, spec: dict) -> dict:
        props = {f["name"]: f["schema"] for f in self.output_fields(spec)}
        return {
            "type": "object",
            "properties": props,
            "required": list(props.keys()),
        }

    def render(self, row_values: dict[str, Any], spec: dict) -> RenderedCall:
        raise NotImplementedError

    async def execute(
        self, row_values: dict[str, Any], spec: dict, ctx: "OpContext"
    ) -> dict[str, Any]:
        """Non-LLM recipes override this. Returns {field_name: value}."""
        raise NotImplementedError


@dataclass
class OpContext:
    """Runtime services available to non-LLM recipes."""

    project: Any = None
    http: Any = None  # shared httpx client
    extras: dict[str, Any] = field(default_factory=dict)
    # Request-scoped, typed payer facts. Kept out of ``extras`` so an
    # untrusted/nested action payload cannot pose as credential authority.
    credential_use_context: CredentialUseContext | None = None
    execution_limits: ExecutionLimits | None = None
