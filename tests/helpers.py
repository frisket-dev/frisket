"""Shared test-client construction helpers.

Plain importable module (not conftest.py) so `from helpers import
make_client` has a single, unambiguous target under pytest's default
prepend import mode — a same-named conftest.py living in another test
directory collected in the same session would otherwise race for the
one `sys.modules["conftest"]` slot.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter, ResponseCache
from frisket.server.app import create_app
from frisket.testing import llm_cache_path


def replace_test_source_cell(
    project: Any,
    *,
    row_id: int,
    column_id: int,
    value: Any,
    label: str = "test source update",
    db: Any | None = None,
) -> int:
    """Replace one fixture source cell through the authoritative write path."""

    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        replace_base_cells,
    )

    target_db = project.db if db is None else db

    def write() -> int:
        if target_db is project.db:
            op_id = project.append_op(
                "test.source.update",
                {"row_id": int(row_id), "column_id": int(column_id)},
                label=label,
                commit=False,
            )
        else:
            from frisket.engine.store.op_log import append_op

            op_id = append_op(
                SimpleNamespace(db=target_db),
                "test.source.update",
                {"row_id": int(row_id), "column_id": int(column_id)},
                label=label,
                commit=False,
            )
        producer_id = create_base_cell_producer(
            target_db,
            stage_id=f"op:{op_id}",
            op_id=op_id,
        )
        replace_base_cells(
            target_db,
            producer_id=producer_id,
            column_ids=[int(column_id)],
            row_ids=[int(row_id)],
            cells=[BaseCellWrite(int(row_id), int(column_id), value)],
        )
        return op_id

    if db is not None:
        return write()
    with target_db:
        return write()


def initialize_test_source_cells(
    project: Any,
    cells: Iterable[tuple[int, int, Any]],
    *,
    label: str = "test source initialization",
) -> int:
    """Initialize a fixture base-cell batch through one source producer."""

    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        initialize_base_cells,
    )

    writes = [
        BaseCellWrite(int(row_id), int(column_id), value)
        for row_id, column_id, value in cells
    ]
    with project.db:
        op_id = project.append_op(
            "test.source.initialize",
            {"count": len(writes)},
            label=label,
            commit=False,
        )
        producer_id = create_base_cell_producer(
            project.db,
            stage_id=f"op:{op_id}",
            op_id=op_id,
        )
        initialize_base_cells(project.db, producer_id=producer_id, cells=writes)
    return op_id


def replace_test_source_cells(
    project: Any,
    cells: Iterable[tuple[int, int, Any]],
    *,
    label: str = "test source update",
) -> int:
    """Replace fixture source cells under one logical source producer."""

    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        replace_base_cells,
    )

    writes = [
        BaseCellWrite(int(row_id), int(column_id), value)
        for row_id, column_id, value in cells
    ]
    by_column: dict[int, list[BaseCellWrite]] = {}
    for write in writes:
        by_column.setdefault(write.column_id, []).append(write)
    with project.db:
        op_id = project.append_op(
            "test.source.update",
            {"count": len(writes)},
            label=label,
            commit=False,
        )
        producer_id = create_base_cell_producer(
            project.db,
            stage_id=f"op:{op_id}",
            op_id=op_id,
        )
        for column_id, column_writes in by_column.items():
            replace_base_cells(
                project.db,
                producer_id=producer_id,
                column_ids=[column_id],
                row_ids=[write.row_id for write in column_writes],
                cells=column_writes,
            )
    return op_id


@dataclass(frozen=True)
class RunWriterAuthorityFixture:
    """Explicit execution authority for store-level run writer fixtures."""

    writer_attempt_id: str
    claim_token: str | None
    claimless_direct_effect: bool

    def kwargs(self) -> dict[str, Any]:
        return {
            "writer_attempt_id": self.writer_attempt_id,
            "claim_token": self.claim_token,
            "claimless_direct_effect": self.claimless_direct_effect,
            "authorized_attempt_id": self.writer_attempt_id,
        }


def run_writer_authority_fixture(
    project: Any,
    run_id: int,
    *,
    output_column_ids: set[int] | frozenset[int] = frozenset(),
    claimless_direct_effect: bool = False,
) -> RunWriterAuthorityFixture:
    """Install a real dispatching writer and, when needed, an exact claim.

    Store-level tests used to write directly to a run with no execution
    attempt.  That is no longer a legal production state.  This helper keeps
    those tests explicit: result-producing fixtures name their exact output
    columns, while fact-only fixtures must deliberately select the claimless
    direct-effect contract.
    """

    from frisket.engine.store.output_claims import OutputColumnClaimStore

    column_ids = {int(column_id) for column_id in output_column_ids}
    if claimless_direct_effect and column_ids:
        raise AssertionError("claimless test authority cannot name output columns")

    run = project.db.execute(
        "SELECT sheet_id, action_kind, current_attempt_id FROM runs WHERE id=?",
        (int(run_id),),
    ).fetchone()
    if run is None:
        raise AssertionError(f"missing fixture run {run_id}")

    attempt_id = (
        None if run["current_attempt_id"] is None else str(run["current_attempt_id"])
    )
    if attempt_id is not None:
        attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None or str(attempt["state"]) != "dispatching":
            attempt_id = None
    if attempt_id is None:
        attempt_id = f"attempt_test_writer_{uuid.uuid4().hex}"
        seq = int(
            project.db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM execution_attempts "
                "WHERE run_id=?",
                (int(run_id),),
            ).fetchone()[0]
        )
        project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
            "VALUES (?, ?, ?, 'dispatching', 'test-store-writer', '[]', "
            "datetime('now'))",
            (attempt_id, int(run_id), seq),
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (attempt_id, int(run_id)),
        )
        project.db.commit()

    active_claims = project.db.execute(
        "SELECT claim_token, column_id FROM output_column_claims "
        "WHERE run_id=? AND status='active' ORDER BY claim_token, column_id",
        (int(run_id),),
    ).fetchall()
    if claimless_direct_effect:
        if active_claims:
            raise AssertionError(
                "claimless test authority cannot reuse a claimed output run"
            )
        return RunWriterAuthorityFixture(attempt_id, None, True)

    by_token: dict[str, set[int]] = {}
    for claim in active_claims:
        if claim["claim_token"] is None or claim["column_id"] is None:
            continue
        by_token.setdefault(str(claim["claim_token"]), set()).add(
            int(claim["column_id"])
        )
    for token, claimed_ids in by_token.items():
        if column_ids <= claimed_ids:
            return RunWriterAuthorityFixture(attempt_id, token, False)
    if active_claims:
        raise AssertionError(
            "fixture run already has an active claim that does not cover "
            f"the requested output columns {sorted(column_ids)}"
        )
    if not column_ids:
        raise AssertionError(
            "claimed test authority requires at least one output column"
        )

    placeholders = ",".join("?" for _ in column_ids)
    columns = project.db.execute(
        "SELECT id, sheet_id, name FROM columns "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        tuple(sorted(column_ids)),
    ).fetchall()
    if {int(column["id"]) for column in columns} != column_ids:
        raise AssertionError("claimed test authority names a missing output column")
    if {int(column["sheet_id"]) for column in columns} != {int(run["sheet_id"])}:
        raise AssertionError("claimed test authority crosses the run's sheet")

    token = f"output-claim:test-store-writer:{uuid.uuid4().hex}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=int(run["sheet_id"]),
        output_names=[str(column["name"]) for column in columns],
        action_kind=str(run["action_kind"]),
        run_id=int(run_id),
        claim_token=token,
        lease_seconds=int(timedelta(hours=6).total_seconds()),
    )
    if conflict is not None or len(claims) != len(column_ids):
        raise AssertionError("could not acquire exact test output authority")
    OutputColumnClaimStore(project).bind_to_run(
        claim_token=token,
        run_id=int(run_id),
        expected_output_names=[str(column["name"]) for column in columns],
    )
    return RunWriterAuthorityFixture(attempt_id, token, False)


def write_claimed_test_results(
    project: Any,
    run_id: int,
    batch: list[dict[str, Any]],
    **kwargs: Any,
) -> None:
    """Publish a fixture batch through the universal generation protocol.

    Store fixtures used to stop after writing ``results`` and later made the
    values visible by assigning ``columns.current_run_id``.  That scalar is no
    longer read authority.  A fixture which means "this AI result is current"
    must therefore do the same three things as a real producer: declare every
    output generation before the first result, attach an explicit publication
    effect to every attempted cell, and seal the complete output group while
    its exact claim is still active.

    This helper intentionally seals on each call.  Tests for open-generation
    recovery use the lower-level generation helpers directly; silently
    reopening a sealed fixture run here would recreate the retired in-place
    resume contract.
    """

    from frisket.engine.runner.result_generations import _compatibility_key
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore

    output_column_ids = {
        int(item["column_id"]) for item in batch if item.get("column_id") is not None
    }
    authority = run_writer_authority_fixture(
        project,
        run_id,
        output_column_ids=output_column_ids,
    )
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    try:
        assert authority.claim_token is not None
        generations = ResultGenerationStore(project)
        columns = {
            int(row["id"]): row
            for row in project.db.execute(
                "SELECT id,name,type,semantic_type,format FROM columns "
                f"WHERE id IN ({','.join('?' for _ in output_column_ids)})",
                tuple(sorted(output_column_ids)),
            ).fetchall()
        }
        if set(columns) != output_column_ids:
            raise AssertionError("fixture generation names a missing output column")
        for column_id in sorted(output_column_ids):
            if generations.get_binding(int(run_id), column_id) is not None:
                continue
            column = columns[column_id]
            origin_run_ids = generations.origin_run_ids(
                column_id,
                [
                    int(item["row_id"])
                    for item in batch
                    if int(item["column_id"]) == column_id
                ],
            )
            if len(origin_run_ids) > 1:
                raise AssertionError("fixture replacement has mixed-origin heads")
            base_binding = (
                generations.get_binding(origin_run_ids[0], column_id)
                if origin_run_ids
                else None
            )
            generations.declare(
                int(run_id),
                column_id,
                output_role=(
                    base_binding.output_role
                    if base_binding is not None
                    else str(column["name"])
                ),
                compatibility_key=(
                    base_binding.compatibility_key
                    if base_binding is not None
                    else _compatibility_key(
                        field={
                            "name": str(column["name"]),
                            "column_type": str(column["type"]),
                            "semantic_type": column["semantic_type"],
                            "format": column["format"],
                        }
                    )
                ),
                write_mode=(
                    "replace_scope"
                    if generations.is_generation_managed(column_id)
                    else "create"
                ),
                claim_token=authority.claim_token,
            )
        published_batch: list[dict[str, Any]] = []
        for item in batch:
            published = dict(item)
            if published.get("publication_effect") is None:
                published["publication_effect"] = (
                    "publish_error"
                    if published.get("error") is not None
                    or published.get("error_code") is not None
                    else "publish_null"
                    if published.get("value") is None
                    else "publish_value"
                )
            published_batch.append(published)
        RunResultStore(project).write_results(
            run_id,
            published_batch,
            **authority.kwargs(),
            **kwargs,
        )
        generations.seal(
            int(run_id),
            output_column_ids,
            claim_token=authority.claim_token,
            terminal_disposition="completed",
        )
    except BaseException:
        OutputColumnClaimStore(project).finish_current_writer(
            run_id=run_id,
            writer_attempt_id=authority.writer_attempt_id,
            claim_token=authority.claim_token,
            attempt_state="halted",
        )
        raise
    project.db.commit()
    OutputColumnClaimStore(project).finish_current_writer(
        run_id=run_id,
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
        attempt_state="effected",
    )


def write_claimless_test_model_calls(
    project: Any,
    run_id: int,
    batch: list[dict[str, Any]],
    **kwargs: Any,
) -> float:
    """Write fact-only fixture accounting under explicit direct authority."""

    from frisket.engine.store.runs import RunResultStore

    authority = run_writer_authority_fixture(
        project,
        run_id,
        claimless_direct_effect=True,
    )
    result = RunResultStore(project).write_model_calls(
        run_id,
        batch,
        **authority.kwargs(),
        **kwargs,
    )
    project.db.commit()
    return result


# Whole-file or per-test marker for anything that spins up the real RapidOCR
# runtime (the sandboxed subprocess in frisket.engine._workers.rapidocr_worker,
# or product code that imports the `rapidocr` package directly). `find_spec`
# is a package-presence check only -- it never imports rapidocr, so it costs
# nothing even applied broadly, and it answers the same question that
# `sys.modules["rapidocr"] = None` (the standard "simulate absent" probe)
# answers: is the distribution importable at all. Tests that only exercise the
# OCR recipe with `_page_images`/`_ocr_rapidocr` monkeypatched, or a
# fully-faked `RapidOCRProcessPool`, do NOT need this guard -- the real
# runtime is never reached. Tests that select engine="rapidocr" through
# `OcrRecipe.execution_scope` (media.ocr / map.extract with an unmocked
# execution_scope) DO, because that path always constructs a real
# RapidOCRProcessPool regardless of what the recipe's own OCR methods are
# monkeypatched to.
requires_ocr_runtime = pytest.mark.skipif(
    importlib.util.find_spec("rapidocr") is None,
    reason="base OCR runtime not installed (run uv sync)",
)


def stub_rapidocr_run_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass pool preflight only when a test already fakes OCR inference.

    These tests exercise action/evidence behavior after inference. The real
    process-pool lifecycle remains covered by its worker/session/recipe suites.
    """
    from frisket.ops import ocr_engines

    @asynccontextmanager
    async def fake_execution_scope(
        *,
        expected_rows: int,
        language: str | None,
        cancelled=None,
    ):
        yield None

    monkeypatch.setattr(ocr_engines, "rapidocr_execution_scope", fake_execution_scope)


def stub_parakeet_run_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass session preflight only when a test already fakes inference."""
    from frisket.sdk.ops import transcribe_engines

    @asynccontextmanager
    async def fake_execution_scope(
        spec: dict[str, Any],
        ctx: Any,
        *,
        expected_rows: int,
    ):
        del ctx, expected_rows
        if spec.get("engine") != "parakeet-tdt":
            raise AssertionError("fake Parakeet scope used for a different engine")
        yield

    monkeypatch.setattr(
        transcribe_engines, "transcription_execution_scope", fake_execution_scope
    )


def make_client(
    tmp_path: Path, *, router: ModelRouter | None = None, **kwargs: Any
) -> TestClient:
    """One TestClient over a create_app workspace rooted in the test's tmp dir.

    ``router=None`` keeps create_app's local-tier default (per-project replay
    cache + workspace file keys). Extra ``create_app`` kwargs pass through for
    the handful of tests that tune the server (grace windows, executor deps).
    """
    return TestClient(create_app(tmp_path / "ws", router=router, **kwargs))


def replay_router() -> ModelRouter:
    """The canonical replay-strict router over the committed LLM cache."""
    return ModelRouter(
        keys={"anthropic": "k", "gemini": "k", "openai": "k"},
        cache=ResponseCache(llm_cache_path()),
        cache_mode="replay_strict",
    )


@dataclass
class CliResult:
    """subprocess.CompletedProcess-shaped result, so existing `_cli()`
    call sites (``result.returncode`` / ``.stdout`` / ``.stderr``) need no
    edits beyond swapping the helper they call."""

    returncode: int
    stdout: str
    stderr: str


class _StdinShim:
    """Stand-in for sys.stdin when a test feeds the CLI a spec over stdin.

    `frisket.cli` reads `sys.stdin.buffer.read(...)` for `spec == "-"`, so a
    plain io.StringIO (no `.buffer`) won't do.
    """

    def __init__(self, data: str) -> None:
        self.buffer = io.BytesIO(data.encode("utf-8"))
        self._text = io.StringIO(data)

    def isatty(self) -> bool:
        return False

    def read(self, *args: Any, **kwargs: Any) -> str:
        return self._text.read(*args, **kwargs)

    def readline(self, *args: Any, **kwargs: Any) -> str:
        return self._text.readline(*args, **kwargs)


def run_cli(*args: str, input_data: str | None = None) -> CliResult:
    """In-process stand-in for ``subprocess.run([sys.executable, "-m",
    "frisket.cli", *args], ...)``.

    Calls `frisket.cli.main()` directly instead of spawning a subprocess,
    skipping the ~1.3s interpreter-startup/import cost that dominates the
    `_cli()`-per-test-file pattern. Snapshots and restores everything
    `main()`'s subcommand paths are known to touch — sys.argv, sys.stdin,
    os.environ, cwd, and the root logger's handlers/level — so back-to-back
    calls stay as isolated as separate subprocesses would be.

    Not a fit for tests asserting genuine process-boundary behavior: exit
    signals, crash exit codes, timeouts, worker/sandbox process spawning, or
    the standalone lifetime lock. Those need a real subprocess.
    """
    from frisket import cli as frisket_cli

    argv_before = sys.argv[:]
    stdin_before = sys.stdin
    environ_before = dict(os.environ)
    cwd_before = os.getcwd()
    root_logger = logging.getLogger()
    handlers_before = root_logger.handlers[:]
    level_before = root_logger.level

    sys.argv = ["frisket", *args]
    if input_data is not None:
        sys.stdin = _StdinShim(input_data)  # type: ignore[assignment]

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    returncode = 0
    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            try:
                frisket_cli.main()
            except SystemExit as exc:
                code = exc.code
                if code is None:
                    returncode = 0
                elif isinstance(code, int):
                    returncode = code
                else:
                    returncode = 1
    finally:
        sys.argv = argv_before
        sys.stdin = stdin_before
        os.environ.clear()
        os.environ.update(environ_before)
        os.chdir(cwd_before)
        root_logger.handlers = handlers_before
        root_logger.setLevel(level_before)

    return CliResult(
        returncode=returncode,
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
    )
