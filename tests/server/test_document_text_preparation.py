from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.document_text import document_text_role
from frisket.server.services.search_preparation import SearchPreparationService


def _source(project: Project, *, name: str = "Document", column_type: str = "file"):
    sheet_id = project.add_sheet("Documents")
    source_column_id = project.add_column(sheet_id, name, type=column_type)
    return sheet_id, source_column_id


def _body(
    project: Project,
    *,
    sheet_id: int,
    source_column_id: int,
    source_name: str,
    kind: str,
    force_ocr: bool = False,
):
    row = SimpleNamespace(id=7, sheet_id=sheet_id, force_ocr=force_ocr)
    return SearchPreparationService._action_body(
        object(),
        project,
        row,
        kind,
        source_name,
        [{"row_id": 11, "column_id": source_column_id}],
        f"{kind}:fixture",
    )


def test_native_and_ocr_preparation_share_one_designated_text_column(tmp_path) -> None:
    with closing(Project.create(tmp_path / "shared-text.frisket")) as project:
        sheet_id, source_column_id = _source(project)

        native = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="native_pdf",
        )
        ocr = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="ocr",
            force_ocr=True,
        )

        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        assert native["output_names"]["text"] == "Text"
        assert ocr["output_names"]["text"] == "Text"
        assert native["expected_output_columns"]["Text"] == role.text_column_id
        assert ocr["expected_output_columns"]["Text"] == role.text_column_id
        assert ocr["output_names"]["blocks"] == "Search positions · Document"


def test_designated_text_name_is_unique_without_claiming_a_collision(tmp_path) -> None:
    with closing(Project.create(tmp_path / "text-collision.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        occupied = project.add_column(sheet_id, "Text", type="text")

        body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="ocr",
        )

        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        assert role.text_column_id != occupied
        assert body["output_names"]["text"] == "Text_2"
        assert body["expected_output_columns"]["Text_2"] == role.text_column_id
        assert project.get_column(occupied)["ai_generated"] == 0


def test_image_ocr_preparation_uses_the_designated_text_column(tmp_path) -> None:
    with closing(Project.create(tmp_path / "image-text.frisket")) as project:
        sheet_id, source_column_id = _source(
            project, name="Scan", column_type="image"
        )

        body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Scan",
            kind="ocr",
        )

        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        assert body["output_names"]["text"] == "Text"
        assert body["expected_output_columns"]["Text"] == role.text_column_id


def test_hidden_designated_role_keeps_exact_target_without_fallback(tmp_path) -> None:
    with closing(Project.create(tmp_path / "hidden-text.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        first = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="native_pdf",
        )
        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?", (role.text_column_id,)
        )
        project.db.commit()

        replacement = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="ocr",
            force_ocr=True,
        )

        assert replacement["output_names"]["text"] == first["output_names"]["text"]
        assert (
            replacement["expected_output_columns"][first["output_names"]["text"]]
            == role.text_column_id
        )
        assert [
            column["name"]
            for column in project.columns(sheet_id, include_hidden=True)
            if column["name"].startswith("Text")
        ] == ["Text"]


def test_inactive_designated_text_column_is_not_silently_replaced(tmp_path) -> None:
    with closing(Project.create(tmp_path / "inactive-text.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="native_pdf",
        )
        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        project.db.execute(
            "UPDATE columns SET active=0,hidden=1 WHERE id=?", (role.text_column_id,)
        )
        project.db.commit()

        with pytest.raises(ValueError, match="document text column is inactive"):
            _body(
                project,
                sheet_id=sheet_id,
                source_column_id=source_column_id,
                source_name="Document",
                kind="ocr",
            )

        assert document_text_role(
            project, source_column_id=source_column_id
        ).text_column_id == role.text_column_id


def test_transcription_does_not_create_a_document_text_role(tmp_path) -> None:
    with closing(Project.create(tmp_path / "transcript.frisket")) as project:
        sheet_id, source_column_id = _source(
            project, name="Recording", column_type="audio"
        )

        body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Recording",
            kind="transcribe",
        )

        assert body["output_names"]["text"] == "Transcript · Recording"
        assert document_text_role(project, source_column_id=source_column_id) is None
