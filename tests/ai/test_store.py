"""Deterministic core tests for the project store."""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.result_generations import GenerationSealedError
from frisket.engine.store.runs import RunResultStore
from helpers import run_writer_authority_fixture, write_claimed_test_results


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "test.frisket", name="test")
    yield p
    p.close()


def make_sheet(p: Project, n_rows: int = 5) -> tuple[int, dict[str, int]]:
    sheet = p.add_sheet("data")
    cols = {"name": p.add_column(sheet, "name"), "text": p.add_column(sheet, "text")}
    p.add_rows(
        sheet,
        [{"name": f"row{i}", "text": f"content {i}"} for i in range(n_rows)],
        cols,
    )
    return sheet, cols


class TestBasics:
    def test_create_open_roundtrip(self, tmp_path):
        p = Project.create(tmp_path / "a.frisket", name="hello")
        assert p.get_meta("name") == "hello"
        p.close()
        p2 = Project(tmp_path / "a.frisket")
        assert p2.get_meta("name") == "hello"
        p2.close()

    def test_source_values(self, project):
        sheet, cols = make_sheet(project, 3)
        vals = project.get_values(sheet, cols["name"])
        assert list(vals.values()) == ["row0", "row1", "row2"]

    def test_typed_column_rejects_unknown(self, project):
        sheet = project.add_sheet("s")
        with pytest.raises(ValueError):
            project.add_column(sheet, "bad", type="hologram")

    def test_default_hidden_column_remains_available_to_normal_readers(self, project):
        sheet = project.add_sheet("s")
        column_id = project.add_column(
            sheet,
            "provider_metadata",
            type="json",
            default_hidden=True,
        )

        columns = project.columns(sheet)
        assert [column["id"] for column in columns] == [column_id]
        assert bool(columns[0]["hidden"]) is False
        assert bool(columns[0]["default_hidden"]) is True


class TestRunsAndPointers:
    def test_run_results_resolve(self, project):
        sheet, cols = make_sheet(project, 3)
        out_col = project.add_column(sheet, "score", type="number", ai_generated=True)
        op = project.append_op("map", {"action_kind": "map.classify"})
        run = RunResultStore(project).start_run(op, sheet, "map.classify", total_rows=3)
        rows = [
            r["id"]
            for r in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=?", (sheet,)
            )
        ]
        write_claimed_test_results(
            project,
            run,
            [
                {
                    "row_id": rid,
                    "column_id": out_col,
                    "value": i,
                    "tokens_in": 10,
                    "tokens_out": 5,
                    "cost": 0.001,
                    # runs.cost_actual is a projection of these durable facts;
                    # the batch-level `cost` field is progress display only.
                    "model_calls": [
                        {
                            "id": f"call-store-{rid}",
                            "fact_version": "frisket.model-call-fact.v1",
                            "capability": "classify",
                            "engine": "fixture",
                            "provider": "fixture",
                            "provider_kind": "test",
                            "credential_source": "platform_key",
                            "provider_cost_usd": 0.001,
                        }
                    ],
                }
                for i, rid in enumerate(rows)
            ],
        )
        RunResultStore(project).finish_run(run)
        RunResultStore(project).point_column_at_run(op, out_col, run)
        vals = project.get_values(sheet, out_col)
        assert sorted(vals.values()) == [0, 1, 2]
        r = project.db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        assert r["completed_rows"] == 3
        assert r["cost_actual"] == pytest.approx(0.003)

    def test_result_writes_idempotent(self, project):
        sheet, cols = make_sheet(project, 1)
        out_col = project.add_column(sheet, "x", ai_generated=True)
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "test.r", total_rows=1)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        batch = [{"row_id": rid, "column_id": out_col, "value": "a"}]
        write_claimed_test_results(project, run, batch)
        with pytest.raises(GenerationSealedError):
            write_claimed_test_results(project, run, batch)
        n = project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        assert n == 1

    def test_model_calls_written_with_results(self, project):
        sheet, _ = make_sheet(project, 1)
        out_col = project.add_column(sheet, "transcript", ai_generated=True)
        op = project.append_op("map")
        run = RunResultStore(project).start_run(
            op, sheet, "media.transcribe", total_rows=1
        )
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        write_claimed_test_results(
            project,
            run,
            [
                {
                    "row_id": rid,
                    "column_id": out_col,
                    "value": "hello",
                    "cost": 0.02,
                    "model_calls": [
                        {
                            "id": "call-1",
                            "fact_version": "frisket.model-call-fact.v1",
                            "capability": "transcribe",
                            "engine": "parakeet-tdt",
                            "provider": "modal",
                            "provider_kind": "platform_function",
                            "model_ids": ["parakeet-tdt"],
                            "credential_source": "platform_key",
                            "provider_reported_cost_usd": 0.01,
                            "provider_cost_usd": 0.01,
                            "cost_source": "provider_reported",
                            "units": {"audio_seconds": 1.0},
                            "cache": {},
                            "request_id": "modal-req-1",
                            "warnings": [],
                        }
                    ],
                }
            ],
        )
        calls = RunResultStore(project).model_calls(run)
        assert len(calls) == 1
        call = calls[0]
        assert call["id"] == "call-1"
        assert call["run_id"] == run
        assert call["row_id"] == rid
        assert call["column_id"] == out_col
        assert call["provider"] == "modal"
        # Neutral fact row (split-1b-usage-facts-v1): provider cost + credential
        # provenance are persisted; tariff/billability fields are not columns.
        assert call["provider_cost_usd"] == pytest.approx(0.01)
        assert call["credential_source"] == "platform_key"
        assert not set(call.keys()) & {
            "billable",
            "credit_charge_usd",
            "billing_owner",
        }
        assert json.loads(call["model_ids"]) == ["parakeet-tdt"]
        assert json.loads(call["units"]) == {"audio_seconds": 1.0}

    def test_rerun_creates_new_version_old_kept(self, project):
        sheet, _ = make_sheet(project, 2)
        out_col = project.add_column(sheet, "label", ai_generated=True)
        rows = [r["id"] for r in project.db.execute("SELECT id FROM rows")]

        op1 = project.append_op("map")
        run1 = RunResultStore(project).start_run(op1, sheet, "map.classify")
        write_claimed_test_results(
            project,
            run1,
            [{"row_id": r, "column_id": out_col, "value": "v1"} for r in rows],
        )
        RunResultStore(project).point_column_at_run(op1, out_col, run1)

        op2 = project.append_op("map")
        run2 = RunResultStore(project).start_run(op2, sheet, "map.classify")
        write_claimed_test_results(
            project,
            run2,
            [{"row_id": r, "column_id": out_col, "value": "v2"} for r in rows],
        )
        RunResultStore(project).point_column_at_run(op2, out_col, run2)

        assert set(project.get_values(sheet, out_col).values()) == {"v2"}
        # history intact: run1 values still stored
        old = project.db.execute(
            "SELECT value FROM results WHERE run_id=?", (run1,)
        ).fetchall()
        assert all(json.loads(r["value"]) == "v1" for r in old)


class TestEditsOverlay:
    def test_edit_beats_run(self, project):
        sheet, _ = make_sheet(project, 2)
        out_col = project.add_column(sheet, "label", ai_generated=True)
        rows = [r["id"] for r in project.db.execute("SELECT id FROM rows")]
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "test.c")
        write_claimed_test_results(
            project,
            run,
            [{"row_id": r, "column_id": out_col, "value": "model"} for r in rows],
        )
        RunResultStore(project).point_column_at_run(op, out_col, run)
        project.apply_edits(
            [{"row_id": rows[0], "column_id": out_col, "value": "human"}]
        )
        vals = project.get_values(sheet, out_col)
        assert vals[rows[0]] == "human"
        assert vals[rows[1]] == "model"

    def test_undo_edit_restores_model_value(self, project):
        sheet, _ = make_sheet(project, 1)
        out_col = project.add_column(sheet, "label", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "test.c")
        write_claimed_test_results(
            project, run, [{"row_id": rid, "column_id": out_col, "value": "model"}]
        )
        RunResultStore(project).point_column_at_run(op, out_col, run)
        project.apply_edits([{"row_id": rid, "column_id": out_col, "value": "human"}])
        assert project.get_values(sheet, out_col)[rid] == "human"
        project.undo()
        assert project.get_values(sheet, out_col)[rid] == "model"
        project.redo()
        assert project.get_values(sheet, out_col)[rid] == "human"


class TestUndoRedo:
    def test_undo_run_moves_pointer_back(self, project):
        sheet, _ = make_sheet(project, 1)
        out_col = project.add_column(sheet, "x", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        op1 = project.append_op("map")
        r1 = RunResultStore(project).start_run(op1, sheet, "test.c")
        write_claimed_test_results(
            project, r1, [{"row_id": rid, "column_id": out_col, "value": "first"}]
        )
        RunResultStore(project).point_column_at_run(op1, out_col, r1)
        op2 = project.append_op("map")
        r2 = RunResultStore(project).start_run(op2, sheet, "test.c")
        write_claimed_test_results(
            project, r2, [{"row_id": rid, "column_id": out_col, "value": "second"}]
        )
        RunResultStore(project).point_column_at_run(op2, out_col, r2)

        assert project.get_values(sheet, out_col)[rid] == "second"
        project.undo()
        assert project.get_values(sheet, out_col)[rid] == "first"
        project.undo()
        assert project.get_values(sheet, out_col)[rid] is None
        project.redo()
        assert project.get_values(sheet, out_col)[rid] == "first"
        project.redo()
        assert project.get_values(sheet, out_col)[rid] == "second"

    def test_undo_nothing_returns_none(self, project):
        assert project.undo() is None

    def test_barrier_blocks_undo(self, project):
        project.append_op("compaction", barrier=True)
        with pytest.raises(ValueError):
            project.undo()

    def test_new_op_truncates_redo_future(self, project):
        sheet, _ = make_sheet(project, 1)
        out_col = project.add_column(sheet, "x", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        op1 = project.append_op("map")
        r1 = RunResultStore(project).start_run(op1, sheet, "test.c")
        write_claimed_test_results(
            project, r1, [{"row_id": rid, "column_id": out_col, "value": "a"}]
        )
        RunResultStore(project).point_column_at_run(op1, out_col, r1)
        project.undo()
        # a new op while undone — the undone op stays undone; redo finds nothing new
        project.apply_edits([{"row_id": rid, "column_id": out_col, "value": "fresh"}])
        assert project.get_values(sheet, out_col)[rid] == "fresh"


# ---------- property tests: any op sequence steps back/forward identically ----------


@st.composite
def op_sequences(draw):
    return draw(st.lists(st.sampled_from(["run", "edit"]), min_size=1, max_size=8))


@settings(max_examples=50, deadline=None)
@given(seq=op_sequences(), undos=st.integers(min_value=1, max_value=8))
def test_property_undo_all_then_redo_all_roundtrips(tmp_path_factory, seq, undos):
    tmp = tmp_path_factory.mktemp("prop")
    p = Project.create(tmp / "p.frisket")
    try:
        sheet, _ = make_sheet(p, 2)
        col = p.add_column(sheet, "out", ai_generated=True)
        rows = [r["id"] for r in p.db.execute("SELECT id FROM rows")]
        for i, kind in enumerate(seq):
            if kind == "run":
                op = p.append_op("map")
                run = RunResultStore(p).start_run(op, sheet, "test.c")
                write_claimed_test_results(
                    p,
                    run,
                    [{"row_id": r, "column_id": col, "value": f"run{i}"} for r in rows],
                )
                RunResultStore(p).point_column_at_run(op, col, run)
            else:
                p.apply_edits(
                    [{"row_id": rows[0], "column_id": col, "value": f"edit{i}"}]
                )

        snapshot = p.get_values(sheet, col)
        n_undone = 0
        for _ in range(undos):
            if p.undo() is not None:
                n_undone += 1
        for _ in range(n_undone):
            assert p.redo() is not None
        assert p.get_values(sheet, col) == snapshot
    finally:
        p.close()


@settings(max_examples=25, deadline=None)
@given(seq=op_sequences())
def test_property_full_undo_returns_to_source_state(tmp_path_factory, seq):
    tmp = tmp_path_factory.mktemp("prop2")
    p = Project.create(tmp / "p.frisket")
    try:
        sheet, _ = make_sheet(p, 2)
        col = p.add_column(sheet, "out", ai_generated=True)
        rows = [r["id"] for r in p.db.execute("SELECT id FROM rows")]
        baseline = p.get_values(sheet, col)
        seeded_op_cursor = p.op_cursor
        for i, kind in enumerate(seq):
            if kind == "run":
                op = p.append_op("map")
                run = RunResultStore(p).start_run(op, sheet, "test.c")
                write_claimed_test_results(
                    p,
                    run,
                    [{"row_id": r, "column_id": col, "value": f"run{i}"} for r in rows],
                )
                RunResultStore(p).point_column_at_run(op, col, run)
            else:
                p.apply_edits(
                    [{"row_id": rows[0], "column_id": col, "value": f"edit{i}"}]
                )
        while p.op_cursor > seeded_op_cursor:
            assert p.undo() is not None
        assert p.get_values(sheet, col) == baseline
    finally:
        p.close()


class TestBundle:
    def test_export_import_roundtrip(self, project, tmp_path):
        sheet, cols = make_sheet(project, 3)
        digest = project.add_blob(b"hello media", filename="x.txt", mime="text/plain")
        zip_path = project.export(tmp_path / "out.frisket.zip")
        p2 = Project.import_bundle(zip_path, tmp_path / "imported")
        assert (
            p2.row_count(
                p2.db.execute("SELECT id FROM sheets WHERE name='data'").fetchone()[
                    "id"
                ]
            )
            == 3
        )
        assert p2.read_blob(digest) == b"hello media"
        p2.close()

    def test_import_rejects_tampered_blob(self, project, tmp_path):
        make_sheet(project, 1)
        digest = project.add_blob(b"original")
        zip_path = project.export(tmp_path / "out.zip")
        # tamper inside the zip
        import zipfile

        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path) as zin, zipfile.ZipFile(evil, "w") as zout:
            for item in zin.namelist():
                data = zin.read(item)
                if item.endswith(digest):
                    data = b"tampered!"
                zout.writestr(item, data)
        with pytest.raises(ValueError, match="hash verification"):
            Project.import_bundle(evil, tmp_path / "imp2")

    def test_import_rejects_path_traversal_member(self, project, tmp_path):
        make_sheet(project, 1)
        zip_path = project.export(tmp_path / "out_trav.zip")
        import zipfile

        evil = tmp_path / "evil_traversal.zip"
        with zipfile.ZipFile(zip_path) as zin, zipfile.ZipFile(evil, "w") as zout:
            for item in zin.namelist():
                zout.writestr(item, zin.read(item))
            zout.writestr("../escape.txt", b"pwned")
        target = tmp_path / "imp_traversal"
        with pytest.raises(ValueError, match="escapes the target directory"):
            Project.import_bundle(evil, target)
        assert not (tmp_path / "escape.txt").exists()
        assert not target.exists()

    def test_import_rejects_absolute_path_member(self, project, tmp_path):
        make_sheet(project, 1)
        zip_path = project.export(tmp_path / "out_abs.zip")
        import zipfile

        evil = tmp_path / "evil_absolute.zip"
        with zipfile.ZipFile(zip_path) as zin, zipfile.ZipFile(evil, "w") as zout:
            for item in zin.namelist():
                zout.writestr(item, zin.read(item))
            zout.writestr("/etc/pwned_absolute.txt", b"pwned")
        target = tmp_path / "imp_absolute"
        with pytest.raises(ValueError, match="unsafe absolute path"):
            Project.import_bundle(evil, target)
        assert not target.exists()

    def test_import_rejects_symlink_member(self, project, tmp_path):
        make_sheet(project, 1)
        zip_path = project.export(tmp_path / "out_link.zip")
        import stat
        import zipfile

        evil = tmp_path / "evil_symlink.zip"
        with zipfile.ZipFile(zip_path) as zin, zipfile.ZipFile(evil, "w") as zout:
            for item in zin.namelist():
                zout.writestr(item, zin.read(item))
            link_info = zipfile.ZipInfo("blobs/sneaky_link")
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zout.writestr(link_info, "/etc/passwd")
        target = tmp_path / "imp_symlink"
        with pytest.raises(ValueError, match="is a symlink"):
            Project.import_bundle(evil, target)
        assert not target.exists()

    def test_db_only_export(self, project, tmp_path):
        make_sheet(project, 1)
        project.add_blob(b"big video bytes")
        zip_path = project.export(tmp_path / "slim.zip", include_media=False)
        import zipfile

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "project.db" in names
        assert not any(n.startswith("blobs/") for n in names)


class TestStoreRegressions:
    """Regression coverage for store branching, compaction, and cache limits."""

    def test_redo_cannot_resurrect_discarded_branch(self, project):
        sheet, _ = make_sheet(project, 1)
        col = project.add_column(sheet, "x", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]

        def run_with(value):
            op = project.append_op("map")
            run = RunResultStore(project).start_run(op, sheet, "test.c")
            write_claimed_test_results(
                project, run, [{"row_id": rid, "column_id": col, "value": value}]
            )
            RunResultStore(project).point_column_at_run(op, col, run)

        run_with("a")
        project.undo()  # branch A undone
        run_with("b")  # new branch B — A is discarded
        project.undo()  # undo B
        # redo must restore B, never resurrect A
        project.redo()
        assert project.get_values(sheet, col)[rid] == "b"
        assert project.redo() is None

    def test_repeat_pointing_in_one_op_undoes_to_pre_op_state(self, project):
        sheet, _ = make_sheet(project, 1)
        col = project.add_column(sheet, "x", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        op = project.append_op("map")
        r1 = RunResultStore(project).start_run(op, sheet, "test.c")
        write_claimed_test_results(
            project, r1, [{"row_id": rid, "column_id": col, "value": "v1"}]
        )
        RunResultStore(project).point_column_at_run(op, col, r1)
        r2 = RunResultStore(project).start_run(
            op, sheet, "test.c"
        )  # e.g. resume repoints
        write_claimed_test_results(
            project, r2, [{"row_id": rid, "column_id": col, "value": "v2"}]
        )
        RunResultStore(project).point_column_at_run(op, col, r2)
        project.undo()
        # must be pre-op (None), not intermediate r1
        assert project.get_values(sheet, col)[rid] is None

    def test_hidden_column_revived_on_recreate(self, project):
        sheet, _ = make_sheet(project, 1)
        col = project.add_column(sheet, "score", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        op = project.append_op("map")
        project.set_undo_info(op, {"created_columns": [col]})
        run = RunResultStore(project).start_run(op, sheet, "test.c")
        write_claimed_test_results(
            project, run, [{"row_id": rid, "column_id": col, "value": 5}]
        )
        RunResultStore(project).point_column_at_run(op, col, run)
        project.undo()
        assert "score" not in [c["name"] for c in project.columns(sheet)]
        # re-creating the same name revives instead of UNIQUE-violating
        col2 = project.add_column(sheet, "score", ai_generated=True)
        assert col2 == col
        assert "score" in [c["name"] for c in project.columns(sheet)]


class TestCompactionGC:
    """The store must not be
    append-forever. gc_blobs reclaims unreferenced blobs; compact prunes
    discarded run branches + VACUUMs; neither touches live (reachable) data."""

    def _image_sheet(self, p: Project) -> tuple[int, int]:
        sheet = p.add_sheet("media")
        col = p.add_column(sheet, "media", type="image")
        return sheet, col

    def test_gc_blobs_collects_only_orphans(self, project):
        sheet, col = self._image_sheet(project)
        ref = project.add_blob(
            b"REFERENCED-BYTES", filename="ref.png", mime="image/png"
        )
        orphan = project.add_blob(
            b"ORPHAN-BYTES-XYZ", filename="o.png", mime="image/png"
        )
        project.add_rows(
            sheet, [{"media": {"blob": ref, "mime": "image/png"}}], {"media": col}
        )

        assert ref in project._referenced_blob_hashes()
        assert orphan not in project._referenced_blob_hashes()

        # dry-run reports but does not delete
        dry = project.gc_blobs(dry_run=True)
        assert dry["blobs_removed"] == 1
        assert dry["hashes"] == [orphan]
        with project.materialize_blob(orphan) as path:
            assert path.exists()

        res = project.gc_blobs()
        assert res["blobs_removed"] == 1
        # Blob rows may be pruned, but canonical bytes are retained until the
        # separate reference-safe, restore-window-aware object GC lands.
        assert res["bytes_freed"] == 0
        with project.materialize_blob(orphan) as path:
            assert path.exists()
        with project.materialize_blob(ref) as path:
            assert path.exists()
        hashes = [r["hash"] for r in project.db.execute("SELECT hash FROM blobs")]
        assert hashes == [ref]

    def test_gc_keeps_blobs_referenced_by_results(self, project):
        sheet, col = self._image_sheet(project)
        rid = project.add_rows(sheet, [{}], {})[0]
        blob = project.add_blob(b"RESULT-BLOB", mime="image/png")
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "test.c")
        write_claimed_test_results(
            project, run, [{"row_id": rid, "column_id": col, "value": {"blob": blob}}]
        )
        RunResultStore(project).point_column_at_run(op, col, run)
        # the live column points at this run → blob is reachable
        assert project.gc_blobs(dry_run=True)["blobs_removed"] == 0

    def test_compact_prunes_discarded_runs_and_vacuums(self, project):
        sheet, _ = make_sheet(project, 2)
        col = project.add_column(sheet, "score", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "test.c")
        write_claimed_test_results(
            project, run, [{"row_id": rid, "column_id": col, "value": 5}]
        )
        RunResultStore(project).point_column_at_run(op, col, run)
        project.undo()
        # appending a new op discards the undone branch (no longer redoable)
        project.append_op("noop")
        assert (
            project.db.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()[
                "status"
            ]
            == "discarded"
        )

        summary = project.compact()
        assert summary["results_pruned"] >= 1
        # the discarded run's results are gone, and the empty run row with them
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run,)
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM runs WHERE id=?", (run,)
            ).fetchone()[0]
            == 0
        )
        # live source rows still resolve untouched
        assert len(project.get_values(sheet, project.columns(sheet)[0]["id"])) == 2

    def _discarded_run_with_checkpoints(self, project):
        """A discarded, unreferenced run carrying one ``reserved`` and one
        ``returned`` row-effect checkpoint — the shape compaction prunes."""
        from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

        sheet, _ = make_sheet(project, 2)
        col = project.add_column(sheet, "score", ai_generated=True)
        rids = [r["id"] for r in project.db.execute("SELECT id FROM rows")]
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "test.c")
        RunResultStore(project).point_column_at_run(op, col, run)
        authority = run_writer_authority_fixture(
            project,
            run,
            claimless_direct_effect=True,
        )
        for cp_id, rid in (("cp-reserved", rids[0]), ("cp-returned", rids[1])):
            assert RunResultStore(project).reserve_row_effect_checkpoint(
                cp_id,
                run_id=run,
                row_id=rid,
                action_kind="c",
                identity=f"identity-{rid}",
                **authority.kwargs(),
            )
        # The second one's provider response came back and is durable, but no
        # result ever committed, so it was never consumed/retired.
        EffectCheckpointStore(project.db).complete(
            "cp-returned",
            family="row_effect",
            group_key=str(run),
            unit_key=str(rids[1]),
            action_kind="c",
            identity=f"identity-{rids[1]}",
            payload={"score": {"value": "paid answer", "error": None}},
            run_id=run,
            writer_attempt_id=authority.writer_attempt_id,
            claimless_direct_effect=True,
        )
        from frisket.execution.attempt import set_attempt_state

        set_attempt_state(project, authority.writer_attempt_id, "halted")
        project.undo()
        project.append_op("noop")
        return run, rids

    def test_compact_preserves_row_effect_checkpoints_that_carry_money(
        self, project, capsys
    ):
        """Compaction is reachability-based; a paid effect's ambiguity is not.

        The retired per-run checkpoint table carried a ``runs(id) ON DELETE
        CASCADE`` and the fold reproduced it as an explicit prune — faithfully
        preserving a behaviour that was wrong all along.  A ``reserved`` row
        means the provider MAY have been charged and a ``returned`` row means
        it WAS; neither fact stops being true because the operator discarded
        the op and compacted the bundle.  The checkpoint schema carries no run
        foreign key on purpose, so these rows outlive their run as orphaned
        reconcilable records and only the OPERATOR (`frisket reconcile`) may
        retire them.  Cutting the state-awareness deletes the only durable
        record that money may have moved.
        """
        from frisket.cli.reconcile import _list

        run, rids = self._discarded_run_with_checkpoints(project)

        project.compact()

        # The unreachable run itself is still reclaimed...
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM runs WHERE id=?", (run,)
            ).fetchone()[0]
            == 0
        )
        # ...but both money-bearing checkpoints survive it.
        surviving = {
            row["id"]: row["state"]
            for row in project.db.execute(
                "SELECT id, state FROM effect_checkpoints WHERE family='row_effect'"
            )
        }
        assert surviving == {"cp-reserved": "reserved", "cp-returned": "returned"}

        # And the operator can still see and decide them: each lists as an
        # orphan naming the run that no longer exists.
        assert _list(project) == 0
        listed = capsys.readouterr().out
        assert "cp-reserved" in listed and "cp-returned" in listed
        for line in listed.splitlines():
            if "cp-reserved" in line or "cp-returned" in line:
                assert "ORPHANED" in line
                assert f"run {run}" in line

    def test_compact_preserved_checkpoint_is_operator_decidable(self, project):
        """The survivors are not inert debris: the operator lever that owns
        them still works on a checkpoint whose run was compacted away."""
        from frisket.cli.reconcile import _discard
        from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

        self._discarded_run_with_checkpoints(project)
        project.compact()

        store = EffectCheckpointStore(project.db)
        # accept-charged records the ambiguous reservation as a real charge
        assert store.operator_accept_charged("cp-reserved")["state"] == "consumed"
        # and the orphaned returned response, which can never be consumed by
        # any run, is discardable through the operator surface.
        assert _discard(project, "cp-returned") == 0
        assert store.get("cp-returned") is None

    def test_cache_lru_prune(self, tmp_path):
        from frisket.ai.llm.cache import ResponseCache
        from frisket.ai.llm.types import LLMResponse

        cache = ResponseCache(tmp_path / "c.db")
        for i in range(5):
            cache.put(
                f"k{i}",
                LLMResponse(
                    content=f"t{i}",
                    data=None,
                    tokens_in=1,
                    tokens_out=1,
                    cost=0.0,
                    model="m",
                ),
            )
        # No inter-insert delay needed: prune_lru orders by
        # `created_at DESC, rowid DESC`, and SQLite rowids already increase
        # monotonically with insertion order, so the rowid tiebreak alone
        # orders correctly even when created_at (second resolution) ties.
        assert cache.count() == 5
        assert cache.prune_lru(2) == 3
        assert cache.count() == 2
        assert cache.get("k4") is not None  # newest kept
        assert cache.get("k0") is None  # oldest evicted
        assert cache.prune_lru(10) == 0  # no-op under cap
        cache.close()


class TestCompactionRedoSafety:
    """Regression: GC must NOT delete a blob referenced only by an undone-but-
    still-redoable run, because deleting it would make redo fail."""

    def test_gc_keeps_redoable_run_blobs(self, project):
        sheet, _ = make_sheet(project, 1)
        col = project.add_column(sheet, "img", type="image", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        digest = project.add_blob(b"redoable image bytes", mime="image/png")
        op = project.append_op("map")
        run = RunResultStore(project).start_run(op, sheet, "map.extract")
        write_claimed_test_results(
            project,
            run,
            [
                {
                    "row_id": rid,
                    "column_id": col,
                    "value": {"blob": digest, "mime": "image/png"},
                }
            ],
        )
        RunResultStore(project).point_column_at_run(op, col, run)
        # undo: the run is now 'undone' (redoable), not the column's current run
        project.undo()
        if hasattr(project, "gc_blobs"):
            dead = project.gc_blobs(dry_run=True)
            assert digest not in dead.get("hashes", []), (
                "GC would delete a blob a redo still needs"
            )
        # redo must restore the value with its blob intact
        project.redo()
        vals = project.get_values(sheet, col, row_ids=[rid])
        assert vals[rid] and vals[rid].get("blob") == digest
        with project.materialize_blob(digest) as path:
            assert path.exists()

    def test_gc_keeps_superseded_run_blob_needed_by_undo(self, project):
        sheet, _ = make_sheet(project, 1)
        col = project.add_column(sheet, "img", type="image", ai_generated=True)
        rid = project.db.execute("SELECT id FROM rows").fetchone()["id"]
        store = RunResultStore(project)

        def publish(generation, content):
            blob = project.add_blob(
                content,
                filename=f"generation-{generation.lower()}.png",
                mime="image/png",
                metadata={"generation": generation},
            )
            op = project.append_op("map")
            run = store.start_run(op, sheet, "map.extract")
            write_claimed_test_results(
                project,
                run,
                [
                    {
                        "row_id": rid,
                        "column_id": col,
                        "value": {"blob": blob, "mime": "image/png"},
                    }
                ],
            )
            store.finish_run(run)
            store.point_column_at_run(op, col, run)
            return blob, op, run

        first_bytes = b"generation A image bytes"
        first_blob, first_op, first_run = publish("A", first_bytes)
        _second_blob, second_op, second_run = publish("B", b"generation B image bytes")

        assert project.get_column(col)["current_run_id"] == second_run
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE id=?", (first_op,)
            ).fetchone()["status"]
            == "applied"
        )
        undo_info = json.loads(
            project.db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (second_op,)
            ).fetchone()["undo_info"]
        )
        assert undo_info["column_pointers"][str(col)] == first_run

        project.gc_blobs()
        assert project.undo() == second_op

        values, refs = project.get_values_with_refs(sheet, col, row_ids=[rid])
        assert values[rid] == {"blob": first_blob, "mime": "image/png"}
        assert refs[rid] == {
            "kind": "run_result",
            "op_id": first_op,
            "row_id": rid,
            "column_id": col,
            "run_id": first_run,
        }
        assert project.read_blob(first_blob) == first_bytes
        assert MediaBlobStore(project).metadata(first_blob) == {"generation": "A"}


class TestConcurrentOpen:
    """The frontend fires hot read endpoints (/sheets, /review/queue) at the
    same time, so two requests open the same per-project bundle concurrently.

    This used to be about the open-time additive ALTERs, whose loser raised
    ``sqlite3.OperationalError: duplicate column name: hidden`` in prod (top
    error, 42 occurrences / 4 days). Those are gone with the migration layer,
    but the per-DB open lock they were guarded by is not: what remains at open
    is ``ensure_standing_cost_consent``, a read-then-INSERT with no CAS. Two
    threads that both read "no standing consent for this principal" both mint
    one, and the bundle grows a duplicate policy row per concurrent open --
    silent, and directly on the "did I approve it" path. Drop
    ``bundle_open._open_lock`` and this test goes red.
    """

    def test_a_knob_change_mints_one_successor_not_one_per_open(
        self, tmp_path, monkeypatch
    ):
        import shutil
        from concurrent.futures import ThreadPoolExecutor

        from frisket.engine.store.execution_routes import (
            COST_CONSENT_ENV,
            STANDING_COST_POLICY,
        )

        base = tmp_path / "base.frisket"
        monkeypatch.setenv(COST_CONSENT_ENV, "1")
        seed = Project.create(base, name="base")
        sheet = seed.add_sheet("data")
        seed.add_rows(sheet, [{"name": "a"}], {"name": seed.add_column(sheet, "name")})
        seed.close()

        # The operator moved the knob and restarted. Every concurrent open now
        # sees "the persisted head's threshold differs" and wants a successor.
        monkeypatch.setenv(COST_CONSENT_ENV, "7")

        errors: list[BaseException] = []

        def open_and_close(bundle):
            try:
                project = Project(bundle)
                project.sheets()  # the hot read path the prod error fired on
                project.close()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                errors.append(exc)

        for trial in range(10):
            work = tmp_path / f"trial-{trial}.frisket"
            shutil.copytree(base, work)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for future in [pool.submit(open_and_close, work) for _ in range(8)]:
                    future.result()

            project = Project(work)
            try:
                standing = project.db.execute(
                    "SELECT COUNT(*) AS n FROM consents WHERE standing_policy=?",
                    (STANDING_COST_POLICY,),
                ).fetchone()["n"]
                assert standing == 2, (
                    f"trial {trial}: {standing} standing consent rows after 8 "
                    "concurrent opens across one knob change; expected the "
                    "original plus exactly one successor"
                )
            finally:
                project.close()

        assert not errors, (
            f"concurrent open raised {len(errors)} error(s); first: {errors[0]!r}"
        )
