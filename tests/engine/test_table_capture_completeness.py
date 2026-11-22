"""Public table actions must retain their actual consumed source capture."""

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.staleness import sheet_freshness
from frisket.engine.store.change_log import step_change


def _assert_captured(project, result):
    assert result.status == "completed", result.errors
    sheet = next(output.sheet_id for output in result.outputs if output.kind == "sheet")
    count = project.db.execute(
        "SELECT count(*) FROM consumed_source_bundles WHERE output_sheet_id=?", (sheet,)
    ).fetchone()[0]
    assert count > 0, "successful source-backed table has no consumed input capture"
    assert sheet_freshness(project, sheet) == "synced"
    return sheet


def _assert_equal_contribution_roundtrip(project, output, sheet, column, row):
    unrelated = next(
        (c["id"] for c in project.columns(sheet) if c["name"] == "unrelated"), None
    )
    if unrelated is None:
        unrelated = project.add_column(sheet, "unrelated")
    project.apply_edits([{"row_id": row, "column_id": unrelated, "value": "unrelated"}])
    assert sheet_freshness(project, output) == "synced"
    value = project.get_values(sheet, column)[row]
    edit = project.apply_edits([{"row_id": row, "column_id": column, "value": value}])
    assert sheet_freshness(project, output) == "stale"
    with project.transaction_scope():
        step_change(project, "revert", change_id=edit)
    assert sheet_freshness(project, output) == "synced"


def test_transcript_segments_capture_actual_transcript_source(make_project):
    from tests.engine.test_transcript_segments_action import (
        _seed_timestamped_transcript,
        _run,
    )

    project = make_project("transcript")
    source = _seed_timestamped_transcript(project)
    result = _run(
        project,
        source,
        selection={"kind": "draft_points", "items": [{"at_ms": 2500}]},
        target="Segments",
        key="segments-capture",
    )
    output = _assert_captured(project, result)
    _assert_equal_contribution_roundtrip(
        project,
        output,
        source["sheet_id"],
        source["transcript_column_id"],
        source["row_id"],
    )


def test_positioned_document_table_keeps_nested_sheet_reader_capture(make_project):
    from tests.engine.test_visual_document_extraction_action import seed, request

    project = make_project("documents")
    sheet, column, rows, template = seed(project, count=1)
    result = run_action_spec(project, request(sheet, template), project_id="capture")
    output = _assert_captured(project, result)
    _assert_equal_contribution_roundtrip(project, output, sheet, column, rows[0])


def test_temporal_media_table_captures_media_and_inherited_transcript(make_project):
    from tests.engine.test_temporal_split import (
        _seed_media,
        _request,
        _run,
        _fake_stage,
    )
    from tests.engine.test_temporal_transcript_inheritance import (
        _add_timestamped_transcript,
    )
    from frisket.engine.store.artifact_timeline import resolve_timeline

    project = make_project("media")
    source = _seed_media(project)
    lease = resolve_timeline(
        project,
        sheet_id=source["sheet_id"],
        row_id=source["row_id"],
        column_id=source["columns"]["video"],
    )
    source["columns"]["transcript"], _ = _add_timestamped_transcript(
        project,
        sheet_id=source["sheet_id"],
        row_id=source["row_id"],
        artifact_id=lease.anchor.artifact_id,
        name="transcript",
        text="First sentence. Second sentence.",
        span_values=[
            (0, 3000, "First sentence."),
            (3000, 7000, "Second sentence."),
        ],
    )
    result = _run(
        project,
        _request(
            source,
            {"kind": "draft_ranges", "items": [{"start_ms": 1000, "end_ms": 4000}]},
        ),
        stage_fn=_fake_stage,
        project_id="capture",
    )
    output = _assert_captured(project, result)
    _assert_equal_contribution_roundtrip(
        project,
        output,
        source["sheet_id"],
        source["columns"]["video"],
        source["row_id"],
    )
    _assert_equal_contribution_roundtrip(
        project,
        output,
        source["sheet_id"],
        source["columns"]["transcript"],
        source["row_id"],
    )


def test_temporal_clock_uses_the_value_and_ref_already_read(make_project, monkeypatch):
    from frisket.engine.executor import temporal_materialization
    from tests.engine.test_temporal_split import _seed_media, _request, _plan
    from frisket.engine.store.media_blobs import owned_media_metadata_document

    project = make_project("clock-read")
    source = _seed_media(project)
    newer = project.add_blob(
        b"new recording",
        filename="new.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 10, "kind": "video"}
        ),
    )
    resolve = temporal_materialization.resolve_source_timeline

    def interleave(*args, **kwargs):
        project.apply_edits(
            [
                {
                    "row_id": source["row_id"],
                    "column_id": source["columns"]["video"],
                    "value": {
                        "blob": newer,
                        "filename": "new.mp4",
                        "mime": "video/mp4",
                    },
                }
            ]
        )
        return resolve(*args, **kwargs)

    monkeypatch.setattr(temporal_materialization, "resolve_source_timeline", interleave)
    plan = _plan(
        project,
        _request(
            source,
            {"kind": "draft_ranges", "items": [{"start_ms": 1000, "end_ms": 4000}]},
        ),
    )
    captured = plan.sources[0].source
    assert captured.value["blob"] == captured.lease.blob_hash == source["blob_hash"]
    assert captured.value_ref == captured.lease.current_value_ref


def test_semantic_link_table_captures_both_actual_sides(tmp_path, make_project):
    from tests.engine.test_derive_link_table_executor import _seed, _derive_link_action

    project = make_project("semantic")
    source = _seed(project, tmp_path)
    result = run_action_spec(
        project,
        _derive_link_action(source["join_receipt_id"]),
        project_id="capture",
    )
    output = _assert_captured(project, result)
    company = next(
        c["id"]
        for c in project.columns(source["target_sheet_id"])
        if c["name"] == "company"
    )
    _assert_equal_contribution_roundtrip(
        project,
        output,
        source["target_sheet_id"],
        company,
        source["target_row_ids"][0],
    )


def test_semantic_join_raw_child_inherits_actual_comparison_corpus(
    make_project, monkeypatch
):
    def embed(texts):
        return [[1.0, 0.0] for _ in texts]

    embed.embedding_dimension = 2
    monkeypatch.setattr(
        "frisket.ai.embeddings.backends.resolve_embedder",
        lambda *args, **kwargs: (embed, "fastembed/test"),
    )
    project = make_project("raw-join")
    left = project.add_sheet("Donors")
    donor = project.add_column(left, "donor")
    [row] = project.add_rows(left, [{"donor": "ACME"}], {"donor": donor})
    right = project.add_sheet("Registry")
    company = project.add_column(right, "company")
    [target] = project.add_rows(right, [{"company": "Acme"}], {"company": company})
    result = run_action_spec(
        project,
        {
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": left},
            "params": {
                "source": "donor",
                "target": {"sheet_id": right, "column": "company"},
            },
            "sheet_name": "Raw links",
            "idempotency_key": "raw-capture",
        },
        project_id="capture",
    )
    output = _assert_captured(project, result)
    _assert_equal_contribution_roundtrip(project, output, right, company, target)
    _assert_equal_contribution_roundtrip(project, output, left, donor, row)


def test_entity_table_captures_actual_cluster_source(make_project):
    from tests.engine.test_cluster_receipt_reader import _request

    project = make_project("entities")
    sheet = project.add_sheet("Names")
    column = project.add_column(sheet, "name")
    rows = project.add_rows(
        sheet, [{"name": "Jon Smith"}, {"name": "Smith Jon"}], {"name": column}
    )
    clusters = run_action_spec(
        project,
        {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "name", "method": "fingerprint"},
            "idempotency_key": "capture-clusters",
        },
        project_id="capture",
    )
    assert clusters.status == "completed", clusters.errors
    result = run_action_spec(
        project, _request(clusters.receipt_id), project_id="capture"
    )
    output = _assert_captured(project, result)
    _assert_equal_contribution_roundtrip(project, output, sheet, column, rows[0])


def test_semantic_capture_tracks_scoped_effective_judgment_but_value_only_capture_does_not(
    tmp_path,
    make_project,
):
    from tests.engine.test_derive_link_table_executor import _seed, _derive_link_action
    from frisket.engine.store.consumed_sources import (
        read_consumed_source,
        store_capture,
        bundle_state,
    )

    project = make_project("judgments")
    source = _seed(project, tmp_path)
    sheet, row, outside = (
        source["source_sheet_id"],
        source["source_row_ids"][0],
        source["source_row_ids"][1],
    )
    column = next(
        c["id"] for c in project.columns(sheet) if c["name"] == "company_match_row_id"
    )
    run = project.db.execute(
        "SELECT run_id FROM receipts WHERE id=?", (source["join_receipt_id"],)
    ).fetchone()[0]

    def decide(row_id, key):
        result = run_action_spec(
            project,
            {
                "action_id": "review.decision",
                "scope": {"kind": "project"},
                "params": {
                    "run_id": run,
                    "row_id": row_id,
                    "column_id": column,
                    "decision": "accept",
                },
                "idempotency_key": key,
            },
            project_id="judgments",
        )
        assert result.status == "completed", result.errors
        return result.change_ids[0]

    first = decide(row, "first")
    request = _derive_link_action(
        source["join_receipt_id"], row_ids=[row], source_sheet_id=sheet
    )
    result = run_action_spec(project, request, project_id="capture")
    output = _assert_captured(project, result)
    _, _, value_capture = read_consumed_source(
        project, sheet, [column], row_ids=[row], scope_intent="selected"
    )
    with project.transaction_scope():
        value_bundle = store_capture(
            project,
            value_capture,
            owner_change_id=result.change_ids[0],
            output_sheet_id=sheet,
        )
    decide(outside, "outside")
    assert sheet_freshness(project, output) == "synced"
    second = decide(row, "same-decision-new-owner")
    assert sheet_freshness(project, output) == "stale"
    assert bundle_state(project, value_bundle) == "fresh"
    with project.transaction_scope():
        step_change(project, "revert", change_id=second)
    assert sheet_freshness(project, output) == "synced"
    with project.transaction_scope():
        step_change(project, "revert", change_id=first)
    assert sheet_freshness(project, output) == "stale"
    with project.transaction_scope():
        step_change(project, "restore", change_id=first)
    assert sheet_freshness(project, output) == "synced"
