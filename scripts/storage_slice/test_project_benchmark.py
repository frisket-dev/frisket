from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .fixtures import stress_project_marker, stress_project_records
from .project_benchmark import run_qualification


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


if __name__ == "__main__":
    unittest.main()
