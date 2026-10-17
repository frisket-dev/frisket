from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from time import perf_counter

import pytest

from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.retrieval_lifecycle import read_binding_revision
from frisket.engine.store.runs import RunResultStore
from frisket.search_index import bind_retrieval_sidecar
from frisket.retrieval import native
from frisket.retrieval.contracts import (
    BindingRevision,
    ChannelCoverage,
    PageRange,
    RetrievalScope,
    RankingPolicy,
    SearchRequest,
    SemanticExclusion,
    SheetScope,
    SourceAnchor,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TextRange,
    WeightedSemanticQuery,
    WeightedTerm,
)
from frisket.retrieval.query import SemanticQueryVectors, search
from frisket.retrieval.sources import input_hash
from frisket.retrieval.store import (
    BindingDependency,
    CachedRepresentation,
    LexicalInput,
    NamedDatasetMember,
    PartRepresentation,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
    StoredPart,
)
from tests.helpers import write_claimed_test_results


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def query_native_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    try:
        native.packaged_vec1_path()
    except native.Vec1Unavailable:
        output = tmp_path_factory.mktemp("query-vec1")
        # Exercise the real native boundary with the repository-owned builder;
        # every argument is fixed or a pytest-created path, with no shell.
        subprocess.run(  # noqa: S603
            [
                # subprocess-boundary: compile the actual SQLite extension
                sys.executable,
                str(ROOT / "scripts" / "build_vec1.py"),
                "--output-root",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        return output
    return native._NATIVE_ROOT


@pytest.fixture(autouse=True)
def packaged_query_native(monkeypatch: pytest.MonkeyPatch, query_native_root: Path):
    monkeypatch.setattr(native, "_NATIVE_ROOT", query_native_root)


def _project(tmp_path):
    project = Project.create(tmp_path / "query.frisket", name="query")
    sheet = project.add_sheet("Documents")
    status = project.add_column(sheet, "status", type="category")
    body = project.add_column(sheet, "body")
    rows = project.add_rows(
        sheet,
        [
            {"status": "outside", "body": "target global best"},
            {"status": "included", "body": "target eligible second"},
            {"status": "included", "body": "target distant"},
            {"status": "included", "body": "target hidden"},
            {"status": "included", "body": "target stale"},
        ],
        {"status": status, "body": body},
    )
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[3],))
    project.db.execute(
        "UPDATE current_cells SET retrieval_revision=retrieval_revision+1 "
        "WHERE row_id=? AND column_id=?",
        (rows[4], body),
    )
    project.db.commit()
    return project, sheet, status, body, rows


def _publish(
    store: RetrievalStore,
    *,
    row_id: int,
    sheet_id: int,
    column_id: int,
    text: str,
    vector: tuple[float, float, float],
    version: int,
    parts: tuple[tuple[str, tuple[float, float, float], int, int], ...] | None = None,
    extra_bindings: tuple[StoredBinding, ...] = (),
):
    revision = SourceRevision(
        source_id=f"cell:{row_id}:{column_id}",
        source_kind="text",
        version_kind="retrieval_revision",
        version_value=version,
        content_hash=input_hash(text),
    )
    specs = parts or ((f"part:{row_id}:{column_id}", vector, 0, len(text)),)
    stored_parts = []
    representations = []
    attachments = []
    memberships = []
    for index, (part_id, part_vector, start, end) in enumerate(specs):
        locator = SourceLocator(
            kind="text",
            text=TextRange(
                text_surface_id=revision.source_id,
                content_hash=revision.content_hash or "missing",
                start=start,
                end=end,
            ),
        )
        stored_parts.append(
            StoredPart(part_id, "text_passage", locator, group_key=revision.source_id)
        )
        representation_id = f"representation:{row_id}:{column_id}:{index}"
        representations.append(
            CachedRepresentation(
                representation_id=representation_id,
                space_id="space:test",
                input_hash=f"input:{row_id}:{column_id}:{index}",
                vector=native.pack_vector(part_vector, dimensions=3),
                dimension=3,
            )
        )
        attachments.append(PartRepresentation(part_id, representation_id, "text"))
        memberships.append(
            NamedDatasetMember("named:included", part_id, representation_id)
        )
    store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
    assert store.publish_source(
        SourcePublication(
            source_revision=revision,
            recipe_id="passages:v1",
            channel="text",
            parts=tuple(stored_parts),
            bindings=(
                StoredBinding(
                    f"binding:{row_id}:{column_id}",
                    sheet_id,
                    row_id,
                    column_id=column_id,
                    dependencies=(BindingDependency(row_id, column_id, version),),
                ),
                *extra_bindings,
            ),
            representations=tuple(representations),
            attachments=tuple(attachments),
            lexical=LexicalInput(text, SourceLocator(kind="text")),
            named_memberships=tuple(memberships),
        )
    )


def _prepare_project(tmp_path):
    project, sheet, status, body, rows = _project(tmp_path)
    values = [
        ("target global best", (1.0, 0.0, 0.0)),
        ("target eligible second", (0.8, 0.6, 0.0)),
        ("target distant", (0.0, 1.0, 0.0)),
        ("target hidden", (1.0, 0.0, 0.0)),
        ("target stale", (1.0, 0.0, 0.0)),
    ]
    with RetrievalStore(project).open() as store:
        for row_id, (text, vector) in zip(rows, values, strict=True):
            revision = read_binding_revision(project.db, row_id=row_id, column_id=body)
            assert revision is not None
            _publish(
                store,
                row_id=row_id,
                sheet_id=sheet,
                column_id=body,
                text=text,
                vector=vector,
                version=revision - 1 if row_id == rows[4] else revision,
            )
    bind_retrieval_sidecar(project)
    project.db.execute("DELETE FROM search_dirty_scopes")
    project.db.commit()
    return project, sheet, status, body, rows


def _included_scope(sheet: int, body: int) -> RetrievalScope:
    return RetrievalScope(
        sheets=(
            SheetScope(
                sheet_id=sheet,
                column_ids=(body,),
                filter_json='{"status":{"eq":"included"}}',
            ),
        )
    )


def test_large_correlated_cell_scope_is_bounded_and_private(tmp_path):
    project = Project.create(tmp_path / "large-scope.frisket", name="large-scope")
    try:
        sheet = project.add_sheet("Documents")
        left = project.add_column(sheet, "left")
        right = project.add_column(sheet, "right")
        row_count = 600
        rows = project.add_rows(
            sheet,
            [
                {"left": f"target left {index}", "right": f"target right {index}"}
                for index in range(row_count)
            ],
            {"left": left, "right": right},
        )
        selected_columns = {
            row_id: left if index % 2 == 0 else right
            for index, row_id in enumerate(rows)
        }
        with RetrievalStore(project).open() as store:
            for index, row_id in enumerate(rows):
                column_id = selected_columns[row_id]
                side = "left" if column_id == left else "right"
                text = f"target {side} {index}"
                revision = read_binding_revision(
                    project.db, row_id=row_id, column_id=column_id
                )
                assert revision is not None
                _publish(
                    store,
                    row_id=row_id,
                    sheet_id=sheet,
                    column_id=column_id,
                    text=text,
                    vector=(1.0, 0.0, 0.0),
                    version=revision,
                )
        bind_retrieval_sidecar(project)
        project.db.execute("DELETE FROM search_dirty_scopes")
        project.db.commit()
        request = SearchRequest(
            scope=RetrievalScope(
                sheets=tuple(
                    SheetScope(
                        sheet,
                        row_ids=(row_id,),
                        column_ids=(selected_columns[row_id],),
                    )
                    for row_id in rows
                )
            ),
            query=TextQuery("target"),
            mode="lexical",
            group_by="row",
            limit=row_count,
            ranking=RankingPolicy(rerank="off"),
        )

        started = perf_counter()
        response = search(project, request)
        elapsed = perf_counter() - started

        assert {
            (group.primary.binding.row_id, group.primary.binding.column_id)
            for group in response.results
        } == set(selected_columns.items())
        # The former inlined scope took 1.5s locally and over 10s in the
        # sandboxed probe. Materializing once leaves ample platform headroom.
        assert elapsed < 1.0
    finally:
        project.close()


@pytest.mark.parametrize("identifier", ["ORCHID-79", "Case:123"])
def test_keyword_search_treats_identifiers_as_text_when_fts_rejects_them(
    tmp_path, identifier
):
    from frisket.search import drain_index, search_project

    project = Project.create(tmp_path / "identifiers.frisket")
    try:
        sheet = project.add_sheet("Records")
        column = project.add_column(sheet, "body")
        rows = project.add_rows(
            sheet, [{"body": f"Grant {identifier} approved."}], {"body": column}
        )
        drain_index(project)
        hits = search_project(project, identifier, rerank="off")
        assert [hit["row_id"] for hit in hits] == rows
    finally:
        project.close()


def test_unique_long_lexical_match_reports_exact_original_text_range(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "unique-lexical-range.frisket")
    try:
        sheet = project.add_sheet("Contracts")
        column = project.add_column(sheet, "body")
        prefix = "Background material. " * 3_800
        query = "ORCHID-79"
        match = "Orchid-79"
        text = f"{prefix}{match} approved a $4,200 roof repair."
        project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery(query),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        [result] = response.results
        location = result.primary.part.locator
        assert result.primary.precision == "exact"
        assert location.kind == "whole_source"
        assert location.text is not None
        assert (location.text.start, location.text.end) == (
            len(prefix),
            len(prefix) + len(match),
        )
        assert text[location.text.start : location.text.end] == match
    finally:
        project.close()


def test_unique_prepared_lexical_match_narrows_location_to_owning_page(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "prepared-lexical-page.frisket")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("prepare.test", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            page_count=2,
        )
        prepared = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(
                PreparedPageDraft(1, "Harbor minutes and attendance."),
                PreparedPageDraft(2, "The council approved the bluewharf repair."),
            ),
        )
        project.add_rows(sheet, [{"body": prepared}], {"body": column}, commit=False)
        project.db.commit()
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery("bluewharf"),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        [result] = response.results
        location = result.primary.part.locator
        assert result.primary.precision == "exact"
        assert location.pages == PageRange(2, 2)
        assert location.text is not None
        assert (
            PreparedContentStore(project)
            .resolve(prepared.ref_id)
            .text[location.text.start : location.text.end]
            == "bluewharf"
        )
    finally:
        project.close()


def test_prepared_ocr_indexes_one_text_source_and_skips_positions_metadata(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "prepared-ocr-canonical.frisket")
    try:
        sheet = project.add_sheet("Scans")
        ocr_text = project.add_column(sheet, "OCR text", ai_generated=True)
        positions = project.add_column(
            sheet, "OCR positions", type="json", ai_generated=True
        )
        notes = project.add_column(sheet, "notes", type="json")
        phrase = "Inspection of the western pier"
        row = project.add_rows(
            sheet,
            [{"notes": {"description": f"User JSON: {phrase}"}}],
            {"notes": notes},
        )[0]
        output_names = {"text": "OCR text", "blocks": "OCR positions"}
        op_id = project.append_op(
            "media.ocr", {"output_names": output_names}, label="OCR fixture"
        )
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            page_count=1,
        )
        project.db.execute("BEGIN IMMEDIATE")
        prepared = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(PreparedPageDraft(1, phrase),),
        )
        project.db.commit()
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet,
            "media.ocr",
            params={"output_names": output_names},
            row_ids=[row],
            total_rows=1,
        )
        write_claimed_test_results(
            project,
            run_id,
            [
                {"row_id": row, "column_id": ocr_text, "value": prepared},
                {
                    "row_id": row,
                    "column_id": positions,
                    "value": {
                        "blocks": [
                            {
                                "bbox": [[10, 20], [90, 20], [90, 40], [10, 40]],
                                "text": phrase,
                            }
                        ]
                    },
                },
            ],
        )
        drain_index(project)
        request = SearchRequest(
            scope=RetrievalScope(sheets=(SheetScope(sheet),)),
            query=TextQuery(phrase),
            mode="lexical",
            group_by="source",
            limit=10,
            ranking=RankingPolicy(rerank="off"),
        )
        before_grounding = search(project, request)
        before_prepared = next(
            result
            for result in before_grounding.results
            if result.primary.part.source.source_kind == "prepared_text"
        )
        assert before_prepared.primary.part.locator.source_span_ids == ()

        region = record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="region",
            page_start=1,
            page_end=1,
            quote=phrase,
            bbox=[{"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.4}],
        )
        _values, refs = project.get_values_with_refs(sheet, ocr_text, row_ids=[row])
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=refs[row],
            spans=[{"span_id": int(region["id"])}],
            sheet_id=sheet,
            row_id=row,
            column_id=ocr_text,
            op_id=op_id,
            link_role="media_ocr_grounding",
            producer={"action_kind": "media.ocr", "engine": "fixture"},
        )

        drain_index(project)
        response = search(project, request)

        assert {
            (result.primary.part.source.source_kind, result.primary.binding.column_id)
            for result in response.results
        } == {("prepared_text", ocr_text), ("text", notes)}
        prepared_result = next(
            result
            for result in response.results
            if result.primary.part.source.source_kind == "prepared_text"
        )
        assert prepared_result.primary.part.locator.pages == PageRange(1, 1)
        assert prepared_result.primary.part.locator.source_span_ids == (
            str(region["stable_id"]),
        )
        assert (
            prepared_result.primary.part.source.version_value
            != before_prepared.primary.part.source.version_value
        )
        with RetrievalStore(project).open() as store:
            bound = store.db.execute(
                "SELECT source.source_kind,binding.column_id "
                "FROM bindings binding JOIN sources source "
                "ON source.source_key=binding.source_key "
                "WHERE binding.sheet_id=? AND binding.row_id=? "
                "ORDER BY binding.column_id,source.source_kind",
                (sheet, row),
            ).fetchall()
        assert [(item["source_kind"], item["column_id"]) for item in bound] == [
            ("prepared_text", ocr_text),
            ("text", notes),
        ]
    finally:
        project.close()


def test_repeated_lexical_anchor_keeps_whole_source_location(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "repeated-lexical-anchor.frisket")
    try:
        sheet = project.add_sheet("Contracts")
        column = project.add_column(sheet, "body")
        query = "ORCHID-79"
        text = "Orchid-79 opened the file. " + "Background. " * 800 + "orchid-79"
        project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery(query),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        [result] = response.results
        location = result.primary.part.locator
        assert result.primary.precision == "source_unit"
        assert location.kind == "whole_source"
        assert location.text is not None
        assert (location.text.start, location.text.end) == (0, len(text))
    finally:
        project.close()


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("cafe", "Le café was busy; the cafeteria was not.", "café"),
        ("red car", "the scarred carpet; a car painted red", None),
        ("red car", "the red car stopped", "red car"),
    ],
)
def test_lexical_location_follows_fts_marked_tokens_not_raw_substrings(
    tmp_path, query, text, expected
):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "fts-marked-lexical-range.frisket")
    try:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery(query),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        [result] = response.results
        location = result.primary.part.locator
        assert location.text is not None
        if expected is None:
            assert result.primary.precision == "source_unit"
            assert (location.text.start, location.text.end) == (0, len(text))
        else:
            assert result.primary.precision == "exact"
            assert text[location.text.start : location.text.end] == expected
    finally:
        project.close()


def test_quoted_lexical_match_ignores_literal_snippet_markup(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "literal-snippet-markup.frisket")
    try:
        sheet = project.add_sheet("Contracts")
        column = project.add_column(sheet, "body")
        prefix = "Background material. " * 800
        match = "ACTUALNEEDLE"
        text = f"{prefix}<b>Methodology</b> immediately before {match}."
        project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery(f'"{match}"'),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        [result] = response.results
        hit = result.primary
        location = hit.part.locator
        assert hit.precision == "exact"
        assert hit.snippet is not None
        assert (
            "<b>Methodology</b> immediately before <b>ACTUALNEEDLE</b>" in hit.snippet
        )
        assert location.text is not None
        assert text[location.text.start : location.text.end] == match
    finally:
        project.close()


def test_semantic_scope_and_freshness_are_applied_before_top_k(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="semantic",
                group_by="source",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert [group.primary.binding.row_id for group in response.results] == [
            rows[1],
            rows[2],
        ]
        assert rows[0] not in [
            group.primary.binding.row_id for group in response.results
        ]
        assert rows[3] not in [
            group.primary.binding.row_id for group in response.results
        ]
        assert rows[4] not in [
            group.primary.binding.row_id for group in response.results
        ]
        assert (
            response.results[0].primary.binding.dependencies[0].retrieval_revision > 0
        )
        assert "<b>target eligible second</b>" in (
            response.results[0].primary.snippet or ""
        )
    finally:
        project.close()


def test_lexical_search_hydrates_authoritative_text_and_honors_named_membership(
    tmp_path,
):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target AND eligible"),
                mode="lexical",
                group_by="source",
                named_index_id="named:included",
                limit=10,
            ),
        )

        assert [group.primary.binding.row_id for group in response.results] == [rows[1]]
        assert "<b>target</b>" in (response.results[0].primary.snippet or "")
        assert response.results[0].primary.precision == "source_unit"
    finally:
        project.close()


def test_lexical_search_hydrates_noncell_source_through_authority_adapter(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "transcript.frisket", name="transcript")
    try:
        sheet = project.add_sheet("Media")
        column = project.add_column(sheet, "media")
        [row] = project.add_rows(
            sheet, [{"media": "video blob handle"}], {"media": column}
        )
        revision = read_binding_revision(project.db, row_id=row, column_id=column)
        assert revision is not None
        text = "spoken needle"
        source = SourceRevision(
            "transcript-link:evidence",
            "transcript",
            "transcript_snapshot",
            "sha256:snapshot",
            input_hash(text),
        )
        locator = SourceLocator("transcript_chunk", source_span_ids=("span:spoken",))
        with RetrievalStore(project).open() as store:
            store.begin_preparation(
                source, recipe_id="transcript:v1", channel="transcript"
            )
            assert store.publish_source(
                SourcePublication(
                    source,
                    "transcript:v1",
                    "transcript",
                    parts=(),
                    bindings=(
                        StoredBinding(
                            "binding:transcript",
                            sheet,
                            row,
                            column,
                            (BindingDependency(row, column, revision),),
                        ),
                    ),
                    lexical=LexicalInput(text, locator),
                )
            )

        def resolve(_reader, *, source: SourceRevision, locator, binding):
            assert source.source_kind == "transcript"
            assert locator.source_span_ids == ("span:spoken",)
            assert binding.dependencies == (BindingRevision(row, column, revision),)
            return text

        monkeypatch.setattr("frisket.retrieval.query.resolve_source_text", resolve)
        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet, column_ids=(column,)),)),
                query=TextQuery("needle"),
                mode="lexical",
            ),
        )

        assert [result.primary.binding.row_id for result in response.results] == [row]
        assert "<b>needle</b>" in (response.results[0].primary.snippet or "")
    finally:
        project.close()


def test_semantic_threshold_exclusion_and_source_grouping(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        request = SearchRequest(
            scope=_included_scope(sheet, body),
            query=WeightedSemanticQuery(
                terms=(WeightedTerm("target"),),
                exclusions=(SemanticExclusion("eligible", threshold=0.9),),
                minimum_similarity=-0.1,
            ),
            mode="semantic",
            group_by="source",
            space_id="space:test",
            limit=10,
        )
        response = search(
            project,
            request,
            semantic_vectors=SemanticQueryVectors(
                term_vectors=((1.0, 0.0, 0.0),),
                exclusion_vectors=((0.8, 0.6, 0.0),),
            ),
        )
        assert [group.primary.binding.row_id for group in response.results] == [rows[2]]
    finally:
        project.close()


def test_source_anchor_uses_exact_authorized_part_and_excludes_only_that_part(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        initial = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="semantic",
                group_by="part",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        selected = initial.results[0].primary
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=SourceAnchor(
                    source=selected.part.source,
                    locator=selected.part.locator,
                    binding=selected.binding,
                ),
                mode="semantic",
                group_by="part",
                space_id="space:test",
                limit=10,
            ),
        )

        assert selected.binding.row_id == rows[1]
        assert [group.primary.binding.row_id for group in response.results] == [rows[2]]
        assert all(
            group.primary.part.part_id != selected.part.part_id
            for group in response.results
        )
    finally:
        project.close()


def test_source_anchor_accepts_an_explicit_vector_when_location_is_not_prepared(
    tmp_path,
):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        source = SourceRevision(
            "resolved:external-anchor",
            "text",
            "resolved_snapshot",
            "snapshot:one",
            input_hash("an exact source chosen outside the retrieval index"),
        )
        locator = SourceLocator(
            kind="text",
            text=TextRange(
                text_surface_id=source.source_id,
                content_hash=source.content_hash or "missing",
                start=0,
                end=15,
            ),
        )

        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=SourceAnchor(source=source, locator=locator),
                mode="semantic",
                group_by="source",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert [group.primary.binding.row_id for group in response.results] == [
            rows[1],
            rows[2],
        ]
    finally:
        project.close()


def test_hybrid_fuses_source_ranks_and_retains_both_contributions(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="hybrid",
                group_by="source",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert {group.primary.binding.row_id for group in response.results} == {
            rows[1],
            rows[2],
        }
        assert {
            contribution.method
            for group in response.results
            for contribution in group.primary.contributions
        } == {"lexical", "semantic"}
        assert response.results[0].primary.rank_score < 1.0
    finally:
        project.close()


@pytest.mark.parametrize("sparse", [False, True])
def test_composite_binding_is_invalidated_when_any_dependency_changes(tmp_path, sparse):
    project = Project.create(tmp_path / "composite.frisket", name="composite")
    try:
        sheet = project.add_sheet("Rows")
        title = project.add_column(sheet, "title")
        body = project.add_column(sheet, "body")
        [row] = project.add_rows(
            sheet,
            [{"title": "alpha", "body": "bravo"}],
            {"title": title, "body": body},
        )
        if sparse:
            project.db.execute(
                "DELETE FROM current_cells WHERE row_id=? AND column_id=?", (row, body)
            )
            project.db.commit()
        title_revision = read_binding_revision(project.db, row_id=row, column_id=title)
        body_revision = read_binding_revision(project.db, row_id=row, column_id=body)
        assert title_revision is not None
        body_revision = body_revision or 0
        revision = SourceRevision(
            "composite:row:1",
            "row_composite",
            "members",
            f"alpha@{title_revision},bravo@{body_revision}",
        )
        locator = SourceLocator(kind="row_composite")
        with RetrievalStore(project).open() as store:
            part = StoredPart("composite:part", "row_composite", locator)
            representation = CachedRepresentation(
                "composite:representation",
                "space:test",
                "composite:input",
                native.pack_vector((1.0, 0.0, 0.0), dimensions=3),
                3,
            )
            store.begin_preparation(revision, recipe_id="row:v1", channel="row")
            assert store.publish_source(
                SourcePublication(
                    source_revision=revision,
                    recipe_id="row:v1",
                    channel="row",
                    parts=(part,),
                    bindings=(
                        StoredBinding(
                            "composite:binding",
                            sheet,
                            row,
                            dependencies=(
                                BindingDependency(row, title, title_revision),
                                BindingDependency(row, body, body_revision),
                            ),
                        ),
                    ),
                    representations=(representation,),
                    attachments=(
                        PartRepresentation(
                            part.part_id, representation.representation_id, "row"
                        ),
                    ),
                )
            )

        request = SearchRequest(
            scope=RetrievalScope(sheets=(SheetScope(sheet),)),
            query=TextQuery("row"),
            mode="semantic",
            group_by="row",
            space_id="space:test",
        )
        vectors = SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),))
        assert len(search(project, request, semantic_vectors=vectors).results) == 1
        if sparse:
            project.apply_edits(
                [{"row_id": row, "column_id": body, "value": "new body"}]
            )
            assert search(project, request, semantic_vectors=vectors).results == ()
            return

        project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (title,))
        project.db.commit()
        assert search(project, request, semantic_vectors=vectors).results == ()

        project.db.execute("UPDATE columns SET hidden=0 WHERE id=?", (title,))
        project.db.execute(
            "UPDATE current_cells SET retrieval_revision=retrieval_revision+1 "
            "WHERE row_id=? AND column_id=?",
            (row, title),
        )
        project.db.commit()
        assert search(project, request, semantic_vectors=vectors).results == ()
    finally:
        project.close()


def test_overlapping_moments_group_to_best_part(tmp_path):
    project = Project.create(tmp_path / "moments.frisket", name="moments")
    try:
        sheet = project.add_sheet("Videos")
        body = project.add_column(sheet, "body")
        text = "first second third fourth fifth sixth"
        [row] = project.add_rows(sheet, [{"body": text}], {"body": body})
        revision = read_binding_revision(project.db, row_id=row, column_id=body)
        assert revision is not None
        with RetrievalStore(project).open() as store:
            _publish(
                store,
                row_id=row,
                sheet_id=sheet,
                column_id=body,
                text=text,
                vector=(1.0, 0.0, 0.0),
                version=revision,
                parts=(
                    ("moment:1", (1.0, 0.0, 0.0), 0, 5),
                    ("moment:2", (0.995, 0.1, 0.0), 6, 12),
                    ("moment:3", (0.98, 0.2, 0.0), 13, 18),
                    ("moment:4", (0.954, 0.3, 0.0), 19, 25),
                    ("moment:5", (0.9165, 0.4, 0.0), 26, 31),
                    ("moment:6", (0.8660254, 0.5, 0.0), 32, 37),
                ),
            )

        grouped = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery("moment"),
                mode="semantic",
                group_by="source",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        parts = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery("moment"),
                mode="semantic",
                group_by="part",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        hybrid = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery("first"),
                mode="hybrid",
                group_by="source",
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert [group.primary.part.part_id for group in grouped.results] == ["moment:1"]
        assert [hit.part.part_id for hit in grouped.results[0].supporting] == [
            "moment:2",
            "moment:3",
            "moment:4",
        ]
        assert grouped.results[0].additional_count == 2
        assert [group.primary.part.part_id for group in parts.results] == [
            "moment:1",
            "moment:2",
            "moment:3",
            "moment:4",
            "moment:5",
            "moment:6",
        ]
        assert len(hybrid.results[0].supporting) == 3
        assert {hit.part.part_id for hit in hybrid.results[0].supporting} >= {
            "moment:2",
            "moment:3",
        }
        assert hybrid.results[0].additional_count == 3
        assert {item.method for item in hybrid.results[0].primary.contributions} == {
            "lexical",
            "semantic",
        }
    finally:
        project.close()


def test_row_groups_bound_scoped_locations_and_report_hidden_count(tmp_path):
    project = Project.create(tmp_path / "row-supporting.frisket", name="row-supporting")
    try:
        sheet = project.add_sheet("Documents")
        columns = [project.add_column(sheet, f"body_{index}") for index in range(8)]
        values = {f"body_{index}": f"target text {index}" for index in range(8)}
        [row] = project.add_rows(
            sheet,
            [values],
            {f"body_{index}": column for index, column in enumerate(columns)},
        )
        with RetrievalStore(project).open() as store:
            for index, column in enumerate(columns):
                revision = read_binding_revision(
                    project.db, row_id=row, column_id=column
                )
                assert revision is not None
                _publish(
                    store,
                    row_id=row,
                    sheet_id=sheet,
                    column_id=column,
                    text=values[f"body_{index}"],
                    vector=(0.6, 0.8, 0.0) if index == 0 else (1.0, 0.0, 0.0),
                    version=revision,
                )
        project.db.execute(
            "UPDATE current_cells SET retrieval_revision=retrieval_revision+1 "
            "WHERE row_id=? AND column_id=?",
            (row, columns[5]),
        )
        project.db.commit()
        scope = RetrievalScope((SheetScope(sheet, column_ids=tuple(columns[:6])),))

        lexical = search(
            project,
            SearchRequest(
                scope=scope,
                query=TextQuery("target"),
                mode="lexical",
                group_by="row",
            ),
        )
        semantic = search(
            project,
            SearchRequest(
                scope=scope,
                query=TextQuery("target"),
                mode="semantic",
                group_by="row",
                space_id="space:test",
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        hybrid = search(
            project,
            SearchRequest(
                scope=scope,
                query=TextQuery("target"),
                mode="hybrid",
                group_by="row",
                space_id="space:test",
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        for response in (lexical, semantic):
            [group] = response.results
            assert len(group.supporting) == 3
            assert group.additional_count == 1
            returned = {group.primary.binding.column_id} | {
                hit.binding.column_id for hit in group.supporting
            }
            assert returned <= set(columns[:5])
        [hybrid_group] = hybrid.results
        assert len(hybrid_group.supporting) == 3
        assert hybrid_group.additional_count == 6
        assert {item.method for item in hybrid_group.primary.contributions} == {
            "semantic"
        }
        assert any(
            hit.contributions[0].method == "lexical" for hit in hybrid_group.supporting
        )
        assert any(
            hit.contributions[0].method == "lexical"
            and hit.part.source != hybrid_group.primary.part.source
            for hit in hybrid_group.supporting
        )
    finally:
        project.close()


def test_source_hit_counts_other_authorized_bindings(tmp_path):
    project = Project.create(tmp_path / "binding-count.frisket", name="binding-count")
    try:
        sheet = project.add_sheet("Documents")
        body = project.add_column(sheet, "body")
        rows = project.add_rows(
            sheet,
            [{"body": "shared target"}, {"body": "shared target"}],
            {"body": body},
        )
        revisions = [
            read_binding_revision(project.db, row_id=row, column_id=body)
            for row in rows
        ]
        assert all(revision is not None for revision in revisions)
        with RetrievalStore(project).open() as store:
            _publish(
                store,
                row_id=rows[0],
                sheet_id=sheet,
                column_id=body,
                text="shared target",
                vector=(1.0, 0.0, 0.0),
                version=revisions[0],
                extra_bindings=(
                    StoredBinding(
                        "binding:shared:second",
                        sheet,
                        rows[1],
                        column_id=body,
                        dependencies=(BindingDependency(rows[1], body, revisions[1]),),
                    ),
                ),
            )

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope((SheetScope(sheet),)),
                query=TextQuery("target"),
                mode="semantic",
                group_by="source",
                space_id="space:test",
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        [group] = response.results
        assert group.primary.additional_binding_count == 1
        assert group.supporting == ()
        assert group.additional_count == 0
    finally:
        project.close()


@pytest.mark.parametrize(
    ("group_by", "group_count", "hits_per_group"),
    (("source", 1, 4), ("row", 2, 4), ("part", 2, 2)),
)
def test_semantic_group_aggregation_preserves_global_binding_counts_and_channels(
    tmp_path, group_by, group_count, hits_per_group
):
    project = Project.create(tmp_path / f"aggregate-{group_by}.frisket")
    try:
        sheet = project.add_sheet("Documents")
        body = project.add_column(sheet, "body")
        rows = project.add_rows(
            sheet,
            [{"body": "shared target"}, {"body": "shared target"}],
            {"body": body},
        )
        revisions = [
            read_binding_revision(project.db, row_id=row, column_id=body)
            for row in rows
        ]
        assert None not in revisions
        revision = SourceRevision(
            f"cell:{rows[0]}:{body}",
            "text",
            "retrieval_revision",
            revisions[0],
            input_hash("shared target"),
        )
        locator = SourceLocator(kind="row_composite")
        parts = tuple(
            StoredPart(f"part:{index}", "text_passage", locator) for index in range(2)
        )
        representations = tuple(
            CachedRepresentation(
                f"representation:{index}",
                "space:test",
                f"input:{index}",
                native.pack_vector((1.0, 0.0, 0.0), dimensions=3),
                3,
            )
            for index in range(2)
        )
        attachments = tuple(
            PartRepresentation(part.part_id, representation.representation_id, channel)
            for part, representation in zip(parts, representations, strict=True)
            for channel in ("text", "transcript")
        )
        with RetrievalStore(project).open() as store:
            store.begin_preparation(revision, recipe_id="ties:v1", channel="text")
            assert store.publish_source(
                SourcePublication(
                    source_revision=revision,
                    recipe_id="ties:v1",
                    channel="text",
                    parts=parts,
                    bindings=(
                        StoredBinding(
                            "binding:z",
                            sheet,
                            rows[0],
                            column_id=body,
                            dependencies=(
                                BindingDependency(rows[0], body, revisions[0]),
                            ),
                        ),
                        StoredBinding(
                            "binding:a",
                            sheet,
                            rows[1],
                            column_id=body,
                            dependencies=(
                                BindingDependency(rows[1], body, revisions[1]),
                            ),
                        ),
                    ),
                    representations=representations,
                    attachments=attachments,
                )
            )

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope((SheetScope(sheet),)),
                query=TextQuery("target"),
                mode="semantic",
                group_by=group_by,
                space_id="space:test",
                limit=10,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert len(response.results) == group_count
        for group in response.results:
            hits = (group.primary, *group.supporting)
            assert len(hits) == hits_per_group
            assert {hit.contributions[0].channel for hit in hits} == {
                "text",
                "transcript",
            }
            assert {hit.additional_binding_count for hit in hits} == {1}
            assert group.additional_count == 0
        if group_by != "row":
            assert {group.primary.binding.binding_id for group in response.results} == {
                "binding:a"
            }
    finally:
        project.close()


def test_repeated_sheet_scope_union_does_not_leak_columns_between_rows(tmp_path):
    project = Project.create(tmp_path / "scope-union.frisket", name="scope-union")
    try:
        sheet = project.add_sheet("Rows")
        left = project.add_column(sheet, "left")
        right = project.add_column(sheet, "right")
        rows = project.add_rows(
            sheet,
            [
                {"left": "allowed one", "right": "other"},
                {"left": "forbidden", "right": "allowed two"},
            ],
            {"left": left, "right": right},
        )
        with RetrievalStore(project).open() as store:
            first_left_revision = read_binding_revision(
                project.db, row_id=rows[0], column_id=left
            )
            second_right_revision = read_binding_revision(
                project.db, row_id=rows[1], column_id=right
            )
            second_left_revision = read_binding_revision(
                project.db, row_id=rows[1], column_id=left
            )
            assert None not in (
                first_left_revision,
                second_right_revision,
                second_left_revision,
            )
            _publish(
                store,
                row_id=rows[0],
                sheet_id=sheet,
                column_id=left,
                text="allowed one",
                vector=(0.8, 0.6, 0.0),
                version=first_left_revision,
            )
            _publish(
                store,
                row_id=rows[1],
                sheet_id=sheet,
                column_id=right,
                text="allowed two",
                vector=(0.6, 0.8, 0.0),
                version=second_right_revision,
            )
            _publish(
                store,
                row_id=rows[1],
                sheet_id=sheet,
                column_id=left,
                text="forbidden",
                vector=(1.0, 0.0, 0.0),
                version=second_left_revision,
            )

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(
                    sheets=(
                        SheetScope(sheet, row_ids=(rows[0],)),
                        SheetScope(sheet, row_ids=(rows[1],), column_ids=(right,)),
                    )
                ),
                query=TextQuery("allowed"),
                mode="semantic",
                group_by="part",
                space_id="space:test",
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert {
            (hit.primary.binding.row_id, hit.primary.binding.column_id)
            for hit in response.results
        } == {
            (rows[0], left),
            (rows[1], right),
        }
    finally:
        project.close()


def test_search_honors_cancellation_and_injected_reranker(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        request = SearchRequest(
            scope=_included_scope(sheet, body),
            query=TextQuery("target"),
            mode="lexical",
            group_by="source",
            limit=2,
            ranking=RankingPolicy(rerank="on"),
        )
        response = search(
            project,
            request,
            reranker=lambda _query, documents: [
                1.0 if "distant" in document else 0.0 for document in documents
            ],
        )
        assert response.results[0].primary.binding.row_id == rows[2]
        assert response.results[0].primary.rank_score == 1.0

        class Cancelled:
            def is_set(self) -> bool:
                return True

        with pytest.raises(InterruptedError, match="stopped"):
            search(
                project,
                request,
                reranker=lambda _q, docs: [0.0] * len(docs),
                cancel_event=Cancelled(),
            )
    finally:
        project.close()


def test_coverage_is_scoped_and_unavailable_without_eligible_records(tmp_path):
    project, sheet, status, _body, _rows = _prepare_project(tmp_path)
    try:
        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet, column_ids=(status,)),)),
                query=TextQuery("target"),
                mode="semantic",
                group_by="source",
                space_id="space:test",
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert response.results == ()
        assert response.coverage.state == "unavailable"
        assert response.coverage.channels[0].state == "unavailable"
        assert response.coverage.channels[0].prepared_count is None
        assert response.coverage.channels[0].failed_count is None
    finally:
        project.close()


@pytest.mark.parametrize(
    ("column_type", "mime_type"),
    (("file", "application/pdf"), ("audio", "audio/wav")),
)
def test_lexical_coverage_reports_scoped_unprepared_media_without_intent(
    tmp_path, column_type, mime_type
):
    from frisket.search import drain_index

    project = Project.create(tmp_path / f"unprepared-{column_type}.frisket")
    try:
        sheet = project.add_sheet("Media")
        status = project.add_column(sheet, "status", type="category")
        media = project.add_column(sheet, "media", type=column_type)
        digest = project.add_blob(b"media", mime=mime_type)
        project.add_rows(
            sheet,
            [
                {
                    "status": "included",
                    "media": {"blob": digest, "mime": mime_type},
                },
                {
                    "status": "excluded",
                    "media": {"blob": digest, "mime": mime_type},
                },
            ],
            {"status": status, "media": media},
        )
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(
                    sheets=(
                        SheetScope(
                            sheet,
                            column_ids=(media,),
                            filter_json='{"status":{"eq":"included"}}',
                        ),
                    )
                ),
                query=TextQuery("needle"),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        assert response.coverage.state == "partial"
        assert response.coverage.searched_channels == ("lexical",)
        gap = next(
            item
            for item in response.coverage.channels
            if item.channel == "search_preparation"
        )
        assert gap == ChannelCoverage(
            channel="search_preparation",
            state="unavailable",
            eligible_count=1,
            prepared_count=0,
            failed_count=0,
            reason="media_needs_preparation",
            gap_sheet_ids=(sheet,),
        )
    finally:
        project.close()


def test_coverage_reports_new_media_before_its_sidecar_binding_is_indexed(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "unindexed-media.frisket")
    try:
        seed_sheet = project.add_sheet("Seed")
        seed = project.add_column(seed_sheet, "seed")
        project.add_rows(seed_sheet, [{"seed": "indexed"}], {"seed": seed})
        drain_index(project)

        media_sheet = project.add_sheet("Media")
        media = project.add_column(media_sheet, "media", type="file")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        project.add_rows(
            media_sheet,
            [{"media": {"blob": digest, "mime": "application/pdf"}}],
            {"media": media},
        )

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(),
                query=TextQuery("not-yet-searchable"),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        gap = next(
            item
            for item in response.coverage.channels
            if item.channel == "search_preparation"
        )
        assert (gap.eligible_count, gap.prepared_count) == (1, 0)
        assert gap.state == "unavailable"
        assert gap.gap_sheet_ids == (media_sheet,)
    finally:
        project.close()


def test_coverage_counts_an_imported_pdf_page_instead_of_its_rendered_image(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "imported-pdf-coverage.frisket")
    try:
        sheet = project.add_sheet("Imported PDF")
        document = project.add_column(sheet, "source", type="file")
        page_image = project.add_column(sheet, "page image", type="image")
        unrelated_image = project.add_column(sheet, "other image", type="image")
        prepared_text = project.add_column(sheet, "prepared text")
        pdf_hash = project.add_blob(b"pdf", mime="application/pdf")
        page_hash = project.add_blob(b"page", mime="image/png")
        unrelated_hash = project.add_blob(b"other", mime="image/png")
        row = project.add_rows(
            sheet,
            [
                {
                    "source": {
                        "blob": pdf_hash,
                        "mime": "application/pdf",
                        "page": 1,
                    },
                    "page image": {"blob": page_hash, "mime": "image/png"},
                    "other image": {
                        "blob": unrelated_hash,
                        "mime": "image/png",
                    },
                }
            ],
            {
                "source": document,
                "page image": page_image,
                "other image": unrelated_image,
            },
        )[0]
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=pdf_hash,
            page_count=1,
        )
        span = record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="page",
            page_start=1,
            page_end=1,
            preview={"blob_hash": page_hash, "mime": "image/png"},
        )
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                "kind": "source_cell",
                "sheet_id": sheet,
                "row_id": row,
                "column_id": page_image,
            },
            spans=[{"span_id": int(span["id"])}],
            sheet_id=sheet,
            row_id=row,
            column_id=page_image,
            link_role="primary_support",
        )
        drain_index(project)

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(),
                query=TextQuery("needle"),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        gap = next(
            item
            for item in response.coverage.channels
            if item.channel == "search_preparation"
        )
        assert (gap.eligible_count, gap.prepared_count) == (2, 0)

        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                "kind": "source_cell",
                "sheet_id": sheet,
                "row_id": row,
                "column_id": document,
            },
            spans=[{"span_id": int(span["id"])}],
            sheet_id=sheet,
            row_id=row,
            column_id=document,
            link_role="primary_support",
        )
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("import.pdf", commit=False)
        prepared = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(PreparedPageDraft(1, "native imported text"),),
            page_number=1,
        )
        project.db.commit()
        project.apply_edits(
            [{"row_id": row, "column_id": prepared_text, "value": prepared}]
        )

        prepared_response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(),
                query=TextQuery("needle"),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )
        prepared_gap = next(
            item
            for item in prepared_response.coverage.channels
            if item.channel == "search_preparation"
        )
        assert (prepared_gap.eligible_count, prepared_gap.prepared_count) == (2, 1)
    finally:
        project.close()


def test_material_coverage_counts_exact_current_prepared_binding_appearances(tmp_path):
    from frisket.search import drain_index

    project = Project.create(tmp_path / "prepared-media-coverage.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        prepared_text = project.add_column(sheet, "prepared text")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        rows = project.add_rows(
            sheet,
            [
                {
                    "document": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 1,
                    }
                },
                {
                    "document": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 2,
                    }
                },
            ],
            {"document": document},
        )
        # Pin the source media in the retrieval inventory before its ordinary
        # prepared text result exists.
        drain_index(project)
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.read_pdf_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=digest,
            page_count=2,
            source_sheet_id=sheet,
            source_row_id=rows[0],
            source_column_id=document,
        )
        prepared = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(
                PreparedPageDraft(1, "searchable prepared text"),
                PreparedPageDraft(2, "different page text"),
            ),
        )
        prepared = PreparedContentStore(project).stage_selector(
            set_id=PreparedContentStore(project).resolve(prepared.ref_id).set_id,
            page_number=1,
        )
        project.db.commit()
        project.apply_edits(
            [{"row_id": rows[0], "column_id": prepared_text, "value": prepared}]
        )

        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(
                    sheets=(SheetScope(sheet, column_ids=(document,)),)
                ),
                query=TextQuery("needle"),
                mode="lexical",
                ranking=RankingPolicy(rerank="off"),
            ),
        )

        gap = next(
            item
            for item in response.coverage.channels
            if item.channel == "search_preparation"
        )
        assert (gap.eligible_count, gap.prepared_count) == (2, 1)
        assert gap.state == "unavailable"
    finally:
        project.close()


def test_coverage_includes_failed_channel_without_a_top_k_hit(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        revision = read_binding_revision(project.db, row_id=rows[1], column_id=body)
        assert revision is not None
        source = SourceRevision(
            f"cell:{rows[1]}:{body}",
            "text",
            "retrieval_revision",
            revision,
            input_hash("target eligible second"),
        )
        with RetrievalStore(project).open() as store:
            store.begin_preparation(
                source,
                recipe_id="transcript:v1",
                channel="transcript",
                space_id="space:test",
            )
            assert store.fail_preparation(
                source,
                recipe_id="transcript:v1",
                channel="transcript",
                space_id="space:test",
                error_code="encoder_failed",
                error_message="transcript preparation failed",
            )

        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="semantic",
                group_by="source",
                space_id="space:test",
                limit=1,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )

        assert len(response.results) == 1
        assert response.coverage.state == "partial"
        assert response.coverage.searched_channels == ("text",)
        assert {item.channel: item.state for item in response.coverage.channels} == {
            "text": "ready",
            "transcript": "failed",
        }
    finally:
        project.close()


@pytest.mark.parametrize(
    "channel,space_id,expected",
    [
        ("lexical", None, "partial"),
        ("transcript", "other-space", "complete"),
    ],
)
def test_coverage_respects_preparation_channel_and_space(
    tmp_path, channel, space_id, expected
):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        revision = read_binding_revision(project.db, row_id=rows[1], column_id=body)
        source = SourceRevision(
            f"cell:{rows[1]}:{body}",
            "text",
            "retrieval_revision",
            revision,
            input_hash("target eligible second"),
        )
        with RetrievalStore(project) as store:
            store.begin_preparation(
                source, recipe_id="pending:v1", channel=channel, space_id=space_id
            )
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="lexical" if channel == "lexical" else "semantic",
                space_id="space:test",
                ranking=RankingPolicy(rerank="off"),
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        assert response.coverage.state == expected
        assert "transcript" not in {item.channel for item in response.coverage.channels}
    finally:
        project.close()


def test_unavailable_work_in_ready_channel_is_partial(tmp_path):
    project, sheet, _status, body, rows = _prepare_project(tmp_path)
    try:
        revision = read_binding_revision(project.db, row_id=rows[1], column_id=body)
        source = SourceRevision(
            f"cell:{rows[1]}:{body}",
            "text",
            "retrieval_revision",
            revision,
            input_hash("target eligible second"),
        )
        with RetrievalStore(project) as store:
            store.begin_preparation(
                source, recipe_id="optional:v1", channel="text", space_id="space:test"
            )
            store.mark_preparation_unavailable(
                source,
                recipe_id="optional:v1",
                channel="text",
                space_id="space:test",
                reason="missing_runtime",
            )
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="semantic",
                space_id="space:test",
                ranking=RankingPolicy(rerank="off"),
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        assert response.results
        assert response.coverage.state == "partial"
    finally:
        project.close()


def test_named_scope_does_not_claim_readiness_from_other_membership(tmp_path):
    project, sheet, _status, body, _rows = _prepare_project(tmp_path)
    try:
        response = search(
            project,
            SearchRequest(
                scope=_included_scope(sheet, body),
                query=TextQuery("target"),
                mode="semantic",
                space_id="space:test",
                named_index_id="nonexistent",
                ranking=RankingPolicy(rerank="off"),
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        assert not response.results
        assert response.coverage.state == "unavailable"
        assert not response.coverage.searched_channels
    finally:
        project.close()


def test_hybrid_reranks_context_before_limiting_results(tmp_path):
    project = Project.create(tmp_path / "rerank.frisket", name="rerank")
    try:
        sheet = project.add_sheet("Documents")
        body = project.add_column(sheet, "body")
        texts = [
            "target " + "context " * 25 + ending
            for ending in ("ordinary", "other", "winner & details")
        ]
        rows = project.add_rows(
            sheet, [{"body": text} for text in texts], {"body": body}
        )
        with RetrievalStore(project).open() as store:
            for row, text in zip(rows, texts, strict=True):
                _publish(
                    store,
                    row_id=row,
                    sheet_id=sheet,
                    column_id=body,
                    text=text,
                    vector=(1.0, 0.0, 0.0),
                    version=read_binding_revision(
                        project.db, row_id=row, column_id=body
                    ),
                )
        request = SearchRequest(
            scope=RetrievalScope((SheetScope(sheet),)),
            query=TextQuery("target"),
            mode="hybrid",
            limit=1,
            space_id="space:test",
            ranking=RankingPolicy(rerank="on"),
        )
        seen = []

        def reranker(query, documents):
            assert query == "target"
            seen.extend(documents)
            assert all("<b>" not in doc and "&amp;" not in doc for doc in documents)
            return [10.0 if "winner & details" in doc else 0.0 for doc in documents]

        result = search(
            project,
            request,
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
            reranker=reranker,
        )
        assert len(seen) == 3
        assert [group.primary.binding.row_id for group in result.results] == [rows[2]]
        assert "&amp;" not in result.results[0].primary.snippet
    finally:
        project.close()


def test_recreated_sidecar_reports_preparing_before_worker_binds_it(tmp_path):
    project, sheet, _status, body, _rows = _prepare_project(tmp_path)
    try:
        for suffix in ("", "-wal", "-shm"):
            (project.path / ("project.retrieval.db" + suffix)).unlink(missing_ok=True)
        # An analytical writer may recreate the cache before the lexical worker.
        with RetrievalStore(project).open():
            pass
        request = SearchRequest(
            scope=RetrievalScope(sheets=(SheetScope(sheet, column_ids=(body,)),)),
            query=TextQuery("target"),
            mode="lexical",
        )
        assert search(project, request).coverage.state == "preparing"
        # A recreated cache must not imply pending work outside visible scope.
        project.db.execute("UPDATE rows SET hidden=1 WHERE sheet_id=?", (sheet,))
        project.db.commit()
        assert search(project, request).coverage.state == "complete"
    finally:
        project.close()


def test_semantic_snippet_bounds_display_without_moving_the_source_range(tmp_path):
    project = Project.create(tmp_path / "semantic-snippet.frisket")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        prefix = "😀 <b>literal</b> & café "
        token = "longpart" * 1000
        text = prefix + token + " Ω tail"
        [row] = project.add_rows(sheet, [{"body": text}], {"body": column})
        revision = read_binding_revision(project.db, row_id=row, column_id=column)
        with RetrievalStore(project).open() as store:
            _publish(
                store,
                row_id=row,
                sheet_id=sheet,
                column_id=column,
                text=text,
                vector=(1.0, 0.0, 0.0),
                version=revision,
                parts=(
                    (
                        "part:bounded",
                        (1.0, 0.0, 0.0),
                        len(prefix),
                        len(prefix) + len(token),
                    ),
                ),
            )
        bind_retrieval_sidecar(project)
        response = search(
            project,
            SearchRequest(
                scope=RetrievalScope(sheets=(SheetScope(sheet),)),
                query=TextQuery("anything"),
                mode="semantic",
                space_id="space:test",
                ranking=RankingPolicy(rerank="off"),
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=((1.0, 0.0, 0.0),)),
        )
        [group] = response.results
        hit = group.primary
        assert hit.precision == "exact"
        assert hit.part.locator.text.start == len(prefix)
        assert hit.part.locator.text.end == len(prefix) + len(token)
        assert len(hit.snippet) <= 520
        assert len(hit.snippet.encode("utf-8")) <= 4096
        assert prefix in hit.snippet
        assert "<b>longpart" in hit.snippet
        assert hit.snippet.endswith("</b>…")
        assert hit.snippet.count("<b>") == hit.snippet.count("</b>") == 2
        assert "FRISKET_SEARCH_HIT" not in hit.snippet
    finally:
        project.close()
