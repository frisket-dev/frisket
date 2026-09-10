"""Materialize reviewed cluster groups as typed entity rows."""

from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.entity_types import (
    ClusterReceiptReader,
    ClusterReceiptSource,
    EntityVariant,
)
from frisket.actions.types import ActionParams, TableError, TableResult, TableRow


class ResolveEntitiesParams(ActionParams):
    source: ClusterReceiptSource
    cluster_keys: list[StrictStr] | None = None
    canonical_overrides: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @field_validator("cluster_keys")
    @classmethod
    def _keys(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [key.strip() for key in value]
        if (
            not cleaned
            or any(not key for key in cleaned)
            or len(set(cleaned)) != len(cleaned)
        ):
            raise ValueError("cluster_keys must be nonblank and unique")
        return cleaned

    @field_validator("canonical_overrides")
    @classmethod
    def _overrides(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {key.strip(): canonical.strip() for key, canonical in value.items()}
        if len(cleaned) != len(value) or any(
            not key or not canonical for key, canonical in cleaned.items()
        ):
            raise ValueError(
                "canonical overrides must have unique nonblank keys and values"
            )
        return cleaned

    @model_validator(mode="after")
    def _override_scope(self):
        if self.cluster_keys is not None and set(self.canonical_overrides) - set(
            self.cluster_keys
        ):
            raise ValueError("canonical overrides must refer to selected clusters")
        return self


class EntityOutput(BaseModel):
    entity: str
    key: str
    variants: str
    mentions: int
    source_variants: list[EntityVariant]


def resolve_entities(
    params: ResolveEntitiesParams, clusters: ClusterReceiptReader
) -> TableResult[EntityOutput]:
    groups = clusters.read(params.source)
    by_key = {group.key: group for group in groups}
    selected = params.cluster_keys if params.cluster_keys is not None else list(by_key)
    if set(selected) - by_key.keys() or set(params.canonical_overrides) - set(selected):
        raise TableError(
            "invalid_input_ref",
            "Selected keys and overrides must identify source clusters",
        )
    return TableResult(
        rows=[
            TableRow(
                output=EntityOutput(
                    entity=params.canonical_overrides.get(key, by_key[key].canonical),
                    key=key,
                    variants=", ".join(
                        variant.value for variant in by_key[key].variants
                    ),
                    mentions=by_key[key].mentions,
                    source_variants=[
                        variant.model_copy(deep=True)
                        for variant in by_key[key].variants
                    ],
                ),
                sources=by_key[key].sources,
            )
            for key in selected
        ]
    )


RESOLVE_ENTITIES = action(
    name="entities",
    title="Resolve entities",
    description="Materialize selected cluster groups and their source variants as canonical entities.",
    category=ActionCategory.CLEANUP,
    run=create_sheet(resolve_entities),
    examples=(
        ResolveEntitiesParams(
            source=ClusterReceiptSource(
                kind="cluster_values", receipt_id="receipt:clusters"
            )
        ),
    ),
)
