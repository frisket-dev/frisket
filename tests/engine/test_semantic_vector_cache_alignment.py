"""The sidecar vector cache refuses a batch it cannot align to its inputs.

``cell_vec`` is keyed by ``sha1(model_id + content)``. A vector written under
the wrong content's key is therefore wrong FOREVER: nothing re-derives it,
and ``rebuild_index`` clears only ``cell_fts`` (the vector cache deliberately
survives FTS rebuilds). So a provider response with a different vector count
than the batch it was given must be refused whole, before any row is written
— a short response cannot say WHICH input it dropped, and binding what
survived positionally shifts every later vector onto the wrong document.

Regression: both write sites zipped with ``strict=False`` and committed
before the resulting ``KeyError`` surfaced, so a one-off provider hiccup
durably poisoned the cache for every later run, including honest ones.
"""

from __future__ import annotations

import asyncio
from array import array

import pytest

from frisket.engine.store import Project
from frisket.search import _sidecar, rebuild_index
from frisket.semantic import _doc_vectors, _doc_vectors_async, _vec_key

MODEL = "stub/align-v1"
NOTES = ["alpha document", "bravo document", "charlie document"]
TRUE = {
    "alpha document": [1.0, 0.0, 0.0],
    "bravo document": [0.0, 1.0, 0.0],
    "charlie document": [0.0, 0.0, 1.0],
}


def _seed(tmp_path) -> tuple[Project, list[dict]]:
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {"note": p.add_column(sheet, "note")}
    p.add_rows(sheet, [{"note": n} for n in NOTES], cols)
    rebuild_index(p)
    corpus = [
        {
            "content": n,
            "sheet_id": sheet,
            "row_id": i,
            "column_id": 1,
            "column_name": "note",
        }
        for i, n in enumerate(NOTES, start=1)
    ]
    return p, corpus


def _honest(texts: list[str]) -> list[list[float]]:
    return [TRUE[t] for t in texts]


def _drops_first(texts: list[str]) -> list[list[float]]:
    """A provider that silently omits its FIRST input from the response.

    The surviving vectors are individually correct; only their POSITIONS
    lie. That is what makes the misalignment invisible without this check.
    """
    return [TRUE[t] for t in texts[1:]]


def _returns_extra(texts: list[str]) -> list[list[float]]:
    return [TRUE[t] for t in texts] + [[0.5, 0.5, 0.5]]


def _cached_rows(project: Project) -> dict[str, list[float]]:
    db = _sidecar(project)
    try:
        out = {}
        for key, blob in db.execute("SELECT key, vec FROM cell_vec").fetchall():
            a = array("f")
            a.frombytes(blob)
            out[key] = list(a)
        return out
    finally:
        db.close()


def test_short_batch_writes_nothing_and_never_poisons_later_runs(tmp_path):
    p, corpus = _seed(tmp_path)

    with pytest.raises(RuntimeError, match="2 vectors for 3 inputs"):
        _doc_vectors(p, corpus, _drops_first, MODEL)

    # the refusal must precede the write, not follow it: the old code
    # committed two misaligned rows and only then raised KeyError.
    assert _cached_rows(p) == {}, "a refused batch still reached cell_vec"

    # and a later, entirely healthy run gets the truth
    served = _doc_vectors(p, corpus, _honest, MODEL)
    assert served == [TRUE[n] for n in NOTES]
    p.close()


def test_over_long_batch_is_refused_rather_than_silently_truncated(tmp_path):
    p, corpus = _seed(tmp_path)

    with pytest.raises(RuntimeError, match="4 vectors for 3 inputs"):
        _doc_vectors(p, corpus, _returns_extra, MODEL)

    assert _cached_rows(p) == {}
    p.close()


def test_rebuild_index_cannot_undo_a_poisoned_vector_cache(tmp_path):
    """Why the refusal has to be at the write: there is no repair downstream.

    Seeded by hand (not via a bad provider — the fix makes that
    unreachable), this pins the property that motivates failing closed.
    """
    p, corpus = _seed(tmp_path)
    _doc_vectors(p, corpus, _honest, MODEL)

    wrong_key = _vec_key(MODEL, "alpha document")
    db = _sidecar(p)
    db.execute(
        "INSERT OR REPLACE INTO cell_vec (key, vec) VALUES (?, ?)",
        (wrong_key, array("f", TRUE["bravo document"]).tobytes()),
    )
    db.commit()
    db.close()

    rebuild_index(p)

    assert _cached_rows(p)[wrong_key] == TRUE["bravo document"], (
        "rebuild_index now clears cell_vec — if that is intended, the "
        "operator remedy for a poisoned cache changed and this test's "
        "premise (only deleting the sidecar heals it) needs revisiting"
    )
    assert _doc_vectors(p, corpus, _honest, MODEL)[0] == TRUE["bravo document"]
    p.close()


def test_async_short_batch_writes_nothing(tmp_path):
    p, corpus = _seed(tmp_path)

    with pytest.raises(RuntimeError, match="2 vectors for 3 inputs"):
        asyncio.run(_doc_vectors_async(p, corpus, _drops_first, MODEL))

    assert _cached_rows(p) == {}
    served = asyncio.run(_doc_vectors_async(p, corpus, _honest, MODEL))
    assert served == [TRUE[n] for n in NOTES]
    p.close()


def test_async_refusal_still_records_the_paid_fact_first(tmp_path):
    """A wrong-count provider answered, and billed. The ledger hears about it.

    ``on_fresh_batch`` is where the run-scoped caller makes the paid call
    durable. Refusing BEFORE it would eat a real charge silently, which is
    the failure the callback exists to prevent — so the alignment refusal
    sits after it.
    """
    p, corpus = _seed(tmp_path)
    recorded: list[list[str]] = []

    with pytest.raises(RuntimeError, match="2 vectors for 3 inputs"):
        asyncio.run(
            _doc_vectors_async(
                p,
                corpus,
                _drops_first,
                MODEL,
                on_fresh_batch=lambda batch, texts: recorded.append(list(texts)),
            )
        )

    assert recorded == [NOTES], "the paid batch was refused before it was recorded"
    assert _cached_rows(p) == {}
    p.close()
