from contextlib import closing
import pytest
from frisket.engine.store import Project
from frisket.engine.store.purge import (
    PurgedMaterialUnavailable,
    begin_purge,
    finish_purge,
    require_no_pending_purge,
    require_publication_targets,
    require_restore_available,
)


@pytest.fixture
def project(tmp_path):
    with closing(Project.create(tmp_path / "purge.frisket")) as project:
        yield project


def add(project, name, value):
    sheet = project.add_sheet(name)
    column = project.add_column(sheet, "value", type="json")
    row = project.add_rows(sheet, [{"value": value}], {"value": column})[0]
    return sheet, column, row


def test_scope_preserves_independent_owner_and_blocks_stale_publication(project):
    blob = project.add_blob(b"original")
    first = add(project, "first", {"blob": blob})
    second = add(project, "copy", {"blob": blob})
    purge = begin_purge(project, row_ids=[first[2]])
    with pytest.raises(PurgedMaterialUnavailable):
        require_no_pending_purge(project.db)
    with pytest.raises(PurgedMaterialUnavailable):
        require_publication_targets(project.db, row_ids=[first[2]])
    with pytest.raises(PurgedMaterialUnavailable):
        require_restore_available(project.db, row_ids=[first[2]])
    require_publication_targets(project.db, row_ids=[second[2]])
    result = finish_purge(project, purge, batch_size=1)
    assert result["blobs_removed"] == 0
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cells WHERE row_id=?", (first[2],)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cells WHERE row_id=?", (second[2],)
        ).fetchone()[0]
        == 1
    )
    require_no_pending_purge(project.db)
    assert finish_purge(project, purge)["phase"] == "complete"
    final = begin_purge(project, sheet_ids=[second[0]])
    assert finish_purge(project, final)["blobs_removed"] == 1
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_invalid_scope_rolls_back_purge(project):
    add(project, "first", "hello")
    with pytest.raises(ValueError, match="Unknown row"):
        begin_purge(project, row_ids=[900])
    assert project.db.execute("SELECT COUNT(*) FROM project_purges").fetchone()[0] == 0


def test_interrupted_content_cleanup_resumes(project, monkeypatch):
    from frisket.engine.store import purge as module

    first = add(project, "first", "private")
    untouched = add(project, "second", "public")
    purge = begin_purge(project, column_ids=[first[1]])
    original = module._purge_files
    monkeypatch.setattr(
        module,
        "_purge_files",
        lambda *args: (_ for _ in ()).throw(OSError("interrupted")),
    )
    with pytest.raises(OSError):
        finish_purge(project, purge)
    assert (
        project.db.execute(
            "SELECT phase FROM project_purges WHERE id=?", (purge,)
        ).fetchone()[0]
        == "files"
    )
    with pytest.raises(PurgedMaterialUnavailable):
        require_no_pending_purge(project.db)
    monkeypatch.setattr(module, "_purge_files", original)
    finish_purge(project, purge)
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=?", (untouched[2],)
        ).fetchone()[0]
        == "public"
    )


def test_purge_cleans_copied_watch_text_and_only_related_trace(project):
    from frisket.engine.store.runs import RunResultStore

    first = add(project, "first", "secret")
    second = add(project, "second", "public")
    op = project.append_op("test.run")
    run = RunResultStore(project).start_run(op, first[0], "test.run")
    other_op = project.append_op("test.run")
    other_run = RunResultStore(project).start_run(other_op, second[0], "test.run")
    RunResultStore(project).finish_run(run)
    RunResultStore(project).finish_run(other_run)
    traces = project.path / "traces"
    traces.mkdir()
    (traces / f"run-{run}.jsonl.gz").write_bytes(b"secret")
    (traces / f"run-{other_run}.jsonl.gz").write_bytes(b"public")
    with project.db:
        view = project.db.execute(
            "INSERT INTO views(name,spec) VALUES ('w','{}')"
        ).lastrowid
        watch = project.db.execute(
            "INSERT INTO watches(name,view_id) VALUES ('w',?)", (view,)
        ).lastrowid
        wr = project.db.execute(
            "INSERT INTO watch_runs(watch_id,op_cursor_before,op_cursor_after) VALUES (?,0,0)",
            (watch,),
        ).lastrowid
        project.db.execute(
            "INSERT INTO watch_run_hits(run_id,sheet_id,row_id,rank,snippet) VALUES (?,?,?,1,?)",
            (wr, first[0], first[2], "secret"),
        )
        project.db.execute(
            "INSERT INTO notification_items(source_kind,source_ref,dedupe_key,title,summary) VALUES ('watch',json_object('watch_id',?,'run_id',?),'private','private','secret')",
            (watch, wr),
        )
        project.db.execute(
            "INSERT INTO notification_items(source_kind,source_ref,dedupe_key,title,summary) VALUES ('other','{}','public','public','unrelated')"
        )
    purge = begin_purge(project, sheet_ids=[first[0]])
    finish_purge(project, purge)
    assert not (traces / f"run-{run}.jsonl.gz").exists()
    assert (traces / f"run-{other_run}.jsonl.gz").exists()
    assert [
        r[0] for r in project.db.execute("SELECT summary FROM notification_items")
    ] == ["unrelated"]
    assert project.db.execute("SELECT count(*) FROM watch_runs").fetchone()[0] == 0
    assert project.db.execute("SELECT count(*) FROM watches").fetchone()[0] == 1


def test_removed_notification_does_not_become_route_test(project):
    from frisket.engine.jobs.notifications_delivery import (
        _render_message,
        NotificationContentUnavailable,
    )

    request = {
        "digest_run_id": None,
        "notification_id": 42,
        "delivery_kind": "immediate",
    }
    with pytest.raises(NotificationContentUnavailable):
        _render_message(project, project_id="test", request=request)


def test_independent_saved_result_copy_keeps_payload_until_last_owner(project):
    from frisket.engine.store.runs import RunResultStore

    first = add(project, "first", "input")
    second = add(project, "independent", "seed")
    blob = project.add_blob(b"independent saved output")
    op = project.append_op("test.run")
    run = RunResultStore(project).start_run(op, first[0], "test.run")
    RunResultStore(project).finish_run(run)
    copy_op = project.append_op("test.reapply")
    with project.db:
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value) VALUES (?,?,?,'json',json_object('blob',?))",
            (run, first[2], first[1], blob),
        )
        project.db.execute(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id) VALUES (?,?,?,?,?,?)",
            (copy_op, second[2], second[1], run, first[2], first[1]),
        )
    original = begin_purge(project, sheet_ids=[first[0]])
    assert finish_purge(project, original)["blobs_removed"] == 0
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=?", (run,)
        ).fetchone()[0]
        == 1
    )
    copy = begin_purge(project, sheet_ids=[second[0]])
    assert finish_purge(project, copy)["blobs_removed"] == 1
    assert project.db.execute("SELECT count(*) FROM results").fetchone()[0] == 0


def test_retrieval_purge_drops_only_selected_binding_and_unowned_text(project):
    from frisket.retrieval.store import RetrievalStore

    first = add(project, "first", "private")
    other = add(project, "other", "public")
    with RetrievalStore(project.path) as store:
        for num, coordinates in enumerate((first, other), 1):
            store.db.execute(
                "INSERT INTO sources(source_key,source_id,source_kind,version_kind,version_value,version_type) VALUES (?,?,'cell','hash',?,'text')",
                (num, str(num), str(num)),
            )
            store.db.execute(
                "INSERT INTO bindings(binding_id,source_key,sheet_id,row_id,column_id) VALUES (?,?,?,?,?)",
                (str(num), num, coordinates[0], coordinates[2], coordinates[1]),
            )
        store.db.commit()
    finish_purge(project, begin_purge(project, row_ids=[first[2]]))
    with RetrievalStore(project.path) as store:
        assert [r[0] for r in store.db.execute("SELECT binding_id FROM bindings")] == [
            "2"
        ]
        assert [r[0] for r in store.db.execute("SELECT source_id FROM sources")] == [
            "2"
        ]


def test_partial_content_batch_failure_resumes_without_touching_other_sheet(
    project, monkeypatch
):
    from frisket.engine.store import purge as module

    first = add(project, "first", "private")
    second = add(project, "second", "keep")
    identity = begin_purge(project, row_ids=[first[2]])
    original = module._delete_batches

    def interrupted(db, table, *args, **kwargs):
        original(db, table, *args, **kwargs)
        if table == "cells":
            raise OSError("stopped after an acknowledged batch")

    monkeypatch.setattr(module, "_delete_batches", interrupted)
    with pytest.raises(OSError):
        finish_purge(project, identity, batch_size=1)
    assert (
        project.db.execute(
            "SELECT phase FROM project_purges WHERE id=?", (identity,)
        ).fetchone()[0]
        == "content"
    )
    monkeypatch.setattr(module, "_delete_batches", original)
    finish_purge(project, identity, batch_size=1)
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=?", (second[2],)
        ).fetchone()[0]
        == "keep"
    )


def test_column_purge_preserves_unselected_source_baseline_fields(project):
    import json

    sheet, name, row = add(project, "incoming", "name")
    key = project.add_column(sheet, "key", type="text")
    with project.db:
        project.db.execute(
            "INSERT INTO keyed_relationships VALUES (?,?,?)",
            ("source", sheet, json.dumps([[key, "text"]])),
        )
        project.db.execute(
            "INSERT INTO keyed_observations(relationship_id,row_id,key,record) VALUES (?,?,?,?)",
            (
                "source",
                row,
                "identity",
                json.dumps({str(key): "id-1", str(name): "secret"}),
            ),
        )
    finish_purge(project, begin_purge(project, column_ids=[name]))
    assert json.loads(
        project.db.execute("SELECT record FROM keyed_observations").fetchone()[0]
    ) == {str(key): "id-1"}
    finish_purge(project, begin_purge(project, column_ids=[key]))
    assert (
        project.db.execute("SELECT count(*) FROM keyed_relationships").fetchone()[0]
        == 0
    )


def test_source_purge_scrubs_citation_text_but_preserves_materialized_child(project):
    from frisket.engine.store.evidence import (
        record_source_artifact,
        record_text_surface,
    )

    original = project.add_blob(b"private original")
    child = project.add_blob(b"independent derived file")
    source = add(project, "source", {"blob": original})
    add(project, "saved", {"blob": child})
    project.record_blob_derivation(
        derived_hash=child, source_hash=original, op="render"
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=original,
        source_sheet_id=source[0],
        source_row_id=source[2],
        source_column_id=source[1],
        title="secret title",
    )
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash="sha256:" + "a" * 64,
        offset_unit="unicode_codepoint",
        text_sheet_id=source[0],
        text_row_id=source[2],
        text_column_id=source[1],
        surface_ref={"private": "copied diagnostic"},
    )
    with project.db:
        project.db.execute(
            "INSERT INTO source_spans(stable_id,artifact_id,span_kind,quote,snippet,text_surface_id,metadata) VALUES ('test-span',?,'page','secret quote','secret snippet',?,'{}')",
            (artifact["id"], surface["id"]),
        )
    result = finish_purge(project, begin_purge(project, sheet_ids=[source[0]]))
    assert result["blobs_removed"] == 1
    span = project.db.execute("SELECT quote,snippet FROM source_spans").fetchone()
    assert tuple(span) == (None, None)
    assert (
        project.db.execute("SELECT surface_ref_json FROM text_surfaces").fetchone()[0]
        == "{}"
    )
    assert project.db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='trg_text_surfaces_no_update'"
    ).fetchone()
    assert project.blob_derivation_chain(child)[0]["source_hash"] == original
    with project.materialize_blob(child) as path:
        assert path.read_bytes() == b"independent derived file"


def test_active_or_unaccounted_work_must_resolve_before_purge(project):
    from frisket.engine.store.runs import RunResultStore

    first = add(project, "first", "private")
    owner = project.append_op("test.run")
    run = RunResultStore(project).start_run(owner, first[0], "test.run")
    with pytest.raises(ValueError, match="Cancel or finish"):
        begin_purge(project, sheet_ids=[first[0]])
    assert project.db.execute("SELECT count(*) FROM project_purges").fetchone()[0] == 0
    RunResultStore(project).finish_run(run)
    with project.db:
        project.db.execute(
            "INSERT INTO effect_checkpoints(id,family,group_key,unit_key,action_kind,identity,state,payload) VALUES ('checkpoint','row_effect',?,'1','test.run','fingerprint','returned','{}')",
            (str(run),),
        )
    with pytest.raises(ValueError, match="unrecorded usage"):
        begin_purge(project, sheet_ids=[first[0]])
    with project.db:
        project.db.execute(
            "UPDATE effect_checkpoints SET accounting_persisted=1,payload='{\"secret\":42}'"
        )
    finish_purge(project, begin_purge(project, sheet_ids=[first[0]]))
    checkpoint = project.db.execute(
        "SELECT id,state,accounting_persisted,payload FROM effect_checkpoints"
    ).fetchone()
    assert tuple(checkpoint) == ("checkpoint", "returned", 1, "{}")


def test_purge_source_forgets_consumed_capture_and_related_trace_only(project):
    from frisket.engine.store.consumed_sources import (
        read_consumed_source,
        store_capture,
        verify_bundle,
    )
    from frisket.engine.store.runs import RunResultStore

    source = add(project, "source", "private")
    output = add(project, "independent answer", "keep me")
    _, _, capture = read_consumed_source(project, source[0], [source[1]])
    owner = project.append_op("test.capture", {})
    run = RunResultStore(project).start_run(owner, output[0], "test.capture")
    with project.db:
        project.db.execute("BEGIN")
        bundle = store_capture(
            project,
            capture,
            owner_op_id=owner,
            output_sheet_id=output[0],
            run_id=run,
            row_id=output[2],
        )
    RunResultStore(project).finish_run(run)
    traces = project.path / "traces"
    traces.mkdir()
    trace = traces / f"run-{run}.jsonl.gz"
    trace.write_bytes(b"captured input")
    finish_purge(project, begin_purge(project, row_ids=[source[2]]), batch_size=1)
    assert not trace.exists()
    assert verify_bundle(project, bundle) == "unknown"
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=?", (output[2],)
        ).fetchone()[0]
        == "keep me"
    )


def test_purge_preserves_shared_descriptor_authority_for_other_columns(project):
    from frisket.engine.store.changes import start_change
    from frisket.engine.store.column_descriptors import (
        record_descriptor_changes,
        descriptor_identity,
    )

    sheet, selected, _ = add(project, "sheet", "secret")
    retained = project.add_column(sheet, "retained", type="number")
    info = {
        "column_formats": {str(selected): None, str(retained): None},
        "column_formats_after": {str(selected): "currency", str(retained): "percent"},
    }
    with project.db:
        project.db.execute("BEGIN")
        operation = project.append_op("column.format", commit=False)
        start_change(project.db, operation)
        project.set_undo_info(operation, info, commit=False)
        record_descriptor_changes(project.db, operation, info)
    assert descriptor_identity(project, retained)["format"] == "percent"
    finish_purge(project, begin_purge(project, column_ids=[selected]))
    assert descriptor_identity(project, retained)["format"] == "percent"


def test_normal_open_refuses_interrupted_purge_and_maintenance_can_finish(tmp_path):
    path = tmp_path / "resume.frisket"
    with closing(Project.create(path)) as original:
        sheet, column, row = add(original, "first", "private content")
        purge = begin_purge(original, row_ids=[row])
    with pytest.raises(PurgedMaterialUnavailable, match="unfinished"):
        Project(path)
    with closing(Project(path, maintenance=True)) as maintenance:
        finish_purge(maintenance, purge)
    with closing(Project(path)) as restored:
        assert restored.visible_row_ids(sheet) == []
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
