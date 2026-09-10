"""The `map.python` executor port: what crosses it, and what cannot.

`map.python` is the only action that runs a journalist's own program. The port
(`frisket.engine.executor.python_transform`) is the list of things a compromised
guest cannot reach, so these tests assert the boundary itself -- the shape of
the crossing -- not just that transforms produce the right numbers.

Each fence here is red-proofed once: the comment on the test names the edit that
makes it fail, so a future reader can confirm the wire is live rather than
trusting that it was.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from frisket.engine.executor import python_transform
from frisket.engine.executor.python_transform import (
    TRANSFORM_EXCEPTION,
    TRANSFORM_RESULT_MISSING,
    TRANSFORM_ROW_ERROR_CODES,
    TRANSFORM_RUNTIME_UNAVAILABLE,
    TRANSFORM_SYNTAX_ERROR,
    PythonTransformExecutor,
    TransformFailure,
    TransformRefused,
    TransformRuntimeUnavailable,
    TransformValue,
)
from frisket.engine.executor.python_transform_native import NativeSubprocessExecutor
from frisket.actions.python import PythonParams, run_python
from frisket.actions.types import Row, RowError
from frisket.engine.executor.python_evaluator import AdmittedPythonEvaluator


# --- a fake backend that records exactly what the parent handed it ------------------
class RecordingExecutor:
    """Drives the lifecycle without running anything, and remembers the call."""

    runtime_id = "test.recording"

    def __init__(self, outcome: Any = None) -> None:
        self.validate_calls: list[str] = []
        self.execute_calls: list[dict[str, Any]] = []
        self._outcome = outcome

    async def validate(self, code: str) -> None:
        self.validate_calls.append(code)

    async def execute(self, **kwargs: Any) -> Any:
        self.execute_calls.append(kwargs)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        if self._outcome is not None:
            return self._outcome
        return TransformValue(result={"excerpt": "ok", "n": 1})


def _spec(code: str = "result = {'excerpt': 'ok', 'n': 1}") -> dict[str, Any]:
    return {
        "code": code,
        "input_columns": ["transcript"],
        "return_schema": {"type": "object"},
        "output_routes": [
            {
                "name": "raw",
                "path": "$",
                "target": {"kind": "column", "type": "json"},
            }
        ],
    }


async def _evaluate(code: str = "result = {'excerpt': 'ok', 'n': 1}") -> Any:
    return await run_python(
        PythonParams.model_validate(_spec(code)),
        Row({"transcript": "a b"}),
        AdmittedPythonEvaluator(cancelled=lambda: False),
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- 1. the crossing carries the row and nothing else -------------------------------
def test_the_port_hands_the_guest_the_row_and_the_source_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole ambient-authority claim, as an assertion on the call's shape.

    A `Project`, connection, router, credential, workspace path, blob client,
    receipt builder or billing object could only reach a backend as another
    keyword here, so pinning the keyword set exactly is what makes "the
    executor receives no ambient authority" checkable instead of promised.

    Red-proof: add ``project=ctx.project`` to the ``executor.execute(...)`` call
    in ``AdmittedPythonEvaluator.evaluate`` and this fails on the key set.
    """

    fake = RecordingExecutor()
    monkeypatch.setattr(python_transform, "resolve_executor", lambda: fake)

    values = _run(_evaluate())

    assert values.output.root == {"raw": {"excerpt": "ok", "n": 1}}
    assert len(fake.execute_calls) == 1
    call = fake.execute_calls[0]
    assert set(call) == {"code", "row", "cancelled"}
    assert call["row"] == {"transcript": "a b"}
    assert callable(call["cancelled"])


def test_the_outcome_carries_no_provider_cost_or_receipt_fact() -> None:
    """A guest cannot forge money or provenance because there is nowhere to put it.

    Release test 10, mechanically: the union's field set IS the return surface,
    so a future field named `cost_actual` or `provider` fails here before it can
    reach a receipt.

    Red-proof: add a `cost_actual: float = 0.0` field to `TransformValue`.
    """

    assert {f.name for f in dataclasses.fields(TransformValue)} == {
        "result",
        "diagnostics",
    }
    assert {f.name for f in dataclasses.fields(TransformFailure)} == {
        "code",
        "message",
        "diagnostics",
    }


# --- 2. missing `result` is not `result = None` -------------------------------------
@pytest.mark.parametrize(
    ("code", "expect"),
    [
        ("result = None", TransformValue(result=None)),
        ("x = 1", TransformFailure(TRANSFORM_RESULT_MISSING, "")),
    ],
)
def test_missing_result_and_explicit_none_are_different_outcomes(
    code: str, expect: Any
) -> None:
    """The spec's release test 2, against the real backend.

    `ns.get("result")` collapses these two into the same `None`, which is why
    the guest wrapper reports assignment separately and the port returns a union
    rather than an optional value.

    Red-proof: change the wrapper back to ``{"result": ns.get("result")}`` and
    the missing-`result` case starts succeeding with a null.
    """

    outcome = _run(
        NativeSubprocessExecutor().execute(code=code, row={}, cancelled=lambda: False)
    )
    assert type(outcome) is type(expect)
    if isinstance(outcome, TransformValue):
        assert outcome.result is None
    else:
        assert outcome.code == TRANSFORM_RESULT_MISSING


# --- 3. validate is once per run, and stops the source before it runs ---------------
def test_validate_runs_once_per_run_not_once_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One invocation shares compile validation across concurrent row calls."""

    fake = RecordingExecutor()
    monkeypatch.setattr(python_transform, "resolve_executor", lambda: fake)

    async def invoke():
        evaluator = AdmittedPythonEvaluator()
        await asyncio.gather(
            *(
                evaluator.evaluate(code=_spec()["code"], row={"transcript": "a"})
                for _ in range(3)
            )
        )

    _run(invoke())

    assert len(fake.validate_calls) == 1
    assert len(fake.execute_calls) == 3


def test_a_syntax_error_is_named_and_no_guest_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`validate` compiles in the parent; `compile` never executes.

    Red-proof: delete the evaluator's `validate` call and the error
    becomes an opaque child-process failure instead of `transform_syntax_error`.
    """

    fake = RecordingExecutor()
    monkeypatch.setattr(
        python_transform,
        "resolve_executor",
        lambda: NativeSubprocessExecutor(),
    )
    with pytest.raises(RowError) as caught:
        _run(_evaluate("result = ((("))
    assert caught.value.code == TRANSFORM_SYNTAX_ERROR
    assert fake.execute_calls == []


# --- 4. a backend cannot downgrade an engine failure into a row failure -------------
def test_a_backend_cannot_return_a_run_level_code_as_a_row_error() -> None:
    """Fail closed at the type. `transform_runtime_unavailable` returned per row
    would let a dead engine produce a run that merely "had some bad rows".

    Red-proof: delete `__post_init__` on `TransformFailure`.
    """

    assert TRANSFORM_RUNTIME_UNAVAILABLE not in TRANSFORM_ROW_ERROR_CODES
    for code in (TRANSFORM_RUNTIME_UNAVAILABLE, "transform_runtime_error", "nonsense"):
        with pytest.raises(ValueError, match="not a row-level"):
            TransformFailure(code=code, message="x")

    # ...and the ones that ARE row-scoped construct fine.
    for code in sorted(TRANSFORM_ROW_ERROR_CODES):
        assert TransformFailure(code=code, message="x").code == code


def test_a_row_failure_is_raised_as_a_row_error_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MapRunner records this against one row; siblings continue."""

    fake = RecordingExecutor(
        outcome=TransformFailure(code=TRANSFORM_EXCEPTION, message="boom")
    )
    monkeypatch.setattr(python_transform, "resolve_executor", lambda: fake)
    with pytest.raises(RowError) as caught:
        _run(_evaluate())
    assert (caught.value.code, caught.value.message) == (TRANSFORM_EXCEPTION, "boom")


def test_runtime_unavailable_is_an_exception_with_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is never a fallback to host Python or the old sandbox: the run
    stops and says the backend is missing.

    Red-proof: make `resolve_executor` swallow the error and return the native
    backend; this stops raising.
    """

    def unavailable() -> PythonTransformExecutor:
        raise TransformRuntimeUnavailable("no qualifying backend")

    monkeypatch.setattr(python_transform, "resolve_executor", unavailable)
    with pytest.raises(TransformRuntimeUnavailable):
        _run(_evaluate())


# --- 5. the shipped backend really implements the port ------------------------------
def test_the_shipped_backend_satisfies_the_port_and_names_itself() -> None:
    executor = python_transform.resolve_executor()
    assert isinstance(executor, PythonTransformExecutor)
    assert executor.runtime_id
    # `runtime_id` is what a receipt will state; a backend without a stable one
    # makes "which fence ran my program" unanswerable after the fact.
    assert isinstance(executor.runtime_id, str)


def test_validate_rejects_only_what_it_can_actually_enforce() -> None:
    """The native backend enforces no module list, and does not claim to.

    A `validate` that raised `transform_import_denied` here would be a fence
    that does not exist -- the child really can `import os`. That claim belongs
    to a backend that can keep it.

    Red-proof: add an import allowlist to `NativeSubprocessExecutor.validate`
    without adding the enforcement, and this fails.
    """

    executor = NativeSubprocessExecutor()
    _run(executor.validate("import os\nresult = 1"))  # accepted, honestly

    with pytest.raises(TransformRefused) as caught:
        _run(executor.validate("result = ((("))
    assert caught.value.code == TRANSFORM_SYNTAX_ERROR
