"""Small, bounded observations of the tools invoked by one research row."""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any


class ResearchRecorder:
    """Diagnostic space never limits execution, and failures never escape."""

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        self._truncated = False

    def start(self, tool: str, **fields: str) -> tuple[int, float] | None:
        try:
            if len(self._steps) >= 24:
                self._truncated = True
                return None
            step: dict[str, Any] = {
                "tool": tool,
                "status": "started",
                "truncated": False,
            }
            if tool == "search":
                step.update(provider=None, service=None, results=[])
            else:
                step["text"] = ""
            for key, value in fields.items():
                step[key] = self._clip(step, value, 500 if key == "query" else 2000)
            self._steps.append(step)
            return len(self._steps) - 1, time.monotonic()
        except Exception:
            self._truncated = True
            return None

    @staticmethod
    def _clip(step: dict[str, Any], value: str, limit: int) -> str:
        if len(value) > limit:
            step["truncated"] = True
        return value[:limit]

    def finish(
        self,
        token: tuple[int, float] | None,
        *,
        response: Any = None,
        text: str | None = None,
        error: BaseException | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        try:
            if token is None:
                return
            index, started = token
            step = self._steps[index]
            step["duration_ms"] = max(0, round((time.monotonic() - started) * 1000))
            step["status"] = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "error"
                if error is not None or error_code is not None
                else "ok"
            )
            if error is not None:
                error_code = getattr(error, "code", None) or type(error).__name__
                error_detail = str(error)
            if error_code is not None:
                step["error_code"] = self._clip(step, str(error_code), 500)
            if error_detail:
                step["error"] = self._clip(step, error_detail, 500)
            if response is not None:
                step["provider"] = response.provider
                step["service"] = response.usage.service
                results = response.results
                if len(results) > 6:
                    step["truncated"] = True
                step["results"] = [
                    {
                        "title": self._clip(step, hit.title, 300),
                        "url": self._clip(step, hit.url, 2000),
                        "snippet": self._clip(step, hit.excerpt, 1000),
                    }
                    for hit in results[:6]
                ]
            if text is not None:
                step["text"] = self._clip(step, text, 8000)
        except Exception:
            self._truncated = True

    def snapshot(self) -> dict[str, Any]:
        steps = copy.deepcopy(self._steps)
        for step in steps:
            if step["status"] == "started":
                step["status"] = "cancelled"
                step["truncated"] = True
        return {
            "steps": steps,
            "truncated": self._truncated or any(step["truncated"] for step in steps),
        }
