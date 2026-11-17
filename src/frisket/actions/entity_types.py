"""Typed cluster-receipt inputs and admitted entity groups."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field, StrictInt, StrictStr

from frisket.actions.types import ActionParams, RowSource
from frisket.contracts.actions.schemas._validators import nonblank


class ClusterReceiptSource(ActionParams):
    kind: Literal["cluster_values"]
    receipt_id: Annotated[StrictStr, nonblank("receipt_id must not be blank")] = Field(
        min_length=1
    )


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
