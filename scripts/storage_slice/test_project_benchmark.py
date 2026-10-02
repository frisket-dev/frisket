from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from frisket.engine.store import Project

from .benchmark import PhaseSampler
from .fixtures import stress_project_marker, stress_project_records
from .project_benchmark import ResourceStop, _governed_query, run_qualification


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


if __name__ == "__main__":
    unittest.main()
