"""Public-operation proofs for the opt-in storage comparison."""

from __future__ import annotations

import concurrent.futures
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from scripts.storage_slice.store import (
    Conflict,
    Document,
    Extraction,
    InvalidCitation,
    ProjectStore,
)
from scripts.storage_slice.fixtures import stress_documents, stress_extractions

ENGINES = ("sqlite", "duckdb")


def documents():
    return [
        Document(
            1,
            "Alpha",
            "awarded",
            "100.00",
            "Award:\n\n$100.00.\nAward: $100.00.",
            b"synthetic asset one",
        ),
        Document(
            2,
            "Beta",
            "awarded",
            "not disclosed",
            "The award is $200.00.",
            b"synthetic asset two",
        ),
        Document(3, "Gamma", "pending", None, "No amount was specified."),
    ]


def seed(store):
    store.import_documents("import-1", documents(), batch_size=2)
    rows = store.read_rows()
    store.publish_extraction(
        "extract-1",
        [
            Extraction(1, rows[0]["source_version"], 10000, ("Award: $100.00",)),
            Extraction(2, rows[1]["source_version"], 20000, ("$200.00",)),
            Extraction(3, rows[2]["source_version"], None),
        ],
    )
    return store.read_rows()


class StorageSliceTests(unittest.TestCase):
    def test_stress_fixture_is_streaming_mixed_and_source_backed(self):
        generated = stress_documents(1, 200)
        self.assertIs(iter(generated), generated)
        documents = list(generated)
        lengths = [len(document.text.encode()) for document in documents]
        self.assertEqual(len({document.category for document in documents}), 8)
        self.assertTrue(any(length < 2 * 1024 for length in lengths))
        self.assertTrue(any(16 * 1024 <= length < 96 * 1024 for length in lengths))
        self.assertTrue(any(length >= 96 * 1024 for length in lengths))
        self.assertEqual(
            {"valid", "invalid", "missing"},
            {
                "missing"
                if document.raw_amount is None
                else "invalid"
                if document.raw_amount == "not-stated"
                else "valid"
                for document in documents
            },
        )
        for document in documents[::37]:
            row = {
                "row_id": document.row_id,
                "source_version": "source-test",
            }
            outputs = [
                stress_extractions([row], generation=generation)[0]
                for generation in range(3)
            ]
            self.assertEqual(
                {output.amount_cents for output in outputs}, {outputs[0].amount_cents}
            )
            self.assertEqual({output.quotes for output in outputs}, {outputs[0].quotes})
            for output in outputs:
                self.assertIn(output.quotes[0], document.text)

    def test_stress_history_join_and_checkpoint_preserve_results(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    store.import_documents("stress-import", stress_documents(1, 20))
                    rows = store.read_rows(limit=20)
                    store.publish_extraction(
                        "stress-generation-0", stress_extractions(rows)
                    )
                    repeated = [row for row in rows if row["row_id"] % 5 == 0]
                    store.publish_extraction(
                        "stress-generation-1",
                        stress_extractions(repeated, generation=1),
                    )
                    before = store.logical_counts()
                    joined = store.history_join(row_id_start=1, row_id_end=20)
                    self.assertEqual(before["result_versions"], 24)
                    self.assertEqual(sum(row["version_count"] for row in joined), 24)
                    store.checkpoint_storage()
                    self.assertEqual(store.logical_counts(), before)

    def test_import_preserves_raw_state_and_retry_identity(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    receipt = store.import_documents("i", documents(), batch_size=2)
                    self.assertEqual(receipt, store.import_documents("i", documents()))
                    rows = store.read_rows()
                    self.assertEqual(
                        [r["amount_state"] for r in rows],
                        ["valid", "invalid", "missing"],
                    )
                    self.assertEqual(rows[1]["raw_amount"], "not disclosed")
                    with self.assertRaises(Conflict):
                        store.import_documents("i", documents()[:1])
                    self.assertEqual(len(store.read_rows()), 3)
                    self.assertEqual(store.logical_counts()["receipts"], 1)
                    store.import_documents(
                        "append", [Document(4, "Delta", "pending", "0", "No claim.")]
                    )
                    self.assertEqual(
                        [row["row_id"] for row in store.read_rows()], [1, 2, 3, 4]
                    )

    def test_terminated_import_remains_unpublished_after_restart(self):
        child = """
import os, sys
from pathlib import Path
from scripts.storage_slice.store import ProjectStore
from scripts.storage_slice.test_slice import documents

def checkpoint(label):
    if label == "import.batch_committed": os._exit(77)
with ProjectStore(Path(sys.argv[1]), sys.argv[2], checkpoint=checkpoint) as store:
    store.import_documents("interrupted", documents(), batch_size=1)
"""
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "project"
                outcome = subprocess.run(
                    [sys.executable, "-c", child, str(path), engine],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(outcome.returncode, 77, outcome.stderr)
                with ProjectStore(path, engine) as store:
                    self.assertEqual(store.read_rows(), [])
                    self.assertEqual(store.logical_counts()["receipts"], 0)
                    self.assertGreater(store.logical_counts()["staged_rows"], 0)
                    with self.assertRaises((ValueError, RuntimeError)):
                        store.export_project(Path(tmp) / "unfinished-export")
                    store.discard_staged("interrupted")
                    self.assertEqual(store.logical_counts()["staged_rows"], 0)
                    store.import_documents("recovered", documents())
                    self.assertEqual(len(store.read_rows()), 3)

    def test_concurrent_identical_imports_publish_once(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    gate = threading.Barrier(2)

                    def run(_):
                        gate.wait(timeout=10)
                        return store.import_documents(
                            "same-import", documents(), batch_size=1
                        )

                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        receipts = list(pool.map(run, range(2)))
                    self.assertEqual(receipts[0], receipts[1])
                    self.assertEqual(len(store.read_rows()), 3)
                    self.assertEqual(store.logical_counts()["receipts"], 1)

    def test_extraction_and_evidence_rollback_together(self):
        def checkpoint(label):
            if label == "extract.before_commit":
                raise RuntimeError("simulated interrupted publication")

        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "project"
                with ProjectStore(path, engine, checkpoint=checkpoint) as store:
                    store.import_documents("i", documents())
                    output = Extraction(
                        1, store.read_rows()[0]["source_version"], 10000, ("$100.00",)
                    )
                    with self.assertRaises(RuntimeError):
                        store.publish_extraction("e", [output])
                    self.assertIsNone(store.read_rows()[0]["current_version"])
                    self.assertEqual(store.citations(1), [])
                    self.assertEqual(store.logical_counts()["result_versions"], 0)
                with ProjectStore(path, engine) as store:
                    receipt = store.publish_extraction("e", [output])
                    self.assertEqual(store.publish_extraction("e", [output]), receipt)
                    self.assertEqual(store.logical_counts()["result_versions"], 1)
                    with self.assertRaises(InvalidCitation):
                        store.publish_extraction(
                            "bad",
                            [
                                Extraction(
                                    2,
                                    store.read_rows()[1]["source_version"],
                                    9,
                                    ("absent quote",),
                                )
                            ],
                        )
                    self.assertEqual(store.logical_counts()["result_versions"], 1)

    def test_review_cas_concurrency_and_aggregate_read_your_writes(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    old = seed(store)[0]["current_version"]

                    def edit(value):
                        try:
                            store.review_value(
                                f"edit-{value}",
                                1,
                                "edit",
                                expected_version=old,
                                value=value,
                            )
                            return "ok"
                        except Conflict:
                            return "conflict"

                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        outcomes = list(pool.map(edit, (30000, 50000)))
                    self.assertCountEqual(outcomes, ["ok", "conflict"])
                    row = store.read_rows()[0]
                    self.assertEqual(row["review_state"], "edited")
                    expected_sum = row["amount_cents"] + 20000
                    group = store.aggregate(category="awarded")[0]
                    self.assertEqual(group["sum_cents"], expected_sum)
                    self.assertEqual(group["mean_cents"], expected_sum / 2)
                    self.assertEqual(group["median_cents"], expected_sum / 2)
                    self.assertEqual(group["row_count"], 2)
                    self.assertEqual(group["value_count"], 2)
                    self.assertTrue(
                        store.resolve_citation(store.citations(1)[0]["citation_id"])[
                            "stale"
                        ]
                    )
                    before = store.logical_counts()
                    with self.assertRaises(Conflict):
                        store.review_value("stale", 1, "reject", expected_version=old)
                    self.assertEqual(store.logical_counts(), before)

    def test_accept_reject_null_and_manual_precedence(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    rows = seed(store)
                    original = rows[0]["current_version"]
                    store.review_value("accept", 1, "accept", expected_version=original)
                    self.assertEqual(store.read_rows()[0]["current_version"], original)
                    self.assertEqual(store.read_rows()[0]["review_state"], "accepted")
                    receipt = store.review_value(
                        "reject", 1, "reject", expected_version=original
                    )
                    self.assertEqual(
                        store.review_value(
                            "reject", 1, "reject", expected_version=original
                        ),
                        receipt,
                    )
                    store.publish_extraction(
                        "rerun",
                        [Extraction(1, rows[0]["source_version"], 888, ("$100.00",))],
                    )
                    self.assertIsNone(store.read_rows()[0]["amount_cents"])
                    self.assertEqual(store.read_rows()[0]["review_state"], "rejected")
                    self.assertEqual(
                        store.aggregate(category="awarded")[0]["value_count"], 1
                    )
                    store.publish_extraction(
                        "clear-generated",
                        [Extraction(2, rows[1]["source_version"], None)],
                    )
                    self.assertIsNone(store.read_rows()[1]["amount_cents"])
                    group = store.aggregate(category="awarded")[0]
                    self.assertEqual(group["value_count"], 0)
                    self.assertIsNone(group["sum_cents"])

    def test_frozen_citation_with_newlines_duplicate_matches_and_changed_source(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    seed(store)
                    citations = store.citations(1)
                    self.assertEqual(len(citations), 2)
                    before = [
                        store.resolve_citation(c["citation_id"]) for c in citations
                    ]
                    self.assertTrue(
                        any("\n" in c["text"][c["start"] : c["end"]] for c in before)
                    )
                    for citation in before:
                        self.assertEqual(
                            " ".join(
                                citation["text"][
                                    citation["start"] : citation["end"]
                                ].split()
                            ),
                            "Award: $100.00",
                        )
                        self.assertFalse(citation["stale"])
                    store.replace_text("source-edit", 1, "Corrected source: no award.")
                    for old in before:
                        now = store.resolve_citation(old["citation_id"])
                        self.assertEqual(now["text"], old["text"])
                        self.assertEqual(now["source_version"], old["source_version"])
                        self.assertTrue(now["stale"])

    def test_queries_match_between_engines_and_page_is_bounded(self):
        observed = []
        for engine in ENGINES:
            with tempfile.TemporaryDirectory() as tmp:
                with ProjectStore(Path(tmp) / "project", engine) as store:
                    seed(store)
                    observed.append(store.aggregate())
                    self.assertEqual(
                        [r["row_id"] for r in store.read_rows(limit=1, after_row_id=1)],
                        [2],
                    )
                    self.assertEqual(
                        [row["row_id"] for row in store.read_rows(sort="amount_cents")],
                        [1, 2, 3],
                    )
                    self.assertEqual(
                        [r["row_id"] for r in store.read_rows(category="pending")], [3]
                    )
        self.assertEqual(observed[0], observed[1])

    def test_numeric_domain_and_review_rollback(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "project"
                with ProjectStore(path, engine) as store:
                    rows = seed(store)
                    counts = store.logical_counts()
                    for invalid in (True, 2**63):
                        with self.assertRaises((ValueError, TypeError)):
                            store.publish_extraction(
                                f"invalid-{invalid}",
                                [Extraction(1, rows[0]["source_version"], invalid)],
                            )
                        with self.assertRaises((ValueError, TypeError)):
                            store.review_value(
                                f"invalid-edit-{invalid}",
                                1,
                                "edit",
                                expected_version=rows[0]["current_version"],
                                value=invalid,
                            )
                    self.assertEqual(store.logical_counts(), counts)

                def checkpoint(label):
                    if label == "review.before_commit":
                        raise RuntimeError("interrupted review")

                with ProjectStore(path, engine, checkpoint=checkpoint) as store:
                    before = store.read_rows()
                    with self.assertRaises(RuntimeError):
                        store.review_value(
                            "failed-review",
                            1,
                            "edit",
                            expected_version=before[0]["current_version"],
                            value=500,
                        )
                    self.assertEqual(store.read_rows(), before)
                    self.assertEqual(store.logical_counts(), counts)

    def test_cross_engine_streaming_export_restore_preserves_evidence_and_rejects_corruption(
        self,
    ):
        for engine, other in (("sqlite", "duckdb"), ("duckdb", "sqlite")):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with ProjectStore(root / "original", engine) as store:
                    rows = seed(store)
                    store.review_value(
                        "edited",
                        2,
                        "edit",
                        expected_version=rows[1]["current_version"],
                        value=45000,
                    )
                    export = store.export_project(root / "export")
                    expected = (
                        store.read_rows(),
                        store.aggregate(),
                        store.logical_counts(),
                        store.resolve_citation(store.citations(1)[0]["citation_id"]),
                    )
                with ProjectStore.restore_project(
                    export, root / "restored", other
                ) as restored:
                    actual = (
                        restored.read_rows(),
                        restored.aggregate(),
                        restored.logical_counts(),
                        restored.resolve_citation(expected[3]["citation_id"]),
                    )
                    self.assertEqual(actual, expected)
                table = export / "documents.jsonl"
                original_table = table.read_text()
                table.write_text(original_table.replace("Alpha", "Altered"))
                with self.assertRaises((ValueError, RuntimeError)):
                    ProjectStore.restore_project(
                        export, root / "bad-table-target", other
                    )
                self.assertFalse((root / "bad-table-target").exists())
                table.write_text(original_table)
                assets = list((export / "assets").iterdir())
                self.assertTrue(assets)
                assets[0].write_bytes(b"corrupt bytes")
                with self.assertRaises((ValueError, RuntimeError)):
                    ProjectStore.restore_project(export, root / "corrupt-target", other)
                self.assertFalse((root / "corrupt-target").exists())


if __name__ == "__main__":
    unittest.main()
