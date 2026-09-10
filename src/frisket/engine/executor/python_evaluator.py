"""Invocation-owned evaluator over the existing confined subprocess port."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable

from pydantic import JsonValue, TypeAdapter

from frisket.actions.types import RowError
from frisket.engine.executor import python_transform
from frisket.engine.executor.python_transform import TransformFailure, TransformRefused


class AdmittedPythonEvaluator:
    def __init__(
        self, *, cancelled=None, record_code_hash: Callable[[str], None] | None = None
    ):
        self._executor = python_transform.resolve_executor()
        self._cancelled = cancelled or (lambda: False)
        self._validated: set[str] = set()
        self._lock = asyncio.Lock()
        self._record_code_hash = record_code_hash
        self._observed_hashes: set[str] = set()

    async def evaluate(self, *, code: str, row: dict[str, JsonValue]) -> JsonValue:
        # Own an actual JSON copy: no host objects or shared mutable row data cross.
        adapter = TypeAdapter(dict[str, JsonValue])
        values = adapter.validate_json(adapter.dump_json(adapter.validate_python(row)))
        code_hash = "sha256:" + hashlib.sha256(code.encode()).hexdigest()
        if code_hash not in self._observed_hashes:
            if self._record_code_hash is not None:
                self._record_code_hash(code_hash)
            self._observed_hashes.add(code_hash)
        async with self._lock:
            if code not in self._validated:
                try:
                    await self._executor.validate(code)
                except TransformRefused as error:
                    raise RowError(error.code, error.message) from error
                except SyntaxError as error:
                    raise RowError("transform_syntax_error", str(error)) from error
                self._validated.add(code)
        outcome = await self._executor.execute(
            code=code, row=values, cancelled=self._cancelled
        )
        if isinstance(outcome, TransformFailure):
            raise RowError(outcome.code, outcome.message)
        return outcome.result

    async def aclose(self) -> None:
        pass
