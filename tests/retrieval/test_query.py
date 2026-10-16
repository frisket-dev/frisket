from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.retrieval_lifecycle import read_binding_revision
from frisket.retrieval import native
from frisket.retrieval.contracts import (
    BindingRevision,
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


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def query_native_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    try:
        native.packaged_vec1_path()
    except native.Vec1Unavailable:
        output = tmp_path_factory.mktemp("query-vec1")
        subprocess.run(
            [
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

        monkeypatch.setattr(
            "frisket.retrieval.query.resolve_lexical_source_text", resolve
        )
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


def test_composite_binding_is_invalidated_when_any_dependency_changes(tmp_path):
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
        title_revision = read_binding_revision(project.db, row_id=row, column_id=title)
        body_revision = read_binding_revision(project.db, row_id=row, column_id=body)
        assert title_revision is not None and body_revision is not None
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
        [row] = project.add_rows(sheet, [{"body": "first second"}], {"body": body})
        revision = read_binding_revision(project.db, row_id=row, column_id=body)
        assert revision is not None
        with RetrievalStore(project).open() as store:
            _publish(
                store,
                row_id=row,
                sheet_id=sheet,
                column_id=body,
                text="first second",
                vector=(1.0, 0.0, 0.0),
                version=revision,
                parts=(
                    ("moment:1", (1.0, 0.0, 0.0), 0, 5),
                    ("moment:2", (0.8, 0.6, 0.0), 6, 12),
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

        assert [group.primary.part.part_id for group in grouped.results] == ["moment:1"]
        assert [group.primary.part.part_id for group in parts.results] == [
            "moment:1",
            "moment:2",
        ]
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
