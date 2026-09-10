"""Strict wire models for the Program G Copilot reply contract.

The Copilot HTTP boundary (POST ``/api/projects/{pid}/copilot``) exchanges a
bounded ``CopilotRequest`` chat history for a strict ``CopilotReply``. The
reply pins ``schema_version="frisket.copilot_reply.v1"``, a nonnegative optional
``cost_usd`` (never the ambiguous ``cost``), a closed proposal-family union
whose discriminator agrees with the spec's canonical action id, and the
runtime invariant ``needs_import=True => proposals=[]``.

The invariant is enforced twice, on purpose: a Pydantic ``model_validator`` fails
it at Python validation time, and a ``not`` JSON-Schema clause (injected via
``json_schema_extra``) makes the *generated* TypeScript validator reject the same
shape. The two must stay in agreement.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import ConfigDict, Field, JsonValue, RootModel, model_validator

from frisket.actions.core import CreateSheet, _ProjectAction
from frisket.actions.find_types import FindScanner
from frisket.actions.group_summary_types import GroupSummarizer
from frisket.actions.registry import ACTION_REGISTRY, COPILOT_ACTION_IDS
from frisket.actions.semantic_match_types import SemanticMatchReader
from frisket.actions.types import ProjectScope
from frisket.contracts.http.models import WireModel
from frisket.local_model_ids import parse_local_model_id


COPILOT_REPLY_SCHEMA_VERSION = "frisket.copilot_reply.v1"

# The Copilot proposal wire contract is intentionally narrower than the action
# catalog. These actions require dedicated authoring/review surfaces that the
# current Copilot product does not admit. Keep them runnable from the normal
# action surface, but do not advertise unsupported proposals.
COPILOT_UNSUPPORTED_ACTION_KINDS = frozenset(
    {
        # The canonical action requires two sheet ids, structured join-key and
        # projection arrays, and an authored target-sheet name.
        "derive.join",
        # Coupled semantic joins likewise need both a source scope and a new
        # sheet name, which the current proposal families cannot carry.
        "join.semantic",
        # Temporal child-sheet proposals additionally require a structured
        # selection, optional row scope, and an authored target-sheet name.
        # The strict/native proposal base owns none of those fields.
        "derive.temporal_segments",
        "derive.transcript_segments",
    }
)


def _family_kinds(family: str) -> tuple[str, ...]:
    """Closed set of registered action ids in one Copilot proposal family.

    The proposal discriminator (map|derive|reduce) must agree with the spec's
    ``action_id`` prefix. Each family enumerates the Copilot-admitted
    ``<family>.*`` ids from the registry — no invented kinds,
    and no actions whose required nested parameters the wire model would drop.
    """

    return tuple(
        sorted(
            kind
            for kind in COPILOT_ACTION_IDS
            if kind.split(".", 1)[0] == family
            and kind not in COPILOT_UNSUPPORTED_ACTION_KINDS
        )
    )


_MAP_ACTION_KINDS = _family_kinds("map")
_DERIVE_ACTION_KINDS = tuple(
    sorted(kind for kind in COPILOT_ACTION_IDS if kind.startswith("derive."))
)
_REDUCE_ACTION_KINDS = _family_kinds("reduce")

_REGISTERED_ACTION_KINDS = tuple(sorted(COPILOT_ACTION_IDS))
_REGISTERED_ROW_CREATE_SHEET_KINDS = tuple(
    kind
    for kind in _REGISTERED_ACTION_KINDS
    if isinstance(ACTION_REGISTRY.get(kind).definition.run, _ProjectAction)
    and ACTION_REGISTRY.get(kind).definition.run.capabilities
    in ((GroupSummarizer,), (FindScanner,))
)
_REGISTERED_CREATE_SHEET_KINDS = tuple(
    kind
    for kind in _REGISTERED_ACTION_KINDS
    if isinstance(ACTION_REGISTRY.get(kind).definition.run, CreateSheet)
    or kind in _REGISTERED_ROW_CREATE_SHEET_KINDS
)
_REGISTERED_SELECTED_CREATE_SHEET_KINDS = tuple(
    kind
    for kind in _REGISTERED_CREATE_SHEET_KINDS
    if isinstance(ACTION_REGISTRY.get(kind).definition.run, CreateSheet)
    and SemanticMatchReader in ACTION_REGISTRY.get(kind).definition.run.capabilities
)
_RESOLVE_ACTION_KINDS = tuple(
    kind for kind in _REGISTERED_ACTION_KINDS if kind.startswith("resolve.")
)
_MEDIA_ACTION_KINDS = tuple(
    kind for kind in _REGISTERED_ACTION_KINDS if kind.startswith("media.")
)
_ENRICH_ACTION_KINDS = tuple(
    kind for kind in _REGISTERED_ACTION_KINDS if kind.startswith("enrich.")
)
_WEB_ACTION_KINDS = tuple(
    kind for kind in _REGISTERED_ACTION_KINDS if kind.startswith("web.")
)

RegisteredActionKind = Literal[_REGISTERED_ACTION_KINDS]  # type: ignore[valid-type]


class CopilotMessage(WireModel):
    """One bounded, nonblank chat turn from the user or the assistant."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def _content_is_not_blank(self) -> CopilotMessage:
        if not self.content.strip():
            raise ValueError("message content must not be blank")
        return self


# Router routing-id shape: a lowercase provider token, one slash, then a
# whitespace-free model name (ollama tags carry ':' and '.', gemini names
# carry '.', so the name half only excludes whitespace). Bounded so an
# arbitrary blob can't ride the field into logs/receipts.
_MODEL_ID_RE = re.compile(r"[a-z0-9_-]+/\S+")


class CopilotRequest(WireModel):
    """The Copilot chat request: one or more bounded, nonblank messages, plus
    an optional caller-chosen model. ``model`` is the router's routing id
    (``provider/model-name`` for platform providers and
    ``ollama/@<endpoint-id>/<model>`` for local endpoints); ``None`` asks the
    server to choose its configured default."""

    messages: list[CopilotMessage] = Field(min_length=1)
    model: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _model_is_provider_slash_name(self) -> CopilotRequest:
        if self.model is None:
            return self
        if not _MODEL_ID_RE.fullmatch(self.model):
            raise ValueError("model must be 'provider/model-name'")
        if self.model.startswith("ollama/"):
            parse_local_model_id(self.model)
        return self


class CopilotRegisteredSheetRowsScope(WireModel):
    """Request-level sheet scope saved without execution authorization."""

    kind: Literal["sheet_rows"]
    sheet_id: int = Field(gt=0)
    row_ids: list[int] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _unique_positive_rows(self) -> CopilotRegisteredSheetRowsScope:
        if self.row_ids is not None and (
            any(row_id <= 0 for row_id in self.row_ids)
            or len(self.row_ids) != len(set(self.row_ids))
        ):
            raise ValueError("row_ids must be positive and unique")
        return self


class CopilotRegisteredActionDraft(WireModel):
    """A keyless ActionRequest draft; execution adds authorization fields."""

    # Keep target admission in generated contracts as well as Python. Only
    # explicitly Copilot-admitted table actions acquire the project variant.
    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "action_id": {"enum": list(_REGISTERED_ROW_CREATE_SHEET_KINDS)},
                        "scope": {
                            "type": "object",
                            "properties": {
                                "kind": {"const": "sheet_rows"},
                                "sheet_id": True,
                                "row_ids": True,
                            },
                            "required": ["kind"],
                            "additionalProperties": False,
                        },
                        "params": True,
                        "output_names": True,
                        "sheet_name": {"type": "string", "minLength": 1},
                    },
                    "required": ["sheet_name"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action_id": {
                            "enum": list(_REGISTERED_SELECTED_CREATE_SHEET_KINDS)
                        },
                        "scope": {
                            "type": "object",
                            "properties": {
                                "kind": {"const": "sheet_rows"},
                                "sheet_id": True,
                                "row_ids": {"type": "array", "minItems": 1},
                            },
                            "required": ["kind", "row_ids"],
                            "additionalProperties": False,
                        },
                        "params": True,
                        "output_names": True,
                        "sheet_name": {"type": "string", "minLength": 1},
                    },
                    "required": ["sheet_name"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action_id": {
                            "enum": [
                                kind
                                for kind in _REGISTERED_CREATE_SHEET_KINDS
                                if kind not in _REGISTERED_ROW_CREATE_SHEET_KINDS
                            ]
                        },
                        "scope": {
                            "type": "object",
                            "properties": {"kind": {"const": "project"}},
                            "required": ["kind"],
                            "additionalProperties": False,
                        },
                        "params": True,
                        "output_names": True,
                        "sheet_name": {"type": "string", "minLength": 1},
                    },
                    "required": ["sheet_name"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action_id": {
                            "enum": [
                                kind
                                for kind in _REGISTERED_ACTION_KINDS
                                if kind not in _REGISTERED_CREATE_SHEET_KINDS
                            ]
                        },
                        "scope": {
                            "type": "object",
                            "properties": {
                                "kind": {"const": "sheet_rows"},
                                "sheet_id": True,
                                "row_ids": True,
                            },
                            "required": ["kind"],
                            "additionalProperties": False,
                        },
                        "params": True,
                        "output_names": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "sheet_name": {"type": "null"},
                    },
                    "additionalProperties": False,
                },
            ]
        }
    )

    action_id: RegisteredActionKind
    scope: ProjectScope | CopilotRegisteredSheetRowsScope = Field(discriminator="kind")
    params: dict[str, JsonValue]
    output_names: dict[str, str] = Field(default_factory=dict)
    sheet_name: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _valid_output_names(self) -> CopilotRegisteredActionDraft:
        if self.action_id in _REGISTERED_CREATE_SHEET_KINDS:
            if self.action_id in _REGISTERED_ROW_CREATE_SHEET_KINDS and isinstance(
                self.scope, ProjectScope
            ):
                raise ValueError("this create_sheet proposal requires sheet_rows scope")
            if not isinstance(self.scope, ProjectScope) and not (
                self.action_id in _REGISTERED_ROW_CREATE_SHEET_KINDS
                or self.action_id in _REGISTERED_SELECTED_CREATE_SHEET_KINDS
                and bool(self.scope.row_ids)
            ):
                raise ValueError("create_sheet proposals require project scope")
            if self.sheet_name is None or not self.sheet_name.strip():
                raise ValueError("create_sheet proposals require sheet_name")
            self.sheet_name = self.sheet_name.strip()
        else:
            if not isinstance(self.scope, CopilotRegisteredSheetRowsScope):
                raise ValueError("row proposals require sheet_rows scope")
            if self.sheet_name is not None:
                raise ValueError("row proposals do not accept sheet_name")
        if any(
            not key or not name or key != key.strip() or name != name.strip()
            for key, name in self.output_names.items()
        ) or len(self.output_names.values()) != len(set(self.output_names.values())):
            raise ValueError("output names must be non-empty, trimmed, and unique")
        return self


class CopilotMapProposal(WireModel):
    kind: Literal["map"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _map_family_matches(self) -> CopilotMapProposal:
        action_id = self.spec.action_id
        if not action_id.startswith("map."):
            raise ValueError("map proposal action id must belong to the map family")
        return self


class CopilotDeriveProposal(WireModel):
    kind: Literal["derive"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _derive_family_matches(self) -> CopilotDeriveProposal:
        action_id = self.spec.action_id
        if not action_id.startswith("derive."):
            raise ValueError(
                "derive proposal action id must belong to the derive family"
            )
        return self


class CopilotResolveProposal(WireModel):
    kind: Literal["resolve"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _resolve_family_matches(self) -> CopilotResolveProposal:
        if not self.spec.action_id.startswith("resolve."):
            raise ValueError(
                "resolve proposal action id must belong to the resolve family"
            )
        return self


class CopilotReduceProposal(WireModel):
    kind: Literal["reduce"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _reduce_family_matches(self) -> CopilotReduceProposal:
        if not self.spec.action_id.startswith("reduce."):
            raise ValueError(
                "reduce proposal action id must belong to the reduce family"
            )
        return self


class CopilotMediaProposal(WireModel):
    kind: Literal["media"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _media_family_matches(self) -> CopilotMediaProposal:
        if self.spec.action_id not in _MEDIA_ACTION_KINDS:
            raise ValueError("media proposal action id must belong to the media family")
        return self


class CopilotEnrichProposal(WireModel):
    kind: Literal["enrich"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _enrich_family_matches(self) -> CopilotEnrichProposal:
        if self.spec.action_id not in _ENRICH_ACTION_KINDS:
            raise ValueError(
                "enrich proposal action id must belong to the enrich family"
            )
        return self


class CopilotWebProposal(WireModel):
    kind: Literal["web"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _web_family_matches(self) -> CopilotWebProposal:
        if self.spec.action_id not in _WEB_ACTION_KINDS:
            raise ValueError("web proposal action id must belong to the web family")
        return self


class CopilotResearchProposal(WireModel):
    kind: Literal["research"]
    title: str
    spec: CopilotRegisteredActionDraft

    @model_validator(mode="after")
    def _research_family_matches(self) -> CopilotResearchProposal:
        if not self.spec.action_id.startswith("research."):
            raise ValueError(
                "research proposal action id must belong to the research family"
            )
        return self


class CopilotProposal(
    RootModel[
        Union[
            CopilotMapProposal,
            CopilotResolveProposal,
            CopilotDeriveProposal,
            CopilotReduceProposal,
            CopilotMediaProposal,
            CopilotEnrichProposal,
            CopilotWebProposal,
            CopilotResearchProposal,
        ]
    ]
):
    """The closed runnable proposal-family union."""


# JSON-Schema half of the ``needs_import=True => proposals=[]`` invariant. A reply
# that has ``needs_import`` true AND at least one proposal matches this negated
# subschema, so the generated validator rejects it. Every top-level field is
# enumerated (with ``additionalProperties: false``) so the clause is a strict
# wire object; the non-discriminating fields are permissive (`true`).
_NEEDS_IMPORT_FORBIDS_PROPOSALS_NOT: dict[str, Any] = {
    "additionalProperties": False,
    "properties": {
        "schema_version": True,
        "reply": True,
        "cost_usd": True,
        "needs_import": {"const": True},
        "proposals": {"minItems": 1},
    },
}


class CopilotReply(WireModel):
    """The strict Copilot reply wire model."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        json_schema_extra={"not": _NEEDS_IMPORT_FORBIDS_PROPOSALS_NOT},
    )

    schema_version: Literal["frisket.copilot_reply.v1"] = COPILOT_REPLY_SCHEMA_VERSION
    reply: str
    proposals: list[CopilotProposal]
    needs_import: bool
    cost_usd: Annotated[float, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _needs_import_forbids_proposals(self) -> CopilotReply:
        if self.needs_import and self.proposals:
            raise ValueError("needs_import=True forbids proposals")
        return self


__all__ = [
    "COPILOT_REPLY_SCHEMA_VERSION",
    "COPILOT_UNSUPPORTED_ACTION_KINDS",
    "CopilotDeriveProposal",
    "CopilotMapProposal",
    "CopilotMediaProposal",
    "CopilotMessage",
    "CopilotProposal",
    "CopilotReduceProposal",
    "CopilotResolveProposal",
    "CopilotRegisteredActionDraft",
    "CopilotRegisteredSheetRowsScope",
    "CopilotReply",
    "CopilotResearchProposal",
    "CopilotRequest",
    "CopilotWebProposal",
]
