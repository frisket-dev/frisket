from __future__ import annotations

import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from frisket.engine.store import Project
from frisket.server.services.project_qa_analytics import evaluate_analytics

from .benchmark import PhaseSampler
from .fixtures import stress_project_marker, stress_project_records
from .project_benchmark import ResourceStop, _governed_query, run_qualification


def _remove_retained_tree(root: Path) -> None:
    for item in sorted(root.rglob("*"), reverse=True):
        item.chmod(0o700 if item.is_dir() else 0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


class ProjectQualificationTests(unittest.TestCase):
    def test_fixture_preserves_missing_invalid_and_late_search_token(self):
        records = list(stress_project_records(199, 3))
        self.assertTrue(records[0]["body"].endswith(stress_project_marker(199)))
        self.assertIsNone(records[1]["amount"])
        self.assertEqual(records[2]["amount"], "not-stated")

    def test_real_project_smoke(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_qualification(200, Path(raw))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rows_requested"], 200)
        self.assertTrue(result["owned_scratch_removed"])
        self.assertEqual(result["fixture"]["amount_states"]["missing"], 20)
        self.assertEqual(result["fixture"]["amount_states"]["invalid"], 20)
        self.assertEqual(len(result["search"]) - 1, 4)
        self.assertEqual(len(result["grid_queries"]), 9)
        self.assertEqual(len(result["analytics"]), 2)
        self.assertEqual(result["mutations"]["ordinary"]["count"], 5)
        self.assertEqual(result["mutations"]["batch"]["count"], 20)
        self.assertEqual(result["history"]["total"], 7)
        self.assertEqual(len(result["checkpoint_reopen"]["grid"]), 9)
        self.assertEqual(len(result["checkpoint_reopen"]["analytics"]), 2)
        self.assertEqual(result["checkpoint_reopen"]["history"]["total"], 7)
        self.assertEqual(result["checkpoint_reopen"]["marker_search"]["hits"], 1)
        self.assertFalse(result["retention"]["requested"])
        self.assertIsNone(result["retention"]["bundle"])
        accounting = result["checkpoint_reopen"]["physical_accounting"]
        self.assertGreater(accounting["project"]["file_bytes"], 0)
        self.assertGreater(accounting["search"]["file_bytes"], 0)
        self.assertTrue(accounting["project"]["objects"])
        self.assertTrue(accounting["search"]["objects"])

        with tempfile.TemporaryDirectory() as raw:
            retained = run_qualification(200, Path(raw), retain_success=True)
            retained_bundle = Path(retained["retention"]["bundle"]["bundle_path"])
            self.assertEqual(retained["status"], "completed")
            self.assertTrue(retained_bundle.is_dir())
            self.assertEqual(
                retained["retention"]["bundle"]["owner"],
                "storage-layout columnar and physical measurement lanes",
            )
            self.assertTrue(retained["retention"]["bundle"]["read_only"])
            self.assertEqual(stat.S_IMODE(retained_bundle.stat().st_mode), 0o555)
            self.assertTrue(
                all(not path.is_symlink() for path in retained_bundle.rglob("*"))
            )
            self.assertTrue(
                all(
                    stat.S_IMODE(path.stat().st_mode) in {0o444, 0o555}
                    for path in retained_bundle.rglob("*")
                )
            )
            _remove_retained_tree(retained_bundle.parent)

        with tempfile.TemporaryDirectory() as raw:
            work_root = Path(raw)
            with patch(
                "scripts.storage_slice.project_benchmark."
                "StreamingSheetWriter.start_session",
                side_effect=KeyboardInterrupt,
            ):
                interrupted = run_qualification(200, work_root, retain_success=True)
            self.assertEqual(list(work_root.glob("project-qualification-*")), [])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertTrue(interrupted["owned_scratch_removed"])
        self.assertTrue(interrupted["retention"]["requested"])
        self.assertIsNone(interrupted["retention"]["bundle"])

    def test_sampler_interrupts_an_active_sqlite_query(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = Project.create(root / "interrupt.frisket")
            samples = 0

            def trip_after_initial_guard(_observed):
                nonlocal samples
                samples += 1
                return "forced sampled limit" if samples > 1 else None

            sampler = PhaseSampler(
                root, hard_limit_bytes=1024**3, limit_probe=trip_after_initial_guard
            )

            def long_query():
                sampler.start()
                return project.db.execute(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                    "SELECT x+1 FROM n WHERE x<1000000000) SELECT COUNT(*) FROM n"
                ).fetchone()

            try:
                with self.assertRaisesRegex(ResourceStop, "forced sampled limit"):
                    _governed_query(project, sampler, 512 * 1024**2, long_query)
            finally:
                sampler.stop()
                project.close()

    def test_analytics_cancellation_becomes_resource_stop(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = Project.create(root / "analytics-interrupt.frisket")
            sheet = project.add_sheet("records")
            amount = project.add_column(sheet, "amount", type="integer")
            project.add_rows(sheet, [{"amount": 1}], {"amount": amount})
            sampler = PhaseSampler(root, hard_limit_bytes=1024**3)

            def cancelled_analytics():
                sampler.limit_reason = "forced analytics limit"
                sampler.hard_limit.set()
                return evaluate_analytics(
                    project,
                    {
                        "sheet_id": sheet,
                        "metrics": [{"id": "sum", "kind": "sum", "column_id": amount}],
                    },
                    {"kind": "sheet", "sheet_id": sheet},
                    cancel_event=sampler.hard_limit,
                )

            try:
                with self.assertRaisesRegex(ResourceStop, "forced analytics limit"):
                    _governed_query(
                        project, sampler, 512 * 1024**2, cancelled_analytics
                    )
            finally:
                project.close()


if __name__ == "__main__":
    unittest.main()
