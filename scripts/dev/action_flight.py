#!/usr/bin/env python3
"""Action-panel flight report generator.

A runnable sweep across the frisket action registry: for every action, shows
(1) its name+description from the registry, (2) what Haiku predicts the
panel's form should ask for, from name+description ALONE, (3) what the
rendered panel actually shows -- form structure DOM-extracted via Playwright,
plus a screenshot, (4) an LLM judgment of the gap between (2) and (3), and (5)
the screenshot thumbnail. This is a local developer quality-of-life tool,
not a CI gate.

Usage:
    set -a; . .secrets/frisket.env; set +a   # needs ANTHROPIC_API_KEY
    uv run python scripts/dev/action_flight.py

    # Iterate on layout without re-booting the app or spending on the LLM:
    uv run python scripts/dev/action_flight.py --skip-capture --skip-llm

    # Debug a single action (still boots the stack unless --skip-capture):
    uv run python scripts/dev/action_flight.py --only summarize

Output: .artifacts/action-flight/index.html (open it directly in a browser --
no server needed), plus intermediate JSON under .artifacts/action-flight/data/
for debugging.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dev"))

from action_flight.catalog import ActionEntry, load_catalog  # noqa: E402
from action_flight.dom_extract import format_dom_summary  # noqa: E402
from action_flight.judge import judge_dissonance  # noqa: E402
from action_flight.llm import AnthropicKeyMissing  # noqa: E402
from action_flight.predict import predict_panel  # noqa: E402
from action_flight.report import render_html  # noqa: E402

OUT_DIR = REPO_ROOT / ".artifacts" / "action-flight"
DATA_DIR = OUT_DIR / "data"
EXTRA_DIR = DATA_DIR / "extra"
DOM_EXTRACT_JSON = DATA_DIR / "dom-extract.json"
PREDICTIONS_JSON = DATA_DIR / "predictions.json"
JUDGMENTS_JSON = DATA_DIR / "judgments.json"

LLM_WORKERS = 8


def run_capture() -> None:
    print(
        "[action-flight] booting local stack + capturing DOM/screenshots via Playwright (ribbon idiom, 30 actions)..."
    )
    web_dir = REPO_ROOT / "web"
    result = subprocess.run(
        ["npx", "playwright", "test", "tests/e2e/action-flight-capture.spec.ts"],
        cwd=web_dir,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"[action-flight] ribbon capture failed (playwright exit {result.returncode})"
        )

    print(
        "[action-flight] capturing per-idiom openers for non-ribbon actions (import/source/embeddings/"
        "cluster/column/row/cell/review/replay/export/operations/plugin)..."
    )
    extra_result = subprocess.run(
        ["npx", "playwright", "test", "tests/e2e/action-flight-capture-extra.spec.ts"],
        cwd=web_dir,
    )
    if extra_result.returncode != 0:
        print(
            "[action-flight] WARNING: one or more per-idiom openers failed or were "
            f"flaky (playwright exit {extra_result.returncode}) -- continuing with "
            "whatever .artifacts/action-flight/data/extra/*.json got written this "
            "run (each family test writes its own file and any prior run's file "
            "stays on disk if this run didn't touch it). Failed families render as "
            "'not captured' in the report rather than aborting the whole sweep.",
            file=sys.stderr,
        )


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_extra_dom_extracts() -> dict[str, Any]:
    """Merge every .artifacts/action-flight/data/extra/<family>.json (one
    per action-flight-capture-extra.spec.ts test, keyed by catalog kind) into
    one dict. Each family owns a distinct file so parallel Playwright workers
    never race a shared read-modify-write."""
    merged: dict[str, Any] = {}
    if not EXTRA_DIR.exists():
        return merged
    for path in sorted(EXTRA_DIR.glob("*.json")):
        merged.update(load_json(path))
    return merged


def dom_by_catalog_kind(
    catalog: list[ActionEntry],
    ribbon_dom_extracts: dict[str, Any],
    extra_dom_extracts: dict[str, Any],
) -> dict[str, Any]:
    """Unify the two capture sources into one lookup keyed by catalog kind
    (entry.kind) -- the ribbon capture is keyed by launcher_kind, the extra
    capture is keyed by catalog kind directly."""
    unified: dict[str, Any] = {}
    for entry in catalog:
        if entry.ribbon_launchable and entry.launcher_kind in ribbon_dom_extracts:
            unified[entry.kind] = ribbon_dom_extracts[entry.launcher_kind]
        elif entry.kind in extra_dom_extracts:
            unified[entry.kind] = extra_dom_extracts[entry.kind]
    return unified


def predict_one(entry: ActionEntry) -> tuple[str, str]:
    try:
        return entry.kind, predict_panel(entry.kind, entry.title, entry.description)
    except AnthropicKeyMissing:
        raise
    except Exception as exc:  # noqa: BLE001 -- one bad call must not kill the sweep
        return entry.kind, f"(prediction failed: {exc})"


def judge_one(
    entry: ActionEntry, prediction: str, dom_summary: str
) -> tuple[str, dict[str, str]]:
    try:
        return entry.kind, judge_dissonance(
            entry.kind, entry.title, entry.description, prediction, dom_summary
        )
    except AnthropicKeyMissing:
        raise
    except Exception as exc:  # noqa: BLE001
        return entry.kind, {
            "verdict": "unknown",
            "why": f"(judgment failed: {exc})",
            "raw": "",
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--skip-capture",
        action="store_true",
        help="reuse the existing .artifacts/action-flight/data/dom-extract.json "
        "instead of booting the local stack and re-running Playwright",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="skip Haiku predict/judge calls; columns 2 and 4 render as "
        "'(skipped)' -- for iterating on layout without spend",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="restrict predict/judge/render to one action (matches launcher "
        "kind or full catalog kind) -- debug only, capture still covers the "
        "full ribbon-launchable set unless combined with --skip-capture",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "shots").mkdir(parents=True, exist_ok=True)

    if not args.skip_capture:
        run_capture()
    elif not DOM_EXTRACT_JSON.exists():
        print(
            f"[action-flight] --skip-capture but {DOM_EXTRACT_JSON} does not exist "
            "-- run without --skip-capture at least once first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    ribbon_dom_extracts = load_json(DOM_EXTRACT_JSON)
    extra_dom_extracts = load_extra_dom_extracts()

    catalog = load_catalog()
    dom_by_kind = dom_by_catalog_kind(catalog, ribbon_dom_extracts, extra_dom_extracts)
    if args.only:
        catalog = [
            e for e in catalog if e.launcher_kind == args.only or e.kind == args.only
        ]
        if not catalog:
            raise SystemExit(f"[action-flight] --only {args.only!r} matched no action")

    predictions: dict[str, str] = {}
    judgments: dict[str, dict[str, str]] = {}

    if args.skip_llm:
        for entry in catalog:
            predictions[entry.kind] = "(skipped)"
            judgments[entry.kind] = {"verdict": "skipped", "why": "", "raw": ""}
    else:
        print(
            f"[action-flight] predicting panels for {len(catalog)} actions (Haiku, {LLM_WORKERS} workers)..."
        )
        with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
            futures = {pool.submit(predict_one, entry): entry for entry in catalog}
            for i, future in enumerate(as_completed(futures), 1):
                kind, prediction = future.result()
                predictions[kind] = prediction
                if i % 10 == 0 or i == len(catalog):
                    print(f"[action-flight]   predicted {i}/{len(catalog)}")

        judge_targets = [
            e for e in catalog if dom_by_kind.get(e.kind, {}).get("success")
        ]
        print(
            f"[action-flight] judging dissonance for {len(judge_targets)} captured panels..."
        )
        with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
            futures = {}
            for entry in judge_targets:
                dom = dom_by_kind.get(entry.kind)
                dom_summary = format_dom_summary(dom)
                futures[
                    pool.submit(judge_one, entry, predictions[entry.kind], dom_summary)
                ] = entry
            for i, future in enumerate(as_completed(futures), 1):
                kind, judgment = future.result()
                judgments[kind] = judgment
                if i % 10 == 0 or i == len(judge_targets):
                    print(f"[action-flight]   judged {i}/{len(judge_targets)}")

        for entry in catalog:
            if entry.kind not in judgments:
                if entry.kind not in dom_by_kind:
                    judgments[entry.kind] = {
                        "verdict": "n/a",
                        "why": "No opener reaches this action's panel in this capture pass, so there is no rendered panel to compare against.",
                        "raw": "",
                    }
                else:
                    judgments[entry.kind] = {
                        "verdict": "n/a",
                        "why": "DOM capture failed for this action's opener; see the actual-panel column.",
                        "raw": "",
                    }

    PREDICTIONS_JSON.write_text(json.dumps(predictions, indent=2, sort_keys=True))
    JUDGMENTS_JSON.write_text(json.dumps(judgments, indent=2, sort_keys=True))

    html = render_html(catalog, dom_by_kind, predictions, judgments)
    (OUT_DIR / "index.html").write_text(html)
    print(f"[action-flight] wrote {OUT_DIR / 'index.html'}")


if __name__ == "__main__":
    try:
        main()
    except AnthropicKeyMissing as exc:
        print(f"[action-flight] {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
