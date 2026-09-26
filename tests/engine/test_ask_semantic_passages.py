from __future__ import annotations

import threading

from frisket.engine.store import Project
from frisket.search import rebuild_index
from frisket.semantic import semantic_passage_search


def _embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] if "needle" in text else [0.0, 1.0] for text in texts]


def test_scoped_passages_cache_coordinates_and_file_cells(tmp_path):
    project = Project.create(tmp_path / "passages.frisket", name="p")
    sheet = project.add_sheet("data")
    text = project.add_column(sheet, "text")
    rows = project.add_rows(
        sheet,
        [{"text": "decoy needle"}, {"text": "x" * 400 + " needle"}],
        {"text": text},
    )
    rebuild_index(project)
    first = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids={rows[1]},
        file_cells=set(),
        query="needle",
        limit=5,
        embed=_embed,
        embed_id="stub/v1",
    )
    assert first["hits"][0]["row_id"] == rows[1]
    assert first["hits"][0]["char_start"] > 0
    assert first["new_embeddings"] > 0
    second = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=set(),
        file_cells={(rows[1], text)},
        query="needle",
        limit=5,
        embed=_embed,
        embed_id="stub/v1",
    )
    assert [hit["row_id"] for hit in second["hits"]] == [rows[1]]
    assert second["new_embeddings"] == 0
    project.close()


def test_budget_and_cancellation_are_honest(tmp_path):
    project = Project.create(tmp_path / "budget.frisket", name="p")
    sheet = project.add_sheet("data")
    text = project.add_column(sheet, "text")
    project.add_rows(sheet, [{"text": "needle " * 100}], {"text": text})
    rebuild_index(project)
    limited = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=None,
        file_cells=set(),
        query="needle",
        limit=5,
        embed=_embed,
        embed_id="stub/v2",
        remaining_embeddings=0,
    )
    assert limited["coverage"]["reason"] == "embedding_budget"
    stop = threading.Event()
    stop.set()
    cancelled = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=None,
        file_cells=set(),
        query="needle",
        limit=5,
        embed=_embed,
        embed_id="stub/v2",
        cancel_event=stop,
    )
    assert cancelled["coverage"]["reason"] == "cancelled"
    project.close()


def test_whole_sheet_ignores_file_narrowing_and_cache_identity(tmp_path):
    project = Project.create(tmp_path / "whole.frisket", name="p")
    sheet = project.add_sheet("data")
    text = project.add_column(sheet, "text")
    rows = project.add_rows(
        sheet, [{"text": "needle one"}, {"text": "needle two"}], {"text": text}
    )
    rebuild_index(project)
    whole = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=None,
        file_cells={(rows[0], text)},
        query="needle",
        limit=5,
        embed=_embed,
        embed_id="stub/v3",
    )
    assert {hit["row_id"] for hit in whole["hits"]} == set(rows)
    isolated = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=None,
        file_cells=set(),
        query="needle",
        limit=5,
        embed=_embed,
        embed_id="stub/v4",
    )
    assert isolated["new_embeddings"] == whole["new_embeddings"]
    project.close()


def test_batches_are_bounded_and_partial_failure_keeps_first_batch_cached(tmp_path):
    project = Project.create(tmp_path / "batches.frisket", name="p")
    sheet = project.add_sheet("data")
    text = project.add_column(sheet, "text")
    project.add_rows(
        sheet,
        [{"text": "".join(f"{i:03d}" + "x" * 317 for i in range(20))}],
        {"text": text},
    )
    rebuild_index(project)
    batches: list[int] = []

    def flaky(values):
        batches.append(len(values))
        if len(batches) == 2:
            raise RuntimeError("offline")
        return _embed(values)

    result = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=None,
        file_cells=set(),
        query="needle",
        limit=2,
        embed=flaky,
        embed_id="stub/fail",
    )
    assert batches == [16, 4]
    assert result["new_embeddings"] == 16
    assert result["coverage"]["reason"] == "embedding_failed"
    project.close()


def test_unicode_ranges_and_cancel_after_completed_batch(tmp_path):
    project = Project.create(tmp_path / "unicode.frisket", name="p")
    sheet = project.add_sheet("data")
    text = project.add_column(sheet, "text")
    value = "界" * 110 + "needle" + "界" * 110
    project.add_rows(sheet, [{"text": value}], {"text": text})
    rebuild_index(project)
    stop = threading.Event()
    calls = []

    def cancelling(values):
        calls.append(len(values))
        stop.set()
        return _embed(values)

    result = semantic_passage_search(
        project,
        sheet_id=sheet,
        row_ids=None,
        file_cells=set(),
        query="needle",
        limit=2,
        embed=cancelling,
        embed_id="stub/cancel",
        cancel_event=stop,
    )
    assert calls == [3]
    assert result["new_embeddings"] == 3
    assert result["coverage"]["reason"] == "cancelled"
    project.close()
