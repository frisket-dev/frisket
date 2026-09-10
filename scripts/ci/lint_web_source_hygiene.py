#!/usr/bin/env python3
"""Guard the web raw-cost rendering and palette/font source invariants."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
WEB = REPO / "web"
SRC = WEB / "src"

RAW_ESTIMATE_COST = re.compile(
    r"[A-Za-z_$][A-Za-z_$0-9]*[Ee]stimate[A-Za-z_$0-9]*\??\.cost\b"
    r"|\bestimate\??\.cost\b"
)
WIRE_COST_PROJECTION = re.compile(r"^\s*cost: estimate\.cost,$")
NATIVE_SELECT = re.compile(r"<select\b")
NATIVE_SELECT_ESCAPE = re.compile(r'data-native-select-escape="([^"]+)"')

# PanelSelect deliberately retains a native select as its semantic/form trigger.
# Two older rich controls retain hidden mirrors for selectOption()/toHaveValue()
# compatibility. Keeping the exact file + reason pairs here makes an exception a
# reviewed design-system decision rather than a copyable inline suppression.
NATIVE_SELECT_ESCAPES = {
    "src/components/EnginePicker.tsx": "engine-picker-mirror",
    "src/components/PanelPrimitives.tsx": "segmented-toggle-mirror",
    "src/components/PanelSelect.tsx": "panel-select-trigger",
}

LIGHT_TOKENS = {
    "--bg": "#ffffff",
    "--accent": "#4b53d9",
    "--accent-bg": "#ecedfb",
    "--ai": "#7c5ce6",
    "--text": "#211f1b",
    "--canvas": "#e9e5dd",
    "--act": "#c53e6b",
    "--discover": "#0e9384",
    "--monitor": "#c07d1f",
    "--focus": "#7c5ce6",
}
DARK_TOKENS = {
    "--bg": "#1d1b17",
    "--text": "#ece9e2",
}
DARK_REQUIRED = (
    "--accent",
    "--ai",
    "--act",
    "--discover",
    "--monitor",
    "--focus",
    "--canvas",
)


def token_in_block(source: str, selector: str, name: str) -> str | None:
    start = source.find(selector)
    if start < 0:
        return None
    open_brace = source.find("{", start)
    close_brace = source.find("}", open_brace)
    if open_brace < 0 or close_brace < 0:
        return None
    block = source[open_brace + 1 : close_brace]
    match = re.search(rf"^\s*{re.escape(name)}\s*:\s*([^;]+);", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def raw_cost_failures() -> list[str]:
    failures: list[str] = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in {".ts", ".tsx"} or re.search(r"\.test\.tsx?$", path.name):
            continue
        rel = path.relative_to(WEB).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not RAW_ESTIMATE_COST.search(line):
                continue
            if rel == "src/actions/quotedCost.ts":
                continue
            if rel == "src/api/actionEstimateValidation.ts" and WIRE_COST_PROJECTION.match(line):
                continue
            failures.append(
                f"{path.relative_to(REPO)}:{lineno}: a surface reads an estimate "
                "cost directly instead of using actions/quotedCost: "
                f"{line.strip()}"
            )
    return failures


def native_select_failures() -> list[str]:
    failures: list[str] = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in {".ts", ".tsx"} or re.search(r"\.test\.tsx?$", path.name):
            continue
        rel = path.relative_to(WEB).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not NATIVE_SELECT.search(line):
                continue
            match = NATIVE_SELECT_ESCAPE.search(line)
            reason = match.group(1) if match else None
            allowed_reason = NATIVE_SELECT_ESCAPES.get(rel)
            if reason == allowed_reason:
                continue
            escape_help = (
                f"; unrecognized escape {reason!r}"
                if reason is not None
                else ""
            )
            failures.append(
                f"{path.relative_to(REPO)}:{lineno}: native <select> is forbidden; "
                "use components/PanelSelect. Hidden mirrors require an explicit "
                "reviewed entry in NATIVE_SELECT_ESCAPES"
                f"{escape_help}: {line.strip()}"
            )
    return failures


def token_failures() -> list[str]:
    styles_path = SRC / "styles.css"
    mount_path = SRC / "entries" / "mount.tsx"
    styles = styles_path.read_text(encoding="utf-8")
    mount = mount_path.read_text(encoding="utf-8")
    failures: list[str] = []

    for name, expected in LIGHT_TOKENS.items():
        actual = token_in_block(styles, ":root {", name)
        if actual != expected:
            failures.append(
                f"{styles_path.relative_to(REPO)}: light {name} must be "
                f"{expected}, found {actual!r}"
            )

    dark_selector = ':root[data-frisket-theme="dark"]'
    for name, expected in DARK_TOKENS.items():
        actual = token_in_block(styles, dark_selector, name)
        if actual != expected:
            failures.append(
                f"{styles_path.relative_to(REPO)}: dark {name} must be "
                f"{expected}, found {actual!r}"
            )
    for name in DARK_REQUIRED:
        if not token_in_block(styles, dark_selector, name):
            failures.append(
                f"{styles_path.relative_to(REPO)}: dark theme must define {name}"
            )

    font = token_in_block(styles, ":root {", "--font") or ""
    if not font.startswith("'IBM Plex Sans'"):
        failures.append(
            f"{styles_path.relative_to(REPO)}: --font must start with "
            f"'IBM Plex Sans', found {font!r}"
        )
    for package in ("@fontsource/ibm-plex-sans", "@fontsource/ibm-plex-mono"):
        if package not in mount:
            failures.append(
                f"{mount_path.relative_to(REPO)}: missing self-hosted import for {package}"
            )
    google_fonts = re.compile(r"fonts\.googleapis\.com|fonts\.gstatic\.com")
    for path, source in ((styles_path, styles), (mount_path, mount)):
        if google_fonts.search(source):
            failures.append(
                f"{path.relative_to(REPO)}: Google Fonts references are forbidden"
            )
    return failures


def check_raw_cost_regex_non_vacuous() -> str | None:
    """Guard against a silently weakened RAW_ESTIMATE_COST regex."""
    positives = [
        "formatUsd(costGate.estimate.cost)",
        "v1Estimate?.cost ?? (knownZero ? 0 : null)",
        "estimate.cost === null ? UNKNOWN : ''",
    ]
    negative = "const q = quotedCost(estimate);"
    if not all(RAW_ESTIMATE_COST.search(line) for line in positives):
        return "RAW_ESTIMATE_COST no longer matches a known-broken fixture shape"
    if RAW_ESTIMATE_COST.search(negative):
        return "RAW_ESTIMATE_COST now matches the safe quotedCost negative"
    return None


def check_native_select_regex_non_vacuous() -> str | None:
    if not NATIVE_SELECT.search("  <select value={value}>"):
        return "NATIVE_SELECT no longer matches a native JSX select"
    if NATIVE_SELECT.search("  <PanelSelect value={value}>"):
        return "NATIVE_SELECT now matches the design-system PanelSelect"
    escape = '<select data-native-select-escape="panel-select-trigger"'
    match = NATIVE_SELECT_ESCAPE.search(escape)
    if match is None or match.group(1) != "panel-select-trigger":
        return "NATIVE_SELECT_ESCAPE no longer reads a known escape reason"
    return None


def main() -> int:
    self_check_failures = [
        failure
        for failure in (
            check_raw_cost_regex_non_vacuous(),
            check_native_select_regex_non_vacuous(),
        )
        if failure is not None
    ]
    if self_check_failures:
        print(
            "web source hygiene lint self-check failed: "
            + "; ".join(self_check_failures),
            file=sys.stderr,
        )
        return 1
    failures = raw_cost_failures() + native_select_failures() + token_failures()
    if failures:
        print("web source hygiene lint failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
