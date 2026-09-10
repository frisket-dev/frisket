"""Format the raw DOM extract (written by the Playwright capture spec) into
plain text -- used both for column 3 of the report and as the "actual panel"
half of the dissonance-judgment prompt.

The JSON shape this reads (per action, keyed by launcher kind) is written by
web/tests/e2e/action-flight-capture.spec.ts's ``extractForm`` browser-side
function:

    {
      "success": true,
      "title": "Summarize" | null,
      "fields": [
        {"label": "Model", "kind": "select", "value": "gpt-4o-mini",
         "options": ["gpt-4o-mini", "claude-haiku-4-5", ...]},
        {"label": "Instruction", "kind": "textarea", "value": "", "placeholder": "e.g. ..."},
        {"label": null, "kind": "segmented",
         "options": [{"text": "Concise", "selected": true}, {"text": "Detailed", "selected": false}]},
        ...
      ],
      "screenshot": "shots/summarize.png"
    }

or ``{"success": false, "error": "..."}`` when opening the panel failed.
"""

from __future__ import annotations

from typing import Any


def format_dom_summary(dom: dict[str, Any] | None) -> str:
    if not dom:
        return "(no capture)"
    lines: list[str] = []
    title = dom.get("title")
    if title:
        lines.append(f"Panel title: {title}")
    fields = dom.get("fields") or []
    if not fields:
        lines.append(
            "(no labeled fields extracted -- panel may be empty, text-only, or use a widget this extractor doesn't recognize)"
        )
    for f in fields:
        label = f.get("label") or "(unlabeled)"
        kind = f.get("kind", "?")
        bits = [f"{label} [{kind}]"]
        options = f.get("options")
        if options:
            if kind == "segmented":
                opt_strs = [
                    f"{o.get('text', '')}{'*' if o.get('selected') else ''}"
                    for o in options
                ]
                bits.append("options=" + ", ".join(opt_strs))
            else:
                bits.append("options=" + ", ".join(str(o) for o in options))
        value = f.get("value")
        if value:
            bits.append(f"value={value!r}")
        if "checked" in f:
            bits.append(f"checked={f['checked']}")
        placeholder = f.get("placeholder")
        if placeholder:
            bits.append(f"placeholder={placeholder!r}")
        lines.append(" - " + " | ".join(bits))
    return "\n".join(lines)
