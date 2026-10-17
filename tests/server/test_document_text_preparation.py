from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.map_find_source import resolve_find_sources
from frisket.engine.store import Project
from frisket.engine.store.document_text import (
    document_text_role,
    ensure_document_text_role,
    is_generation_free_document_text_target,
    resolve_document_text_selection,
)
from frisket.engine.store.document_text_adoption import adopt_document_text_role
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import PreparedContentRef
from frisket.engine.jobs.search_preparation_store import canonical_plan_hash
from frisket.server.route_errors import RouteError
from frisket.server.services.search_preparation import SearchPreparationService
from helpers import write_claimed_test_results


_BLOB = "sha256:" + "1" * 64
_STALE_BLOB = "sha256:" + "2" * 64


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


def _prepared_ref(
    project: Project,
    *,
    op_id: int,
    sheet_id: int,
    row_id: int,
    source_column_id: int,
    blob_hash: str,
    text: str,
) -> PreparedContentRef:
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        blob_hash=blob_hash,
        page_count=1,
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=source_column_id,
    )
    with project.transaction_scope():
        return PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=[PreparedPageDraft(1, text)],
        )


def _publish_legacy_results(
    project: Project,
    *,
    op_id: int,
    sheet_id: int,
    values: list[tuple[int, int, PreparedContentRef]],
) -> None:
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "media.ocr",
        row_ids=sorted({row_id for row_id, _column_id, _ref in values}),
        total_rows=len({row_id for row_id, _column_id, _ref in values}),
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {"row_id": row_id, "column_id": column_id, "value": ref}
            for row_id, column_id, ref in values
        ],
    )
    RunResultStore(project).finish_run(run_id, "completed")


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
        assert native["output_names"]["text"] == "text"
        assert ocr["output_names"]["text"] == "text"
        assert native["expected_output_columns"]["text"] == role.text_column_id
        assert ocr["expected_output_columns"]["text"] == role.text_column_id
        assert ocr["output_names"]["blocks"] == "Search positions · Document"


@pytest.mark.parametrize(
    ("occupied_columns", "expected_name"),
    [
        ([("text", False)], "text_2"),
        ([("TEXT", True)], "text_2"),
        ([("Text", False), ("TeXt_2", True)], "text_3"),
    ],
)
def test_designated_text_name_is_unique_without_claiming_a_collision(
    tmp_path, occupied_columns, expected_name
) -> None:
    with closing(Project.create(tmp_path / "text-collision.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        occupied = []
        for name, hidden in occupied_columns:
            column_id = project.add_column(sheet_id, name, type="text")
            project.db.execute(
                "UPDATE columns SET hidden=? WHERE id=?", (int(hidden), column_id)
            )
            occupied.append(column_id)
        project.db.commit()

        body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="ocr",
        )

        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        assert role.text_column_id not in occupied
        assert body["output_names"]["text"] == expected_name
        assert body["expected_output_columns"][expected_name] == role.text_column_id
        assert [project.get_column(column_id)["name"] for column_id in occupied] == [
            name for name, _hidden in occupied_columns
        ]
        assert all(
            project.get_column(column_id)["ai_generated"] == 0 for column_id in occupied
        )


def test_existing_designated_text_name_and_identity_are_reused_without_rename(tmp_path):
    with closing(Project.create(tmp_path / "existing-text-role.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        text_column_id = project.add_column(sheet_id, "Text", type="text")
        ensure_document_text_role(
            project,
            source_column_id=source_column_id,
            text_column_id=text_column_id,
            commit=True,
        )
        for kind in ("native_pdf", "ocr"):
            body = _body(
                project,
                sheet_id=sheet_id,
                source_column_id=source_column_id,
                source_name="Document",
                kind=kind,
            )
            assert body["output_names"]["text"] == "Text"
            assert body["expected_output_columns"]["Text"] == text_column_id
        assert [
            (column["id"], column["name"]) for column in project.columns(sheet_id)
        ] == [
            (source_column_id, "Document"),
            (text_column_id, "Text"),
        ]


def test_image_ocr_preparation_uses_the_designated_text_column(tmp_path) -> None:
    with closing(Project.create(tmp_path / "image-text.frisket")) as project:
        sheet_id, source_column_id = _source(project, name="Scan", column_type="image")

        body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Scan",
            kind="ocr",
        )

        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        assert body["output_names"]["text"] == "text"
        assert body["expected_output_columns"]["text"] == role.text_column_id


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
            if column["name"].startswith("text")
        ] == ["text"]


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

        assert (
            document_text_role(
                project, source_column_id=source_column_id
            ).text_column_id
            == role.text_column_id
        )


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


def test_adoption_seeds_only_unambiguous_fresh_generated_prepared_refs(
    tmp_path,
) -> None:
    with closing(Project.create(tmp_path / "legacy-adoption.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        legacy_a = project.add_column(
            sheet_id, "Search text", type="text", ai_generated=True
        )
        legacy_b = project.add_column(
            sheet_id, "OCR text", type="text", ai_generated=True
        )
        hidden_legacy = project.add_column(
            sheet_id, "Hidden text", type="text", ai_generated=True
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {"Document": {"blob": _BLOB, "mime": "application/pdf"}}
                for _ in range(5)
            ],
            {"Document": source_column_id},
        )
        unique, ambiguous, stale, manual, hidden = row_ids
        op_id = project.append_op("media.ocr")
        unique_ref = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=unique,
            source_column_id=source_column_id,
            blob_hash=_BLOB,
            text="preserve me",
        )
        ambiguous_a = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=ambiguous,
            source_column_id=source_column_id,
            blob_hash=_BLOB,
            text="first alternative",
        )
        ambiguous_b = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=ambiguous,
            source_column_id=source_column_id,
            blob_hash=_BLOB,
            text="second alternative",
        )
        stale_ref = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=stale,
            source_column_id=source_column_id,
            blob_hash=_STALE_BLOB,
            text="stale",
        )
        manual_ref = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=manual,
            source_column_id=source_column_id,
            blob_hash=_BLOB,
            text="generated before edit",
        )
        hidden_ref = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=hidden,
            source_column_id=source_column_id,
            blob_hash=_BLOB,
            text="hidden alternative",
        )
        _publish_legacy_results(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            values=[
                (unique, legacy_a, unique_ref),
                (ambiguous, legacy_a, ambiguous_a),
                (ambiguous, legacy_b, ambiguous_b),
                (stale, legacy_a, stale_ref),
                (manual, legacy_a, manual_ref),
                (hidden, hidden_legacy, hidden_ref),
            ],
        )
        project.apply_edits(
            [{"row_id": manual, "column_id": legacy_a, "value": "manual choice"}]
        )
        project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (hidden_legacy,))
        project.db.commit()
        text_column_id = project.add_column(
            sheet_id, "Text", type="text", ai_generated=True
        )
        with project.transaction_scope():
            ensure_document_text_role(
                project,
                source_column_id=source_column_id,
                text_column_id=text_column_id,
                commit=False,
            )
        immutable_counts = tuple(
            int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "prepared_page_versions",
                "prepared_content_sets",
                "prepared_content_refs",
                "ops",
            )
        )

        with project.transaction_scope():
            adoption = adopt_document_text_role(
                project,
                source_column_id=source_column_id,
                text_column_id=text_column_id,
            )

        assert adoption.created is False
        assert adoption.seeded_count == 1
        assert (
            tuple(
                int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "prepared_page_versions",
                    "prepared_content_sets",
                    "prepared_content_refs",
                    "ops",
                )
            )
            == immutable_counts
        )
        stored = project.db.execute(
            "SELECT cell.row_id,cell.value_kind,cell.value,producer.op_id "
            "FROM cells cell JOIN base_cell_producers producer "
            "ON producer.id=cell.producer_id WHERE cell.column_id=?",
            (text_column_id,),
        ).fetchall()
        assert [tuple(row) for row in stored] == [
            (unique, "prepared_content_ref", unique_ref.ref_id, op_id)
        ]
        selection = resolve_document_text_selection(
            project, (sheet_id, unique, source_column_id)
        )
        assert selection.prepared_ref_id == unique_ref.ref_id
        assert project.get_values(sheet_id, text_column_id, [unique]) == {
            unique: "preserve me"
        }
        assert project.get_values(
            sheet_id, text_column_id, [ambiguous, stale, manual, hidden]
        ) == {row_id: None for row_id in [ambiguous, stale, manual, hidden]}
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM edits WHERE column_id=?", (text_column_id,)
            ).fetchone()[0]
            == 0
        )
        assert is_generation_free_document_text_target(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            text_column_id=text_column_id,
        )
        find_sources = resolve_find_sources(
            project,
            FindOperation(
                action_kind="map.find",
                request={},
                sheet_id=sheet_id,
                row_ids=[unique],
                source_column="Document",
                model="test/model",
                instruction="Find preserved text",
                fields=[],
                target_sheet_name="Findings",
                output_names={"match": "Match"},
            ),
        )
        assert isinstance(find_sources, tuple) and len(find_sources) == 1
        assert find_sources[0].value_ref["prepared_ref_id"] == unique_ref.ref_id
        assert [unit.text for unit in find_sources[0].units] == ["preserve me"]


def test_overlapping_batch_replans_after_another_batch_adopts_its_row(tmp_path) -> None:
    with closing(Project.create(tmp_path / "adoption-replan.frisket")) as project:
        sheet_id, source_column_id = _source(project)
        [row_id] = project.add_rows(
            sheet_id,
            [{"Document": {"blob": _BLOB, "mime": "application/pdf"}}],
            {"Document": source_column_id},
        )
        legacy_column_id = project.add_column(
            sheet_id, "Search text", type="text", ai_generated=True
        )
        op_id = project.append_op("media.read_pdf_text")
        legacy_ref = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=row_id,
            source_column_id=source_column_id,
            blob_hash=_BLOB,
            text="prepared by the earlier batch",
        )
        _publish_legacy_results(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            values=[(row_id, legacy_column_id, legacy_ref)],
        )
        item = {
            "kind": "native_pdf",
            "column_id": source_column_id,
            "column_name": "Document",
            "row_id": row_id,
            "blob_hash": _BLOB,
            "retrieval_revision": 1,
        }
        original = {
            "force_ocr": False,
            "groups": [],
            "models": [],
            "items": [item],
            "sources": [item],
        }
        changed = {**original, "items": []}
        row = SimpleNamespace(
            id="batch",
            project_id="project",
            storage_org_id=None,
            sheet_id=sheet_id,
            row_ids=[row_id],
            force_ocr=False,
            plan=original,
            plan_hash=canonical_plan_hash(original),
            stop_requested_at=None,
            errors=(),
            decision=None,
            selected_setup_refs=(),
        )

        # Batch B was planned above. Batch A advances first and adopts every
        # unambiguous legacy result in the source column, including B's row.
        _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="Document",
            kind="native_pdf",
        )
        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        assert project.get_values(sheet_id, role.text_column_id, [row_id]) == {
            row_id: "prepared by the earlier batch"
        }

        class Store:
            decided = False
            replanned = None

            def get(self, *_args, **_kwargs):
                return row

            def replan(self, *_args, plan, **_kwargs):
                self.replanned = plan
                return row

            def decide(self, *_args, **_kwargs):
                self.decided = True
                raise AssertionError("changed plan must be reviewed first")

        service = SearchPreparationService.__new__(SearchPreparationService)
        service.workspace = SimpleNamespace(
            get=lambda _project_id: project,
            queue_storage_org_id=None,
        )
        service.store = Store()
        service._plan = lambda *_args, **_kwargs: changed
        service._consent_facts_current = lambda *_args, **_kwargs: True
        service._project = lambda *_args, **_kwargs: SimpleNamespace(
            model_dump=lambda **_dump_kwargs: {"id": "batch"}
        )

        with pytest.raises(RouteError) as error:
            service.decide(
                "project",
                "batch",
                plan_hash=row.plan_hash,
                decision="prepare",
                selected_setup_refs=[],
                may_setup_models=False,
                initiated_by="test",
            )

        assert error.value.status_code == 409
        assert error.value.detail["code"] == "plan_changed"
        assert service.store.replanned == changed
        assert service.store.decided is False


@pytest.mark.parametrize(
    ("kind", "new_text", "replacement_text"),
    [
        ("native_pdf", "new native text", "old generated text"),
        ("ocr", "new OCR text", "new OCR text"),
    ],
)
def test_adopted_old_row_survives_new_generation_then_can_be_replaced(
    tmp_path, monkeypatch, kind, new_text, replacement_text
) -> None:
    from tests.engine.test_native_pdf_text_action import _pdf_bytes, _request, _source

    if kind == "ocr":
        from frisket.ops import ocr_engines
        from helpers import stub_rapidocr_run_scope

        async def deterministic_ocr(_self, _engine, page_paths, _ctx, **_kwargs):
            return [{"text": new_text, "blocks": []} for _page in page_paths]

        monkeypatch.setattr(
            ocr_engines.OcrEngines, "run_engine_on_pages", deterministic_ocr
        )
        stub_rapidocr_run_scope(monkeypatch)

    with closing(Project.create(tmp_path / "legacy-then-native.frisket")) as project:
        sheet_id, source_column_id, old_row, digest = _source(
            project, tmp_path, pdf=_pdf_bytes("old generated text")
        )
        legacy_column_id = project.add_column(
            sheet_id, "Search text", type="text", ai_generated=True
        )
        op_id = project.append_op("media.read_pdf_text")
        old_ref = _prepared_ref(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            row_id=old_row,
            source_column_id=source_column_id,
            blob_hash=digest,
            text="legacy prepared text",
        )
        _publish_legacy_results(
            project,
            op_id=op_id,
            sheet_id=sheet_id,
            values=[(old_row, legacy_column_id, old_ref)],
        )
        new_blob = project.add_blob(
            _pdf_bytes("new native text"),
            filename="new.pdf",
            mime="application/pdf",
        )
        [new_row] = project.add_rows(
            sheet_id,
            [
                {
                    "source": {
                        "blob": new_blob,
                        "filename": "new.pdf",
                        "mime": "application/pdf",
                    }
                }
            ],
            {"source": source_column_id},
        )
        body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="source",
            kind=kind,
        )
        role = document_text_role(project, source_column_id=source_column_id)
        assert role is not None
        request = {
            **_request(sheet_id, new_row, key="legacy-new-native"),
            "action_id": body["action_id"],
            "params": body["params"],
            "output_names": body["output_names"],
            "replace_existing": True,
            "expected_output_columns": body["expected_output_columns"],
        }

        result = run_action_spec(project, request, project_id="legacy-new-native")

        assert result.status == "completed", result.errors
        assert project.get_values(sheet_id, role.text_column_id) == {
            old_row: "legacy prepared text",
            new_row: new_text,
        }
        replacement_body = _body(
            project,
            sheet_id=sheet_id,
            source_column_id=source_column_id,
            source_name="source",
            kind=kind,
        )
        replacement = {
            **_request(sheet_id, old_row, key="legacy-old-native"),
            "action_id": replacement_body["action_id"],
            "params": replacement_body["params"],
            "output_names": replacement_body["output_names"],
            "replace_existing": True,
            "expected_output_columns": replacement_body["expected_output_columns"],
        }
        result = run_action_spec(project, replacement, project_id="legacy-old-native")
        assert result.status == "completed", result.errors
        assert project.get_values(sheet_id, role.text_column_id) == {
            old_row: replacement_text,
            new_row: new_text,
        }
