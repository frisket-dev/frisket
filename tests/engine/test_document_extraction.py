"""Visual extraction matches layout, without guessing or repairing source text."""

import pytest

from frisket.actions.document_extraction_types import (
    Box,
    ExtractionField,
    ExtractionTemplate,
    IgnoreBand,
    PagePosition,
    PageRegion,
    PageSpan,
    PositionedDocument,
    PositionedPage,
    PositionedToken,
    RepeatedSection,
)
from frisket.engine.document_extraction import compile_template, extract_document


def box(x0=0.1, y0=0.1, x1=0.3, y1=0.13):
    return Box(x0=x0, y0=y0, x1=x1, y1=y1)


def region(y=0.1, x0=0.1, x1=0.3, height=0.03, page=1):
    return PageRegion(page=page, box=box(x0, y, x1, y + height))


def token(text, y=0.1, x0=0.1, x1=None, granularity="word"):
    return PositionedToken(
        text=text,
        box=box(x0, y, x1 if x1 is not None else x0 + 0.18, y + 0.02),
        granularity=granularity,
    )


def document(*pages, fingerprint="reference"):
    return PositionedDocument(
        source_fingerprint=fingerprint,
        pages=[
            PositionedPage(page=index + 1, width=600, height=800, tokens=list(tokens))
            for index, tokens in enumerate(pages)
        ],
    )


def field(name="ARRESTED", y=0.1, section=None, value=None, page=1):
    return ExtractionField(
        id=name,
        name=name,
        key=region(y, page=page),
        value=value or region(y, x0=0.35, x1=0.6, page=page),
        section_id=section,
    )


def value_only(name="AMOUNT", *, value=None, page=1):
    return ExtractionField(
        id=name,
        name=name,
        kind="value_only",
        value=value or region(0.2, x0=0.35, x1=0.6, page=page),
    )


def template(*fields, **kwargs):
    return ExtractionTemplate(
        reference_blob_id="blob-reference",
        reference_fingerprint="reference",
        fields=list(fields),
        **kwargs,
    )


def span(y0, y1, start_page=1, end_page=1):
    return PageSpan(
        start=PagePosition(page=start_page, y=y0), end=PagePosition(page=end_page, y=y1)
    )


def repeat_template(expand=False, continuation=False):
    return template(
        field("Name", 0.1, "people"),
        field("Age", 0.2, "people"),
        sections=[
            RepeatedSection(id="people", first=span(0.08, 0.25), rest=span(0.3, 0.95))
        ],
        expand_values=expand,
        continue_across_pages=continuation,
    )


def repeat_reference():
    return document(
        [token("Name"), token("Age", 0.2), token("Name", 0.4), token("Age", 0.5)]
    )


@pytest.mark.parametrize(
    "value,expected,status", [("X", "X", "extracted"), (None, "", "empty")]
)
def test_fixed_literal_and_blank_keep_region(value, expected, status):
    reference = document([token("ARRESTED"), token("X", x0=0.35, x1=0.38)])
    compiled = compile_template(template(field()), reference)
    target = document(
        [token("ARRESTED", 0.4)]
        + ([token(value, 0.4, x0=0.35, x1=0.38)] if value else [])
    )
    cell = extract_document(compiled, target).records[0].cells["ARRESTED"]
    assert (cell.text, cell.status) == (expected, status)
    assert cell.regions[0].box.y0 == pytest.approx(0.4)


def test_missing_key_produces_zero_records_not_a_blank_record():
    compiled = compile_template(template(field()), document([token("ARRESTED")]))
    result = extract_document(compiled, document([token("UNRELATED")]))
    assert result.outcome == "zero_records"
    assert result.records == []


def test_value_only_field_reads_its_fixed_page_and_region_once():
    item = value_only(page=2)
    reference = document([], [token("reference", 0.2, x0=0.35, x1=0.5)])
    target = document(
        [token("wrong page", 0.2, x0=0.35, x1=0.5)],
        [token("right page", 0.2, x0=0.35, x1=0.5)],
    )

    cell = (
        extract_document(
            compile_template(
                template(item, look_every_page=True, expand_values=True), reference
            ),
            target,
        )
        .records[0]
        .cells[item.id]
    )

    assert (cell.text, cell.status) == ("right page", "extracted")
    assert cell.regions == [item.value]


def test_value_only_field_preserves_empty_region_and_ignore_bands():
    item = value_only(value=region(0.1, x0=0.35, x1=0.6, height=0.2))
    annotation = template(
        item,
        ignore_bands=[IgnoreBand(box=box(0, 0.15, 1, 0.2))],
    )

    cell = (
        extract_document(compile_template(annotation, document([])), document([]))
        .records[0]
        .cells[item.id]
    )

    assert (cell.text, cell.status) == ("", "empty")
    assert len(cell.regions) == 2
    assert (cell.regions[0].box.y0, cell.regions[0].box.y1) == pytest.approx(
        (0.1, 0.15)
    )
    assert (cell.regions[1].box.y0, cell.regions[1].box.y1) == pytest.approx((0.2, 0.3))


def test_value_only_field_missing_page_produces_zero_records():
    item = value_only(page=2)
    compiled = compile_template(template(item), document([], []))

    result = extract_document(compiled, document([]))

    assert result.outcome == "zero_records"
    assert result.records == []
    assert result.diagnostics == ["Fixed value page 2 is unavailable"]


def test_value_only_field_refuses_coarse_partial_text():
    item = value_only()
    compiled = compile_template(template(item), document([]))

    result = extract_document(
        compiled,
        document([token("wide line", 0.2, x0=0.35, x1=0.9, granularity="line")]),
    )

    assert result.outcome == "zero_records"
    assert result.diagnostics == [
        "Positioned text is too coarse to isolate the value region"
    ]


def test_document_value_only_field_is_reused_for_each_repeated_record():
    header = value_only("Case", value=region(0.02, x0=0.35, x1=0.6))
    annotation = repeat_template().model_copy(
        update={"fields": [header, *repeat_template().fields]}
    )
    reference = document(
        [
            token("A-1", 0.02, x0=0.35),
            token("Name"),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
        ]
    )
    target = document(
        [
            token("B-2", 0.02, x0=0.35),
            token("Name"),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
        ]
    )

    result = extract_document(compile_template(annotation, reference), target, "people")

    assert [record.cells[header.id].text for record in result.records] == [
        "B-2",
        "B-2",
    ]


def test_value_only_field_contract_defaults_legacy_and_rejects_invalid_combinations():
    assert field().kind == "key_value"
    assert value_only().key is None
    with pytest.raises(ValueError, match="Key/value fields require a key region"):
        ExtractionField(id="bad", name="Bad", value=region())
    with pytest.raises(ValueError, match="Value-only fields cannot have a key region"):
        ExtractionField(
            id="bad",
            name="Bad",
            kind="value_only",
            key=region(),
            value=region(),
        )
    with pytest.raises(ValueError, match="Value-only fields cannot belong"):
        ExtractionField(
            id="bad",
            name="Bad",
            kind="value_only",
            value=region(),
            section_id="people",
        )


def test_reference_fingerprint_is_checked():
    with pytest.raises(ValueError, match="fingerprint"):
        compile_template(
            template(field()), document([token("ARRESTED")], fingerprint="changed")
        )


def test_drawn_key_padding_does_not_shift_value():
    annotated = field().model_copy(update={"key": region(0.09, x0=0.09, height=0.05)})
    reference = document([token("ARRESTED"), token("X", x0=0.35, x1=0.38)])
    cell = (
        extract_document(compile_template(template(annotated), reference), reference)
        .records[0]
        .cells["ARRESTED"]
    )
    assert cell.text == "X"
    assert cell.regions[0].box.x0 == 0.35


def test_expanding_value_ends_at_next_key_not_reference_gap():
    description = field("Description", value=region(0.14, x1=0.8, height=0.025))
    end = field("ARRESTED", 0.3)
    reference = document(
        [token("Description"), token("short", 0.14), token("ARRESTED", 0.3)]
    )
    target = document(
        [
            token("Description"),
            token("long", 0.14),
            token("continuation", 0.25),
            token("last", 0.38),
            token("ARRESTED", 0.42),
            token("X", 0.42, x0=0.35),
        ]
    )
    compiled = compile_template(
        template(description, end, expand_values=True), reference
    )
    cell = extract_document(compiled, target).records[0].cells["Description"]
    assert cell.text == "long\ncontinuation\nlast"
    assert cell.regions[0].box.y1 == pytest.approx(0.42)


def test_unselected_reference_label_can_bound_expansion():
    item = field("Description", value=region(0.14, x1=0.8))
    reference = document(
        [token("Description"), token("short", 0.14), token("STOP:", 0.3)]
    )
    target = document(
        [
            token("Description"),
            token("one", 0.14),
            token("two", 0.3),
            token("STOP:", 0.4),
            token("unrelated", 0.5),
        ]
    )
    cell = (
        extract_document(
            compile_template(template(item, expand_values=True), reference), target
        )
        .records[0]
        .cells["Description"]
    )
    assert cell.text == "one\ntwo"


def test_annotated_values_are_never_learned_as_boundary_keys():
    item = field("Description", value=region(0.14, x1=0.8, height=0.12))
    reference = document(
        [token("Description"), token("STOP:", 0.14), token("content", 0.2)]
    )
    compiled = compile_template(template(item, expand_values=True), reference)
    assert len(compiled.anchors) == 1
    assert (
        extract_document(compiled, reference).records[0].cells["Description"].text
        == "STOP:\ncontent"
    )


def test_missing_expected_boundary_does_not_choose_a_later_key():
    item = field("Description", value=region(0.14, x1=0.8))
    reference = document(
        [
            token("Description"),
            token("short", 0.14),
            token("STOP:", 0.3),
            token("LATER:", 0.5),
        ]
    )
    target = document([token("Description"), token("one", 0.14), token("LATER:", 0.5)])
    result = extract_document(
        compile_template(template(item, expand_values=True), reference), target
    )
    assert result.outcome == "zero_records"
    assert result.records == []
    assert any("stop" in diagnostic for diagnostic in result.diagnostics)


def test_right_neighbor_bounds_name():
    name = field("NAME", value=region(0.1, x0=0.3, x1=0.5))
    age = ExtractionField(
        id="AGE",
        name="AGE",
        key=region(0.1, x0=0.6, x1=0.7),
        value=region(0.1, x0=0.75, x1=0.8),
    )
    reference = document(
        [
            token("NAME", x1=0.2),
            token("Ann", x0=0.3, x1=0.4),
            token("AGE", x0=0.6, x1=0.7),
            token("32", x0=0.75, x1=0.79),
        ]
    )
    cell = (
        extract_document(
            compile_template(template(name, age, expand_values=True), reference),
            reference,
        )
        .records[0]
        .cells["NAME"]
    )
    assert cell.text == "Ann"
    assert cell.regions[0].box.x1 == 0.6


def test_repeat_variable_heights_blank_values_and_local_missing_field():
    reference = repeat_reference()
    target = document(
        [
            token("Name", 0.1),
            token("Ann", 0.1, x0=0.35),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.65),
            token("Name", 0.8),
            token("Jo", 0.8, x0=0.35),
        ]
    )
    result = extract_document(
        compile_template(repeat_template(), reference), target, "people"
    )
    assert len(result.records) == 3
    assert [row.cells["Name"].text for row in result.records] == ["Ann", "", "Jo"]
    assert result.records[1].cells["Age"].status == "empty"
    assert result.records[2].cells["Age"].status == "not_found"


def test_zero_repeats_has_no_synthetic_document_row():
    compiled = compile_template(repeat_template(), repeat_reference())
    result = extract_document(compiled, document([token("No entries")]), "people")
    assert result.records == []
    assert result.outcome == "zero_records"


def test_repeated_keys_without_record_starts_yield_zero_records_with_diagnostic():
    compiled = compile_template(repeat_template(), repeat_reference())
    result = extract_document(compiled, document([token("Age")]), "people")
    assert result.records == []
    assert result.outcome == "zero_records"
    assert result.diagnostics == [
        "Repeated section start is missing, but later keys were found"
    ]


def test_ambiguous_record_segmentation_withholds_group():
    compiled = compile_template(repeat_template(), repeat_reference())
    result = extract_document(
        compiled,
        document([token("Name"), token("Age", 0.2), token("Age", 0.4)]),
        "people",
    )
    assert result.records == []
    assert result.outcome == "zero_records"
    assert result.diagnostics == [
        "Repeated section boundaries are ambiguous; no rows were guessed"
    ]


def test_records_on_separate_pages_work_without_continuation():
    compiled = compile_template(repeat_template(), repeat_reference())
    result = extract_document(
        compiled,
        document(
            [token("Name"), token("Age", 0.2)], [token("Name"), token("Age", 0.2)]
        ),
        "people",
    )
    assert len(result.records) == 2
    assert result.records[1].cells["Age"].regions[0].page == 2


@pytest.mark.parametrize("continuation", [False, True])
def test_cross_page_expansion_is_explicit_and_has_page_local_citations(continuation):
    item = field("Description", value=region(0.72, x1=0.8), y=0.68)
    end = field("ARRESTED", y=0.85)
    reference = document(
        [token("Description", 0.68), token("short", 0.72), token("ARRESTED", 0.85)]
    )
    target = document(
        [token("Description", 0.68), token("first", 0.72), token("footer", 0.96)],
        [token("header", 0.01), token("second", 0.1), token("ARRESTED", 0.2)],
    )
    annotation = template(
        item,
        end,
        expand_values=True,
        continue_across_pages=continuation,
        ignore_bands=[
            IgnoreBand(box=box(0, 0, 1, 0.06)),
            IgnoreBand(box=box(0, 0.94, 1, 1)),
        ],
    )
    cell = (
        extract_document(compile_template(annotation, reference), target)
        .records[0]
        .cells["Description"]
    )
    if continuation:
        assert cell.text == "first\nsecond"
        assert [region.page for region in cell.regions] == [1, 2]
        assert cell.regions[0].box.y1 == 0.94
        assert cell.regions[1].box.y0 == 0.06
    else:
        assert cell.text == "first"
        assert cell.status == "extracted"
        assert "page boundary" in cell.diagnostic
        assert [region.page for region in cell.regions] == [1]


def test_coarse_line_is_not_split_into_invented_words():
    reference = document([token("ARRESTED X", x1=0.6, granularity="line")])
    with pytest.raises(ValueError, match="too coarse"):
        compile_template(template(field()), reference)


def test_whole_line_value_can_be_extracted_but_partial_line_warns():
    reference = document([token("ARRESTED")])
    compiled = compile_template(template(field()), reference)
    target = document(
        [token("ARRESTED"), token("literal text", x0=0.35, x1=0.55, granularity="line")]
    )
    assert (
        extract_document(compiled, target).records[0].cells["ARRESTED"].text
        == "literal text"
    )
    target = document(
        [
            token("ARRESTED"),
            token("long text outside", x0=0.35, x1=0.9, granularity="line"),
        ]
    )
    result = extract_document(compiled, target)
    assert result.outcome == "zero_records"
    assert result.records == []
    assert result.diagnostics == [
        "Positioned text is too coarse to isolate the value region"
    ]


def test_field_identity_and_output_name_are_unique():
    with pytest.raises(ValueError, match="Duplicate field"):
        template(field(), field())


def test_ignoring_headers_prevents_duplicate_key_ambiguity():
    reference = document([token("ARRESTED", 0.01), token("ARRESTED")])
    annotation = template(field(), ignore_bands=[IgnoreBand(box=box(0, 0, 1, 0.06))])
    result = extract_document(compile_template(annotation, reference), reference)
    assert result.records[0].cells["ARRESTED"].status == "empty"


def test_unknown_repeat_group_is_rejected():
    compiled = compile_template(template(field()), document([token("ARRESTED")]))
    with pytest.raises(ValueError, match="Unknown repeated"):
        extract_document(compiled, document([]), "not-there")


def test_reference_rest_band_must_demonstrate_repetition():
    with pytest.raises(ValueError, match="Remaining instances"):
        compile_template(
            repeat_template(), document([token("Name"), token("Age", 0.2)])
        )


@pytest.mark.parametrize("continuation", [False, True])
def test_a_record_continuing_on_the_next_page_respects_toggle(continuation):
    compiled = compile_template(
        repeat_template(continuation=continuation), repeat_reference()
    )
    target = document(
        [token("Name", 0.8), token("Ann", 0.8, x0=0.35)],
        [
            token("Age", 0.1),
            token("42", 0.1, x0=0.35),
            token("Name", 0.4),
            token("Age", 0.5),
        ],
    )
    result = extract_document(compiled, target, "people")
    assert len(result.records) == 2
    assert result.records[0].cells["Name"].text == "Ann"
    if continuation:
        assert result.records[0].cells["Age"].text == "42"
        assert result.records[0].cells["Age"].regions[0].page == 2
    else:
        assert result.records[0].cells["Age"].status == "not_found"


def test_document_fields_are_copied_onto_repeated_rows():
    annotation = repeat_template().model_copy(
        update={"fields": [field("Case", 0.03), *repeat_template().fields]}
    )
    source = document(
        [
            token("Case", 0.03),
            token("123", 0.03, x0=0.35),
            *repeat_reference().pages[0].tokens,
        ]
    )
    result = extract_document(compile_template(annotation, source), source, "people")
    assert len(result.records) == 2
    assert [record.cells["Case"].text for record in result.records] == ["123", "123"]
    assert (
        result.records[0].cells["Case"].regions
        == result.records[1].cells["Case"].regions
    )
    missing_document_key = extract_document(
        compile_template(annotation, source), repeat_reference(), "people"
    )
    assert len(missing_document_key.records) == 2
    assert all(
        record.cells["Case"].status == "not_found"
        for record in missing_document_key.records
    )


def test_same_named_fields_in_two_groups_do_not_cross_contaminate():
    annotation = template(
        field("Name", 0.1, "people"),
        field("Age", 0.2, "people"),
        sections=[
            RepeatedSection(id="people", first=span(0.08, 0.25), rest=span(0.3, 0.6))
        ],
    )
    source = document(
        [
            token("PEOPLE:", 0.04),
            token("Name"),
            token("Ann", x0=0.35),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
            token("OTHER:", 0.65),
            token("Name", 0.75),
            token("Age", 0.85),
        ]
    )
    result = extract_document(compile_template(annotation, source), source, "people")
    assert len(result.records) == 2
    changed = document(
        [
            token("PEOPLE:", 0.04),
            token("Name"),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
            token("Name", 0.75),
            token("Age", 0.85),
        ]
    )
    result = extract_document(compile_template(annotation, source), changed, "people")
    assert result.outcome == "zero_records"
    assert result.diagnostics == [
        "Repeated section's surrounding labels could not be matched unambiguously"
    ]
    assert result.records == []


def test_expansion_does_not_use_a_neighbor_in_another_horizontal_lane():
    item = field("Description", value=region(0.14, x1=0.5))
    source = document(
        [
            token("Description"),
            token("short", 0.14),
            token("OTHER:", 0.25, x0=0.65),
            token("STOP:", 0.4),
        ]
    )
    target = document(
        [
            token("Description"),
            token("one", 0.14),
            token("OTHER:", 0.25, x0=0.65),
            token("two", 0.3),
            token("STOP:", 0.4),
        ]
    )
    cell = (
        extract_document(
            compile_template(template(item, expand_values=True), source), target
        )
        .records[0]
        .cells["Description"]
    )
    assert cell.text == "one\ntwo"


def test_look_every_page_false_only_searches_reference_page():
    source = document([token("ARRESTED")])
    annotation = template(field(), look_every_page=False)
    result = extract_document(
        compile_template(annotation, source),
        document([], [token("ARRESTED"), token("X", x0=0.35)]),
    )
    assert result.outcome == "zero_records"
    assert result.records == []


def test_multiline_key_matches_wrapped_and_single_line_labels():
    item = field("Runway length").model_copy(update={"key": region(0.1, height=0.08)})
    source = document(
        [token("Runway", 0.1), token("1200", 0.1, x0=0.35), token("length:", 0.14)]
    )
    compiled = compile_template(template(item), source)
    assert extract_document(compiled, source).records[0].cells[item.id].text == "1200"
    target = document([token("Runway length:", 0.25), token("1800", 0.25, x0=0.35)])
    assert extract_document(compiled, target).records[0].cells[item.id].text == "1800"


def test_duplicate_document_keys_use_surrounding_headings_not_nearest_coordinates():
    person, officer = field("PersonName", 0.1), field("OfficerName", 0.4)
    source = document(
        [
            token("Person:", 0.04),
            token("Name", 0.1),
            token("Ann", 0.1, x0=0.35),
            token("Officer:", 0.34),
            token("Name", 0.4),
            token("Bob", 0.4, x0=0.35),
        ]
    )
    compiled = compile_template(template(person, officer), source)
    reference_result = extract_document(compiled, source).records[0]
    assert [reference_result.cells[key].text for key in (person.id, officer.id)] == [
        "Ann",
        "Bob",
    ]
    target = document(
        [
            token("Person:", 0.2),
            token("Name", 0.3),
            token("Carol", 0.3, x0=0.35),
            token("Officer:", 0.65),
            token("Name", 0.8),
            token("David", 0.8, x0=0.35),
        ]
    )
    row = extract_document(compiled, target).records[0]
    assert [row.cells[key].text for key in (person.id, officer.id)] == [
        "Carol",
        "David",
    ]
    missing = document(
        [
            token("Person:", 0.2),
            token("Officer:", 0.65),
            token("Name", 0.8),
            token("David", 0.8, x0=0.35),
        ]
    )
    row = extract_document(compiled, missing).records[0]
    assert row.cells[person.id].status == "not_found"
    assert row.cells[officer.id].text == "David"


def test_duplicate_document_keys_with_extra_occurrences_warn_instead_of_guessing():
    person, officer = field("PersonName", 0.1), field("OfficerName", 0.4)
    source = document(
        [
            token("Person:", 0.04),
            token("Name", 0.1),
            token("Officer:", 0.34),
            token("Name", 0.4),
        ]
    )
    target = document(
        [
            token("Person:", 0.04),
            token("Name", 0.1),
            token("Name", 0.2),
            token("Officer:", 0.34),
            token("Name", 0.4),
        ]
    )
    row = extract_document(
        compile_template(template(person, officer), source), target
    ).records[0]
    assert row.cells[person.id].status == "not_found"
    assert row.cells[officer.id].status == "empty"


def test_repeated_reference_needs_a_guard_on_the_side_with_other_records():
    annotation = template(
        field("Name", 0.1, "people"),
        field("Age", 0.2, "people"),
        sections=[
            RepeatedSection(id="people", first=span(0.08, 0.25), rest=span(0.3, 0.6))
        ],
    )
    source = document(
        [
            token("PEOPLE:", 0.04),
            token("Name"),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
            token("Name", 0.75),
            token("Age", 0.85),
        ]
    )
    with pytest.raises(ValueError, match="outside its annotated section"):
        compile_template(annotation, source)


def test_partially_intersected_words_warn_but_unselected_whole_lines_do_not():
    source = document([token("ARRESTED")])
    compiled = compile_template(template(field()), source)
    crossing = document([token("ARRESTED"), token("literal", x0=0.59, x1=0.7)])
    cell = extract_document(compiled, crossing).records[0].cells["ARRESTED"]
    assert cell.text == ""
    assert "intersects a word" in cell.diagnostic
    separate = document([token("ARRESTED"), token("outside", 0.2, x0=0.35)])
    cell = extract_document(compiled, separate).records[0].cells["ARRESTED"]
    assert cell.text == ""
    assert cell.diagnostic is None


def test_multiword_unselected_boundary_starts_at_its_first_word():
    item = field("Description", value=region(0.14, x1=0.8))
    source = document(
        [
            token("Description"),
            token("short", 0.14),
            token("Incident", 0.3, x1=0.22),
            token("Date:", 0.3, x0=0.24, x1=0.32),
        ]
    )
    target = document(
        [
            token("Description"),
            token("first", 0.14),
            token("last", 0.35),
            token("Incident", 0.4, x1=0.22),
            token("Date:", 0.4, x0=0.24, x1=0.32),
        ]
    )
    compiled = compile_template(template(item, expand_values=True), source)
    assert compiled.fields[0].bottom.text == "incident date"
    cell = extract_document(compiled, target).records[0].cells[item.id]
    assert cell.text == "first\nlast"
    assert cell.regions[0].box.y1 == 0.4


def test_unselected_colon_boundary_does_not_match_an_ordinary_value_word():
    item = field("Description", value=region(0.14, x1=0.8))
    source = document([token("Description"), token("short", 0.14), token("STOP:", 0.3)])
    target = document(
        [
            token("Description"),
            token("first", 0.14),
            token("STOP", 0.2),
            token("last", 0.3),
            token("STOP:", 0.4),
        ]
    )
    cell = (
        extract_document(
            compile_template(template(item, expand_values=True), source), target
        )
        .records[0]
        .cells[item.id]
    )
    assert cell.text == "first\nSTOP\nlast"


def test_expanded_document_field_does_not_absorb_repeated_records_or_disable_footer():
    annotation = template(
        field("Summary", 0.03, value=region(0.05, x0=0.1, x1=0.8, height=0.025)),
        field("Name", 0.2, "people"),
        field("Footer", 0.75, value=region(0.78, x0=0.1, x1=0.8, height=0.025)),
        sections=[
            RepeatedSection(id="people", first=span(0.18, 0.3), rest=span(0.4, 0.65))
        ],
        expand_values=True,
    )
    source = document(
        [
            token("Summary", 0.03),
            token("intro", 0.05),
            token("Name", 0.2),
            token("Ann", 0.2, x0=0.35),
            token("Name", 0.5),
            token("Bob", 0.5, x0=0.35),
            token("Footer", 0.75),
            token("foot", 0.78),
            token("END:", 0.9),
        ]
    )
    result = extract_document(compile_template(annotation, source), source, "people")
    assert len(result.records) == 2
    assert all(row.cells["Summary"].text == "intro" for row in result.records)
    assert all(row.cells["Footer"].text == "foot" for row in result.records)


def test_adjacent_value_regions_do_not_duplicate_a_word_on_the_seam():
    first = field("First", 0.05, value=region(0.125, x0=0.1, x1=0.8, height=0.125))
    second = field("Second", 0.6, value=region(0.25, x0=0.1, x1=0.8, height=0.125))
    source = document([token("First", 0.05), token("seam", 0.24), token("Second", 0.6)])
    result = extract_document(
        compile_template(template(first, second), source), source
    ).records[0]
    assert [result.cells[key].text for key in ("First", "Second")] == ["", "seam"]


def test_after_guard_does_not_admit_records_before_the_annotated_group():
    annotation = template(
        field("Name", 0.3, "people"),
        field("Age", 0.4, "people"),
        sections=[
            RepeatedSection(id="people", first=span(0.28, 0.45), rest=span(0.5, 0.8))
        ],
    )
    source = document(
        [
            token("Name", 0.03),
            token("Age", 0.13),
            token("Name", 0.3),
            token("Age", 0.4),
            token("Name", 0.6),
            token("Age", 0.7),
            token("AFTER:", 0.9),
        ]
    )
    with pytest.raises(ValueError, match="outside its annotated section"):
        compile_template(annotation, source)


def test_final_repeated_value_expands_to_known_closing_anchor_not_reference_tail():
    annotation = template(
        field("Name", 0.1, "people"),
        field("Age", 0.2, "people"),
        sections=[
            RepeatedSection(id="people", first=span(0.08, 0.28), rest=span(0.4, 0.75))
        ],
        expand_values=True,
    )
    source = document(
        [
            token("Name"),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
            token("END:", 0.8),
        ]
    )
    target = document(
        [
            token("Name"),
            token("Age", 0.2),
            token("Name", 0.4),
            token("Age", 0.5),
            token("first", 0.5, x0=0.35),
            token("last", 0.65, x0=0.35),
            token("END:", 0.85),
        ]
    )
    compiled = compile_template(annotation, source)
    result = extract_document(compiled, target, "people")
    assert len(result.records) == 2
    assert result.records[-1].cells["Age"].text == "first\nlast"
    assert result.records[-1].cells["Age"].regions[0].box.y1 == 0.85
    missing = document(target.pages[0].tokens[:-1])
    result = extract_document(compiled, missing, "people")
    assert result.records[-1].cells["Age"].status == "not_found"
    assert (
        "Final repeated section boundary" in result.records[-1].cells["Age"].diagnostic
    )
