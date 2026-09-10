from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
from typing import Any

import pytest

from frisket.ai.llm import (
    ChaosConfig,
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.ops.base import Recipe
from typed_model_fixtures import model_plan
from frisket.engine.runner import MapRunner, validation
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import run_with_exact_confirmation, run_with_output_claim

MODEL = "anthropic/claude-haiku-4-5"


def _classify_spec(sheet_id: int) -> dict[str, Any]:
    return {
        "action_kind": "map.classify",
        "model": MODEL,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "Test rows.",
        "fields": [
            {
                "name": "relevance",
                "type": "score",
                "description": "0-10 relevance to topic",
            }
        ],
    }


def _seed_text_sheet(project: Project, texts: list[str]) -> tuple[int, dict]:
    sheet = project.add_sheet("data")
    cols = {"text": project.add_column(sheet, "text")}
    project.add_rows(sheet, [{"text": t} for t in texts], cols)
    return sheet, cols


class _SlowFailingRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"

    def __init__(self, *, delay: float = 0.02):
        super().__init__(name="test.slow_all_rows_failed", llm=False)
        self.delay = delay
        self.calls = 0

    def source_columns(self, spec: dict) -> list[str]:
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict]:
        return [
            {
                "name": "relevance",
                "column_type": "number",
                "schema": {"type": "number"},
            }
        ]

    async def execute(self, row_values, spec, ctx):  # noqa: ANN001
        del row_values, spec, ctx
        self.calls += 1
        await asyncio.sleep(self.delay)
        raise RuntimeError("deterministic row failure")


def _prepare_claimed_run(
    runner: MapRunner,
    spec: dict[str, Any],
):
    claim_token = f"output-claim:test:{id(runner)}"
    claims, conflict = OutputColumnClaimStore(runner.project).acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=["relevance"],
        action_kind=str(spec["action_kind"]),
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None
    assert len(claims) == 1
    prepared = runner._prepare(  # noqa: SLF001 - runner contract probe
        spec,
        confirmed=False,
        resume_run_id=None,
    )
    assert (
        OutputColumnClaimStore(runner.project).bind_to_run(
            claim_token=claim_token,
            run_id=prepared.run_id,
            expected_output_names=["relevance"],
        )
        == 1
    )
    return prepared, claim_token


async def _wait_for_calls_or_task_exit(
    task: asyncio.Task,
    recipe: _SlowFailingRecipe,
    *,
    expected_calls: int,
    timeout: float = 2.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while recipe.calls < expected_calls:
        remaining = deadline - loop.time()
        assert remaining > 0, (
            f"runner did not start {expected_calls} calls within {timeout}s"
        )
        done, _pending = await asyncio.wait(
            {task},
            timeout=min(0.01, remaining),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task


def _prime_cache(
    cache: ResponseCache,
    spec: dict,
    row_texts: list[str],
    score: int = 7,
) -> None:
    """Pre-compute the exact requests the runner will make for these rows and
    cache real-shaped responses — cache lookups happen BEFORE chaos
    interception (tests/test_runner.py's determinism trick), so priming a
    subset lets the rest chaos-fail deterministically."""
    recipe = model_plan(spec).program
    for text in row_texts:
        call = recipe.render({"text": text}, spec)
        req = LLMRequest(
            model=MODEL,
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        cache.put(
            request_key(req, recipe.version),
            LLMResponse(
                content=None,
                data={"relevance": score},
                tokens_in=50,
                tokens_out=10,
                cost=0.0001,
                model=MODEL,
            ),
        )


class TestMapKindAllRowsFailed:
    def test_all_rows_failed_creates_no_visible_column(self, tmp_path):
        project = Project.create(tmp_path / "map-all-failed.frisket")
        try:
            texts = [f"row {i}" for i in range(6)]
            sheet, _ = _seed_text_sheet(project, texts)
            spec = _classify_spec(sheet)
            router = ModelRouter(
                keys={"anthropic": "k"},
                cache=ResponseCache(tmp_path / "cache.db"),
                cache_mode="replay",
                chaos=ChaosConfig(seed=1, enabled=True, fail_rate=1.0),
                max_retries=0,
            )
            runner = MapRunner(
                project, router, authority=UnroutedOnlyAuthority(project)
            )
            progress = asyncio.run(run_with_exact_confirmation(runner, spec))

            assert progress.done
            assert progress.completed == 6
            assert progress.failed == 6

            run = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (progress.run_id,)
            ).fetchone()
            assert run["status"] == "completed"
            assert run["total_rows"] == 6
            assert run["failed_rows"] == 6

            visible_names = [c["name"] for c in project.columns(sheet)]
            assert "relevance" not in visible_names

            # the column row still exists (hidden), still pointed at the run
            # -- receipt/replay machinery that keys off current_run_id is
            # unaffected by this fix, only the sheet's visible column list.
            col = project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=? AND name=?",
                (sheet, "relevance"),
            ).fetchone()
            assert col is not None
            assert col["hidden"] == 1
            assert col["current_run_id"] == progress.run_id

            # undo of the zero-success op is a coherent no-op: the column was
            # already hidden by finalize, not by undo, and undo must not
            # error trying to "remove" something already gone.
            undone_op_id = project.undo()
            assert undone_op_id is not None
            assert "relevance" not in [c["name"] for c in project.columns(sheet)]

            redone_op_id = project.redo()
            assert redone_op_id == undone_op_id
            assert "relevance" not in [c["name"] for c in project.columns(sheet)]
        finally:
            project.close()

    def test_partial_success_keeps_the_column_visible(self, tmp_path):
        """PARTIAL success (even 1/N) keeps the column — explicit manifest
        constraint, distinct from the all-failed case above."""
        project = Project.create(tmp_path / "map-partial.frisket")
        try:
            texts = [f"row {i}" for i in range(11)]
            sheet, _ = _seed_text_sheet(project, texts)
            spec = _classify_spec(sheet)
            cache = ResponseCache(tmp_path / "cache.db")
            _prime_cache(cache, spec, texts[:1])  # exactly 1 of 11 succeeds
            router = ModelRouter(
                keys={"anthropic": "k"},
                cache=cache,
                cache_mode="replay",
                chaos=ChaosConfig(seed=2, enabled=True, fail_rate=1.0),
                max_retries=0,
            )
            runner = MapRunner(
                project, router, authority=UnroutedOnlyAuthority(project)
            )
            progress = asyncio.run(run_with_exact_confirmation(runner, spec))

            assert progress.done
            assert progress.completed == 11
            assert progress.failed == 10

            visible_names = [c["name"] for c in project.columns(sheet)]
            assert "relevance" in visible_names
            col = next(c for c in project.columns(sheet) if c["name"] == "relevance")
            assert col["hidden"] == 0
            assert col["current_run_id"] == progress.run_id
        finally:
            project.close()

    def test_fresh_backfill_generation_that_succeeds_reveals_the_column(self, tmp_path):
        """A fresh explicitly scoped generation can replace an all-error run."""
        project = Project.create(tmp_path / "map-recovered.frisket")
        try:
            texts = [f"row {i}" for i in range(3)]
            sheet, _ = _seed_text_sheet(project, texts)
            spec = _classify_spec(sheet)
            cache = ResponseCache(tmp_path / "cache.db")
            router = ModelRouter(
                keys={"anthropic": "k"},
                cache=cache,
                cache_mode="replay",
                chaos=ChaosConfig(seed=5, enabled=True, fail_rate=1.0),
                max_retries=0,
            )
            runner = MapRunner(
                project, router, authority=UnroutedOnlyAuthority(project)
            )
            first = asyncio.run(run_with_exact_confirmation(runner, spec))
            assert first.completed == first.failed == 3
            assert "relevance" not in [c["name"] for c in project.columns(sheet)]
            hidden_col = next(
                c
                for c in project.columns(sheet, include_hidden=True)
                if c["name"] == "relevance"
            )
            assert hidden_col["hidden"] == 1

            # The underlying problem is fixed. Backfill creates a fresh run
            # over the failed scope; it never reopens the sealed first run.
            _prime_cache(cache, spec, texts)
            router2 = ModelRouter(
                keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
            )
            runner2 = MapRunner(
                project, router2, authority=UnroutedOnlyAuthority(project)
            )
            recovery_spec = {
                **spec,
                "row_ids": [
                    int(row[0])
                    for row in project.db.execute(
                        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                        (sheet,),
                    ).fetchall()
                ],
                "overwrite": True,
            }
            second = asyncio.run(run_with_exact_confirmation(runner2, recovery_spec))
            assert second.done
            assert second.failed == 0
            assert second.run_id != first.run_id

            visible = next(
                c for c in project.columns(sheet) if c["name"] == "relevance"
            )
            assert visible["hidden"] == 0
            assert visible["current_run_id"] == first.run_id
        finally:
            project.close()

    def test_cancelled_incomplete_run_uses_a_fresh_recovery_generation(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Cancelled terminal publication is sealed; retry gets a new run."""
        project = Project.create(tmp_path / "map-resumed.frisket")
        try:
            texts = [f"row {i}" for i in range(6)]
            sheet, _ = _seed_text_sheet(project, texts)
            recipe = _SlowFailingRecipe()
            original_get_recipe = validation.get_recipe
            monkeypatch.setattr(
                validation,
                "get_recipe",
                lambda name: (
                    recipe if name == recipe.name else original_get_recipe(name)
                ),
            )
            spec = {
                "action_kind": recipe.name,
                "sheet_id": sheet,
                "input_columns": ["text"],
                "output_name": "relevance",
            }
            router = ModelRouter(keys={}, cache=None, cache_mode="off")
            runner = MapRunner(
                project, router, concurrency=1, authority=UnroutedOnlyAuthority(project)
            )
            prepared, claim_token = _prepare_claimed_run(runner, spec)

            async def go_cancel():
                seen: dict = {}
                runner.on_progress = lambda p: seen.setdefault("prog", p)
                task = asyncio.create_task(
                    runner.run(
                        spec,
                        prepared_run=prepared,
                        claim_token=claim_token,
                    )
                )
                await _wait_for_calls_or_task_exit(
                    task,
                    recipe,
                    expected_calls=2,
                )
                seen["prog"].cancel()
                return await asyncio.wait_for(task, timeout=2)

            first = asyncio.run(go_cancel())
            OutputColumnClaimStore(project).release(
                claim_token=claim_token, status="cancelled"
            )
            assert first.cancelled and first.done
            assert 0 < first.completed < first.total
            assert first.failed == first.completed  # every processed row failed

            run = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (first.run_id,)
            ).fetchone()
            assert run["status"] == "cancelled"
            assert run["failed_rows"] < run["total_rows"]

            # The partial terminal result remains visible, but it is sealed.
            assert "relevance" in [c["name"] for c in project.columns(sheet)]

            recipe.delay = 0.0
            router2 = ModelRouter(keys={}, cache=None, cache_mode="off")
            runner2 = MapRunner(
                project, router2, authority=UnroutedOnlyAuthority(project)
            )
            recovery_spec = {
                **spec,
                "row_ids": [
                    int(row[0])
                    for row in project.db.execute(
                        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                        (sheet,),
                    ).fetchall()
                ],
                "overwrite": True,
            }
            second = asyncio.run(
                asyncio.wait_for(
                    run_with_output_claim(runner2, recovery_spec, confirmed=True),
                    timeout=2,
                )
            )
            assert second.done
            assert second.run_id != first.run_id

            run_after = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (second.run_id,)
            ).fetchone()
            assert run_after["failed_rows"] == run_after["total_rows"] == 6

            # A later op cannot retroactively roll back the first op's column.
            assert "relevance" in [c["name"] for c in project.columns(sheet)]
        finally:
            project.close()


class TestMediaKindAllRowsFailed:
    def _seed_media_project(self, tmp_path) -> tuple[Project, int, list[int]]:
        project = Project.create(tmp_path / "media-all-failed.frisket")
        sheet_id = project.add_sheet("Episodes")
        cols = {"media": project.add_column(sheet_id, "media", type="audio")}
        blobs = [
            project.add_blob(
                b"RIFF0000WAVEfmt " + label.encode("ascii"),
                filename=f"{label}.wav",
                mime="audio/wav",
                source_url=f"https://cdn.example/{label}.wav",
                metadata=owned_media_metadata_document(
                    probe={"duration_seconds": 0.5, "kind": "audio"}
                ),
            )
            for label in ("ep1", "ep2")
        ]
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "media": media_cell(
                        blobs[0],
                        mime="audio/wav",
                        filename="ep1.wav",
                    )
                },
                {
                    "media": media_cell(
                        blobs[1],
                        mime="audio/wav",
                        filename="ep2.wav",
                    )
                },
            ],
            cols,
        )
        return project, sheet_id, row_ids

    def test_transcribe_all_rows_failed_creates_no_visible_columns(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        from frisket.engine.executor import run_action_spec

        project, sheet_id, row_ids = self._seed_media_project(tmp_path)
        try:

            async def fail_faster_whisper(
                self: transcribe_engines.FasterWhisperAdapter,
                path: str,
                spec: dict[str, Any],
                *,
                should_cancel: Any = None,
            ) -> dict[str, Any]:
                del self, path, spec
                raise RuntimeError("ASR provider unavailable")

            monkeypatch.setattr(
                transcribe_engines.FasterWhisperAdapter,
                "transcribe",
                fail_faster_whisper,
            )

            action = {
                "action_id": "media.transcribe",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids,
                },
                "output_names": {
                    "text": "transcript",
                    "segments": "transcript_segments",
                },
                "params": {
                    "source": "media",
                    "engine": "faster_whisper",
                },
                "idempotency_key": "media_transcribe@sha256:all-failed",
            }
            result = run_action_spec(
                project,
                action,
                project_id="project-media-all-failed",
            )

            # the existing per-row-failure contract is unchanged: a receipt
            # still records the failure (tests/test_media_transcribe_
            # executor.py's single-row-failure case asserts the same shape).
            assert result.status == "failed"
            assert result.receipt_id is not None
            assert result.errors[0].code == "external_rows_failed"

            visible_names = [c["name"] for c in project.columns(sheet_id)]
            assert "transcript" not in visible_names
            assert "transcript_segments" not in visible_names
            assert "detected_language" not in visible_names

            for name in ("transcript", "transcript_segments", "detected_language"):
                col = project.db.execute(
                    "SELECT * FROM columns WHERE sheet_id=? AND name=?",
                    (sheet_id, name),
                ).fetchone()
                assert col is not None, name
                assert col["hidden"] == 1, name
        finally:
            project.close()

    def test_transcribe_partial_success_keeps_columns_visible(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        from frisket.engine.executor import run_action_spec

        project, sheet_id, row_ids = self._seed_media_project(tmp_path)
        try:
            calls = {"n": 0}

            async def flaky_faster_whisper(
                self: transcribe_engines.FasterWhisperAdapter,
                path: str,
                spec: dict[str, Any],
                *,
                should_cancel: Any = None,
            ) -> dict[str, Any]:
                del self, spec
                calls["n"] += 1
                if calls["n"] == 1:
                    return {
                        "text": "hello world",
                        "segments": [],
                        "language": "en",
                    }
                raise RuntimeError("ASR provider unavailable")

            monkeypatch.setattr(
                transcribe_engines.FasterWhisperAdapter,
                "transcribe",
                flaky_faster_whisper,
            )

            action = {
                "action_id": "media.transcribe",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids,
                },
                "output_names": {
                    "text": "transcript",
                    "segments": "transcript_segments",
                },
                "params": {
                    "source": "media",
                    "engine": "faster_whisper",
                },
                "idempotency_key": "media_transcribe@sha256:partial",
            }
            result = run_action_spec(
                project,
                action,
                project_id="project-media-partial",
            )

            assert result.status == "partial"
            assert result.receipt_id is not None

            visible_names = [c["name"] for c in project.columns(sheet_id)]
            assert "transcript" in visible_names
        finally:
            project.close()
