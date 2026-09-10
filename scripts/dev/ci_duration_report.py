#!/usr/bin/env python3
"""Report recent GitHub Actions duration percentiles.

This is intentionally a thin operator script around the GitHub CLI. It gives
agents and humans a repeatable way to answer "how long should CI/deploy take?"
from recent workflow/job/step history instead of guessing from memory.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_WORKFLOWS = ("ci", "deploy", "docker")
SUMMARY_SECTIONS = {
    "workflows": "workflow",
    "jobs": "job",
    "steps": "step",
}
HTTP_URL_RE = re.compile(r"https?://[^\s)\"']+", re.IGNORECASE)
BARE_HOST_RE = re.compile(
    r"\b(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?:/[^\s)\"']*)?",
    re.IGNORECASE,
)
BARE_DOMAIN_RE = re.compile(
    r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d{1,5}|/[^\s)\"']*)[^\s)\"']*"
)
SECRET_KEY_PATTERN = (
    r"[A-Za-z0-9_-]*(?:api[_-]?key|auth|authorization|bearer|body|cell|cookie|"
    r"credential|password|prompt|raw|secret|session|token)[A-Za-z0-9_-]*"
)
SECRET_ASSIGNMENT_RE = re.compile(
    (
        rf"((?:\"{SECRET_KEY_PATTERN}\"|'{SECRET_KEY_PATTERN}'|"
        rf"{SECRET_KEY_PATTERN})\s*[:=]\s*)"
        r"(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&)\"']+)"
    ),
    re.IGNORECASE,
)
ENV_ASSIGNMENT_RE = re.compile(
    r"\b[A-Z][A-Z0-9_]{2,}\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&)\"']+)"
)
BEARER_TOKEN_RE = re.compile(
    r"\b(Bearer\s+)[A-Za-z0-9._~+/=_-]{8,}\b",
    re.IGNORECASE,
)
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
FRISKET_PAT_RE = re.compile(r"\bfrisket_pat_[A-Za-z0-9_-]{8,}\b")
OPAQUE_TOKEN_RE = re.compile(
    r"(^|[^A-Za-z0-9_=-])([A-Za-z0-9_=-]{40,})(?=$|[^A-Za-z0-9_=-])"
)


def regression_threshold(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 1.0:
        raise argparse.ArgumentTypeError(
            "must be a finite value greater than or equal to 1.0"
        )
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            "must be a finite value greater than or equal to 0"
        )
    return parsed


def safe_name(value: Any, *, default: str = "unknown", max_length: int = 160) -> str:
    text = str(value or default)
    text = HTTP_URL_RE.sub("[redacted-url]", text)
    text = BARE_HOST_RE.sub("[redacted-host]", text)
    text = BARE_DOMAIN_RE.sub("[redacted-url]", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1[redacted]", text)
    text = ENV_ASSIGNMENT_RE.sub("[redacted-env]", text)
    text = BEARER_TOKEN_RE.sub(r"\1[redacted]", text)
    text = OPENAI_KEY_RE.sub("[redacted-key]", text)
    text = FRISKET_PAT_RE.sub("[redacted-token]", text)
    text = OPAQUE_TOKEN_RE.sub(r"\1[redacted-token]", text)
    text = " ".join(text.split()).strip()
    return (text or default)[:max_length]


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_between(start: Any, end: Any) -> float | None:
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def stats(values: list[float]) -> dict[str, float | int | None]:
    clean = [v for v in values if v >= 0]
    return {
        "count": len(clean),
        "p50": percentile(clean, 0.50),
        "p90": percentile(clean, 0.90),
        "max": max(clean) if clean else None,
    }


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    minutes, seconds = divmod(int(round(value)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def gh_json(args: list[str], *, repo: str | None) -> Any:
    cmd = ["gh", *args]
    if repo:
        cmd.extend(["--repo", repo])
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        safe_detail = safe_name(detail, default="no output", max_length=240)
        raise RuntimeError(f"gh failed (exit {proc.returncode}): {safe_detail}")
    return json.loads(proc.stdout or "null")


def collect_from_gh(
    workflows: list[str], *, limit: int, repo: str | None
) -> list[dict[str, Any]]:
    if shutil.which("gh") is None:
        raise RuntimeError("GitHub CLI `gh` is not installed")
    runs: list[dict[str, Any]] = []
    for workflow in workflows:
        rows = gh_json(
            [
                "run",
                "list",
                "--workflow",
                workflow,
                "--limit",
                str(limit),
                "--json",
                "databaseId,workflowName,status,conclusion,createdAt,updatedAt",
            ],
            repo=repo,
        )
        for row in rows:
            viewed = gh_json(
                ["run", "view", str(row["databaseId"]), "--json", "jobs"],
                repo=repo,
            )
            row["jobs"] = viewed.get("jobs", [])
            runs.append(row)
    return runs


def load_runs(path: Path, *, flag: str = "--from-json") -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get("runs"), list):
        return data["runs"]
    if isinstance(data, dict) and looks_like_run(data):
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError(f"{flag} must be a run object, run list, or {{'runs': [...]}}")


def looks_like_run(data: dict[str, Any]) -> bool:
    run_keys = {"workflowName", "name", "createdAt", "updatedAt", "jobs", "databaseId"}
    return bool(run_keys.intersection(data))


def iter_dicts(value: Any):
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


def summarize(runs: list[dict[str, Any]]) -> dict[str, dict[str, list[float]]]:
    workflow_durations: dict[str, list[float]] = defaultdict(list)
    job_durations: dict[str, list[float]] = defaultdict(list)
    step_durations: dict[str, list[float]] = defaultdict(list)
    for run in iter_dicts(runs):
        workflow = safe_name(run.get("workflowName") or run.get("name"))
        duration = seconds_between(run.get("createdAt"), run.get("updatedAt"))
        if duration is not None:
            workflow_durations[workflow].append(duration)
        for job in iter_dicts(run.get("jobs")):
            job_name = safe_name(job.get("name"))
            duration = seconds_between(job.get("startedAt"), job.get("completedAt"))
            if duration is not None:
                job_durations[f"{workflow} / {job_name}"].append(duration)
            for step in iter_dicts(job.get("steps")):
                step_name = safe_name(step.get("name"))
                duration = seconds_between(
                    step.get("startedAt"), step.get("completedAt")
                )
                if duration is not None:
                    step_durations[f"{workflow} / {job_name} / {step_name}"].append(
                        duration
                    )
    return {
        "workflows": dict(workflow_durations),
        "jobs": dict(job_durations),
        "steps": dict(step_durations),
    }


def summarize_stats(
    summary: dict[str, dict[str, list[float]]],
) -> dict[str, dict[str, dict[str, float | int | None]]]:
    return {
        section: {name: stats(values) for name, values in values_by_name.items()}
        for section, values_by_name in summary.items()
    }


def max_duration(values: list[float]) -> float | None:
    clean = [value for value in values if value >= 0]
    if not clean:
        return None
    return max(clean)


def find_regressions(
    baseline_summary: dict[str, dict[str, list[float]]],
    current_summary: dict[str, dict[str, list[float]]],
    *,
    baseline_metric: str,
    threshold: float,
    min_delta_seconds: float,
) -> list[dict[str, str | float]]:
    baseline_stats = summarize_stats(baseline_summary)
    regressions: list[dict[str, str | float]] = []
    for section, section_label in SUMMARY_SECTIONS.items():
        for name, current_values in sorted(current_summary.get(section, {}).items()):
            current_seconds = max_duration(current_values)
            if current_seconds is None:
                continue
            baseline_seconds = (
                baseline_stats.get(section, {}).get(name, {}).get(baseline_metric)
            )
            if not isinstance(baseline_seconds, int | float) or baseline_seconds <= 0:
                continue
            delta_seconds = current_seconds - baseline_seconds
            ratio = current_seconds / baseline_seconds
            if ratio < threshold or delta_seconds < min_delta_seconds:
                continue
            regressions.append(
                {
                    "baseline": baseline_metric,
                    "baseline_seconds": float(baseline_seconds),
                    "current_seconds": float(current_seconds),
                    "delta_seconds": float(delta_seconds),
                    "name": name,
                    "ratio": round(ratio, 2),
                    "section": section_label,
                    "threshold": threshold,
                }
            )
    return regressions


def print_section(title: str, values_by_name: dict[str, list[float]]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if not values_by_name:
        print("no timing data")
        return
    for name, values in sorted(values_by_name.items()):
        s = stats(values)
        print(
            f"{name}: n={s['count']} p50={fmt_seconds(s['p50'])} "
            f"p90={fmt_seconds(s['p90'])} max={fmt_seconds(s['max'])}"
        )


def print_regressions(regressions: list[dict[str, str | float]]) -> None:
    title = "Potential regressions"
    print(f"\n{title}")
    print("-" * len(title))
    if not regressions:
        print("no regressions over threshold")
        return
    for row in regressions:
        baseline = str(row["baseline"])
        print(
            f"REGRESSION {row['section']} {row['name']}: "
            f"current={fmt_seconds(float(row['current_seconds']))} "
            f"{baseline}={fmt_seconds(float(row['baseline_seconds']))} "
            f"delta={fmt_seconds(float(row['delta_seconds']))} "
            f"ratio={float(row['ratio']):.2f}x "
            f"threshold={float(row['threshold']):.2f}x"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow",
        action="append",
        dest="workflows",
        help="Workflow name or file to include; repeatable. Defaults to ci/deploy/docker.",
    )
    parser.add_argument("--limit", type=int, default=20, help="Runs per workflow")
    parser.add_argument("--repo", help="GitHub repo, e.g. owner/name")
    parser.add_argument(
        "--from-json",
        type=Path,
        help="Read captured run JSON instead of calling gh",
    )
    parser.add_argument(
        "--current-from-json",
        type=Path,
        help=(
            "Read a current captured run JSON/list and compare it to the "
            "baseline summary. Regression findings are warning-only."
        ),
    )
    parser.add_argument(
        "--regression-baseline",
        choices=("p50", "p90", "max"),
        default="p90",
        help="Baseline percentile/stat to compare current durations against.",
    )
    parser.add_argument(
        "--regression-threshold",
        type=regression_threshold,
        default=1.25,
        help="Flag current durations at or above this multiple of the baseline.",
    )
    parser.add_argument(
        "--min-regression-seconds",
        type=nonnegative_float,
        default=30.0,
        help="Require at least this many seconds over baseline before flagging.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable summary instead of text",
    )
    args = parser.parse_args(argv)

    try:
        runs = (
            load_runs(args.from_json)
            if args.from_json
            else collect_from_gh(
                args.workflows or list(DEFAULT_WORKFLOWS),
                limit=args.limit,
                repo=args.repo,
            )
        )
        current_runs = (
            load_runs(args.current_from_json, flag="--current-from-json")
            if args.current_from_json
            else []
        )
    except Exception as exc:  # noqa: BLE001 - operator-facing CLI
        print(f"ci-duration-report: {exc}", file=sys.stderr)
        print(
            "Install/authenticate `gh`, or pass --from-json for a captured "
            "baseline and --current-from-json for the current run.",
            file=sys.stderr,
        )
        return 2

    summary = summarize(runs)
    compare_current = args.current_from_json is not None
    current_summary = summarize(current_runs) if compare_current else {}
    regressions = (
        find_regressions(
            summary,
            current_summary,
            baseline_metric=args.regression_baseline,
            threshold=args.regression_threshold,
            min_delta_seconds=args.min_regression_seconds,
        )
        if compare_current
        else []
    )
    if args.json:
        payload = summarize_stats(summary)
        if compare_current:
            payload["regressions"] = regressions
        print(
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"CI duration report from {len(runs)} workflow run(s)")
        print_section("Workflow durations", summary["workflows"])
        print_section("Job durations", summary["jobs"])
        print_section("Step durations", summary["steps"])
        if compare_current:
            print_regressions(regressions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
