"""Renders the action-panel flight HTML report.

Column order (the verbose DOM extract stays at the far right in its own
horizontally scrolling box so the skimmable columns remain readable):
  1. Action name + description (from the registry).
  2. Haiku's prediction of the panel's form, from name+description alone.
     Rendered as Markdown.
  3. Dissonance judgment (LLM compare of 2 vs the DOM extract). Rendered as
     Markdown bullet points (see judge.py's prompt).
  4. Screenshot thumbnail -- click opens a full-size modal instead of
     linking to the file.
  5. Actual panel (DOM extract), far right, own x-scroll container.

Text cells (1-3, 5) use `position: sticky` so they stay readable while the
row scrolls past vertically in the viewport.
"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

from .catalog import ActionEntry
from .dom_extract import format_dom_summary
from .markdown import render_markdown

VERDICT_CLASS = {
    "none": "verdict-none",
    "minor": "verdict-minor",
    "major": "verdict-major",
    "n/a": "verdict-na",
    "unknown": "verdict-na",
    "skipped": "verdict-na",
}

VERDICT_LABEL = {
    "none": "no dissonance",
    "minor": "minor dissonance",
    "major": "MAJOR dissonance",
    "n/a": "not captured",
    "unknown": "unclear",
    "skipped": "skipped",
}


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def _nl2br(s: str | None) -> str:
    return _esc(s).replace("\n", "<br>")


CSS = """
:root {
  --bg: #f7f7f5;
  --panel: #ffffff;
  --text: #1b1b1a;
  --muted: #666560;
  --border: #e2e0da;
  --accent: #9b4d2e;
  --code-bg: #f0efe9;
  --verdict-none-bg: #e7f3e8; --verdict-none-fg: #256029;
  --verdict-minor-bg: #fdf3d6; --verdict-minor-fg: #7a5b00;
  --verdict-major-bg: #fbe4e2; --verdict-major-fg: #9c2b1f;
  --verdict-na-bg: #ececec; --verdict-na-fg: #666560;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17181a; --panel: #202225; --text: #ecece7; --muted: #9a9a94;
    --border: #34353a; --accent: #e2a37e; --code-bg: #26282c;
    --verdict-none-bg: #16301b; --verdict-none-fg: #8fd39a;
    --verdict-minor-bg: #3a3010; --verdict-minor-fg: #eccb6a;
    --verdict-major-bg: #3a1712; --verdict-major-fg: #f0958a;
    --verdict-na-bg: #2b2c2f; --verdict-na-fg: #9a9a94;
  }
}
:root[data-theme="dark"] {
  --bg: #17181a; --panel: #202225; --text: #ecece7; --muted: #9a9a94;
  --border: #34353a; --accent: #e2a37e; --code-bg: #26282c;
  --verdict-none-bg: #16301b; --verdict-none-fg: #8fd39a;
  --verdict-minor-bg: #3a3010; --verdict-minor-fg: #eccb6a;
  --verdict-major-bg: #3a1712; --verdict-major-fg: #f0958a;
  --verdict-na-bg: #2b2c2f; --verdict-na-fg: #9a9a94;
}
:root[data-theme="light"] {
  --bg: #f7f7f5; --panel: #ffffff; --text: #1b1b1a; --muted: #666560;
  --border: #e2e0da; --accent: #9b4d2e; --code-bg: #f0efe9;
  --verdict-none-bg: #e7f3e8; --verdict-none-fg: #256029;
  --verdict-minor-bg: #fdf3d6; --verdict-minor-fg: #7a5b00;
  --verdict-major-bg: #fbe4e2; --verdict-major-fg: #9c2b1f;
  --verdict-na-bg: #ececec; --verdict-na-fg: #666560;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 4rem;
  background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
header {
  padding: 1.25rem 1.5rem 1rem;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
}
header h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
header p { margin: 0.15rem 0; color: var(--muted); font-size: 0.9rem; }
header code {
  background: var(--code-bg); padding: 0.1rem 0.35rem; border-radius: 4px;
  font-size: 0.85em;
}
.stats { display: flex; gap: 1.5rem; flex-wrap: wrap; margin-top: 0.6rem; }
.stat { font-size: 0.85rem; color: var(--muted); }
.stat b { color: var(--text); font-size: 1.05rem; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 1500px; table-layout: fixed; }
col.c-name { width: 16%; }
col.c-predict { width: 21%; }
col.c-judge { width: 19%; }
col.c-shot { width: 13%; }
col.c-dom { width: 31%; }
thead th {
  position: sticky; top: 0; z-index: 3;
  background: var(--panel); border-bottom: 2px solid var(--border);
  text-align: left; padding: 0.6rem 0.75rem; font-size: 0.78rem;
  text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
}
tbody tr { border-bottom: 1px solid var(--border); }
tbody tr:nth-child(even) { background: color-mix(in srgb, var(--panel) 60%, var(--bg)); }
tbody tr.not-captured { opacity: 0.72; }
td { padding: 0; vertical-align: top; }
.cell-inner {
  padding: 0.75rem 0.85rem;
  position: sticky;
  top: 44px; /* clears the sticky header */
}
.kind { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82rem; color: var(--accent); }
.title { font-weight: 600; margin: 0.15rem 0 0.3rem; }
.desc { color: var(--muted); font-size: 0.86rem; }
.launcher-tag {
  display: inline-block; margin-top: 0.4rem; font-size: 0.72rem;
  padding: 0.1rem 0.4rem; border-radius: 3px; background: var(--code-bg); color: var(--muted);
}
.prose { font-size: 0.86rem; white-space: normal; }
/* Markdown rendering for columns 2 (prediction) and 3 (judgment "why") --
   keep it compact since these live in a narrow sticky cell. */
.md :first-child { margin-top: 0; }
.md :last-child { margin-bottom: 0; }
.md p { margin: 0 0 0.5em; }
.md ul, .md ol { margin: 0 0 0.5em; padding-left: 1.2em; }
.md li { margin: 0.15em 0; }
.md li + li { margin-top: 0.15em; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 {
  margin: 0.5em 0 0.3em; font-size: 0.95em;
}
.md code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.92em; background: var(--code-bg); padding: 0.05em 0.3em;
  border-radius: 3px;
}
.md strong { font-weight: 700; }
/* DOM-extract column: far right, own horizontal scroll box so the extracted
   form structure displays at its natural width instead of wrapping/
   truncating -- it stays out of the way of the other columns because it
   scrolls internally rather than widening the cell. */
.dom-wrap {
  max-width: 100%; overflow-x: auto; overflow-y: auto; max-height: 340px;
  background: var(--code-bg); border-radius: 5px;
}
.dom-dump {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.76rem; white-space: pre; word-break: normal;
  padding: 0.5rem; margin: 0; display: inline-block; min-width: 100%;
}
.verdict-badge {
  display: inline-block; font-size: 0.72rem; font-weight: 700;
  padding: 0.15rem 0.45rem; border-radius: 4px; margin-bottom: 0.4rem;
  text-transform: uppercase; letter-spacing: 0.03em;
}
.verdict-none { background: var(--verdict-none-bg); color: var(--verdict-none-fg); }
.verdict-minor { background: var(--verdict-minor-bg); color: var(--verdict-minor-fg); }
.verdict-major { background: var(--verdict-major-bg); color: var(--verdict-major-fg); }
.verdict-na { background: var(--verdict-na-bg); color: var(--verdict-na-fg); }
.shot-link { display: block; cursor: zoom-in; background: none; border: 0; padding: 0; text-align: left; }
.shot-link img {
  max-width: 100%; max-height: 260px; display: block;
  border: 1px solid var(--border); border-radius: 4px;
  background: var(--code-bg);
}
.shot-missing { font-size: 0.8rem; color: var(--muted); font-style: italic; }
footer { padding: 1.5rem; color: var(--muted); font-size: 0.8rem; }

/* Screenshot modal -- click a thumbnail to see it full size, no navigation
   away from the report. Pure CSS/JS, no dependency. */
.shot-modal {
  position: fixed; inset: 0; z-index: 20; display: flex;
  align-items: center; justify-content: center;
  background: color-mix(in srgb, black 78%, transparent);
  padding: 3vh 3vw;
}
.shot-modal[hidden] { display: none; }
.shot-modal img {
  max-width: 100%; max-height: 100%; display: block;
  border-radius: 6px; box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
  background: var(--panel);
}
.shot-modal-close {
  position: fixed; top: 1.25rem; right: 1.5rem; z-index: 21;
  font-size: 1.8rem; line-height: 1; color: #fff; background: none;
  border: 0; cursor: pointer; padding: 0.25rem 0.5rem; opacity: 0.85;
}
.shot-modal-close:hover { opacity: 1; }
"""


def _row_html(
    entry: ActionEntry,
    dom: dict[str, Any] | None,
    prediction: str,
    judgment: dict[str, str],
) -> str:
    captured = bool(dom and dom.get("success"))
    row_class = "" if captured else ' class="not-captured"'

    if entry.ribbon_launchable:
        launcher_tag = (
            f'<span class="launcher-tag">ribbon: {_esc(entry.launcher_kind)}</span>'
        )
    elif dom and dom.get("opener"):
        launcher_tag = (
            f'<span class="launcher-tag">opener: {_esc(dom["opener"])}</span>'
        )
    else:
        launcher_tag = '<span class="launcher-tag">no opener in capture run</span>'

    col1 = (
        f'<div class="kind">{_esc(entry.kind)}</div>'
        f'<div class="title">{_esc(entry.title)}</div>'
        f'<div class="desc">{_esc(entry.description)}</div>'
        f"{launcher_tag}"
    )

    col2 = f'<div class="prose md">{render_markdown(prediction)}</div>'

    if dom is not None:
        dom_summary = (
            format_dom_summary(dom)
            if captured
            else (f"(capture failed: {dom.get('error', 'unknown error')})")
        )
    else:
        dom_summary = (
            "(not captured this run -- no opener reaches this action's panel "
            "in the current capture pass)"
        )
    col5 = (
        f'<div class="dom-wrap"><pre class="dom-dump">{_esc(dom_summary)}</pre></div>'
    )

    verdict = judgment.get("verdict", "n/a")
    verdict_class = VERDICT_CLASS.get(verdict, "verdict-na")
    verdict_label = VERDICT_LABEL.get(verdict, verdict or "n/a")
    why = judgment.get("why", "")
    col3 = (
        f'<span class="verdict-badge {verdict_class}">{_esc(verdict_label)}</span>'
        f'<div class="prose md">{render_markdown(why)}</div>'
    )

    if captured and dom.get("screenshot"):
        shot = dom["screenshot"]
        col4 = (
            f'<button type="button" class="shot-link" data-shot="{_esc(shot)}" '
            f'data-shot-alt="{_esc(entry.kind)} panel screenshot">'
            f'<img src="{_esc(shot)}" loading="lazy" alt="{_esc(entry.kind)} panel screenshot"></button>'
        )
    else:
        col4 = '<span class="shot-missing">no screenshot</span>'

    return (
        f"<tr{row_class}>"
        f'<td><div class="cell-inner">{col1}</div></td>'
        f'<td><div class="cell-inner">{col2}</div></td>'
        f'<td><div class="cell-inner">{col3}</div></td>'
        f'<td><div class="cell-inner">{col4}</div></td>'
        f'<td><div class="cell-inner">{col5}</div></td>'
        f"</tr>"
    )


def render_html(
    catalog: list[ActionEntry],
    dom_by_kind: dict[str, Any],
    predictions: dict[str, str],
    judgments: dict[str, dict[str, str]],
) -> str:
    total = len(catalog)
    captured = sum(1 for e in catalog if dom_by_kind.get(e.kind, {}).get("success"))
    dissonant = sum(
        1
        for e in catalog
        if judgments.get(e.kind, {}).get("verdict") in ("minor", "major")
    )
    major = sum(
        1 for e in catalog if judgments.get(e.kind, {}).get("verdict") == "major"
    )

    rows = "\n".join(
        _row_html(
            entry,
            dom_by_kind.get(entry.kind),
            predictions.get(entry.kind, ""),
            judgments.get(entry.kind, {}),
        )
        for entry in catalog
    )

    generated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Action-panel flight</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Action-panel flight</h1>
  <p>For every action in the frisket action registry: what the name/description
  promises (col. 2, Haiku, name+description only) vs. a dissonance judgment
  (col. 3, bullet points) vs. a screenshot (col. 4, click for full size) vs.
  what the rendered panel actually asks for (col. 5, DOM-extracted via
  Playwright, far right, scrolls horizontally within its own box). Personal
  QoL tool -- not a CI gate.</p>
  <p>Regenerate: <code>set -a; . .secrets/frisket.env; set +a &amp;&amp; uv run python scripts/dev/action_flight.py</code></p>
  <p>Generated {generated}</p>
  <div class="stats">
    <div class="stat"><b>{total}</b> actions in registry</div>
    <div class="stat"><b>{captured}</b> panels captured (DOM + screenshot)</div>
    <div class="stat"><b>{dissonant}</b> flagged minor/major dissonance</div>
    <div class="stat"><b>{major}</b> flagged MAJOR dissonance</div>
  </div>
</header>
<div class="table-wrap">
<table>
  <colgroup>
    <col class="c-name"><col class="c-predict"><col class="c-judge"><col class="c-shot"><col class="c-dom">
  </colgroup>
  <thead>
    <tr>
      <th>Action (registry)</th>
      <th>Haiku prediction (name+desc only)</th>
      <th>Dissonance judgment</th>
      <th>Screenshot</th>
      <th>Actual panel (DOM extract)</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
</div>
<footer>
  Rows dimmed with a "no ribbon tile" / "no opener" badge have no captured
  form panel in this pass -- columns 1 and 2 still cover them fully, and the
  badges keep the missing visual coverage explicit.
</footer>
<div class="shot-modal" id="shot-modal" hidden>
  <button type="button" class="shot-modal-close" id="shot-modal-close" aria-label="Close screenshot">&times;</button>
  <img id="shot-modal-img" src="" alt="">
</div>
<script>
(function () {{
  var modal = document.getElementById('shot-modal');
  var modalImg = document.getElementById('shot-modal-img');
  function openModal(src, alt) {{
    modalImg.src = src;
    modalImg.alt = alt || '';
    modal.hidden = false;
  }}
  function closeModal() {{
    modal.hidden = true;
    modalImg.src = '';
  }}
  document.addEventListener('click', function (ev) {{
    var trigger = ev.target.closest('.shot-link');
    if (trigger) {{
      openModal(trigger.getAttribute('data-shot'), trigger.getAttribute('data-shot-alt'));
      return;
    }}
    if (ev.target === modal || ev.target.id === 'shot-modal-close') {{
      closeModal();
    }}
  }});
  document.addEventListener('keydown', function (ev) {{
    if (ev.key === 'Escape' && !modal.hidden) closeModal();
  }});
}})();
</script>
</body>
</html>
"""
