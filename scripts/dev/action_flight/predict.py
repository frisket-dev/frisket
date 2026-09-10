"""Column 2: what Haiku predicts the panel should ask, from name+description alone.

Rationale: if Haiku can't guess what a panel does from its name and
description, neither can a journalist -- this catches the "passed the
automatic eval but makes no sense" class. This prediction text also doubles
as seed copy for per-action documentation.
"""

from __future__ import annotations

from .llm import complete

PREDICT_SYSTEM = (
    "You are a journalist evaluating a data-investigation tool's action "
    "catalog. You will be given only an action's machine name and one-line "
    "description -- nothing else: no screenshot, no source code, no other "
    "context. Predict, in plain prose (4-8 sentences), what you'd expect the "
    "configuration panel for this action to ask you for: concrete field "
    "names, options, toggles, and a sensible default, plus a one-sentence "
    "journalistic use case for when you'd reach for this action. Be "
    "specific -- guess actual field names and choices, not generic "
    "platitudes. If the name/description gives you nothing to go on, say so "
    "plainly instead of inventing detail."
)


def predict_panel(kind: str, title: str, description: str) -> str:
    prompt = (
        f"Action name: {kind} ({title})\n"
        f"Description: {description}\n\n"
        "What would you expect this action's configuration panel to ask for?"
    )
    return complete(prompt, system=PREDICT_SYSTEM, max_tokens=500)
