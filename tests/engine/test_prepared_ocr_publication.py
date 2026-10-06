from __future__ import annotations

import copy
import json
from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.prepared_ocr import (
    hydrate_prepared_results,
    prepare_ocr_results,
)
from frisket.engine.store.prepared_content import PreparedContentStore


def _setup(project):
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "document", type="file")
    blob = project.add_blob(b"original PDF", mime="application/pdf", filename="a.pdf")
    row = project.add_rows(sheet, [{}], {})[0]
    op = project.append_op("media.ocr", {}, label="OCR")
    run = project.db.execute(
        "INSERT INTO runs(op_id,sheet_id,action_kind) VALUES(?,?,'media.ocr')",
        (op, sheet),
    ).lastrowid
    project.db.commit()
    text = "First page\nline two\n\n\n\nFinal page"
    call = {
        "kind": "ocr_read",
        "call_id": "one-call",
        "engine": "rapidocr",
        "source": {
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
            "blob_hash": blob,
            "mime": "application/pdf",
            "page_count": 3,
        },
        "options": {},
        "text": text,
        "blocks": [],
        "page_images": {},
        "pages": [
            {"text": "First page\nline two", "blocks": []},
            {"text": "", "blocks": []},
            {"text": "Final page", "blocks": []},
        ],
    }
    result = {
        "value": text,
        "row_file_calls": [call],
        "prepared_ocr_call_id": "one-call",
    }
    return run, result


def test_compact_checkpoint_reopens_without_repeating_ocr(tmp_path):
    path = tmp_path / "ocr.frisket"
    with closing(Project.create(path)) as project:
        run, result = _setup(project)
        original = result["value"]
        replay = {"text": copy.deepcopy(result)}
        checkpoints = EffectCheckpointStore(project.db)
        identity = dict(
            family="test",
            group_key="1",
            unit_key="1",
            action_kind="media.ocr",
            identity="same",
        )
        checkpoints.reserve("checkpoint", **identity, authorized_attempt_id=None)

        def accrue(_checkpoint):
            prepare_ocr_results(project, run, [result, *replay.values()])
            return 0.0

        checkpoints.complete("checkpoint", **identity, payload=replay, accrue=accrue)
        stored = json.loads(
            project.db.execute("SELECT payload FROM effect_checkpoints").fetchone()[0]
        )
        assert stored["text"]["value"] is None
        fact = stored["text"]["row_file_calls"][0]
        assert not {"text", "pages", "blocks"} & fact.keys()
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM prepared_page_versions"
            ).fetchone()[0]
            == 3
        )
        ref = stored["text"]["prepared_ref_id"]
        assert PreparedContentStore(project).resolve(ref).text == original
    with closing(Project(path)) as project:
        replay = EffectCheckpointStore(project.db).get("checkpoint")["payload"]
        hydrate_prepared_results(project, list(replay.values()))
        assert replay["text"]["value"] == original
        assert replay["text"]["prepared_ref_id"] == ref


def test_failed_checkpoint_rolls_back_prepared_pages(tmp_path):
    with closing(Project.create(tmp_path / "rollback.frisket")) as project:
        run, result = _setup(project)
        project.db.execute("BEGIN IMMEDIATE")
        result["value"] = "rewritten but incorrectly marked"
        with pytest.raises(ValueError, match="does not match"):
            prepare_ocr_results(project, run, [result])
        project.db.rollback()
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM prepared_page_versions"
            ).fetchone()[0]
            == 0
        )


def test_ocr_uses_the_ordinary_unicode_repair_boundary(tmp_path):
    with closing(Project.create(tmp_path / "unicode.frisket")) as project:
        run, result = _setup(project)
        result["value"] = "Broken \ud800 text"
        call = result["row_file_calls"][0]
        call["source"]["page_count"] = 1
        call["text"] = result["value"]
        call["pages"] = [{"text": result["value"], "blocks": []}]
        project.db.execute("BEGIN IMMEDIATE")
        prepare_ocr_results(project, run, [result])
        hydrate_prepared_results(project, [result])
        project.db.commit()
        assert result["value"] == "Broken \ufffd text"
