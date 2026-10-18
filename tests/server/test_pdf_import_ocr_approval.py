from __future__ import annotations

import hashlib
import json

from frisket.ai.llm import ModelRouter
from frisket.server.services.pdf_import_ocr_approval import (
    PdfImportOcrApprovalQuoter,
    PdfImportOcrGrant,
)
from frisket.server.workspace import Workspace


def test_paid_import_quote_uses_exact_deterministic_child_quotes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    workspace = Workspace(
        tmp_path / "workspace", router=router, enable_local_model_pull=False
    )
    workspace.create("Paid PDF import", project_id="paid")
    child_partitions = [
        {
            "child_key": "commit-ordinals:1-256",
            "commit_ordinal_start": 1,
            "commit_ordinal_end": 256,
            "source_count": 2,
            "page_count": 7,
            "roster_digest": "d" * 64,
        },
        {
            "child_key": "commit-ordinals:257-300",
            "commit_ordinal_start": 257,
            "commit_ordinal_end": 300,
            "source_count": 1,
            "page_count": 3,
            "roster_digest": "e" * 64,
        },
    ]
    binding = {
        "selected_count": 3,
        "known_pages": 10,
        "ocr_action_params": {"engine": "anthropic/claude-haiku-4-5"},
        "manifest_hash": "a" * 64,
        "storage_identity": "storage:test",
        "draft_id": "pdf-draft-test",
        "work_scope_hash": "b" * 64,
        "child_partitions_hash": hashlib.sha256(
            json.dumps(child_partitions, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "child_partitions": child_partitions,
    }

    result = PdfImportOcrApprovalQuoter(workspace).quote("paid", binding)
    estimate = result["estimate"]
    grant = PdfImportOcrGrant.from_mapping(result["authorization"])
    children = grant.allocation["children"]

    assert estimate["rows"] == 3
    assert estimate["billed_cost"] == sum(
        child["quote"]["billed_cost"] for child in children
    )
    assert estimate["promise_set_hash"] == grant.promise_set_hash
    assert grant.scope["storage_identity"] == "storage:test"
    assert grant.scope["draft_id"] == "pdf-draft-test"
    assert grant.scope["page_count"] == 10
    assert [child["scope"]["page_count"] for child in children] == [7, 3]
    assert [child["quote"]["rows"] for child in children] == [2, 1]
    assert grant.allocation["approved_billed_cost"] == estimate["billed_cost"]
    assert grant.allocation["currency"] == "USD"
    assert grant.allocation["row_quote_authority"] == ("canonical_commit_occurrences")
