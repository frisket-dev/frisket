from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
    decode_logical_value,
)
from frisket.engine.store.value_codec import (
    PreparedContentRef,
    decode_stored_value,
    encode_stored_value,
)


def _artifact(project: Project, *, pages: int = 3) -> int:
    return int(
        record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            filename="source.pdf",
            page_count=pages,
        )["id"]
    )


def _pages(middle: str = "") -> list[PreparedPageDraft]:
    return [
        PreparedPageDraft(1, "Résumé 😀", positions=[{"start": 0, "end": 6}]),
        PreparedPageDraft(2, middle),
        PreparedPageDraft(3, "repeated repeated"),
    ]


def test_exact_document_and_page_refs_keep_outputs_independent(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "prepared.frisket", name="Prepared")
    try:
        artifact_id = _artifact(project)
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.ocr", commit=False)
        store = PreparedContentStore(project)
        a = store.stage_reference(
            source_artifact_id=artifact_id,
            producing_op_id=op_id,
            pages=_pages(),
        )
        page_two = store.stage_selector(
            set_id=store.resolve(a.ref_id).set_id, page_number=2
        )
        b = store.stage_reference(
            source_artifact_id=artifact_id,
            producing_op_id=op_id,
            pages=_pages("engine B"),
        )
        project.db.commit()

        assert store.resolve(a.ref_id).text == "Résumé 😀\n\n\n\nrepeated repeated"
        assert store.resolve(page_two.ref_id).text == ""
        assert (
            store.resolve(b.ref_id).text == "Résumé 😀\n\nengine B\n\nrepeated repeated"
        )
        assert store.resolve(a.ref_id).pins[0].positions == [{"end": 6, "start": 0}]
        assert [pin.start for pin in store.resolve(a.ref_id).pins] == [0, 10, 12]
        logical = project.db.execute(
            "SELECT value_kind,value FROM prepared_content_ref_values WHERE ref_id=?",
            (a.ref_id,),
        ).fetchone()
        assert tuple(logical) == ("text", store.resolve(a.ref_id).text)
        assert (
            decode_logical_value(project.db, "prepared_content_ref", a.ref_id)
            == store.resolve(a.ref_id).text
        )
        assert store.resolve_many([a.ref_id, page_two.ref_id, b.ref_id]).keys() == {
            a.ref_id,
            page_two.ref_id,
            b.ref_id,
        }
    finally:
        project.close()


def test_sparse_replacement_reuses_versions_and_artifact_delete_keeps_text(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "replace.frisket", name="Replace")
    try:
        artifact_id = _artifact(project)
        store = PreparedContentStore(project)
        project.db.execute("BEGIN IMMEDIATE")
        first_op = project.append_op("media.ocr", commit=False)
        first = store.stage_reference(
            source_artifact_id=artifact_id,
            producing_op_id=first_op,
            pages=_pages("old"),
        )
        second_op = project.append_op("edit", commit=False)
        second = store.stage_replacement(
            base_ref_id=first.ref_id,
            producing_op_id=second_op,
            replacements=[PreparedPageDraft(2, "new")],
        )
        project.db.commit()

        old = store.resolve(first.ref_id)
        new = store.resolve(second.ref_id)
        assert old.text == "Résumé 😀\n\nold\n\nrepeated repeated"
        assert new.text == "Résumé 😀\n\nnew\n\nrepeated repeated"
        assert [pin.version_id for pin in old.pins][::2] == [
            pin.version_id for pin in new.pins
        ][::2]
        assert old.pins[1].version_id != new.pins[1].version_id

        project.db.execute("DELETE FROM source_artifacts WHERE id=?", (artifact_id,))
        project.db.commit()
        retained = store.resolve(first.ref_id)
        assert retained.text == old.text
        assert retained.source_artifact_id is None
        assert (
            project.db.execute(
                "SELECT source_artifact_id FROM prepared_page_versions LIMIT 1"
            ).fetchone()[0]
            is None
        )
    finally:
        project.close()


def test_stage_is_caller_owned_atomic_and_rejects_incomplete_documents(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "atomic.frisket", name="Atomic")
    try:
        artifact_id = _artifact(project)
        store = PreparedContentStore(project)
        with pytest.raises(RuntimeError, match="caller transaction"):
            store.stage_reference(
                source_artifact_id=artifact_id,
                producing_op_id=1,
                pages=_pages(),
            )

        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.ocr", commit=False)
        with pytest.raises(ValueError, match="every artifact page"):
            store.stage_reference(
                source_artifact_id=artifact_id,
                producing_op_id=op_id,
                pages=_pages()[:-1],
            )
        assert (
            project.db.execute("SELECT COUNT(*) FROM prepared_content_sets").fetchone()[
                0
            ]
            == 0
        )
        project.db.rollback()
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
    finally:
        project.close()


def test_prepared_reference_codec_is_internal_and_strict() -> None:
    ref = PreparedContentRef(7)
    assert encode_stored_value(ref) == ("prepared_content_ref", 7)
    assert decode_stored_value("prepared_content_ref", 7) == ref
    with pytest.raises(ValueError, match="positive"):
        decode_stored_value("prepared_content_ref", 0)
    with pytest.raises(ValueError, match="positive"):
        PreparedContentRef(True)  # type: ignore[arg-type]


def test_sql_view_plan_stays_scoped_to_the_requested_reference(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "plan.frisket", name="Plan")
    try:
        artifact_id = _artifact(project, pages=1000)
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.ocr", commit=False)
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact_id,
            producing_op_id=op_id,
            pages=[PreparedPageDraft(number, "x" * 2048) for number in range(1, 1001)],
        )
        project.db.commit()
        plan = "\n".join(
            str(row[3])
            for row in project.db.execute(
                "EXPLAIN QUERY PLAN SELECT value FROM prepared_content_ref_values "
                "WHERE ref_id=?",
                (ref.ref_id,),
            )
        )
        assert "SEARCH ref USING INTEGER PRIMARY KEY" in plan
        assert "prepared_content_set_pages" not in plan or "SEARCH member" in plan
        assert len(PreparedContentStore(project).resolve(ref.ref_id).text) == (
            1000 * 2048 + 999 * 2
        )
    finally:
        project.close()
