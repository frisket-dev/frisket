from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from frisket.engine._workers import spacy_session as sessions


@pytest.fixture
def worker(monkeypatch, tmp_path):
    from frisket.ai.models import spacy_model

    monkeypatch.setattr(sessions, "is_installed", lambda: True)
    monkeypatch.setattr(
        spacy_model, "model_state", lambda **_: SimpleNamespace(status="present")
    )
    monkeypatch.setattr(spacy_model, "ensure_loadable_model_path", lambda: tmp_path)
    monkeypatch.setattr(sessions, "worker_argv", lambda *a, **kw: ["owned-worker"])
    calls, launches = [], []

    class Handle:
        closed = False
        aborted = False
        invalid = False
        failure = None

        async def exchange_frame(self, payload, **kwargs):
            frame = json.loads(payload)
            calls.append(frame)
            if self.failure:
                raise self.failure
            if frame["type"] == "init":
                return b'{"type":"ready"}'
            entity = {
                "text": "Ada",
                "type": "person",
                "start": 0,
                "end": 3,
                "score": None,
            }
            if self.invalid:
                entity["end"] = 100
            return json.dumps({"type": "result", "entities": [entity]}).encode()

        async def close(self, *a, **kw):
            self.closed = True

        async def abort(self):
            self.aborted = True

    handle = Handle()

    async def open_process(argv, **kwargs):
        launches.append(kwargs)
        return handle

    monkeypatch.setattr(sessions.shim, "open_sandboxed_process", open_process)
    return handle, calls, launches


@pytest.mark.asyncio
async def test_one_lazy_offline_worker_for_many_rows(worker):
    handle, calls, launches = worker
    session = sessions.SpacySession()
    assert launches == []
    assert await session.extract("   ", ["person"]) == []
    for _ in range(3):
        result = await session.extract("Ada visited London", ["People"])
        assert result[0]["text"] == "Ada"
    assert len(launches) == 1
    assert [call["type"] for call in calls] == ["init", "extract", "extract", "extract"]
    assert calls[1]["labels"] == ["person"]
    policy = launches[0]["policy"]
    assert policy.trusted_python_netwall
    await session.aclose()
    assert handle.closed


@pytest.mark.asyncio
async def test_invalid_span_aborts_worker(worker):
    handle, _, _ = worker
    handle.invalid = True
    session = sessions.SpacySession()
    with pytest.raises(ValueError, match="invalid spaCy entity"):
        await session.extract("Ada", ["person"])
    assert handle.aborted
    with pytest.raises(RuntimeError, match="closed"):
        await session.extract("Ada", ["person"])


@pytest.mark.asyncio
async def test_cancelled_exchange_aborts_worker(worker):
    handle, _, _ = worker
    handle.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await sessions.SpacySession().extract("Ada", ["person"])
    assert handle.aborted


@pytest.mark.asyncio
async def test_invocation_owns_one_session_and_closes_it(monkeypatch):
    from frisket.engine.executor import ner_read

    instances = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.calls = []
            self.closed = False
            instances.append(self)

        async def extract(self, text, labels):
            self.calls.append(text)
            return []

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(ner_read.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(sessions, "SpacySession", FakeSession)
    ctx = SimpleNamespace(extras={})
    owner = ner_read.AdmittedNerExtractor(ctx, engine="spacy")
    for row_id in range(1, 4):
        row = owner.bind_row({}, sheet_id=1, row_id=row_id, sources=[], ctx=ctx)
        await row.extract("Ada", labels=["person"], threshold=0.5)
    await owner.aclose()
    assert len(instances) == 1
    assert instances[0].calls == ["Ada"] * 3
    assert instances[0].closed
