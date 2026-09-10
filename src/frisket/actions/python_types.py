"""The narrow Python evaluator and closed row-output routing values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field, JsonValue, field_validator

from frisket.actions.types import ActionParams
from frisket.contracts.actions.schemas._base import EvidenceRetention


class PythonEvaluator(Protocol):
    async def evaluate(self, *, code: str, row: dict[str, JsonValue]) -> JsonValue: ...


class ColumnTarget(ActionParams):
    kind: Literal["column"]
    type: str = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _registered_type(cls, value: str) -> str:
        from frisket.contracts.action import _is_v1_column_type, canonical_column_type

        if not _is_v1_column_type(value):
            raise ValueError("invalid_column_type")
        return canonical_column_type(value)


class NamedResultTarget(ActionParams):
    kind: Literal["named_result"]
    schema_name: str = Field(alias="schema", min_length=1)
    may_feed: list[str] = Field(default_factory=list)


class ReceiptEvidenceTarget(ActionParams):
    kind: Literal["receipt_evidence"]
    retention: EvidenceRetention = "compactable"


class PythonOutputRoute(ActionParams):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    target: ColumnTarget | NamedResultTarget | ReceiptEvidenceTarget = Field(
        discriminator="kind"
    )

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if value != "$" and not value.startswith("$."):
            raise ValueError("invalid_output_route")
        return value


@dataclass(frozen=True)
class RoutedOutput:
    """Params-derived projection; the host alone supplies materialization facts."""

    route: PythonOutputRoute
    schema: dict[str, Any]

    def materialized_name(self, logical: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in logical)
        if self.route.target.kind == "named_result":
            return f"__result_{safe}"
        if self.route.target.kind == "receipt_evidence":
            return f"__evidence_{safe}"
        return logical
