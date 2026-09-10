"""Whole-column clustering options and the admitted canonicalization capability."""

from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator

from frisket.actions.types import ActionParams, ColumnRef
from frisket.authoring.templates import validate_key_template


class ClusterColumn(ColumnRef[str | None]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("text", "category", "link")


class ClusterReview(ActionParams):
    source_hash: StrictStr | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    canonical_overrides: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    excluded_members: dict[StrictStr, list[StrictStr]] = Field(default_factory=dict)

    @field_validator("canonical_overrides")
    @classmethod
    def valid_overrides(cls, values: dict[str, str]) -> dict[str, str]:
        cleaned = {key.strip(): value.strip() for key, value in values.items()}
        if len(cleaned) != len(values) or any(
            not key or not value for key, value in cleaned.items()
        ):
            raise ValueError("Canonical overrides need unique nonblank keys and values")
        return cleaned

    @field_validator("excluded_members")
    @classmethod
    def valid_exclusions(cls, values: dict[str, list[str]]) -> dict[str, list[str]]:
        cleaned = {
            key.strip(): [member.strip() for member in members]
            for key, members in values.items()
        }
        if len(cleaned) != len(values) or any(
            not key
            or any(not member for member in members)
            or len(set(members)) != len(members)
            for key, members in cleaned.items()
        ):
            raise ValueError("Excluded members need unique nonblank keys and surfaces")
        return cleaned


class ClusterOptions(ActionParams):
    method: Literal["fingerprint", "ngram_fingerprint", "semantic"] = "fingerprint"
    min_size: int = Field(default=2, ge=2, strict=True)
    threshold: float | None = Field(default=None, ge=0, le=1)
    ngram_size: int | None = Field(default=None, ge=1, le=6, strict=True)
    key_template: StrictStr | None = None
    review: ClusterReview | None = None

    @field_validator("key_template")
    @classmethod
    def valid_key_template(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        validate_key_template(value)
        return value

    @model_validator(mode="after")
    def valid_method_options(self):
        if self.method != "semantic" and self.threshold is not None:
            raise ValueError("threshold is only valid for semantic clustering")
        if self.method != "ngram_fingerprint" and self.ngram_size is not None:
            raise ValueError("ngram_size is only valid for n-gram clustering")
        return self


@dataclass(frozen=True, eq=False)
class PreparedClustering:
    """An invocation-owned source and options plan, with no embeddings run yet."""

    row_count: int


class ClusteredValues(BaseModel):
    row_count: int
    group_count: int


class ValueClusterer(Protocol):
    def prepare(
        self, source: ClusterColumn, *, options: ClusterOptions
    ) -> PreparedClustering:
        """Bind a source and review without embeddings or output publication."""
        ...
