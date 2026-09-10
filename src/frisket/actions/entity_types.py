"""Typed cluster-receipt inputs and admitted entity groups."""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, StrictInt, StrictStr, field_validator

from frisket.actions.types import ActionParams, RowSource


class ClusterReceiptSource(ActionParams):
    kind: Literal["cluster_values"]
    receipt_id: StrictStr = Field(min_length=1)

    @field_validator("receipt_id")
    @classmethod
    def _receipt_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("receipt_id must not be blank")
        return value.strip()


class EntityVariant(ActionParams):
    value: StrictStr
    count: StrictInt = Field(ge=0)
    row_ids: tuple[StrictInt, ...]


@dataclass(frozen=True)
class EntityCluster:
    key: str
    canonical: str
    variants: tuple[EntityVariant, ...]
    mentions: int
    sources: tuple[RowSource, ...]


class ClusterReceiptReader(Protocol):
    def read(self, source: ClusterReceiptSource) -> tuple[EntityCluster, ...]: ...
