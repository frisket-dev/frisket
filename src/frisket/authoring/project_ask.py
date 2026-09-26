"""Shared default model selection for persisted Project Ask turns."""

from __future__ import annotations

from frisket.ai.llm import ModelRouter

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from frisket.actions.core import CreateSheet, _ProjectAction
from frisket.actions.find_types import FindScanner
from frisket.actions.group_summary_types import GroupSummarizer
from frisket.actions.registry import ACTION_REGISTRY, COPILOT_ACTION_IDS
from frisket.actions.semantic_match_types import SemanticMatchReader
from frisket.actions.types import ProjectScope
from frisket.contracts.http.models import WireModel


PROJECT_ASK_MODEL = "anthropic/claude-sonnet-5"

_PROJECT_ASK_MODEL_BY_PROVIDER: tuple[tuple[str, str], ...] = (
    ("anthropic", PROJECT_ASK_MODEL),
    ("openai", "openai/gpt-5.6-terra"),
    ("gemini", "gemini/gemini-3.6-flash"),
)


def default_project_ask_model(router: ModelRouter) -> str:
    """Choose the configured provider's Project Ask default model."""

    configured = router.configured_keys()
    for provider, model in _PROJECT_ASK_MODEL_BY_PROVIDER:
        if provider in configured:
            return model
    return PROJECT_ASK_MODEL


# The ProjectAsk proposal wire contract is intentionally narrower than the action
# catalog. These actions require dedicated authoring/review surfaces that the
# current ProjectAsk product does not admit. Keep them runnable from the normal
# action surface, but do not advertise unsupported proposals.
PROJECT_ASK_UNSUPPORTED_ACTION_KINDS = frozenset(
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
    """Closed set of registered action ids in one ProjectAsk proposal family.

    The proposal discriminator (map|derive|reduce) must agree with the spec's
    ``action_id`` prefix. Each family enumerates the ProjectAsk-admitted
    ``<family>.*`` ids from the registry — no invented kinds,
    and no actions whose required nested parameters the wire model would drop.
    """

    return tuple(
        sorted(
            kind
            for kind in COPILOT_ACTION_IDS
            if kind.split(".", 1)[0] == family
            and kind not in PROJECT_ASK_UNSUPPORTED_ACTION_KINDS
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

PROJECT_ASK_ACTION_KINDS = frozenset(
    kind
    for kinds in (
        _MAP_ACTION_KINDS,
        _RESOLVE_ACTION_KINDS,
        _DERIVE_ACTION_KINDS,
        _REDUCE_ACTION_KINDS,
        _MEDIA_ACTION_KINDS,
        _ENRICH_ACTION_KINDS,
        _WEB_ACTION_KINDS,
        _family_kinds("research"),
    )
    for kind in kinds
)
PROJECT_ASK_CREATE_SHEET_KINDS = _REGISTERED_CREATE_SHEET_KINDS
PROJECT_ASK_ROW_CREATE_SHEET_KINDS = _REGISTERED_ROW_CREATE_SHEET_KINDS

RegisteredActionKind = Literal[_REGISTERED_ACTION_KINDS]  # type: ignore[valid-type]


class ProjectAskRegisteredSheetRowsScope(WireModel):
    """Request-level sheet scope saved without execution authorization."""

    kind: Literal["sheet_rows"]
    sheet_id: int = Field(gt=0)
    row_ids: list[int] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _unique_positive_rows(self) -> ProjectAskRegisteredSheetRowsScope:
        if self.row_ids is not None and (
            any(row_id <= 0 for row_id in self.row_ids)
            or len(self.row_ids) != len(set(self.row_ids))
        ):
            raise ValueError("row_ids must be positive and unique")
        return self


class ProjectAskRegisteredActionDraft(WireModel):
    """A keyless ActionRequest draft; execution adds authorization fields."""

    # Keep target admission in generated contracts as well as Python. Only
    # explicitly ProjectAsk-admitted table actions acquire the project variant.
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
    scope: ProjectScope | ProjectAskRegisteredSheetRowsScope = Field(
        discriminator="kind"
    )
    params: dict[str, JsonValue]
    output_names: dict[str, str] = Field(default_factory=dict)
    sheet_name: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _valid_output_names(self) -> ProjectAskRegisteredActionDraft:
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
            if not isinstance(self.scope, ProjectAskRegisteredSheetRowsScope):
                raise ValueError("row proposals require sheet_rows scope")
            if self.sheet_name is not None:
                raise ValueError("row proposals do not accept sheet_name")
        if any(
            not key or not name or key != key.strip() or name != name.strip()
            for key, name in self.output_names.items()
        ) or len(self.output_names.values()) != len(set(self.output_names.values())):
            raise ValueError("output names must be non-empty, trimmed, and unique")
        return self
