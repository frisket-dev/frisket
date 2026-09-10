"""The `map.python` executor port: the whole surface user code is allowed.

`map.python` is the one action that runs a journalist's own program. Everything
else in the executor decides *what* to run; this module decides *what the thing
running it is allowed to touch*, and the answer is: one JSON row in, one JSON
value out.

The port is deliberately anaemic. An implementation of
`PythonTransformExecutor` receives no `Project`, no database connection, no
provider router, no credentials, no workspace path, no blob client, no receipt
builder and no billing object; it returns no provider, cost, receipt, route or
provenance facts. Everything on that list stays in the trusted parent
(`AdmittedPythonEvaluator` and the typed `MapRunner` lifecycle above it), which
keeps input selection, result and size validation, `return_schema` validation,
route extraction, target-column validation, project writes and receipts.

That is not a style preference. Guest output that could name a provider or a
cost would be guest-forged money data; a guest that held a `Project` would be
an escape with no engine bug required. The port is the list of things an engine
compromise cannot reach, so it is stated as a type rather than a convention.

There is exactly ONE production mint site for the backend --
:func:`resolve_executor` -- because "which boundary ran this row" is a fact a
receipt states, and two mint sites are two answers. Callers never construct a
backend themselves; tests replace `resolve_executor` (module attribute, looked
up late) rather than threading an optional parameter through the recipe, which
is the optional-wiring defect this codebase keeps paying for.

Outcome shape
-------------
`execute` returns a union, never a value plus a sentinel:

* :class:`TransformValue` -- user code assigned `result`; the value is JSON.
  ``TransformValue(result=None)`` is the honest representation of
  ``result = None``.
* :class:`TransformFailure` -- a typed row error, `result` was never produced.

`None` is a legal result, so a nullable return type would make "assigned null"
and "never assigned" the same object. The spec requires those to differ
(`transform_result_missing`), and a union is what makes them un-confusable at
the type level instead of by everyone remembering to check a second flag.

Failures that stop the whole run (rather than one row) are exceptions, not
outcomes: :class:`TransformRefused` from `validate`, and
:class:`TransformRuntimeUnavailable` when no backend is present or its startup
self-test failed. There is never a fallback to some other backend -- a
degradation the caller cannot see is the exact failure this line of work
exists to correct.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

__all__ = [
    "TRANSFORM_EXCEPTION",
    "TRANSFORM_IMPORT_DENIED",
    "TRANSFORM_LIMIT_EXCEEDED",
    "TRANSFORM_RESULT_MISSING",
    "TRANSFORM_ROW_ERROR_CODES",
    "TRANSFORM_RUNTIME_ERROR",
    "TRANSFORM_RUNTIME_UNAVAILABLE",
    "TRANSFORM_SYNTAX_ERROR",
    "PythonTransformExecutor",
    "TransformFailure",
    "TransformOutcome",
    "TransformRefused",
    "TransformRuntimeUnavailable",
    "TransformValue",
    "resolve_executor",
]


# --- error vocabulary ---------------------------------------------------------------
#
# The spec's table, as constants, so a backend cannot invent a code the parent
# does not classify. Row codes fail one row and let siblings continue; the two
# run codes stop before fan-out or halt the run resumably.

#: backend absent or failed its startup self-test; no rows run.
TRANSFORM_RUNTIME_UNAVAILABLE = "transform_runtime_unavailable"
#: source does not compile; no rows run.
TRANSFORM_SYNTAX_ERROR = "transform_syntax_error"
#: source asks for a module outside the allowed list; no rows run.
TRANSFORM_IMPORT_DENIED = "transform_import_denied"
#: user code raised for this row.
TRANSFORM_EXCEPTION = "transform_exception"
#: code ran to completion but never assigned `result`.
TRANSFORM_RESULT_MISSING = "transform_result_missing"
#: time, memory, input, or output bound.
TRANSFORM_LIMIT_EXCEEDED = "transform_limit_exceeded"
#: engine/cancellation teardown failed; halt the run resumably.
TRANSFORM_RUNTIME_ERROR = "transform_runtime_error"

#: The codes a backend may return from `execute`. Everything else is a run-level
#: failure and must be raised, not returned -- so a backend cannot downgrade
#: "the engine is broken" into "this one row is bad" and let the run look fine.
TRANSFORM_ROW_ERROR_CODES = frozenset(
    {
        TRANSFORM_EXCEPTION,
        TRANSFORM_RESULT_MISSING,
        TRANSFORM_LIMIT_EXCEEDED,
        TRANSFORM_IMPORT_DENIED,
    }
)


# --- outcomes -----------------------------------------------------------------------
@dataclass(frozen=True)
class TransformValue:
    """User code assigned `result`. `result` is a JSON value (possibly null)."""

    result: JsonValue
    #: Captured `print()` output, already truncated by the backend. Debugging
    #: aid only: never the result, never a receipt field, never an ordinary log
    #: line -- it is user data from a user's private row.
    diagnostics: str = ""


@dataclass(frozen=True)
class TransformFailure:
    """One row failed. Siblings continue."""

    code: str
    message: str
    diagnostics: str = ""

    def __post_init__(self) -> None:
        if self.code not in TRANSFORM_ROW_ERROR_CODES:
            raise ValueError(
                f"{self.code!r} is not a row-level transform error; run-level "
                "failures are raised (TransformRefused / "
                "TransformRuntimeUnavailable), not returned"
            )


TransformOutcome = TransformValue | TransformFailure


# --- run-level failures -------------------------------------------------------------
class TransformRefused(Exception):
    """`validate` rejected the source. No rows run."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TransformRuntimeUnavailable(Exception):
    """No backend, or its startup self-test failed. No rows run, no fallback."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = TRANSFORM_RUNTIME_UNAVAILABLE
        self.message = message


# --- the port -----------------------------------------------------------------------
@runtime_checkable
class PythonTransformExecutor(Protocol):
    """Run one user program against one row. Nothing else is in scope.

    Implementations are stateless with respect to a run: `execute` is called
    concurrently for sibling rows and every row must start from a clean guest,
    so no globals, imports, scratch data or diagnostics from row A can be
    observed by row B.
    """

    @property
    def runtime_id(self) -> str:
        """Stable identity of the boundary that produced a row, e.g.
        ``"native.subprocess"``. A receipt states this beside the code hash so
        "which fence ran my program" is answerable after the fact."""
        ...

    async def validate(self, code: str) -> None:
        """Reject source that cannot run, once, before row fan-out.

        Raises :class:`TransformRefused` with `transform_syntax_error` or
        `transform_import_denied`. Must not execute any user code.
        """
        ...

    async def execute(
        self,
        *,
        code: str,
        row: dict[str, JsonValue],
        cancelled: Callable[[], bool],
    ) -> TransformOutcome:
        """Run `code` against `row` and return its `result`.

        `row` is passed by value; mutating it must not be observable outside
        the guest. `cancelled` is a cooperative stop signal owned by the run.
        """
        ...


# --- the one mint site --------------------------------------------------------------
def resolve_executor() -> PythonTransformExecutor:
    """The backend `map.python` runs on. One site, no fallback chain.

    Raises :class:`TransformRuntimeUnavailable` when there is no qualifying
    backend, rather than quietly selecting a weaker one.
    """

    from frisket.engine.executor.python_transform_native import (
        NativeSubprocessExecutor,
    )

    return NativeSubprocessExecutor()
