"""The copilot: agentic authoring, deterministic execution.

A chat endpoint with tool access to the project (schema, sample rows,
action spec docs). It never runs anything itself; it drafts an action proposal
and returns it as a proposal card. The human previews/runs that proposal
through the same gates as a hand-built action. The Braintrust-helper pattern.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from frisket.actions.core import ColumnTransform
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import root_action_catalog
from frisket.actions.types import (
    ColumnTransformContext,
    InputReference,
    ProjectScope,
    SheetRows,
    discover_references,
)
from frisket.ai.llm import LLMError, LLMResponse, ModelRouter
from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
from frisket.ai.llm.types import provider_from_model_id
from frisket.contracts.http.copilot import (
    COPILOT_REPLY_SCHEMA_VERSION,
    CopilotRegisteredActionDraft,
    _DERIVE_ACTION_KINDS,
    _ENRICH_ACTION_KINDS,
    _MAP_ACTION_KINDS,
    _MEDIA_ACTION_KINDS,
    _REDUCE_ACTION_KINDS,
    _REGISTERED_CREATE_SHEET_KINDS,
    _REGISTERED_ROW_CREATE_SHEET_KINDS,
    _RESOLVE_ACTION_KINDS,
    _WEB_ACTION_KINDS,
    _family_kinds,
)
from frisket.engine.runner.row_execution import _wire_accounting_meta
from frisket.engine.runner.validation import assert_provider_spend_cap
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore

logger = logging.getLogger("frisket.copilot")

# The kinds the copilot wire union (contracts/http/copilot.py) can actually
# carry. Every family uses the same registered action draft.
# The spec-schema enum and the
# generated catalog below both draw from this single tuple so neither can
# teach the model a kind the wire drops (tests/authoring/test_copilot_action_catalog.py
# pins schema enum == this == the wire Literal kinds).
WIRE_ACTION_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("map", _MAP_ACTION_KINDS),
    ("resolve", _RESOLVE_ACTION_KINDS),
    ("derive", _DERIVE_ACTION_KINDS),
    ("reduce", _REDUCE_ACTION_KINDS),
    ("media", _MEDIA_ACTION_KINDS),
    ("enrich", _ENRICH_ACTION_KINDS),
    ("web", _WEB_ACTION_KINDS),
    ("research", _family_kinds("research")),
)
WIRE_ACTION_KINDS: frozenset[str] = frozenset(
    kind for _, kinds in WIRE_ACTION_FAMILIES for kind in kinds
)
_ACTION_CATALOG_BY_KIND = {entry.kind: entry for entry in root_action_catalog().actions}


def _first_sentence(description: str) -> str:
    """The first sentence of a registry description, for a compact one-liner."""
    head, sep, _rest = description.partition(". ")
    return head if sep else description.rstrip(".")


def _build_action_catalog() -> str:
    """Family-grouped one-liner action catalog for the system prompt, generated
    from each kind's registry ActionCatalogEntry.title/description
    (actions/registry.py) — never hand-maintained, so a new
    map/derive/reduce kind is well-described the moment it's registered."""
    sections = []
    for family, kinds in WIRE_ACTION_FAMILIES:
        lines = [
            f"- {kind}: {_ACTION_CATALOG_BY_KIND[kind].title} — "
            f"{_first_sentence(_ACTION_CATALOG_BY_KIND[kind].description)}."
            for kind in kinds
        ]
        sections.append(f"{family}:\n" + "\n".join(lines))
    return "\n\n".join(sections)


ACTION_CATALOG = _build_action_catalog()


_SAVED_AUTHORIZATION_FIELDS = frozenset(
    {
        "confirmed",
        "consented_promise_set_hash",
    }
)
_FORBIDDEN_PROPOSAL_FIELDS = _SAVED_AUTHORIZATION_FIELDS | frozenset(
    {"params", "authoring_contract_version", "idempotency_key", "confirmation"}
)


def _inline_local_schema_refs(value: Any, definitions: dict[str, Any]) -> Any:
    """Inline one typed action schema fragment into the model schema.

    Each input schema owns its own ``$defs`` namespace. Copying a property out
    without resolving those local refs produces a dangling schema, so the
    model-facing union is assembled only from self-contained fragments.
    """

    if isinstance(value, list):
        return [_inline_local_schema_refs(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.removeprefix("#/$defs/")
        target = definitions.get(name)
        if not isinstance(target, dict):
            raise RuntimeError(f"copilot schema has unresolved local ref {ref}")
        overlay = {key: item for key, item in value.items() if key != "$ref"}
        return _inline_local_schema_refs({**target, **overlay}, definitions)
    return {
        key: _inline_local_schema_refs(item, definitions)
        for key, item in value.items()
        if key != "$defs"
    }


def _model_authored_param_properties() -> dict[str, Any]:
    variants: dict[str, list[dict[str, Any]]] = {}
    for kind in sorted(WIRE_ACTION_KINDS):
        schema = _ACTION_CATALOG_BY_KIND[kind].input_schema
        definitions = schema.get("$defs")
        definitions = definitions if isinstance(definitions, dict) else {}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise RuntimeError(f"copilot action {kind} has no object properties")
        for name, property_schema in properties.items():
            if name in _SAVED_AUTHORIZATION_FIELDS:
                continue
            if not isinstance(property_schema, dict):
                raise RuntimeError(
                    f"copilot action {kind} has invalid schema for {name}"
                )
            resolved = _inline_local_schema_refs(property_schema, definitions)
            candidates = variants.setdefault(name, [])
            if resolved not in candidates:
                candidates.append(resolved)
    return {
        name: candidates[0] if len(candidates) == 1 else {"anyOf": candidates}
        for name, candidates in sorted(variants.items())
    }


def _proposal_spec_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "action_kind": {"type": "string", "enum": sorted(WIRE_ACTION_KINDS)},
        **_model_authored_param_properties(),
        "sheet_id": {"type": "integer", "minimum": 1},
    }
    if _REGISTERED_CREATE_SHEET_KINDS:
        properties["sheet_name"] = {"type": "string", "minLength": 1}
    output_name_schemas = [
        _registered_output_names_schema(kind) for kind in sorted(WIRE_ACTION_KINDS)
    ]
    if output_name_schemas:
        properties["output_names"] = (
            output_name_schemas[0]
            if len(output_name_schemas) == 1
            else {"anyOf": output_name_schemas}
        )
    variants = []
    for kind in sorted(WIRE_ACTION_KINDS):
        required = _ACTION_CATALOG_BY_KIND[kind].input_schema.get("required", [])
        authored_required = [
            name for name in required if name not in _SAVED_AUTHORIZATION_FIELDS
        ]
        if (
            kind not in _REGISTERED_CREATE_SHEET_KINDS
            or kind in _REGISTERED_ROW_CREATE_SHEET_KINDS
        ) and "sheet_id" not in authored_required:
            authored_required.insert(0, "sheet_id")
        variant_properties: dict[str, Any] = {"action_kind": {"enum": [kind]}}
        if kind in _REGISTERED_CREATE_SHEET_KINDS:
            authored_required.append("sheet_name")
        variant_properties["output_names"] = _registered_output_names_schema(kind)
        variants.append(
            {
                "type": "object",
                "properties": variant_properties,
                "required": ["action_kind", *authored_required],
            }
        )
    return {
        "type": "object",
        "properties": properties,
        "required": ["action_kind"],
        # The model still sees one flat union of canonical fields, while this
        # generated discriminator makes each typed action's own required
        # set authoritative. Derive actions therefore do not inherit map's
        # sheet_id requirement, and no per-kind translator is introduced.
        "anyOf": variants,
    }


def _registered_output_names_schema(kind: str) -> dict[str, Any]:
    from frisket.actions.core import has_dynamic_outputs

    terminal = ACTION_REGISTRY.get(kind).definition.run
    if has_dynamic_outputs(terminal):
        return {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1},
        }
    keys = [field.key for field in terminal.output_fields]
    return {
        "type": "object",
        "properties": {key: {"type": "string", "minLength": 1} for key in keys},
        "additionalProperties": False,
    }


def copilot_reply_schema() -> dict[str, Any]:
    """The structured-output schema for one copilot turn."""
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "needs_import": {"type": "boolean"},
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [family for family, _ in WIRE_ACTION_FAMILIES],
                        },
                        "title": {"type": "string"},
                        "spec": _proposal_spec_schema(),
                    },
                    "required": ["kind", "title", "spec"],
                },
            },
        },
        "required": ["reply", "needs_import", "proposals"],
    }


def _coerce_proposal(
    kind: Any, spec: dict[str, Any]
) -> tuple[dict[str, Any], tuple[InputReference, ...]] | None:
    """Validate one flat model-authored spec and emit the saved envelope."""

    family = kind if isinstance(kind, str) else ""
    action_kind = spec.get("action_kind")
    if (
        family not in {candidate for candidate, _kinds in WIRE_ACTION_FAMILIES}
        or not isinstance(action_kind, str)
        or action_kind not in WIRE_ACTION_KINDS
        or action_kind.split(".", 1)[0] != family
        or _FORBIDDEN_PROPOSAL_FIELDS.intersection(spec)
    ):
        return None
    creates_sheet = action_kind in _REGISTERED_CREATE_SHEET_KINDS
    project_scoped = (
        creates_sheet and action_kind not in _REGISTERED_ROW_CREATE_SHEET_KINDS
    )
    target_fields = (
        {"sheet_name"}
        if project_scoped
        else {"sheet_id", "sheet_name"}
        if creates_sheet
        else {"sheet_id"}
    )
    params = {
        name: value
        for name, value in spec.items()
        if name not in {"action_kind", *target_fields, "output_names"}
    }
    output_names = spec.get("output_names", {})
    if not isinstance(output_names, dict):
        return None
    registered = ACTION_REGISTRY.get(action_kind)
    try:
        draft = CopilotRegisteredActionDraft.model_validate(
            {
                "action_id": action_kind,
                "scope": {"kind": "project"}
                if project_scoped
                else {"kind": "sheet_rows", "sheet_id": spec.get("sheet_id")},
                **({"sheet_name": spec.get("sheet_name")} if creates_sheet else {}),
                "params": params,
                "output_names": output_names,
            },
            strict=True,
        )
        bound_params, _ = registered.bind_values(
            scope=(ProjectScope if project_scoped else SheetRows).model_validate(
                draft.scope.model_dump(), strict=True
            ),
            params=draft.params,
            output_names=draft.output_names,
        )
        references = discover_references(bound_params)
    except Exception:
        return None
    return draft.model_dump(mode="json", exclude_none=True), references


COPILOT_MODEL = "anthropic/claude-sonnet-5"

# When the request names no model, prefer a provider the router actually has
# a key for — in this order, each at its copilot-quality default — before
# falling back to the legacy COPILOT_MODEL constant (which, keyless, at least
# yields the canonical missing-key remediation copy rather than an arbitrary
# provider's). openrouter is deliberately absent: it has no canonical model.
_COPILOT_MODEL_BY_PROVIDER: tuple[tuple[str, str], ...] = (
    ("anthropic", COPILOT_MODEL),
    ("openai", "openai/gpt-5.6-terra"),
    ("gemini", "gemini/gemini-3.6-flash"),
)


def default_copilot_model(router: ModelRouter) -> str:
    """The copilot model to use when the request doesn't name one
    (copilot-model-selection-v1)."""
    configured = router.configured_keys()
    for provider, model in _COPILOT_MODEL_BY_PROVIDER:
        if provider in configured:
            return model
    return COPILOT_MODEL


SYSTEM = """You are the frisket copilot — an assistant inside an AI-powered \
spreadsheet for investigative journalists. The user has a project with sheets \
of data. You help them author actions by returning runnable action proposals.

Rules:
- You DRAFT operations; you never execute them. The user previews and runs.
- Prefer cheap models (gemini/gemini-3.5-flash-lite) unless quality demands more.
- Multi-field = one call: combine related questions into one action.
- When the user's ask maps to an action, respond with BOTH a short \
explanation and the spec. When it doesn't (a question about their data, advice), \
just answer.
- Say "action" in user-facing reply text and titles, not "recipe".
- Never mention the internal key name "recipe" in user-facing reply text or titles.
- Available action kinds and the internal spec format follow this prompt.
- DETERMINISTIC FIRST: when the transformation is mechanical or pattern-based \
(fixed delimiters, prefixes/suffixes, positional substrings — anything a regex, \
template, or a few lines of code can express), prefer the deterministic \
non-LLM kinds — map.regex_extract, map.template, map.python — over LLM map \
actions such as map.extract. Deterministic actions cost $0, run instantly, and \
are reproducible run-to-run; LLM actions are slower, cost money, and can vary. \
Study the sample rows before choosing: if a fixed pattern covers them (e.g. \
"Waffle House-Duluth,GA" → the state is the two letters after the comma), \
propose map.regex_extract, not map.extract. Reserve LLM actions for genuinely \
semantic work: meaning, judgment, or messy text no fixed pattern captures.
- IMPORT PREREQUISITE: an action can only edit data that already exists. If the \
project has no sheets/rows yet, or the user's request depends on data that has \
NOT been imported (a CSV/spreadsheet, uploaded files, a URL to fetch, a YouTube \
or media link to transcribe), you cannot draft a runnable action. In that case \
set "needs_import": true, write a short reply telling them to import the data \
first, and return NO proposals. Only set "needs_import": false and offer \
proposals when the data to operate on is already present in the schema below.

The project schema and sample rows are provided. Respond in JSON:
{"reply": "<markdown for the user>",
 "needs_import": <true|false>,
 "proposals": [{"kind": "map"|"resolve"|"derive"|"reduce"|"media"|"enrich"|"web"|"research", "spec": {...},
                "title": "<short action title>"}]}
When needs_import is true, proposals MUST be empty. Otherwise proposals may be \
empty. Specs must use real sheet_id and column names from the schema. Proposal \
specs must use action_kind/action terms, not public recipe language."""

ACTION_SPEC_DOCS = """Action proposal spec format; hidden from users:
Author one flat object containing action_kind plus only the canonical fields
declared for that action in the supplied response schema. Use exact field
names and value types. Do not add params, authoring_contract_version, aliases,
idempotency_key, confirmation, or null/empty filler fields. output_names is an
optional rename map: include only declared logical output keys that should use
a different column name. Omitted keys keep their logical names.
The server validates the draft and creates its saved execution-neutral envelope."""


def project_context(project: Project, max_sample_rows: int = 3) -> str:
    """Schema + a few sample rows per sheet, compact."""
    lines: list[str] = []
    for s in project.sheets():
        if "(undone:" in s["name"]:
            continue
        cols = project.columns(s["id"])
        lines.append(
            f"Sheet '{s['name']}' (sheet_id={s['id']}, "
            f"{project.row_count(s['id'])} rows"
            + (
                f", derived from sheet {s['parent_sheet_id']}"
                if s["parent_sheet_id"]
                else ""
            )
            + "):"
        )
        for c in cols:
            lines.append(
                f"  - {c['name']} ({c['type']}"
                + (", AI-generated" if c["ai_generated"] else "")
                + ")"
            )
        row_ids = [
            r["id"]
            for r in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position LIMIT ?",
                (s["id"], max_sample_rows),
            )
        ]
        for i, rid in enumerate(row_ids):
            sample = {}
            for c in cols:
                v = project.get_values(s["id"], c["id"], row_ids=[rid]).get(rid)
                if v is not None:
                    sv = str(v)
                    sample[c["name"]] = sv[:120] + ("…" if len(sv) > 120 else "")
            lines.append(f"  sample {i + 1}: {json.dumps(sample, ensure_ascii=False)}")
    return "\n".join(lines)


async def copilot_chat(
    project: Project,
    router: ModelRouter,
    messages: list[dict[str, str]],
    model: str | None = None,
) -> dict[str, Any]:
    """messages: [{role: user|assistant, content: str}] chat history.
    ``model=None`` resolves through :func:`default_copilot_model`."""
    model = model or default_copilot_model(router)
    schema = copilot_reply_schema()
    context = project_context(project)
    # Validation and repair are StructuredCompleter's job (jsonschema-driven),
    # not a hand-rolled `resp.data or {fallback}` guard. repair_attempts=1
    # matches every other non-batch structured caller: one corrective retry.
    req = StructuredRequest(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    f"{SYSTEM}\n\nAvailable action kinds, grouped by family "
                    f"(kind: title — one-line description):\n{ACTION_CATALOG}"
                    f"\n\n{ACTION_SPEC_DOCS}\n\nProject:\n{context}"
                ),
            },
            *messages,
        ],
        schema=schema,
        temperature=0.2,
        repair_attempts=1,
    )
    provider = provider_from_model_id(model)
    # This direct authoring effect has no execution attempt to police it later.
    # Resolve the exact model first, then enforce the cap belonging to the key
    # the router will actually spend through immediately before egress.  A
    # deployment/org/local key must not be refused because an unused project
    # key for the same provider happens to carry a cap.
    if router.credential_source_for(provider) == "project_key":
        assert_provider_spend_cap(project, provider)

    try:
        result = await StructuredCompleter(router).complete(req)
    except LLMError as exc:
        # A schema-invalid response (and a later repair transport failure) can
        # still represent completed, billed transports.  Preserve those facts
        # before the existing service layer maps the model failure to HTTP 502.
        paid_wire_calls = list(getattr(exc, "wire_calls", []) or [])
        if paid_wire_calls:
            _persist_copilot_wire_calls(project, model, paid_wire_calls)
        raise

    cost_usd = _persist_copilot_wire_calls(project, model, result.wire_calls)
    data = result.data
    needs_import = bool(data.get("needs_import"))
    # needs_import replies never carry a proposal (contract invariant); the CTA,
    # not an invented action, is the user-facing surface.
    valid = [] if needs_import else validate_proposals(project, data)
    return {
        "schema_version": COPILOT_REPLY_SCHEMA_VERSION,
        "reply": data.get("reply", ""),
        "needs_import": needs_import,
        "proposals": valid,
        # Derived from the exact per-wire facts written above.  In particular,
        # one unknown live cost keeps the reply unknown instead of presenting a
        # confident subtotal of only the calls we could price.
        "cost_usd": cost_usd,
    }


def _persist_copilot_wire_calls(
    project: Project,
    model: str,
    wire_calls: list[LLMResponse],
) -> float | None:
    """Persist one neutral, unscoped fact per completed copilot transport.

    The durable payload deliberately selects only accounting metadata.  Raw
    response bodies, prompts, project context and credential material never
    enter ``model_calls``.  The shared writer makes each fact and the matching
    project-key spend delta one transaction.

    Copilot requests currently carry no idempotency key.  Consequently a
    database failure after provider success cannot be made retry-safe without
    inventing an identity that would incorrectly deduplicate intentional
    repeated chat turns; fail safely and leave that bounded ambiguity explicit.
    """

    accounting = _wire_accounting_meta(model, wire_calls)
    calls = [
        call for call in accounting.get("model_calls", []) if isinstance(call, dict)
    ]

    if not calls:
        return 0.0
    try:
        RunResultStore(project).write_unscoped_model_calls(
            calls,
            row_id=None,
            column_id=None,
        )
    except Exception as exc:
        raise LLMError(
            "The provider returned a copilot response, but its usage facts "
            "could not be recorded; retrying may make another provider call."
        ) from exc

    cost = accounting.get("cost")
    if cost is None:
        return None
    return float(cost)


def _prune_native_fill(spec: dict[str, Any]) -> dict[str, Any]:
    """Remove union-schema filler without erasing typed action intent.

    Native structured output may populate fields that belong to another
    action in the model-facing union. The selected typed action is the
    owner: discard undeclared fields and null filler, then let its strict
    params model decide whether every declared value is valid. In particular,
    ``False``, numeric zero, empty strings, and empty lists can be meaningful
    values and must not be rewritten into an action default.
    """

    action_kind = spec.get("action_kind")
    entry = (
        _ACTION_CATALOG_BY_KIND.get(action_kind)
        if isinstance(action_kind, str)
        else None
    )
    properties = entry.input_schema.get("properties", {}) if entry is not None else {}
    declared = set(properties) if isinstance(properties, dict) else set()
    target_field = (
        "sheet_name" if action_kind in _REGISTERED_CREATE_SHEET_KINDS else "sheet_id"
    )
    allowed = {"action_kind", target_field, *declared, *_FORBIDDEN_PROPOSAL_FIELDS}
    if action_kind in _REGISTERED_ROW_CREATE_SHEET_KINDS:
        allowed.add("sheet_id")
    allowed.add("output_names")
    return {
        key: value
        for key, value in spec.items()
        if key in allowed and (value is not None or key in _FORBIDDEN_PROPOSAL_FIELDS)
    }


def _proposal_project_reference_error(
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]],
    params: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Check the bounded project references shared by Copilot action params."""

    sheet_id = params.get("sheet_id")
    if sheet_id is not None:
        if sheet_id not in sheet_columns:
            return "unknown_sheet_id", {"sheet_id": sheet_id}
        _column_ids, columns_by_name = sheet_columns[sheet_id]
        for name, value in params.items():
            if not (
                name == "group_by"
                or name.endswith("_column")
                or name.endswith("_columns")
            ):
                continue
            values = value if isinstance(value, list) else [value]
            unknown = [item for item in values if item not in columns_by_name]
            if unknown:
                return "unknown_input_columns", {
                    "sheet_id": sheet_id,
                    "param": name,
                    "unknown_columns": unknown,
                }

    source_sheet_id = params.get("source_sheet_id")
    if source_sheet_id is not None:
        source = sheet_columns.get(source_sheet_id)
        if source is None:
            return "unknown_sheet_id", {"sheet_id": source_sheet_id}
        source_column_id = params.get("source_column_id")
        if source_column_id is not None and source_column_id not in source[0]:
            return "unknown_input_column_id", {
                "sheet_id": source_sheet_id,
                "column_id": source_column_id,
            }

    source = params.get("source")
    if isinstance(source, dict) and "sheet_id" in source:
        nested_sheet_id = source["sheet_id"]
        nested_sheet = sheet_columns.get(nested_sheet_id)
        if nested_sheet is None:
            return "unknown_sheet_id", {"sheet_id": nested_sheet_id}
        nested_column_id = source.get("column_id")
        if nested_column_id is not None and nested_column_id not in nested_sheet[0]:
            return "unknown_input_column_id", {
                "sheet_id": nested_sheet_id,
                "column_id": nested_column_id,
            }
    return None


def _registered_proposal_project_reference_error(
    project: Project,
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]],
    draft: dict[str, Any],
    references: tuple[InputReference, ...],
) -> tuple[str, dict[str, Any]] | None:
    if draft["scope"]["kind"] == "project":
        return _proposal_project_reference_error(sheet_columns, draft["params"])
    sheet_id = draft["scope"]["sheet_id"]
    sheet = sheet_columns.get(sheet_id)
    if sheet is None:
        return "unknown_sheet_id", {"sheet_id": sheet_id}
    columns_by_name = sheet[1]
    unknown = [
        reference.column
        for reference in references
        if reference.column not in columns_by_name
    ]
    if unknown:
        return "unknown_input_columns", {
            "sheet_id": sheet_id,
            "param": "params",
            "unknown_columns": unknown,
        }
    incompatible = [
        {
            "name": reference.column,
            "actual_type": columns_by_name[reference.column],
            "accepted_column_types": list(reference.accepted_column_types),
        }
        for reference in references
        if reference.accepted_column_types is not None
        and reference.column in columns_by_name
        and columns_by_name[reference.column] not in reference.accepted_column_types
    ]
    if incompatible:
        return "incompatible_input_columns", {
            "sheet_id": sheet_id,
            "param": "params",
            "columns": incompatible,
        }
    action_id = draft["action_id"]
    terminal = ACTION_REGISTRY.get(action_id).definition.run
    from frisket.actions.core import ModelRows

    if isinstance(terminal, ModelRows):
        from frisket.engine.executor.map_rows_action import (
            TypedMapRowsPlanError,
            validate_model_rows_project_inputs,
        )

        try:
            typed_params = terminal.params_model.model_validate(
                draft["params"], strict=True
            )
            validate_model_rows_project_inputs(
                project,
                action_id=action_id,
                terminal=terminal,
                params=typed_params,
                sheet_id=sheet_id,
                row_ids=(
                    tuple(draft["scope"]["row_ids"])
                    if "row_ids" in draft["scope"]
                    else None
                ),
            )
        except TypedMapRowsPlanError as error:
            return error.code, dict(error.details)
        except ValidationError as error:
            return "invalid_params", {
                "sheet_id": sheet_id,
                "param": "params",
                "error_message": str(error),
            }
    if isinstance(terminal, ColumnTransform) and terminal.preflight is not None:
        params = terminal.params_model.model_validate(draft["params"], strict=True)
        [reference] = references
        try:
            terminal.preflight(
                params,
                ColumnTransformContext(source_type=columns_by_name[reference.column]),
            )
        except (TypeError, ValueError) as error:
            return "invalid_params", {
                "sheet_id": sheet_id,
                "param": "params",
                "error_message": str(error),
            }
    return None


def validate_proposals(project: Project, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate that proposals reference real sheets/columns; drop broken ones
    (with a logged reason — never a silent vanish,
    copilot-proposal-spec-schema-v1) and coerce each survivor into its strict,
    closed CopilotProposal wire spec."""
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]] = {}
    for sheet in project.sheets():
        columns = project.columns(sheet["id"])
        sheet_columns[sheet["id"]] = (
            {column["id"] for column in columns},
            {str(column["name"]): str(column["type"]) for column in columns},
        )
    valid: list[dict[str, Any]] = []
    for prop in data.get("proposals", []):
        title = prop.get("title", "")
        spec = prop.get("spec", {})
        if not isinstance(spec, dict):
            logger.warning(
                "copilot_proposal_dropped",
                extra={
                    "event": "copilot_proposal_dropped",
                    "reason": "spec_not_object",
                    "title": title,
                },
            )
            continue
        spec = _prune_native_fill(spec)
        coercion = _coerce_proposal(prop.get("kind"), spec)
        if coercion is None:
            logger.warning(
                "copilot_proposal_dropped",
                extra={
                    "event": "copilot_proposal_dropped",
                    "reason": "wire_contract_failed",
                    "title": title,
                    "kind": prop.get("kind"),
                },
            )
            continue
        coerced, references = coercion
        reference_error = _registered_proposal_project_reference_error(
            project,
            sheet_columns,
            coerced,
            references,
        )
        if reference_error is not None:
            reason, details = reference_error
            logger.warning(
                "copilot_proposal_dropped",
                extra={
                    "event": "copilot_proposal_dropped",
                    "reason": reason,
                    "title": title,
                    **details,
                },
            )
            continue
        valid.append({"kind": prop.get("kind"), "title": title, "spec": coerced})
    return valid
