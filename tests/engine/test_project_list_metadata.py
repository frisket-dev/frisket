"""onboard2-picker-day2-badges-v1: the project list payload (GET /api/projects,
Workspace.list() -> src/frisket/server/workspace.py) carries a per-project
`updated_at` (bundle project.db mtime — an already-existing signal, no new
timestamp bookkeeping invented) and `pending_review_count` (a cached summary
recomputed at the write sites that can change it: run completion via
RunResultStore.point_column_at_run, typed review.decision mutations, and
Project.undo/redo — src/frisket/engine/store/project.py's
refresh_pending_review_summary) and persisted into manifest.json so the list
endpoint never has to open every project's sqlite db to answer "how many
pending reviews". Rows are sorted by recency (most-recently-modified first).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi.testclient import TestClient

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.runner.review import review_queue
from frisket.server.app import create_app
from frisket.engine.store import Project
import frisket.engine.store.project as project_module
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from http_test_helpers import operation_step_action
from typed_model_fixtures import run_with_exact_confirmation


class StubAdapter:
    def __init__(self, reply: dict):
        self.reply = reply

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=10,
            tokens_out=10,
            cost=0.0,
            model=req.model,
        )


def accept_review(project: Project, project_id: str, item: dict, key: str) -> None:
    result = run_action_spec(
        project,
        {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "run_id": item["run_id"],
                "row_id": item["row_id"],
                "column_id": item["column_id"],
                "decision": "accept",
            },
            "idempotency_key": key,
        },
        project_id=project_id,
    )
    assert result.status == "completed", result.errors


def stub_router(reply: dict) -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = StubAdapter(reply)  # noqa: SLF001
    return router


def classify_spec(sheet_id: int) -> dict:
    # Bound to the typed ``map.classify`` program by typed_model_fixtures;
    # the runner-spec keys below map onto ClassifyParams (source/model/fields/
    # include_*), and the exact quote -> confirmation echo is exercised.
    return {
        "action_kind": "map.classify",
        "model": "anthropic/claude-haiku-4-5",
        "sheet_id": sheet_id,
        "input_columns": ["story"],
        "fields": [
            {"name": "beat", "type": "category", "labels": ["civic", "private"]}
        ],
        "include_justification": True,
        "include_confidence": True,
    }


def seed_two_row_sheet(project: Project) -> int:
    sheet_id = project.add_sheet("stories")
    columns = {"story": project.add_column(sheet_id, "story")}
    project.add_rows(
        sheet_id,
        [
            {"story": "Mayor met a lobbyist before the vote."},
            {"story": "Council approved a parks grant."},
        ],
        columns,
    )
    return sheet_id


def run_classify(project: Project, sheet_id: int) -> None:
    reply = {
        "beat": "civic",
        "beat_justification": "mentions city government",
        "beat_confidence": 0.4,
    }
    runner = MapRunner(
        project, stub_router(reply), authority=UnroutedOnlyAuthority(project)
    )
    asyncio.run(run_with_exact_confirmation(runner, classify_spec(sheet_id)))


def test_project_list_carries_updated_at_and_zero_pending_count_on_create(tmp_path):
    client = TestClient(create_app(tmp_path / "workspace"))

    created = client.post("/api/projects", json={"name": "Metadata Project"})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    listed = client.get("/api/projects").json()
    entry = next(p for p in listed if p["id"] == pid)
    assert entry["updated_at"], "a freshly created project already has a bundle mtime"
    assert entry["pending_review_count"] == 0


def test_review_summary_manifest_write_carries_envelope_header(tmp_path):
    """artifact-contract consolidation pin: every manifest writer stamps the bundle envelope header.

    refresh_pending_review_summary used to merge pending_review_count into
    manifest.json WITHOUT the format/schema_version header, so a manifest
    last written by the review-summary path could lose its envelope. The
    shared Project._write_manifest now stamps it on every write."""
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Envelope"}).json()["id"]
    project = client.app.state.workspace.get(pid)

    # Strip the manifest to the bare summary field, then write via the
    # review-summary path — the header must come back.
    manifest_path = project.path / "manifest.json"
    manifest_path.write_text(json.dumps({"pending_review_count": 99}))
    project.refresh_pending_review_summary()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["format"] == "frisket-bundle"
    assert manifest["schema_version"] == project_module.PROJECT_SCHEMA_VERSION
    assert manifest["format_version"] == project_module.FORMAT_VERSION
    assert manifest["project_id"] == pid
    assert manifest["name"]  # backfilled from meta, never lost
    assert manifest["pending_review_count"] == 0  # recomputed, not trusted


def test_project_list_pending_review_count_tracks_run_and_review_actions(tmp_path):
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Review Metadata"})
    pid = created.json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = seed_two_row_sheet(project)

    run_classify(project, sheet_id)

    listed = client.get("/api/projects").json()
    entry = next(p for p in listed if p["id"] == pid)
    assert entry["pending_review_count"] == 2

    # Resolve one item: the cached count decrements without a fresh list-time query.
    item = review_queue(project)[0]
    accept_review(project, pid, item, "project-list-accept@sha256:v1")

    listed_after_accept = client.get("/api/projects").json()
    entry_after_accept = next(p for p in listed_after_accept if p["id"] == pid)
    assert entry_after_accept["pending_review_count"] == 1

    # Undo restores the exact review metadata and makes the item pending again.
    undone = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=operation_step_action("operation.undo", expected_op_id=project.op_cursor),
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["status"] == "completed"
    listed_after_undo = client.get("/api/projects").json()
    entry_after_undo = next(p for p in listed_after_undo if p["id"] == pid)
    assert entry_after_undo["pending_review_count"] == 2


def test_cached_pending_count_equals_a_fresh_live_recount(tmp_path):
    """Drift guard: the cached manifest count must always equal a freshly
    recomputed live count (review_bundle_count) after any mutation sequence.
    This is the invariant that protects against a future review-state write
    path forgetting to call refresh_pending_review_summary — if cache and
    live can diverge, this test catches it even when the per-op value tests
    above still pass. (Added at wave-2 landing review, 2026-07-04.)"""
    from frisket.engine.runner.review import review_bundle_count

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Drift Guard"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = seed_two_row_sheet(project)

    def cached() -> int:
        entry = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
        return entry["pending_review_count"]

    def live() -> int:
        return review_bundle_count(client.app.state.workspace.get(pid))

    assert cached() == live() == 0
    run_classify(project, sheet_id)
    assert cached() == live() == 2
    item = review_queue(project)[0]
    accept_review(project, pid, item, "drift-guard-accept@sha256:v1")
    assert cached() == live()
    project.undo()
    assert cached() == live()


def test_project_list_updated_at_reflects_run_activity_not_just_creation(tmp_path):
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Recency Project"})
    pid = created.json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = seed_two_row_sheet(project)

    before = next(p for p in client.get("/api/projects").json() if p["id"] == pid)[
        "updated_at"
    ]

    run_classify(project, sheet_id)

    after = next(p for p in client.get("/api/projects").json() if p["id"] == pid)[
        "updated_at"
    ]
    assert after >= before


def test_project_list_sorts_by_recency(tmp_path):
    """Sort order tracks bundle mtime end-to-end through the list endpoint.
    Timestamps are set explicitly via os.utime rather than relying on two
    real writes landing in different wall-clock seconds — some filesystems
    have 1-second mtime resolution, which would otherwise make this flake."""
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace_root = client.app.state.workspace.root

    a = client.post("/api/projects", json={"name": "Project A"}).json()["id"]
    b = client.post("/api/projects", json={"name": "Project B"}).json()["id"]

    now = time.time()
    os.utime(workspace_root / f"{a}.frisket" / "project.db", (now - 200, now - 200))
    os.utime(workspace_root / f"{b}.frisket" / "project.db", (now - 100, now - 100))

    listed = client.get("/api/projects").json()
    ids = [p["id"] for p in listed]
    assert ids.index(b) < ids.index(a)

    # A becomes the more recently modified project; sort order flips.
    os.utime(workspace_root / f"{a}.frisket" / "project.db", (now, now))
    listed_after = client.get("/api/projects").json()
    ids_after = [p["id"] for p in listed_after]
    assert ids_after.index(a) < ids_after.index(b)


def test_project_list_never_opens_project_bundles(tmp_path, monkeypatch):
    """The NOT-done clause: pending-review counts must come from the list
    endpoint's own cheap read (manifest.json), never by opening each project's
    sqlite db at list-time — that's the one thing that would defeat the point
    on a many-project workspace (measured live: ~5-44ms per Project() open
    depending on OS page-cache state, i.e. seconds-to-a-minute across a
    thousand-project workspace like the repo's own e2e-ws fixture directory)."""
    client = TestClient(create_app(tmp_path / "workspace"))

    pids = []
    for i in range(5):
        pid = client.post("/api/projects", json={"name": f"Bundle {i}"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet_id = seed_two_row_sheet(project)
        run_classify(project, sheet_id)
        pids.append(pid)

    opened = {"count": 0}
    original_init = project_module.Project.__init__

    def counting_init(self, *args, **kwargs):
        opened["count"] += 1
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(project_module.Project, "__init__", counting_init)

    listed = client.get("/api/projects").json()

    assert opened["count"] == 0, "listing projects must not instantiate Project(...)"
    assert len(listed) == 5
    for entry in listed:
        assert entry["pending_review_count"] == 2
