"""Shared default model selection and keyless drafts for Project Ask turns."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.model_catalog import MODEL_ENTRIES
from frisket.contracts.http.models import WireModel
from frisket.execution.provider import ExecutionComposition
from frisket.execution.resolver import preferred_resolved_engine


PROJECT_ASK_MODEL = "anthropic/claude-sonnet-5"

_PROJECT_ASK_MODEL_BY_PROVIDER: tuple[tuple[str, str], ...] = (
    ("anthropic", PROJECT_ASK_MODEL),
    ("openai", "openai/gpt-5.6-terra"),
    ("gemini", "gemini/gemini-3.6-flash"),
)


def project_ask_model_candidates() -> tuple[str, ...]:
    """Return every catalog Ask model in stable product-preference order."""
    preferred = [model for _provider, model in _PROJECT_ASK_MODEL_BY_PROVIDER]
    catalog = [
        f"{provider}/{entry['id']}"
        for provider, entries in MODEL_ENTRIES.items()
        for entry in entries
    ]
    return tuple(dict.fromkeys([*preferred, *catalog]))


def default_project_ask_model(
    router: ModelRouter, composition: ExecutionComposition | None = None
) -> str:
    """Choose Project Ask's default through the request's execution policy."""

    if composition is not None:
        selected = preferred_resolved_engine(
            project_ask_model_candidates(),
            composition.provider_for_resolution(),
            composition.facts,
            options={"tools": True},
        )
        return selected or PROJECT_ASK_MODEL

    configured = router.configured_keys()
    for provider, model in _PROJECT_ASK_MODEL_BY_PROVIDER:
        if provider in configured:
            return model
    return PROJECT_ASK_MODEL


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


class ProjectAskRegisteredProjectScope(WireModel):
    """Explicit project scope at the action-authoring wire boundary."""

    kind: Literal["project"]


class ProjectAskRegisteredActionDraft(WireModel):
    """A keyless, execution-neutral ``ActionRequest`` draft.

    Catalog membership and each action's scope, parameter, and output rules are
    project facts. They are deliberately checked by the project-aware binder,
    rather than by a generated static union of builtin action ids.
    """

    action_id: str = Field(min_length=1)
    scope: ProjectAskRegisteredProjectScope | ProjectAskRegisteredSheetRowsScope = (
        Field(discriminator="kind")
    )
    params: dict[str, JsonValue]
    output_names: dict[str, str] = Field(default_factory=dict)
    replace_existing: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
    )
    sheet_name: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _valid_output_names(self) -> ProjectAskRegisteredActionDraft:
        if any(
            not key or not name or key != key.strip() or name != name.strip()
            for key, name in self.output_names.items()
        ) or len(self.output_names.values()) != len(set(self.output_names.values())):
            raise ValueError("output names must be non-empty, trimmed, and unique")
        if self.sheet_name is not None:
            self.sheet_name = self.sheet_name.strip()
            if not self.sheet_name:
                raise ValueError("sheet_name must be non-empty")
        return self
