"""The cost gate speaks with one voice across the process boundary.

The panel raises its own pre-launch confirmation and the server raises the
402 — different code, different language, and they render in the SAME modal
minutes or seconds apart. They used to phrase one decision three ways:

    "The panel could not estimate this run. Confirm before sending it to the
     server cost gate."
    "The server could not price this run. Confirm before sending it to the
     server cost gate."
    "estimated cost is unknown (this run has no published price) — confirm to
     run it anyway"

Which component failed is not the user's problem, and naming it invited the
reading that three different things had gone wrong. The sentence is now one
string on each side of the boundary, and this is the test that goes red when
one of them moves.
"""

from __future__ import annotations

import re
from pathlib import Path

from frisket.engine.runner.validation import CostGate

ROOT = Path(__file__).resolve().parents[1]


def _ts_constant(name: str) -> str:
    # The Python sentence and the TypeScript one are the two things being
    # compared, so reading the second as text IS the test.
    # rule19: diffs two independently maintained sources across a language
    source = (ROOT / "web/src/actions/model.ts").read_text(encoding="utf-8")
    match = re.search(
        rf"export const {name} =\s*\n?\s*'([^']*)';",
        source,
    )
    assert match is not None, f"{name} is no longer a plain string literal"
    return match.group(1)


def test_the_unknown_cost_sentence_is_identical_on_both_sides() -> None:
    assert str(CostGate(None)) == _ts_constant("UNKNOWN_COST_GATE_MESSAGE")


def test_the_sentence_still_names_the_reason_and_the_way_out() -> None:
    """Not just equal — equal to something that answers "why" and "now what".
    Two identical but useless strings would pass the parity check alone."""
    message = str(CostGate(None))
    assert "no published price" in message
    assert "confirm to run it anyway" in message
