"""Portable annotations and positioned text for visual document extraction."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Box(ExtractionModel):
    x0: float = Field(ge=0, le=1, allow_inf_nan=False)
    y0: float = Field(ge=0, le=1, allow_inf_nan=False)
    x1: float = Field(ge=0, le=1, allow_inf_nan=False)
    y1: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> "Box":
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("A region must have positive width and height")
        return self


class PageRegion(ExtractionModel):
    page: int = Field(ge=1)
    box: Box


class PositionedToken(ExtractionModel):
    text: str
    box: Box
    granularity: Literal["word", "line", "block"] = "word"


class PositionedPage(ExtractionModel):
    page: int = Field(ge=1)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)
    tokens: list[PositionedToken]


class PositionedDocument(ExtractionModel):
    source_fingerprint: str = Field(min_length=1)
    pages: list[PositionedPage]

    @model_validator(mode="after")
    def page_order(self) -> "PositionedDocument":
        numbers = [page.page for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("Document pages must be unique and ordered")
        return self


class ExtractionField(ExtractionModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    key: PageRegion
    value: PageRegion
    section_id: str | None = None


class PagePosition(ExtractionModel):
    page: int = Field(ge=1)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class PageSpan(ExtractionModel):
    """A continuous full-width band, possibly spanning several pages."""

    start: PagePosition
    end: PagePosition

    @model_validator(mode="after")
    def ordered(self) -> "PageSpan":
        if (self.start.page, self.start.y) >= (self.end.page, self.end.y):
            raise ValueError("A section span must end after it starts")
        return self


class RepeatedSection(ExtractionModel):
    id: str = Field(min_length=1)
    name: str = "Repeated section"
    first: PageSpan
    rest: PageSpan

    @model_validator(mode="after")
    def full_width(self) -> "RepeatedSection":
        if (self.rest.start.page, self.rest.start.y) < (
            self.first.end.page,
            self.first.end.y,
        ):
            raise ValueError("Remaining instances must follow the first instance")
        return self


class IgnoreBand(ExtractionModel):
    box: Box


class ExtractionTemplate(ExtractionModel):
    reference_blob_id: str = Field(min_length=1)
    reference_fingerprint: str = Field(min_length=1)
    fields: list[ExtractionField] = Field(min_length=1, max_length=200)
    sections: list[RepeatedSection] = Field(default_factory=list, max_length=20)
    ignore_bands: list[IgnoreBand] = Field(default_factory=list, max_length=20)
    expand_values: bool = False
    look_every_page: bool = True
    continue_across_pages: bool = False

    @model_validator(mode="after")
    def identities(self) -> "ExtractionTemplate":
        for values, label in (
            ([f.id for f in self.fields], "field ids"),
            ([f.name for f in self.fields], "field names"),
            ([s.id for s in self.sections], "section ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label}")
        section_ids = {section.id for section in self.sections}
        if any(
            field.section_id is not None and field.section_id not in section_ids
            for field in self.fields
        ):
            raise ValueError("Field references an unknown repeated section")
        return self


class ExtractedCell(ExtractionModel):
    text: str | None
    status: Literal["extracted", "empty", "not_found"]
    regions: list[PageRegion] = Field(default_factory=list)
    diagnostic: str | None = None


class ExtractedRecord(ExtractionModel):
    cells: dict[str, ExtractedCell]


class DocumentExtraction(ExtractionModel):
    records: list[ExtractedRecord]
    diagnostics: list[str] = Field(default_factory=list)
    outcome: Literal["extracted", "zero_records", "alignment_failed"]
