"""Column 3: dissonance judgment -- Haiku compares the column-2 prediction
against the column-3 DOM extract and flags where the panel would confuse
someone who only read the name/description.

Per review feedback, the ``why`` text is deliberately prompted as concise
Markdown bullet points rather than dense prose -- report.py renders it
through markdown.render_markdown().
"""

from __future__ import annotations

from .llm import complete

JUDGE_SYSTEM = (
    "You are auditing a data tool's UI for name/description-vs-reality "
    "dissonance. You will be given (1) an action's name and description, "
    "(2) a PREDICTION of what its configuration panel should ask for, "
    "written by someone who only read the name and description, and (3) the "
    "ACTUAL form fields extracted from the rendered panel's DOM. Compare "
    "them and answer in EXACTLY this format, nothing else:\n\n"
    "VERDICT: <none|minor|major>\n"
    "WHY:\n"
    "- <one concrete, specific bullet -- name the field that would or would "
    "not surprise someone who only read the name/description>\n"
    "- <a second bullet, only if there is a genuinely separate point -- omit "
    "this line entirely otherwise>\n"
    "- <a third bullet, same rule>\n\n"
    "Write 1-3 short bullet points, NOT a paragraph -- each bullet is one "
    "sentence, concrete, and names a specific field. Do not pad to three "
    "bullets if one or two says everything. "
    "none = the panel roughly matches what the name/description implies. "
    "minor = there are a few surprises but the core purpose stays legible. "
    "major = someone who only read the name/description would be confused "
    "or actively misled by what the panel does -- flag this clearly."
)


def judge_dissonance(
    kind: str, title: str, description: str, prediction: str, dom_summary: str
) -> dict[str, str]:
    prompt = (
        f"Action name: {kind} ({title})\n"
        f"Description: {description}\n\n"
        f"PREDICTION (from name+description alone):\n{prediction}\n\n"
        f"ACTUAL panel fields (extracted from the rendered DOM):\n{dom_summary}\n"
    )
    raw = complete(prompt, system=JUDGE_SYSTEM, max_tokens=300)
    verdict = "unknown"
    why_lines: list[str] = []
    in_why = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            verdict = stripped.split(":", 1)[1].strip().lower()
            in_why = False
            continue
        if stripped.upper().startswith("WHY:"):
            in_why = True
            remainder = stripped.split(":", 1)[1].strip()
            if remainder:
                why_lines.append(remainder)
            continue
        if in_why and stripped:
            why_lines.append(stripped)
    why = "\n".join(why_lines) if why_lines else raw
    return {"verdict": verdict, "why": why, "raw": raw}
