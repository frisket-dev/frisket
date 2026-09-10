"""The pre-WASM `map.python` backend: one fenced `python -I -c` child per row.

This is today's behavior, moved behind :mod:`frisket.engine.executor.python_transform`
without changing it. It is a *native* backend: the guest is real CPython running
on this kernel, confined by the seccomp/Landlock fence in
`frisket.engine.sandbox`, which is why the action still declares
`unsafe:local_code` and why hosted still refuses it.

What this backend does NOT satisfy, stated plainly so nobody reads the port's
contract onto it:

* the guest is native code against our kernel, so a kernel LPE escapes it;
* `cancelled` is accepted and honoured only *before* the child is spawned --
  once a row's child is running, the wall-clock limit, not cancellation, is what
  stops it. Row granularity is the cancellation granularity, as today; and
* the module list is not enforced here at all. The fence, not an import
  allowlist, is this backend's boundary.

The WASM backend is what makes the port's contract true rather than aspirational.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue

from frisket.engine.executor.python_transform import (
    TRANSFORM_EXCEPTION,
    TRANSFORM_RESULT_MISSING,
    TRANSFORM_SYNTAX_ERROR,
    TransformFailure,
    TransformOutcome,
    TransformRefused,
    TransformValue,
)

__all__ = ["MAP_PYTHON_WRAPPER", "NativeSubprocessExecutor"]


#: The guest program. It reports whether `result` was ASSIGNED, separately from
#: its value: `result = None` and never assigning `result` are different
#: outcomes (the spec's release test 2), and `ns.get("result")` collapses them
#: into the same `None`. `allow_nan=False` keeps NaN/Infinity out of what the
#: parent will call JSON.
MAP_PYTHON_WRAPPER = """
import json, sys

payload = json.load(sys.stdin)
row = payload["row"]
USER_CODE = payload["code"]
ns = {"row": row}
exec(USER_CODE, ns)
print(json.dumps(
    {"assigned": "result" in ns, "result": ns.get("result")}, allow_nan=False
))
"""


@dataclass(frozen=True)
class NativeSubprocessExecutor:
    """`PythonTransformExecutor` over the existing sandbox shim."""

    runtime_id: str = "native.subprocess"

    async def validate(self, code: str) -> None:
        """Compile-check the source in the parent. `compile` never executes it.

        There is no import check: this backend does not enforce a module list,
        and claiming one here would be a fence that does not exist.
        """

        try:
            compile(code, "<map.python>", "exec")
        except SyntaxError as exc:
            raise TransformRefused(
                TRANSFORM_SYNTAX_ERROR,
                f"line {exc.lineno}: {exc.msg}",
            ) from exc

    async def execute(
        self,
        *,
        code: str,
        row: dict[str, JsonValue],
        cancelled: Callable[[], bool],
    ) -> TransformOutcome:
        from frisket.engine.sandbox.shim import SandboxPolicy, run_python_op

        # `cancelled` is accepted and unused: this backend cannot interrupt a
        # child mid-row, and MapRunner already declines to dispatch further
        # rows once the run is cancelled, so a check here would add a second
        # answer to a question the run already answers. The WASM backend, which
        # can actually stop a running instance, is the consumer.
        del cancelled

        try:
            out = await run_python_op(
                MAP_PYTHON_WRAPPER,
                {"row": row, "code": code},
                policy=SandboxPolicy(cpu_seconds=10, wall_seconds=30, memory_mb=512),
            )
        except RuntimeError as exc:
            # The child died or printed non-JSON: a user exception, a limit, or
            # a wrapper crash. The native shim does not distinguish them, so we
            # do not pretend to. Message text is the child's stderr tail, which
            # the shim has already truncated.
            return TransformFailure(code=TRANSFORM_EXCEPTION, message=str(exc))

        if not out.get("assigned"):
            return TransformFailure(
                code=TRANSFORM_RESULT_MISSING,
                message="the program never assigned `result`",
            )
        return TransformValue(result=out.get("result"))
