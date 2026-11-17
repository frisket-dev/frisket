"""One lazy, offline spaCy child owned by an action, shared across its rows."""

from __future__ import annotations

from frisket.engine._workers.session_base import FramedSession
from frisket.engine._workers.spacy_entities import SPACY_CANONICAL_TYPES
from frisket.runtime.launch import PythonRuntime, worker_argv
from frisket.runtime.spacy_install import is_installed, runtime_python

_FRAME_LIMIT = 4 * 1024 * 1024


class SpacySession(FramedSession):
    _request_limit = _FRAME_LIMIT
    _response_limit = _FRAME_LIMIT
    _memory_mb = 4096
    _op = "local entity extraction"
    _invalid_response = "invalid spaCy response"

    def _request_too_large(self) -> Exception:
        return ValueError("This input is too long for local entity extraction.")

    def _closed_error(self) -> Exception:
        return RuntimeError("The spaCy session is closed.")

    def _worker_error(self, frame: dict) -> Exception:
        return RuntimeError(
            "The local spaCy worker could not process this row. Retry setup or check available memory."
        )

    async def _start(self) -> None:
        if self._handle is not None:
            return
        from frisket.ai.models.spacy_model import (
            ensure_loadable_model_path,
            model_state,
        )

        from frisket.ops.spacy_ner import DEFAULT_SPACY_MODEL, SPACY_MODEL

        if SPACY_MODEL != DEFAULT_SPACY_MODEL:
            raise RuntimeError(
                "Custom spaCy models require a host-installed spaCy runtime."
            )
        if (
            not is_installed()
            or model_state(include_python_package=False).status != "present"
        ):
            raise RuntimeError("Set up spaCy in the engine picker before running it.")
        path = ensure_loadable_model_path()
        runtime = PythonRuntime(runtime_python(), PythonRuntime.current().app_code_root)
        ready = await self._launch(
            worker_argv("spacy-session", runtime=runtime),
            {"type": "init", "model_path": str(path)},
            reads=(str(path),),
            environment={"OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"},
        )
        if ready != {"type": "ready"}:
            raise ValueError("spaCy did not become ready")

    async def extract(self, text: str, labels: list[str]) -> list[dict]:
        if not labels or any(
            not isinstance(label, str) or not label.strip() for label in labels
        ):
            raise ValueError("NER requires nonblank entity labels")
        return await self._serialized(lambda: self._extract(text, labels))

    async def _extract(self, text: str, labels: list[str]) -> list[dict]:
        if not text.strip():
            return []
        await self._start()
        from frisket.ops.spacy_ner import requested_spacy_types

        wanted = requested_spacy_types(labels)
        result = await self._exchange(
            {"type": "extract", "text": text, "labels": sorted(wanted or ())}
        )
        entities = result.get("entities")
        if result.get("type") != "result" or not isinstance(entities, list):
            raise ValueError("invalid spaCy result")
        for entity in entities:
            if not isinstance(entity, dict) or set(entity) != {
                "text",
                "type",
                "start",
                "end",
                "score",
            }:
                raise ValueError("invalid spaCy entity")
            start, end = entity["start"], entity["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(text)
                or entity["text"] != text[start:end]
                or entity["type"] not in SPACY_CANONICAL_TYPES
                or (wanted is not None and entity["type"] not in wanted)
                or entity["score"] is not None
            ):
                raise ValueError("invalid spaCy entity")
        from frisket.ops.entities import canonicalize_entities

        return canonicalize_entities(entities)
