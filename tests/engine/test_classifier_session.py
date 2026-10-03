from __future__ import annotations

import asyncio
import json

import pytest

from frisket.engine._workers import classifier_session as sessions
from frisket.engine._workers.local_engine_lease import _reset_lease_state_for_tests
from frisket.engine.sandbox.shim import SandboxProcessCancelledError


@pytest.fixture
def worker(monkeypatch, tmp_path):
    _reset_lease_state_for_tests()
    monkeypatch.setattr(sessions, "cached_classifier_path", lambda _: tmp_path)
    monkeypatch.setattr(sessions, "classifier_runtime_present", lambda: True)
    monkeypatch.setattr(sessions, "worker_argv", lambda *a, **kw: ["owned-worker"])
    calls = []

    class Handle:
        closed = False
        fail = None

        async def exchange_frame(self, payload, **kwargs):
            frame = json.loads(payload)
            calls.append(frame)
            if self.fail:
                raise self.fail
            if frame["type"] == "init":
                return b'{"type":"ready"}'
            return json.dumps(
                {
                    "type": "result",
                    "label": frame["labels"][1],
                    "score": 0.8,
                    "model_revision": sessions.LOCAL_CLASSIFIERS["gliclass"].revision,
                }
            ).encode()

        async def close(self, payload, **kwargs):
            self.closed = True
            return 0

        async def abort(self):
            self.closed = True

    handle = Handle()

    async def start(*args, **kwargs):
        return handle

    monkeypatch.setattr(sessions.shim, "open_sandboxed_process", start)
    return handle, calls


def test_reuses_worker_and_preserves_labels_and_text(worker):
    async def exercise():
        handle, calls = worker
        session = sessions.ClassifierSession("gliclass")
        for text in ["first\nrow", "second row"]:
            result = await session.classify(
                text, ["a.b", "c"], descriptions={"c": "same"}, instruction="Context"
            )
            assert result["label"] == "c"
            assert result["score"] == 0.8
        await session.aclose()
        assert handle.closed
        assert [call["type"] for call in calls] == ["init", "classify", "classify"]
        assert calls[1]["text"] == "first\nrow"
        assert calls[1]["labels"] == ["a.b", "c"]
        assert calls[1]["instruction"] == "Context"

    asyncio.run(exercise())


def test_empty_input_starts_no_worker(worker):
    async def exercise():
        session = sessions.ClassifierSession("gliclass")
        with pytest.raises(sessions.ClassifierError) as error:
            await session.classify(" \n", ["a", "b"], descriptions={}, instruction="")
        assert error.value.code == "classify_input_empty"
        assert worker[1] == []
        await session.aclose()

    asyncio.run(exercise())


def test_cancel_releases_process_and_lease(worker):
    async def exercise():
        worker[0].fail = SandboxProcessCancelledError()
        session = sessions.ClassifierSession("gliclass")
        with pytest.raises(asyncio.CancelledError):
            await session.classify("text", ["a", "b"], descriptions={}, instruction="")
        assert worker[0].closed
        await session.aclose()
        other = sessions.ClassifierSession("gliclass")
        worker[0].fail = None
        await other.classify("text", ["a", "b"], descriptions={}, instruction="")
        await other.aclose()

    asyncio.run(exercise())
