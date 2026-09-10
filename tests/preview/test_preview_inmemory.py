from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import ActionError
from frisket.engine.executor import ExecutorDeps, resolve_map_preview
from frisket.engine.executor.actions import build_map_preview_plan
from frisket.engine.runner import validation
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ops.base import RecipeInvocationHalt
from frisket.engine.runner.map_runner import (
    CostGate,
    PreviewResult,
    PreviewRowCapError,
)
from frisket.execution.provider import ExecutionComposition
from frisket.server.app import create_app
from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
from frisket.engine.store import Project

from tests.deterministic_time import controlled_time

# A real-time positive wait for a background preview thread to reach its start
# marker. Generous on purpose: under full-suite parallelism a worker can be
# starved for seconds, and a tight bound turns "hasn't scheduled yet" into a
# false failure. These are the contract's blessed positive waits (waiting FOR
# an event), not raced negative assertions, so a large ceiling is correct.
_STARTED_WAIT_SECONDS = 30.0

PREVIEW_URL = "/api/projects/{pid}/actions/v1/preview"
_ZERO_WRITE_TABLES = ("ops", "runs", "results", "receipts", "model_calls")
_MISSING = object()


def test_preview_runner_receives_composed_browser_runtime(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "browser-preview.frisket", name="Preview")
    browser = object()
    runner = SimpleNamespace(op_context_extras={})
    try:
        plan = build_map_preview_plan(
            project,
            action_kind="web.capture_screenshot",
            runner_spec={},
            router=None,
            deps=ExecutorDeps(
                map_runner_factory=lambda _project, _router: runner,
                url_capture_browser=browser,
            ),
        )
        assert plan.make_runner().op_context_extras["url_capture_browser"] is browser
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
def _python_action(
    sheet_id: int,
    *,
    column_name: str,
    idempotency_key: str,
    code: str | None = None,
    row_ids: list[int] | None = None,
) -> dict:
    action: dict = {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"n": column_name},
        "params": {
            "input_columns": ["title"],
            "code": code or "result = {'n': len(row['title'])}",
            "return_schema": {
                "type": "object",
                "required": ["n"],
                "properties": {"n": {"type": "integer"}},
            },
            "output_routes": [
                {
                    "name": "n",
                    "path": "$.n",
                    "target": {
                        "kind": "column",
                        "type": "integer",
                    },
                }
            ],
        },
        "idempotency_key": idempotency_key,
    }
    if row_ids is not None:
        action["scope"]["row_ids"] = row_ids
    return action


def _summarize_action(
    sheet_id: int,
    *,
    row_ids: list[int],
    model: str = "anthropic/claude-haiku-4-5",
    idempotency_key: str = "preview@summarize",
) -> dict:
    return {
        "action_id": "map.summarize",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {
            "source": ["title"],
            "model": model,
            "preset": "one_line",
            "instruction": "Summarize the title.",
        },
        "output_names": {"summary": "summary"},
        "idempotency_key": idempotency_key,
    }


def _ner_action(
    sheet_id: int,
    *,
    row_ids: list[int] | None,
    idempotency_key: str,
) -> dict:
    scope: dict = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "map.ner",
        "scope": scope,
        "params": {
            "source": ["title"],
            "labels": ["person"],
            "threshold": 0.5,
            "engine": "gliner",
        },
        "output_names": {"entities": "entities"},
        "idempotency_key": idempotency_key,
    }


class _NerSidecarResponse:
    status_code = 200
    text = "ok"

    def json(self) -> dict:
        return {"results": [[]]}


class _NerSidecarHttp:
    is_closed = False

    async def post(self, url: str, **kwargs) -> _NerSidecarResponse:
        del url, kwargs
        return _NerSidecarResponse()


class _PaidSummaryAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        del client
        self.requests.append(req)
        payload = {"summary": "A short summary."}
        return LLMResponse(
            content=json.dumps(payload),
            data=payload,
            tokens_in=20,
            tokens_out=5,
            cost=0.01,
            model=req.model,
        )


def _seed(project: Project, names: list[str]) -> tuple[int, list[int], int]:
    sheet_id = project.add_sheet("Rows")
    title = project.add_column(sheet_id, "title", type="text")
    project.add_rows(sheet_id, [{"title": n} for n in names], {"title": title})
    return sheet_id, project.visible_row_ids(sheet_id), title


def _standalone(
    tmp_path: Path, names: list[str]
) -> tuple[Project, int, list[int], int]:
    project = Project.create(tmp_path / "preview.frisket", name="Preview InMemory")
    sheet_id, row_ids, title = _seed(project, names)
    return project, sheet_id, row_ids, title


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _ZERO_WRITE_TABLES
    }


def _database_counts(project: Project) -> dict[str, int]:
    """Every durable table, so a preview cannot hide a new write category."""
    tables = [
        str(row[0])
        for row in project.db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: int(project.db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def _name_to_row_id(
    project: Project, sheet_id: int, title: int, row_ids: list[int]
) -> dict[str, int]:
    got = project.get_values(sheet_id, title, row_ids=row_ids)
    return {got[rid]: rid for rid in row_ids}


def _route_project(
    client: TestClient, names: list[str]
) -> tuple[str, Project, int, list[int]]:
    pid = client.post("/api/projects", json={"name": "Preview"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id, row_ids, _title = _seed(project, names)
    return pid, project, sheet_id, row_ids


def _poll(client: TestClient, pid: str, preview_id: str, timeout: float = 8.0) -> dict:
    body: dict = {}

    def _reached() -> bool:
        nonlocal body
        resp = client.get(f"{PREVIEW_URL.format(pid=pid)}/{preview_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return body["status"] in {"done", "error", "cancelled"}

    with controlled_time(timeout=timeout) as t:
        try:
            t.wait_until(_reached, message=f"preview {preview_id} did not finish")
        except AssertionError as exc:
            raise AssertionError(
                f"preview {preview_id} did not finish: {body}"
            ) from exc
    return body


# --------------------------------------------------------------------------- #
# MapRunner.preview — writes nothing, returns values
# --------------------------------------------------------------------------- #
def test_preview_computes_sample_and_writes_nothing(tmp_path: Path) -> None:
    project, sheet_id, row_ids, _title = _standalone(
        tmp_path, ["alpha", "beta", "gamma"]
    )
    try:
        before = _counts(project)
        plan = resolve_map_preview(
            project,
            _python_action(
                sheet_id,
                column_name="n_chars",
                idempotency_key="preview@nowrite",
                row_ids=row_ids,
            ),
        )
        assert not isinstance(plan, ActionError), plan
        result = asyncio.run(
            plan.make_runner().preview(plan.runner_spec, program=plan.program)
        )

        assert isinstance(result, PreviewResult)
        assert set(result.values) == set(row_ids)
        assert result.values[row_ids[0]]["n_chars"]["value"] == len("alpha")
        assert result.sampled == len(row_ids)
        assert result.total == len(row_ids)

        # brand-new virtual column, not an overwrite
        assert [c.name for c in result.columns] == ["n_chars"]
        assert result.columns[0].overwrites_column_id is None

        # NOTHING persisted, and no real column materialized
        assert _counts(project) == before
        assert "n_chars" not in {c["name"] for c in project.columns(sheet_id)}
    finally:
        project.close()


def test_preview_marks_overwrite_of_existing_column(tmp_path: Path) -> None:
    project, sheet_id, row_ids, _title = _standalone(tmp_path, ["alpha", "beta"])
    try:
        plan = resolve_map_preview(
            project,
            _python_action(
                sheet_id,
                column_name="n_chars",
                idempotency_key="preview@overwrite",
                row_ids=row_ids,
            ),
        )
        assert not isinstance(plan, ActionError), plan
        existing = project.add_column(
            sheet_id, "n_chars", type="integer", ai_generated=True
        )
        before = _counts(project)
        # Driven at the runner level: an output name that matches an existing
        # column is marked as an overwrite (the overlay replaces that column's
        # sampled values) rather than a brand-new virtual column.
        result = asyncio.run(
            plan.make_runner().preview(plan.runner_spec, program=plan.program)
        )
        assert result.columns[0].overwrites_column_id == existing
        # still nothing written
        assert _counts(project) == before
    finally:
        project.close()


def test_preview_surfaces_per_cell_errors(tmp_path: Path) -> None:
    project, sheet_id, row_ids, title = _standalone(
        tmp_path, ["alpha", "beta", "gamma"]
    )
    try:
        before = _counts(project)
        by_name = _name_to_row_id(project, sheet_id, title, row_ids)
        # 'beta' returns a non-integer -> return_schema mismatch -> per-cell error;
        # the other rows succeed. The run continues past the failed row.
        code = (
            "result = {'n': ('oops' if row['title'] == 'beta' else len(row['title']))}"
        )
        plan = resolve_map_preview(
            project,
            _python_action(
                sheet_id,
                column_name="n_chars",
                idempotency_key="preview@error",
                code=code,
                row_ids=row_ids,
            ),
        )
        assert not isinstance(plan, ActionError), plan
        result = asyncio.run(
            plan.make_runner().preview(plan.runner_spec, program=plan.program)
        )

        alpha = result.values[by_name["alpha"]]["n_chars"]
        assert alpha["value"] == len("alpha")
        assert alpha.get("error") is None

        beta = result.values[by_name["beta"]]["n_chars"]
        assert beta["value"] is None
        assert beta["error"]

        assert _counts(project) == before
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# PREVIEW_MAX_ROWS hard cap
# --------------------------------------------------------------------------- #
def test_preview_cap_rejects_missing_empty_and_oversized(tmp_path: Path) -> None:
    # The cap fires in preview_precheck BEFORE any row scope resolves, so a
    # missing/empty/oversized sample is a hard error — it can NEVER fall back to
    # "all rows" (the shape of the shipped Run-3 whole-sheet-preview bug).
    project, sheet_id, row_ids, _title = _standalone(tmp_path, ["alpha", "beta"])
    try:
        plan = resolve_map_preview(
            project,
            _python_action(
                sheet_id,
                column_name="c",
                idempotency_key="preview@cap",
                row_ids=row_ids,
            ),
        )
        assert not isinstance(plan, ActionError), plan
        runner = plan.make_runner()
        for bad in (_MISSING, None, [], list(range(21))):
            spec = deepcopy(plan.runner_spec)
            spec.pop("row_ids", None)
            if bad is not _MISSING:
                spec["row_ids"] = bad
            with pytest.raises(PreviewRowCapError):
                runner.preview_precheck(spec, program=plan.program)
            # the async entry point rejects the same specs identically
            with pytest.raises(PreviewRowCapError):
                asyncio.run(runner.preview(spec, program=plan.program))
    finally:
        project.close()


def test_preview_empty_row_ids_rejected_via_route(tmp_path: Path) -> None:
    # Through the typed envelope, an empty row_ids is rejected by scope validation
    # (also a 4xx) — the preview is never started for the whole sheet.
    project, sheet_id, _row_ids, _title = _standalone(tmp_path, ["alpha", "beta"])
    try:
        plan = resolve_map_preview(
            project,
            _python_action(
                sheet_id, column_name="c", idempotency_key="preview@empty", row_ids=[]
            ),
        )
        assert isinstance(plan, ActionError)
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# routes / job lifecycle
# --------------------------------------------------------------------------- #
def test_preview_route_start_poll_done_writes_nothing(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, project, sheet_id, row_ids = _route_project(client, ["alpha", "beta", "gamma"])
    before = _counts(project)

    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@done",
            row_ids=row_ids,
        ),
    )
    assert resp.status_code == 202, resp.text
    started = resp.json()
    assert started["preview_id"]
    assert started["total"] == len(row_ids)

    done = _poll(client, pid, started["preview_id"])
    assert done["status"] == "done", done
    result = done["result"]
    assert result["columns"][0]["name"] == "n_chars"
    assert result["columns"][0]["overwrites_column_id"] is None
    assert str(row_ids[0]) in result["rows"]
    assert result["rows"][str(row_ids[0])]["n_chars"]["value"] == len("alpha")
    assert result["sampled"] == len(row_ids)

    # No durable state produced by the preview.
    assert _counts(project) == before
    assert "n_chars" not in {c["name"] for c in project.columns(sheet_id)}


@pytest.mark.parametrize("scope", ["all_rows", "exact_membership"])
def test_scoped_ner_preview_samples_complete_semantic_scope(
    tmp_path: Path,
    scope: str,
) -> None:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = _NerSidecarHttp()  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "ws", router=router))
    pid, project, sheet_id, row_ids = _route_project(
        client,
        [f"row {index}" for index in range(25)],
    )
    exact_ids = row_ids[:21] if scope == "exact_membership" else None
    before = _counts(project)

    started_response = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_ner_action(
            sheet_id,
            row_ids=exact_ids,
            idempotency_key=f"preview@ner-{scope}",
        ),
    )

    assert started_response.status_code == 202, started_response.text
    started = started_response.json()
    expected_total = len(exact_ids) if exact_ids is not None else len(row_ids)
    assert started["total"] == expected_total
    done = _poll(client, pid, started["preview_id"])
    assert done["status"] == "done", done
    assert done["result"]["sampled"] == 20
    assert done["result"]["total"] == expected_total
    assert done["result"]["row_ids"] == row_ids[:20]
    assert _counts(project) == before


def test_exact_empty_ner_scope_is_refused_before_a_preview_starts(
    tmp_path: Path,
) -> None:
    # The typed request boundary refuses an empty exact scope outright (only a
    # host-computed backfill may carry an empty selection), so no preview job
    # exists to sample zero rows and nothing durable is touched.
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = _NerSidecarHttp()  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "ws", router=router))
    pid, project, sheet_id, _row_ids = _route_project(client, ["row"])
    before = _counts(project)

    started_response = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_ner_action(
            sheet_id,
            row_ids=[],
            idempotency_key="preview@ner-empty",
        ),
    )

    assert started_response.status_code == 400, started_response.text
    error = started_response.json()["error"]
    assert error["code"] == "invalid_action_request"
    assert error["action_kind"] == "map.ner"
    assert "row ids must not be empty" in error["message"]
    assert "preview_id" not in started_response.json()
    assert _counts(project) == before


def test_preview_route_unknown_id_is_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, _project, _sheet_id, _row_ids = _route_project(client, ["a"])
    resp = client.get(f"{PREVIEW_URL.format(pid=pid)}/does-not-exist")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "preview_not_found"


def test_preview_route_missing_provider_key_refuses_after_exact_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exact consent does not substitute for the ordinary provider-key gate.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    router = ModelRouter(cache_mode="off")
    client = TestClient(create_app(tmp_path / "ws", router=router))
    pid, project, sheet_id, row_ids = _route_project(client, ["alpha", "beta"])
    action = _summarize_action(sheet_id, row_ids=row_ids)
    before = _database_counts(project)
    before_changes = project.db.total_changes

    # Obtain the exact echo through the same typed program validator. A valid
    # hash must reach the key gate; a made-up hash only proves re-confirmation.
    plan = resolve_map_preview(project, action, router=router)
    assert not isinstance(plan, ActionError), plan
    runner = plan.make_runner()
    with pytest.raises(CostGate) as legacy_gate:
        validation.validate_spec(
            project,
            router,
            runner.run_store,
            plan.runner_spec,
            confirmed=False,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
            persistence="ephemeral",
            program=plan.program,
        )
    exact_hash = legacy_gate.value.promise_set_hash
    assert isinstance(exact_hash, str) and len(exact_hash) == 64

    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=action,
    )

    assert resp.status_code == 402, resp.text
    error = resp.json()["error"]
    assert error["code"] == "cost_gate"
    assert error["needs_confirmation"] is True
    assert error["details"]["promise_set_hash"] == exact_hash
    assert "preview_id" not in resp.json()
    assert client.app.state.action_preview_job_registry._jobs == {}  # noqa: SLF001
    assert _database_counts(project) == before
    assert project.db.total_changes == before_changes

    exact_retry = deepcopy(action)
    exact_retry["confirmation"] = exact_hash
    retry_response = client.post(PREVIEW_URL.format(pid=pid), json=exact_retry)
    assert retry_response.status_code == 400, retry_response.text
    assert retry_response.json()["error"]["code"] == "missing_provider_key"
    assert client.app.state.action_preview_job_registry._jobs == {}  # noqa: SLF001
    assert _database_counts(project) == before
    assert project.db.total_changes == before_changes


@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-haiku-4-5", "anthropic/unknown-frontier"],
)
def test_paid_preview_wrong_confirmation_cannot_authorize_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    """Attacker-supplied consent bits never authorize uncheckpointed effect."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _PaidSummaryAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "ws", router=router))
    pid, project, sheet_id, row_ids = _route_project(client, ["alpha"])
    action = _summarize_action(sheet_id, row_ids=row_ids, model=model)
    action["confirmation"] = "0" * 64
    before = _database_counts(project)
    before_changes = project.db.total_changes

    refused = client.post(PREVIEW_URL.format(pid=pid), json=action)

    assert refused.status_code == 402, refused.text
    error = refused.json()["error"]
    assert error["code"] == "cost_gate"
    assert error["action_kind"] == "map.summarize"
    assert error["needs_confirmation"] is True
    assert error["details"]["promise_set_hash"] != action["confirmation"]
    assert "preview_id" not in refused.json()
    assert client.app.state.action_preview_job_registry._jobs == {}  # noqa: SLF001
    assert adapter.requests == []
    assert _database_counts(project) == before
    assert project.db.total_changes == before_changes


def test_preview_route_unsupported_action_kind_is_4xx(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, _project, sheet_id, _row_ids = _route_project(client, ["a"])
    # reduce.* is not a reserved-maprunner (map) action -> preview unsupported.
    body = {
        "schema_version": "frisket.action.v2",
        "kind": "reduce.group_summary",
        "capabilities": ["project:write", "model:complete"],
        "params": {
            "sheet_id": sheet_id,
            "input_columns": ["title"],
            "group_by": "title",
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Summarize.",
            "target_sheet_name": "Summaries",
            "confirmed": True,
        },
        "idempotency_key": "route@unsupported",
    }
    resp = client.post(PREVIEW_URL.format(pid=pid), json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "unsupported_action_kind"


# --- error / cancel / replace via injected runners --------------------------- #
class _BoomRunner:
    """A runner whose guards pass but whose async compute raises an uncaught
    exception (the kind _execute_row does NOT classify) — it must land in
    job.error, never a hung 'running'."""

    def preview_precheck(self, spec: dict) -> int:
        return 3

    async def preview(self, spec, *, progress_cb=None, cancel_event=None):
        raise RuntimeError("kaboom")


class _SlowRunner:
    """A runner that spins until cancelled, to exercise cancel + replace."""

    def preview_precheck(self, spec: dict) -> int:
        return 3

    async def preview(self, spec, *, progress_cb=None, cancel_event=None):
        for _ in range(2000):
            if cancel_event is not None and cancel_event.is_set():
                break
            await asyncio.sleep(0.005)
        return PreviewResult(
            sheet_id=int(spec.get("sheet_id") or 0),
            columns=[],
            values={},
            row_ids=[],
            sampled=0,
            total=0,
        )


class _HaltRunner:
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail

    def preview_precheck(self, spec: dict) -> int:
        return 1

    async def preview(self, spec, *, progress_cb=None, cancel_event=None):
        raise RecipeInvocationHalt(self.code, self.detail)


class _HaltOnCancelRunner:
    def __init__(self) -> None:
        self.started = threading.Event()

    def preview_precheck(self, spec: dict) -> int:
        return 1

    async def preview(self, spec, *, progress_cb=None, cancel_event=None):
        self.started.set()
        while cancel_event is None or not cancel_event.is_set():
            await asyncio.sleep(0.002)
        raise RecipeInvocationHalt(
            "local_session_failed",
            "teardown raced cancellation",
        )


class _ReplacementJoinRunner:
    """The first call delays cleanup after cancel; call two records whether
    it was allowed to begin before that cleanup completed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self.first_started = threading.Event()
        self.first_cleaned = threading.Event()
        self.successor_saw_cleanup = False

    def preview_precheck(self, spec: dict) -> int:
        return 1

    async def preview(self, spec, *, progress_cb=None, cancel_event=None):
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.first_started.set()
            while cancel_event is None or not cancel_event.is_set():
                await asyncio.sleep(0.002)
            # Stand in for bounded child-process teardown/reap.
            await asyncio.sleep(0.03)
            self.first_cleaned.set()
        else:
            self.successor_saw_cleanup = self.first_cleaned.is_set()
        return PreviewResult(
            sheet_id=int(spec.get("sheet_id") or 0),
            columns=[],
            values={},
            row_ids=[],
            sampled=0,
            total=0,
        )


class _CompositionAwarePreviewRunner:
    """Fresh request carrier around shared lifecycle-test behavior."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.execution_composition: ExecutionComposition | None = None
        self.router = None

    def _require_execution_composition(self) -> ExecutionComposition:
        composition = self.execution_composition
        assert composition is not None
        return composition

    def prepare_preview(
        self, spec: dict, *, program, allow_empty_scope=False, accounted=False
    ):
        from types import SimpleNamespace

        self._require_execution_composition()
        assert program.name == spec["action_kind"]
        count = self._delegate.preview_precheck(spec)
        return SimpleNamespace(recipe=program, row_ids=list(range(count)), est=None)

    async def preview(
        self,
        spec,
        *,
        program,
        allow_empty_scope=False,
        progress_cb=None,
        cancel_event=None,
    ):
        self._require_execution_composition()
        assert program.name == spec["action_kind"]
        return await self._delegate.preview(
            spec,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )


def _client_with_runner(tmp_path: Path, runner) -> TestClient:
    def deps_factory(_pid, _request):
        return ExecutorDeps(
            map_runner_factory=lambda _project, _router: _CompositionAwarePreviewRunner(
                runner
            )
        )

    return TestClient(create_app(tmp_path / "ws", executor_deps_factory=deps_factory))


def test_preview_route_thread_exception_becomes_job_error(tmp_path: Path) -> None:
    client = _client_with_runner(tmp_path, _BoomRunner())
    pid, _project, sheet_id, row_ids = _route_project(
        client, ["alpha", "beta", "gamma"]
    )
    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@boom",
            row_ids=row_ids,
        ),
    )
    assert resp.status_code == 202, resp.text
    body = _poll(client, pid, resp.json()["preview_id"])
    assert body["status"] == "error", body
    assert body["error"]["code"] == "preview_failed"
    assert "kaboom" in body["error"]["message"]


@pytest.mark.parametrize(
    "code",
    [
        "local_engine_busy",
        "local_artifact_unavailable",
        "local_session_failed",
    ],
)
def test_preview_route_preserves_allowlisted_invocation_halt(
    tmp_path: Path, code: str
) -> None:
    secret = "sk-preview-halt-secret-123456789"
    detail = f"parent-owned detail api_key={secret} " + ("x" * 700)
    client = _client_with_runner(tmp_path, _HaltRunner(code, detail))
    pid, _project, sheet_id, row_ids = _route_project(client, ["alpha"])
    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key=f"route@halt-{code}",
            row_ids=row_ids,
        ),
    )
    assert resp.status_code == 202, resp.text
    body = _poll(client, pid, resp.json()["preview_id"])
    assert body["status"] == "error", body
    assert body["error"]["code"] == code
    assert secret not in body["error"]["message"]
    assert "[REDACTED]" in body["error"]["message"]
    assert len(body["error"]["message"]) <= 500


def test_preview_route_flattens_unallowlisted_invocation_halt(tmp_path: Path) -> None:
    secret = "sk-child-protocol-secret-123456789"
    client = _client_with_runner(
        tmp_path,
        _HaltRunner(
            "child_invented_fatal_code",
            f"raw child detail must not cross api_key={secret}",
        ),
    )
    pid, _project, sheet_id, row_ids = _route_project(client, ["alpha"])
    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@unknown-halt",
            row_ids=row_ids,
        ),
    )
    assert resp.status_code == 202, resp.text
    body = _poll(client, pid, resp.json()["preview_id"])
    assert body["status"] == "error", body
    assert body["error"] == {
        "code": "preview_failed",
        "message": "Local preview engine failed.",
    }
    assert "child_invented_fatal_code" not in str(body)
    assert secret not in str(body)


def test_preview_route_cancel_is_idempotent_204(tmp_path: Path) -> None:
    client = _client_with_runner(tmp_path, _SlowRunner())
    pid, _project, sheet_id, row_ids = _route_project(
        client, ["alpha", "beta", "gamma"]
    )
    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@cancel",
            row_ids=row_ids,
        ),
    )
    assert resp.status_code == 202, resp.text
    preview_id = resp.json()["preview_id"]

    cancel = client.delete(f"{PREVIEW_URL.format(pid=pid)}/{preview_id}")
    assert cancel.status_code == 204, cancel.text
    # idempotent: cancelling again (or an unknown id) is still a no-op 204
    assert (
        client.delete(f"{PREVIEW_URL.format(pid=pid)}/{preview_id}").status_code == 204
    )
    assert client.delete(f"{PREVIEW_URL.format(pid=pid)}/nope").status_code == 204

    body = _poll(client, pid, preview_id)
    assert body["status"] == "cancelled", body


def test_preview_route_cancellation_wins_over_concurrent_halt(tmp_path: Path) -> None:
    runner = _HaltOnCancelRunner()
    client = _client_with_runner(tmp_path, runner)
    pid, _project, sheet_id, row_ids = _route_project(client, ["alpha"])
    resp = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@cancel-halt-race",
            row_ids=row_ids,
        ),
    )
    assert resp.status_code == 202, resp.text
    assert runner.started.wait(_STARTED_WAIT_SECONDS)
    preview_id = resp.json()["preview_id"]
    assert (
        client.delete(f"{PREVIEW_URL.format(pid=pid)}/{preview_id}").status_code == 204
    )
    body = _poll(client, pid, preview_id)
    assert body["status"] == "cancelled", body
    assert "error" not in body


def test_preview_route_replace_cancels_prior_job(tmp_path: Path) -> None:
    client = _client_with_runner(tmp_path, _SlowRunner())
    pid, _project, sheet_id, row_ids = _route_project(
        client, ["alpha", "beta", "gamma"]
    )

    first = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@replace1",
            row_ids=row_ids,
        ),
    )
    assert first.status_code == 202, first.text
    first_id = first.json()["preview_id"]

    second = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@replace2",
            row_ids=row_ids,
        ),
    )
    assert second.status_code == 202, second.text
    second_id = second.json()["preview_id"]
    assert second_id != first_id

    # Replace semantics: the prior job is cancelled but stays queryable until
    # TTL eviction — a stale poller of the replaced id sees status=cancelled,
    # never a mystery 404.
    body = _poll(client, pid, first_id)
    assert body["status"] == "cancelled", body
    assert client.get(f"{PREVIEW_URL.format(pid=pid)}/{second_id}").status_code == 200


def test_preview_route_replace_joins_prior_before_successor_starts(
    tmp_path: Path,
) -> None:
    runner = _ReplacementJoinRunner()
    client = _client_with_runner(tmp_path, runner)
    pid, _project, sheet_id, row_ids = _route_project(client, ["alpha"])

    first = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@join-first",
            row_ids=row_ids,
        ),
    )
    assert first.status_code == 202, first.text
    assert runner.first_started.wait(_STARTED_WAIT_SECONDS)

    second = client.post(
        PREVIEW_URL.format(pid=pid),
        json=_python_action(
            sheet_id,
            column_name="n_chars",
            idempotency_key="route@join-second",
            row_ids=row_ids,
        ),
    )
    assert second.status_code == 202, second.text
    assert runner.first_cleaned.is_set()
    # Acceptance does not promise the successor thread has executed yet.
    assert _poll(client, pid, second.json()["preview_id"])["status"] == "done"
    assert runner.successor_saw_cleanup is True
    assert _poll(client, pid, first.json()["preview_id"])["status"] == "cancelled"


def test_preview_registry_shutdown_cancels_and_joins_all_workers() -> None:
    registry = ActionPreviewJobRegistry()
    started = [threading.Event(), threading.Event()]
    stopped = [threading.Event(), threading.Event()]

    def run_at(index: int):
        def run(_progress_cb, cancel_event):
            started[index].set()
            cancel_event.wait()
            stopped[index].set()
            return index

        return run

    jobs = [registry.start(f"project-{index}", 1, run_at(index)) for index in range(2)]
    assert all(event.wait(_STARTED_WAIT_SECONDS) for event in started)

    registry.shutdown()
    registry.shutdown()  # idempotent

    assert all(event.is_set() for event in stopped)
    assert all(job.status == "cancelled" for job in jobs)
    assert all(not thread.is_alive() for thread in registry._threads.values())
    with pytest.raises(RuntimeError, match="shut down"):
        registry.start("project-new", 1, run_at(0))


def test_preview_registry_serializes_concurrent_same_project_replacements() -> None:
    registry = ActionPreviewJobRegistry()
    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    initial_started = threading.Event()
    release_starters = threading.Event()
    jobs = []
    errors = []

    def run(_progress_cb, cancel_event):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        initial_started.set()
        cancel_event.wait()
        with counter_lock:
            active -= 1
        return None

    jobs.append(registry.start("same-project", 1, run))
    assert initial_started.wait(_STARTED_WAIT_SECONDS)

    def replace() -> None:
        release_starters.wait()
        try:
            job = registry.start("same-project", 1, run)
        except BaseException as exc:  # test must report thread failures
            errors.append(exc)
        else:
            jobs.append(job)

    with controlled_time() as t:
        starters = [t.background(replace) for _ in range(2)]
        release_starters.set()
        for thread in starters:
            thread.join(timeout=5.0)
            assert not thread.is_alive()

    registry.shutdown()

    assert errors == []
    assert len(jobs) == 3
    assert max_active == 1
    assert all(job.status == "cancelled" for job in jobs)


def test_local_app_lifespan_joins_preview_workers(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    registry = app.state.action_preview_job_registry
    started = threading.Event()
    stopped = threading.Event()

    def run(_progress_cb, cancel_event):
        started.set()
        cancel_event.wait()
        stopped.set()
        return None

    with TestClient(app):
        job = registry.start("project-lifespan", 1, run)
        assert started.wait(_STARTED_WAIT_SECONDS)

    assert stopped.is_set()
    assert job.status == "cancelled"
    assert all(not thread.is_alive() for thread in registry._threads.values())


# --------------------------------------------------------------------------- #
# registry unit: TTL eviction
# --------------------------------------------------------------------------- #
class _CloseablePreviewResult:
    def __init__(self, *, fail=False):
        self.closed = 0
        self.fail = fail

    def close(self):
        self.closed += 1
        if self.fail:
            raise RuntimeError("cleanup failed")


def test_registry_closes_result_cancelled_as_worker_returns() -> None:
    registry = ActionPreviewJobRegistry()
    produced = threading.Event()
    release = threading.Event()
    result = _CloseablePreviewResult()

    def run(_progress_cb, _cancel_event):
        produced.set()
        release.wait()
        return result

    job = registry.start("proj", None, run)
    assert produced.wait(_STARTED_WAIT_SECONDS)
    assert registry.cancel("proj", job.id) is True
    release.set()
    with controlled_time(timeout=4.0) as t:
        t.wait_until(lambda: job.finished_at is not None, message="job did not finish")
    assert job.status == "cancelled"
    assert job.result is None
    assert result.closed == 1


def test_replacement_keeps_successful_result_until_registry_cleanup() -> None:
    registry = ActionPreviewJobRegistry()
    first_result = _CloseablePreviewResult()
    first = registry.start("proj", None, lambda *_args: first_result)
    with controlled_time(timeout=4.0) as t:
        t.wait_until(lambda: first.status == "done", message="first job did not finish")

    second_result = _CloseablePreviewResult()
    second = registry.start("proj", None, lambda *_args: second_result)
    with controlled_time(timeout=4.0) as t:
        t.wait_until(
            lambda: second.status == "done", message="second job did not finish"
        )
    assert registry.get("proj", first.id).result is first_result
    assert first_result.closed == 0

    registry.shutdown()
    assert first_result.closed == 1
    assert second_result.closed == 1


def test_shutdown_cleanup_failure_does_not_skip_other_results(caplog) -> None:
    registry = ActionPreviewJobRegistry()
    failing = _CloseablePreviewResult(fail=True)
    healthy = _CloseablePreviewResult()
    jobs = [
        registry.start("one", None, lambda *_args: failing),
        registry.start("two", None, lambda *_args: healthy),
    ]
    with controlled_time(timeout=4.0) as t:
        t.wait_until(
            lambda: all(job.status == "done" for job in jobs),
            message="jobs did not finish",
        )

    registry.shutdown()
    assert failing.closed == 1
    assert healthy.closed == 1
    assert "action_preview_result_cleanup_failed" in caplog.text


def test_registry_ttl_evicts_finished_job() -> None:
    registry = ActionPreviewJobRegistry(ttl_seconds=0.0)

    result = _CloseablePreviewResult()

    def run(progress_cb, cancel_event):
        progress_cb(1, None)
        return result

    job = registry.start("proj", 1, run)
    # With ttl 0, the access AFTER the job finishes evicts it -> None.
    with controlled_time(timeout=4.0) as t:
        t.wait_until(
            lambda: registry.get("proj", job.id) is None,
            message="job was never evicted after ttl_seconds=0",
        )
    assert registry.get("proj", job.id) is None
    assert result.closed == 1
