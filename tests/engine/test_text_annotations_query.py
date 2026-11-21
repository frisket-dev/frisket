"""The inverse query resolve_text_annotations and the
annotated_text_column_ids availability signal.

Evidence is constructed directly through the store writers (not the full map.ner
run) so the query logic — guard triple, current-ref intersection, content-hash
positioning, UTF-16 conversion, DTO shape — is exercised in isolation.
"""

from __future__ import annotations

from typing import Any

from frisket.engine.store.citations import (
    _text_hash,
    record_citation,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from frisket.engine.store.text_annotations import (
    annotated_text_column_ids,
    resolve_text_annotations,
)
from frisket.engine.store.transactions import write_transaction


def _seed(project, *, body_text: str = "Ada Lovelace"):
    sheet = project.add_sheet("data")
    body = project.add_column(sheet, "body", type="text")
    ents = project.add_column(sheet, "entities", type="json")
    project.add_rows(
        sheet,
        [{"body": body_text, "entities": [{"text": "x"}]}],
        {"body": body, "entities": ents},
    )
    row_id = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (sheet,)
    ).fetchone()["id"]
    return sheet, body, ents, row_id


def _entities_ref(project, sheet, ents, row_id) -> dict[str, Any]:
    _v, refs = project.get_values_with_refs(sheet, ents, row_ids=[row_id])
    return refs[row_id]


def _write_layer(
    project,
    *,
    sheet,
    body,
    ents,
    row_id,
    text,
    entities,
    content_hash=None,
    subject_ref=None,
):
    """Write one NER-shaped annotation layer: a cell surface + spans + an
    annotation link whose subject is the (current, by default) entities cell."""
    surface_hash = content_hash or _text_hash(text)
    with write_transaction(project.db):
        surf = record_text_surface(
            project,
            surface_kind="cell",
            content_hash=surface_hash,
            offset_unit="unicode_codepoint",
            text_sheet_id=sheet,
            text_row_id=row_id,
            text_column_id=body,
        )
    with write_transaction(project.db):
        art = record_source_artifact(
            project, artifact_kind="text", media_type="text/plain"
        )
    spans = []
    for start, end, etype in entities:
        with write_transaction(project.db):
            s = record_source_span(
                project,
                artifact_id=art["id"],
                span_kind="text",
                char_start=start,
                char_end=end,
                quote=text[start:end],
                text_layer_hash=surface_hash,
                text_surface_id=surf["id"],
                metadata={"entity_type": etype},
            )
        spans.append(s)
    with write_transaction(project.db):
        return record_citation(
            project,
            subject_kind="cell",
            subject_ref=subject_ref
            if subject_ref is not None
            else _entities_ref(project, sheet, ents, row_id),
            spans=[
                {"span_id": s["id"], "rank": i, "span_role": "annotation"}
                for i, s in enumerate(spans)
            ],
            sheet_id=sheet,
            row_id=row_id,
            column_id=ents,
            layer_family="entities",
            producer={"kind": "map.ner", "engine": "gliner"},
        )


def test_positioned_layer(make_project):
    p = make_project("q")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 12, "person")],
    )
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert res["content_hash"] == _text_hash("Ada Lovelace")
    assert res["offset_unit"] == "utf16_code_unit"
    assert res["text"] == "Ada Lovelace"
    assert len(res["layers"]) == 1
    layer = res["layers"][0]
    assert layer["positioned"] is True
    assert layer["layer_family"] == "entities"
    assert layer["producer"] == {"kind": "map.ner", "engine": "gliner"}
    assert layer["output_column"]["name"] == "entities"
    assert layer["counts"] == {"shown": 1, "total": 1, "invalid": 0}
    span = layer["spans"][0]
    assert (span["start"], span["end"]) == (0, 12)
    assert span["quote"] == "Ada Lovelace"
    assert span["metadata"]["entity_type"] == "person"
    # occurrence_id is the stable (span_id, link_id) pair.
    sid, lid = span["occurrence_id"].split(":")
    assert int(sid) > 0 and int(lid) > 0


def test_content_hash_mismatch_is_unpositioned(make_project):
    p = make_project("m")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    # Surface captured against different text -> hash mismatches current cell.
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 12, "person")],
        content_hash=_text_hash("a stale different string"),
    )
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert len(res["layers"]) == 1
    layer = res["layers"][0]
    assert layer["positioned"] is False
    assert layer["unpositioned"]["reason"] == "content_hash_mismatch"
    assert layer["unpositioned"]["total"] == 1
    assert "spans" not in layer  # no content-identifying fields


def test_emoji_offsets_convert_to_utf16(make_project):
    p = make_project("e")
    text = "\U0001f600 Ada"
    sheet, body, ents, row_id = _seed(p, body_text=text)
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text=text,
        entities=[(2, 5, "person")],
    )
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    span = res["layers"][0]["spans"][0]
    # code points (2,5) -> UTF-16 (3,6) because the emoji is 2 code units.
    assert (span["start"], span["end"]) == (3, 6)
    assert span["quote"] == "Ada"


def test_superseded_link_excluded(make_project):
    p = make_project("s")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    # Current link.
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 12, "person")],
    )
    # A prior run's link whose subject ref no longer matches the output cell.
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 3, "person")],
        subject_ref={"kind": "run_result", "run_id": 999, "stale": True},
    )
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    # Only the current link's layer survives.
    assert len(res["layers"]) == 1
    assert res["layers"][0]["counts"]["shown"] == 1


def test_hidden_sheet_or_column_returns_empty(make_project):
    p = make_project("h")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 12, "person")],
    )
    p.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (body,))
    p.db.commit()
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert res["layers"] == [] and res["text"] is None
    p.db.execute("UPDATE columns SET hidden=0 WHERE id=?", (body,))
    p.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet,))
    p.db.commit()
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert res["layers"] == []


def test_surfaceless_and_non_annotation_spans_excluded(make_project):
    p = make_project("x")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    with write_transaction(p.db):
        art = record_source_artifact(p, artifact_kind="text", media_type="text/plain")
    # A span WITH a surface at the cell but reached through a SUPPORT link (no
    # annotation role, no family) must NOT appear — the reader gates on
    # span_role='annotation' AND a non-null layer_family.
    with write_transaction(p.db):
        surf = record_text_surface(
            p,
            surface_kind="cell",
            content_hash=_text_hash("Ada Lovelace"),
            offset_unit="unicode_codepoint",
            text_sheet_id=sheet,
            text_row_id=row_id,
            text_column_id=body,
        )
    with write_transaction(p.db):
        span = record_source_span(
            p,
            artifact_id=art["id"],
            span_kind="text",
            char_start=0,
            char_end=12,
            quote="Ada Lovelace",
            text_layer_hash=_text_hash("Ada Lovelace"),
            text_surface_id=surf["id"],
        )
    with write_transaction(p.db):
        record_citation(
            p,
            subject_kind="cell",
            subject_ref=_entities_ref(p, sheet, ents, row_id),
            spans=[{"span_id": span["id"], "rank": 0}],  # support role, no family
            sheet_id=sheet,
            row_id=row_id,
            column_id=ents,
        )
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert res["layers"] == []


def test_unsupported_offset_unit_is_unpositioned(make_project):
    p = make_project("u")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    with write_transaction(p.db):
        surf = record_text_surface(
            p,
            surface_kind="cell",
            content_hash=_text_hash("Ada Lovelace"),
            offset_unit="utf16_code_unit",  # no v1 producer writes this
            text_sheet_id=sheet,
            text_row_id=row_id,
            text_column_id=body,
        )
    with write_transaction(p.db):
        art = record_source_artifact(p, artifact_kind="text", media_type="text/plain")
    with write_transaction(p.db):
        span = record_source_span(
            p,
            artifact_id=art["id"],
            span_kind="text",
            char_start=0,
            char_end=12,
            quote="Ada Lovelace",
            text_layer_hash=_text_hash("Ada Lovelace"),
            text_surface_id=surf["id"],
            metadata={"entity_type": "person"},
        )
    with write_transaction(p.db):
        record_citation(
            p,
            subject_kind="cell",
            subject_ref=_entities_ref(p, sheet, ents, row_id),
            spans=[{"span_id": span["id"], "rank": 0, "span_role": "annotation"}],
            sheet_id=sheet,
            row_id=row_id,
            column_id=ents,
            layer_family="entities",
        )
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert res["layers"][0]["positioned"] is False
    assert res["layers"][0]["unpositioned"]["reason"] == "unsupported_offset_unit"


def test_corrupt_geometry_with_matching_hash_is_counted_not_drawn(make_project):
    """A span whose offsets are out of range for the current text, on a surface
    whose hash still matches, is counted invalid — never drawn."""
    p = make_project("c")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    text = "Ada Lovelace"
    with write_transaction(p.db):
        surf = record_text_surface(
            p,
            surface_kind="cell",
            content_hash=_text_hash(text),
            offset_unit="unicode_codepoint",
            text_sheet_id=sheet,
            text_row_id=row_id,
            text_column_id=body,
        )
    with write_transaction(p.db):
        art = record_source_artifact(p, artifact_kind="text", media_type="text/plain")
    with write_transaction(p.db):
        good = record_source_span(
            p,
            artifact_id=art["id"],
            span_kind="text",
            char_start=0,
            char_end=3,
            quote="Ada",
            text_layer_hash=_text_hash(text),
            text_surface_id=surf["id"],
            metadata={"entity_type": "person"},
        )
    with write_transaction(p.db):
        bad = record_source_span(
            p,
            artifact_id=art["id"],
            span_kind="text",
            char_start=0,
            char_end=999,
            quote="Ada",
            text_layer_hash=_text_hash(text),
            text_surface_id=surf["id"],
            metadata={"entity_type": "person"},
        )
    with write_transaction(p.db):
        record_citation(
            p,
            subject_kind="cell",
            subject_ref=_entities_ref(p, sheet, ents, row_id),
            spans=[
                {"span_id": good["id"], "rank": 0, "span_role": "annotation"},
                {"span_id": bad["id"], "rank": 1, "span_role": "annotation"},
            ],
            sheet_id=sheet,
            row_id=row_id,
            column_id=ents,
            layer_family="entities",
        )
    layer = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)[
        "layers"
    ][0]
    assert layer["positioned"] is True
    assert layer["counts"] == {"shown": 1, "total": 2, "invalid": 1}
    assert len(layer["spans"]) == 1 and layer["spans"][0]["quote"] == "Ada"


def test_all_corrupt_geometry_is_unpositioned(make_project):
    p = make_project("all-corrupt")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 999, "person")],
    )
    layer = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)[
        "layers"
    ][0]
    assert layer == {
        "toggle_key": f"{sheet}:{ents}:entities",
        "layer_family": "entities",
        "producer": {"kind": "map.ner", "engine": "gliner"},
        "output_column": {"id": ents, "name": "entities"},
        "positioned": False,
        "unpositioned": {"reason": "invalid_geometry", "total": 1},
    }


def test_hidden_output_row_excludes_layer(make_project):
    """The output/subject cell's row visibility is guarded independently of the
    rendered source cell."""
    p = make_project("o")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 12, "person")],
    )
    # Hide the row: get_values drops it, so the output cell is not current.
    p.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
    p.db.commit()
    res = resolve_text_annotations(p, sheet_id=sheet, row_id=row_id, column_id=body)
    assert res["layers"] == [] and res["text"] is None


def test_annotated_text_column_ids(make_project):
    p = make_project("a")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    assert annotated_text_column_ids(p, sheet) == []
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 12, "person")],
    )
    assert annotated_text_column_ids(p, sheet) == [body]
    # The entities OUTPUT column is never itself an annotated text column.
    assert ents not in annotated_text_column_ids(p, sheet)


def test_annotated_text_column_ids_excludes_superseded_output(make_project):
    p = make_project("availability")
    sheet, body, ents, row_id = _seed(p, body_text="Ada Lovelace")
    _write_layer(
        p,
        sheet=sheet,
        body=body,
        ents=ents,
        row_id=row_id,
        text="Ada Lovelace",
        entities=[(0, 3, "person")],
        subject_ref={"kind": "run_result", "run_id": 999},
    )
    assert annotated_text_column_ids(p, sheet) == []


def test_cross_sheet_locator_is_not_visible(make_project):
    p = make_project("ownership")
    sheet, body, _ents, row_id = _seed(p, body_text="Ada Lovelace")
    other = p.add_sheet("other")
    other_column = p.add_column(other, "body", type="text")
    assert (
        resolve_text_annotations(
            p, sheet_id=sheet, row_id=row_id, column_id=other_column
        )["layers"]
        == []
    )
    assert (
        resolve_text_annotations(
            p, sheet_id=sheet, row_id=row_id, column_id=other_column
        )["text"]
        is None
    )
    assert body != other_column
