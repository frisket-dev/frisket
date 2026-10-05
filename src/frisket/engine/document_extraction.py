"""Deterministic, geometry-preserving extraction from annotated document text.

This module does no OCR, model inference, I/O or publication. A template learns
its anchors once, from its immutable reference. Targets cannot invent new keys.
"""

from dataclasses import dataclass
import re

from frisket.actions.document_extraction_types import (
    Box,
    DocumentExtraction,
    ExtractedCell,
    ExtractedRecord,
    ExtractionField,
    ExtractionTemplate,
    PageRegion,
    PageSpan,
    PositionedDocument,
    PositionedToken,
)


@dataclass(frozen=True)
class Anchor:
    id: str
    text: str
    region: PageRegion
    section_id: str | None


@dataclass(frozen=True)
class CompiledField:
    field: ExtractionField
    anchor: Anchor
    bottom: Anchor | None
    right: Anchor | None


@dataclass(frozen=True)
class AnchorContext:
    anchor: Anchor
    before: Anchor | None
    after: Anchor | None
    occurrence: int
    count: int
    total_count: int


@dataclass(frozen=True)
class CompiledTemplate:
    template: ExtractionTemplate
    fields: tuple[CompiledField, ...]
    anchors: tuple[Anchor, ...]
    section_limits: tuple[tuple[str, Anchor | None, Anchor | None], ...]
    document_context: tuple[AnchorContext, ...]


@dataclass(frozen=True)
class _Hit:
    region: PageRegion

    @property
    def start(self) -> float:
        return self.region.page - 1 + self.region.box.y0

    @property
    def end(self) -> float:
        return self.region.page - 1 + self.region.box.y1


def _normalized(text: str) -> str:
    return " ".join(text.casefold().strip().rstrip(":").split())


def _center_inside(inner: Box, outer: Box) -> bool:
    return (
        outer.x0 <= (inner.x0 + inner.x1) / 2 <= outer.x1
        and outer.y0 <= (inner.y0 + inner.y1) / 2 <= outer.y1
    )


def _overlap(a: Box, b: Box) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def _contains(outer: Box, inner: Box) -> bool:
    epsilon = 0.002
    return (
        outer.x0 - epsilon <= inner.x0
        and outer.y0 - epsilon <= inner.y0
        and outer.x1 + epsilon >= inner.x1
        and outer.y1 + epsilon >= inner.y1
    )


def _union(tokens: list[PositionedToken]) -> Box:
    return Box(
        x0=min(t.box.x0 for t in tokens),
        y0=min(t.box.y0 for t in tokens),
        x1=max(t.box.x1 for t in tokens),
        y1=max(t.box.y1 for t in tokens),
    )


def _tokens(
    document: PositionedDocument, page: int, template: ExtractionTemplate
) -> list[PositionedToken]:
    source = next((item for item in document.pages if item.page == page), None)
    if source is None:
        return []
    return [
        token
        for token in source.tokens
        if token.text.strip()
        and not any(
            _center_inside(token.box, band.box) for band in template.ignore_bands
        )
    ]


def _region_tokens(
    document: PositionedDocument, region: PageRegion, template: ExtractionTemplate
) -> tuple[list[PositionedToken], bool]:
    tokens = _tokens(document, region.page, template)
    coarse = any(
        token.granularity != "word"
        and _overlap(token.box, region.box)
        and not _contains(region.box, token.box)
        for token in tokens
    )
    return [token for token in tokens if _center_inside(token.box, region.box)], coarse


def _text(tokens: list[PositionedToken]) -> str:
    result: list[str] = []
    previous: PositionedToken | None = None
    for token in tokens:
        if previous is not None:
            same_line = (
                abs(token.box.y0 - previous.box.y0)
                < max(token.box.y1 - token.box.y0, previous.box.y1 - previous.box.y0)
                * 0.6
            )
            result.append(" " if same_line else "\n")
        result.append(token.text)
        previous = token
    return "".join(result).strip()


def _inside_span(region: PageRegion, span: PageSpan) -> bool:
    return (span.start.page, span.start.y) <= (region.page, region.box.y0) and (
        region.page,
        region.box.y1,
    ) <= (span.end.page, span.end.y)


def compile_template(
    template: ExtractionTemplate, reference: PositionedDocument
) -> CompiledTemplate:
    """Freeze selected keys and neighboring label boundaries from the reference."""
    if reference.source_fingerprint != template.reference_fingerprint:
        raise ValueError(
            "Reference document fingerprint does not match the extraction template"
        )
    pages = {page.page for page in reference.pages}
    sections = {section.id: section for section in template.sections}
    for section in template.sections:
        if any(
            position.page not in pages
            for span in (section.first, section.rest)
            for position in (span.start, span.end)
        ):
            raise ValueError("Repeated section references a missing page")
        if (
            not template.continue_across_pages
            and section.first.start.page != section.first.end.page
        ):
            raise ValueError(
                "Enable Continue across pages for a reference instance spanning pages"
            )
    anchors: list[Anchor] = []
    for field in template.fields:
        if field.key.page not in pages or field.value.page not in pages:
            raise ValueError(f"Field {field.name!r} references a missing page")
        if field.key.page != field.value.page:
            raise ValueError("Draw each example key and value on the same page")
        if field.section_id is not None:
            section = sections[field.section_id]
            if not _inside_span(field.key, section.first) or not _inside_span(
                field.value, section.first
            ):
                raise ValueError(
                    f"Field {field.name!r} must lie inside its first repeated instance"
                )
        tokens, coarse = _region_tokens(reference, field.key, template)
        if coarse:
            raise ValueError(
                f"The positioned text is too coarse to isolate key {field.name!r}"
            )
        if not tokens:
            raise ValueError(f"Key {field.name!r} contains no positioned text")
        anchors.append(
            Anchor(
                field.id,
                _normalized(_text(tokens)),
                PageRegion(page=field.key.page, box=_union(tokens)),
                field.section_id,
            )
        )
    # Only explicit colon-terminated reference labels qualify as unselected
    # boundaries. Never train on selected values or arbitrary target prose.
    for page in reference.pages:
        for index, token in enumerate(_tokens(reference, page.page, template)):
            if not re.fullmatch(r"[\w][\w \-/]{0,79}:", token.text.strip()):
                continue
            region = PageRegion(page=page.page, box=token.box)
            if any(_inside_span(region, section.rest) for section in template.sections):
                continue
            if any(
                field.key.page == page.page
                and _overlap(field.key.box, token.box)
                or field.value.page == page.page
                and _overlap(field.value.box, token.box)
                for field in template.fields
            ):
                continue
            section_id = next(
                (
                    section.id
                    for section in template.sections
                    if _inside_span(region, section.first)
                ),
                None,
            )
            anchors.append(
                Anchor(
                    f"boundary:{page.page}:{index}",
                    _normalized(token.text),
                    region,
                    section_id,
                )
            )
    compiled: list[CompiledField] = []
    for field, own in zip(
        template.fields, anchors[: len(template.fields)], strict=True
    ):
        candidates = [
            anchor
            for anchor in anchors
            if anchor.id != own.id and anchor.section_id == field.section_id
        ]
        value = field.value
        bottom = [
            anchor
            for anchor in candidates
            if (anchor.region.page, anchor.region.box.y0) >= (value.page, value.box.y1)
            and abs(anchor.region.box.x0 - own.region.box.x0) < 0.08
        ]
        right = [
            anchor
            for anchor in candidates
            if anchor.region.page == value.page
            and anchor.region.box.x0 >= value.box.x1
            and anchor.region.box.y0 < value.box.y1
            and anchor.region.box.y1 > value.box.y0
        ]
        compiled.append(
            CompiledField(
                field=field,
                anchor=own,
                bottom=min(
                    bottom,
                    key=lambda anchor: (anchor.region.page, anchor.region.box.y0),
                )
                if bottom
                else None,
                right=min(right, key=lambda anchor: anchor.region.box.x0)
                if right
                else None,
            )
        )
    section_limits = []
    for section in template.sections:
        group_fields = sorted(
            (field for field in compiled if field.field.section_id == section.id),
            key=lambda field: (
                field.anchor.region.page,
                field.anchor.region.box.y0,
                field.anchor.region.box.x0,
            ),
        )
        if not group_fields:
            raise ValueError("Each repeated section needs at least one annotated field")
        first = group_fields[0].anchor
        reference_hits = _find_hits(first, reference, template)
        if not any(_inside_span(hit.region, section.rest) for hit in reference_hits):
            raise ValueError(
                f"Remaining instances for {section.name!r} contain no matching starting key"
            )
        # A surrounding unique label identifies the group when two groups use
        # the same field names. Missing target guards are an alignment failure,
        # not permission to consume records from the neighboring group.
        unique = [
            anchor
            for anchor in anchors
            if anchor.section_id is None
            and len(_find_hits(anchor, reference, template)) == 1
        ]
        before = [
            anchor
            for anchor in unique
            if (anchor.region.page, anchor.region.box.y1)
            <= (section.first.start.page, section.first.start.y)
        ]
        after = [
            anchor
            for anchor in unique
            if (anchor.region.page, anchor.region.box.y0)
            >= (section.rest.end.page, section.rest.end.y)
        ]
        outside = [
            hit
            for hit in reference_hits
            if not _inside_span(hit.region, section.first)
            and not _inside_span(hit.region, section.rest)
        ]
        needs_lower = any(
            (hit.region.page, hit.region.box.y0)
            < (section.first.start.page, section.first.start.y)
            for hit in outside
        )
        needs_upper = any(
            (hit.region.page, hit.region.box.y0)
            >= (section.rest.end.page, section.rest.end.y)
            for hit in outside
        )
        lower = (
            max(before, key=lambda anchor: (anchor.region.page, anchor.region.box.y1))
            if before and needs_lower
            else None
        )
        upper = (
            min(after, key=lambda anchor: (anchor.region.page, anchor.region.box.y0))
            if after and needs_upper
            else None
        )
        if any(
            (
                lower is None
                or (hit.region.page, hit.region.box.y0)
                >= (lower.region.page, lower.region.box.y1)
            )
            and (
                upper is None
                or (hit.region.page, hit.region.box.y0)
                < (upper.region.page, upper.region.box.y0)
            )
            for hit in outside
        ):
            raise ValueError(
                "Repeated starting key also occurs outside its annotated section without distinguishing labels"
            )
        section_limits.append((section.id, lower, upper))
    document_anchors = [anchor for anchor in anchors if anchor.section_id is None]
    reference_hits = {
        anchor.id: _find_hits(anchor, reference, template)
        for anchor in document_anchors
    }
    context = []
    for anchor in document_anchors:
        neighbors = [
            other
            for other in document_anchors
            if other.id != anchor.id
            and len(reference_hits[other.id]) == 1
            and abs(other.region.box.x0 - anchor.region.box.x0) < 0.08
        ]
        previous = [
            other
            for other in neighbors
            if (other.region.page, other.region.box.y1)
            <= (anchor.region.page, anchor.region.box.y0)
        ]
        following = [
            other
            for other in neighbors
            if (other.region.page, other.region.box.y0)
            >= (anchor.region.page, anchor.region.box.y1)
        ]
        before = (
            max(previous, key=lambda item: (item.region.page, item.region.box.y1))
            if previous
            else None
        )
        after = (
            min(following, key=lambda item: (item.region.page, item.region.box.y0))
            if following
            else None
        )
        lower = _Hit(before.region).end if before else -1
        upper = _Hit(after.region).start if after else float("inf")
        candidates = [
            hit for hit in reference_hits[anchor.id] if lower <= hit.start < upper
        ]
        occurrence = next(
            (
                index
                for index, hit in enumerate(candidates)
                if hit.region == anchor.region
            ),
            None,
        )
        if occurrence is None:
            raise ValueError(
                f"Reference key {anchor.text!r} cannot be matched in its annotated context"
            )
        context.append(
            AnchorContext(
                anchor,
                before,
                after,
                occurrence,
                len(candidates),
                len(reference_hits[anchor.id]),
            )
        )
    return CompiledTemplate(
        template, tuple(compiled), tuple(anchors), tuple(section_limits), tuple(context)
    )


def _find_hits(
    anchor: Anchor, document: PositionedDocument, template: ExtractionTemplate
) -> list[_Hit]:
    hits: list[_Hit] = []
    for page in document.pages:
        if not template.look_every_page and page.page != anchor.region.page:
            continue
        tokens = _tokens(document, page.page, template)
        for index, token in enumerate(tokens):
            if abs(token.box.x0 - anchor.region.box.x0) > 0.08:
                continue
            chosen: list[PositionedToken] = []
            for candidate in tokens[index : index + 30]:
                max_height = max(
                    (anchor.region.box.y1 - anchor.region.box.y0) * 2, 0.08
                )
                if chosen and candidate.box.y0 - token.box.y0 > max_height:
                    break
                text = _normalized(" ".join(item.text for item in [*chosen, candidate]))
                if not anchor.text.startswith(text):
                    # Reading order can interleave an inline value before the
                    # second line of its label. Skip only the separate right
                    # lane, never arbitrary words inside the label's lane.
                    if chosen and candidate.box.x0 > anchor.region.box.x1 + 0.04:
                        continue
                    break
                if (
                    chosen
                    and candidate.box.y0 > chosen[-1].box.y1 + 0.004
                    and abs(candidate.box.x0 - anchor.region.box.x0) > 0.08
                ):
                    break
                chosen.append(candidate)
                if text == anchor.text:
                    hits.append(_Hit(PageRegion(page=page.page, box=_union(chosen))))
                    break
    return sorted(hits, key=lambda hit: (hit.start, hit.region.box.x0))


def _missing(message: str) -> ExtractedCell:
    return ExtractedCell(text=None, status="not_found", diagnostic=message)


def _regions(
    document: PositionedDocument,
    template: ExtractionTemplate,
    left: float,
    right: float,
    start: float,
    end: float,
) -> list[PageRegion]:
    regions: list[PageRegion] = []
    if left < 0 or right > 1 or left >= right or start >= end:
        return regions
    for page in document.pages:
        low, high = max(0, start - (page.page - 1)), min(1, end - (page.page - 1))
        if low >= high:
            continue
        parts = [(low, high)]
        for band in template.ignore_bands:
            if band.box.x0 > left or band.box.x1 < right:
                continue
            parts = [
                interval
                for a, b in parts
                for interval in ((a, min(b, band.box.y0)), (max(a, band.box.y1), b))
                if interval[0] < interval[1]
            ]
        regions.extend(
            PageRegion(page=page.page, box=Box(x0=left, x1=right, y0=a, y1=b))
            for a, b in parts
        )
    return regions


def _cell(
    compiled: CompiledTemplate,
    field: CompiledField,
    document: PositionedDocument,
    matched: dict[str, _Hit],
    bounds: tuple[float, float] | None = None,
) -> ExtractedCell:
    hit = matched.get(field.anchor.id)
    if hit is None:
        return _missing("Key was not found unambiguously")
    reference, value = field.anchor.region.box, field.field.value.box
    dx, dy = hit.region.box.x0 - reference.x0, hit.region.box.y0 - reference.y0
    left, right = value.x0 + dx, value.x1 + dx
    start = hit.region.page - 1 + value.y0 + dy
    end = hit.region.page - 1 + value.y1 + dy
    if compiled.template.expand_values:
        for boundary, edge in ((field.bottom, "bottom"), (field.right, "right")):
            if boundary is None:
                continue
            boundary_hit = matched.get(boundary.id)
            if boundary_hit is None:
                return _missing(
                    f"Expected {edge} boundary {boundary.text!r} was not found unambiguously"
                )
            if edge == "bottom":
                end = boundary_hit.start
            else:
                if boundary_hit.region.page != hit.region.page:
                    return _missing("Right boundary moved to another page")
                right = boundary_hit.region.box.x0
        if bounds is not None and field.bottom is None:
            end = bounds[1]
    if bounds is not None:
        if not compiled.template.expand_values and (
            start < bounds[0] - 0.002 or end > bounds[1] + 0.002
        ):
            return _missing(
                "Fixed value region extends outside its matched repeated record"
            )
        start, end = max(start, bounds[0]), min(end, bounds[1])
    if not compiled.template.continue_across_pages and (
        start < hit.region.page - 1 or end > hit.region.page
    ):
        return _missing("Value continues across a page; enable Continue across pages")
    regions = _regions(document, compiled.template, left, right, start, end)
    if not regions:
        return _missing(
            "Matched value region is outside the page or has inconsistent boundaries"
        )
    pieces: list[str] = []
    for region in regions:
        tokens, coarse = _region_tokens(document, region, compiled.template)
        if coarse:
            return _missing("Positioned text is too coarse to isolate the value region")
        text = _text(tokens)
        if text:
            pieces.append(text)
    text = "\n".join(pieces)
    return ExtractedCell(
        text=text, status="extracted" if text else "empty", regions=regions
    )


def _unique(
    hits: dict[str, list[_Hit]],
    start: float = -1,
    end: float = float("inf"),
    page: int | None = None,
) -> dict[str, _Hit]:
    result: dict[str, _Hit] = {}
    for identity, options in hits.items():
        candidates = [
            hit
            for hit in options
            if start <= hit.start < end and (page is None or hit.region.page == page)
        ]
        if len(candidates) == 1:
            result[identity] = candidates[0]
    return result


def _document_matches(
    compiled: CompiledTemplate, hits: dict[str, list[_Hit]]
) -> dict[str, _Hit]:
    result: dict[str, _Hit] = {}
    for context in compiled.document_context:
        before, after = context.before, context.after
        options = hits[context.anchor.id]
        # A unique label remains useful even when an unrelated neighboring
        # field is missing. Duplicate labels require their learned brackets.
        if context.total_count == 1 and len(options) == 1:
            result[context.anchor.id] = options[0]
            continue
        if any(
            anchor is not None and len(hits[anchor.id]) != 1
            for anchor in (before, after)
        ):
            continue
        lower = hits[before.id][0].end if before else -1
        upper = hits[after.id][0].start if after else float("inf")
        candidates = [hit for hit in options if lower <= hit.start < upper]
        if len(candidates) == context.count:
            result[context.anchor.id] = candidates[context.occurrence]
    # Verify neighborhood ordering rather than independently selecting each
    # label's ordinal. Reordered form blocks must not swap named outputs.
    invalid: set[str] = set()
    anchors = [
        item.anchor for item in compiled.document_context if item.anchor.id in result
    ]
    for index, left in enumerate(anchors):
        for right in anchors[index + 1 :]:
            if abs(left.region.box.x0 - right.region.box.x0) >= 0.08:
                continue
            source_order = _Hit(left.region).start - _Hit(right.region).start
            target_order = result[left.id].start - result[right.id].start
            if source_order * target_order < 0:
                invalid.update((left.id, right.id))
    return {
        identity: hit for identity, hit in result.items() if identity not in invalid
    }


def extract_document(
    compiled: CompiledTemplate,
    document: PositionedDocument,
    repeat_group_id: str | None = None,
) -> DocumentExtraction:
    """Return one document's records without mutating inputs or inventing text."""
    template = compiled.template
    if repeat_group_id is not None and repeat_group_id not in {
        section.id for section in template.sections
    }:
        raise ValueError("Unknown repeated section")
    hits = {
        anchor.id: _find_hits(anchor, document, template) for anchor in compiled.anchors
    }
    document_fields = [
        field for field in compiled.fields if field.field.section_id is None
    ]
    document_matches = _document_matches(compiled, hits)
    document_cells = {
        field.field.id: _cell(compiled, field, document, document_matches)
        for field in document_fields
    }
    if repeat_group_id is None:
        if not document_fields:
            raise ValueError("Choose a repeated section to extract its fields")
        cells = list(document_cells.values())
        outcome = (
            "alignment_failed"
            if all(cell.status == "not_found" for cell in cells)
            else "extracted"
        )
        return DocumentExtraction(
            records=[ExtractedRecord(cells=document_cells)], outcome=outcome
        )
    fields = sorted(
        (
            field
            for field in compiled.fields
            if field.field.section_id == repeat_group_id
        ),
        key=lambda field: (
            field.anchor.region.page,
            field.anchor.region.box.y0,
            field.anchor.region.box.x0,
        ),
    )
    if not fields:
        raise ValueError("Repeated section has no annotated fields")
    section = next(
        section for section in template.sections if section.id == repeat_group_id
    )
    first = fields[0]
    _, before, after = next(
        item for item in compiled.section_limits if item[0] == repeat_group_id
    )
    if any(
        anchor is not None and len(hits[anchor.id]) != 1 for anchor in (before, after)
    ):
        return DocumentExtraction(
            records=[],
            outcome="alignment_failed",
            diagnostics=[
                "Repeated section's surrounding labels could not be matched unambiguously"
            ],
        )
    scope_start = hits[before.id][0].end if before is not None else -1
    scope_end = hits[after.id][0].start if after is not None else float("inf")
    if scope_start >= scope_end:
        return DocumentExtraction(
            records=[],
            outcome="alignment_failed",
            diagnostics=["Repeated section's surrounding labels are out of order"],
        )
    hits = {
        identity: [hit for hit in values if scope_start <= hit.start < scope_end]
        for identity, values in hits.items()
    }
    starts = hits[first.anchor.id]
    if not starts:
        partial = any(hits[field.anchor.id] for field in fields[1:])
        return DocumentExtraction(
            records=[],
            outcome="alignment_failed" if partial else "zero_records",
            diagnostics=["Repeated section start is missing, but later keys were found"]
            if partial
            else [],
        )
    group_anchors = {
        anchor.id for anchor in compiled.anchors if anchor.section_id == repeat_group_id
    }
    group_hits = {
        identity: values
        for identity, values in hits.items()
        if identity in group_anchors
    }
    records: list[ExtractedRecord] = []
    for index, start_hit in enumerate(starts):
        next_hit = starts[index + 1] if index + 1 < len(starts) else None
        end = next_hit.start if next_hit else float("inf")
        page = None if template.continue_across_pages else start_hit.region.page
        matches = _unique(group_hits, start_hit.start, end, page)
        # More than one instance of a non-start key between starts cannot be
        # assigned safely. Withhold this group instead of merging records.
        ambiguous = any(
            len(
                [
                    hit
                    for hit in group_hits[field.anchor.id]
                    if start_hit.start <= hit.start < end
                    and (page is None or hit.region.page == page)
                ]
            )
            > 1
            for field in fields[1:]
        )
        if ambiguous:
            return DocumentExtraction(
                records=[],
                outcome="alignment_failed",
                diagnostics=[
                    "Repeated section boundaries are ambiguous; no rows were guessed"
                ],
            )
        offset = (
            first.anchor.region.page
            - section.first.start.page
            + first.anchor.region.box.y0
            - section.first.start.y
        )
        lower = start_hit.start - offset
        if next_hit is not None and (
            template.continue_across_pages
            or next_hit.region.page == start_hit.region.page
        ):
            upper = next_hit.start - offset
        else:
            last_matched = next(
                (field for field in reversed(fields) if field.anchor.id in matches),
                first,
            )
            last_hit = matches.get(last_matched.anchor.id, start_hit)
            tail = (
                section.first.end.page
                - last_matched.anchor.region.page
                + section.first.end.y
                - last_matched.anchor.region.box.y0
            )
            upper = last_hit.start + tail
        upper = min(upper, scope_end)
        cells = dict(document_cells)
        cells.update(
            {
                field.field.id: _cell(
                    compiled, field, document, matches, (lower, upper)
                )
                for field in fields
            }
        )
        records.append(ExtractedRecord(cells=cells))
    return DocumentExtraction(records=records, outcome="extracted")
