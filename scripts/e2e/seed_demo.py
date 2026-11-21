"""Seed demo projects into a frisket server (local or hosted) via its API.

Usage:
    uv run python scripts/e2e/seed_demo.py http://localhost:8000 [session_cookie]
    uv run python scripts/e2e/seed_demo.py http://localhost:8000 --keyless <workspace>
    ... [--status-file <path>] [--strict]

Creates each of these that the server does not already hold:
  1. "Local stories" — classify demo (beats + justification)
  2. "Council audio" — packaged CC0 council excerpt → local Whisper base
     transcription → summarize
  3. "Tariff impacts" — countries; web_search → summarize → score chain
  4. "People mentioned" — fixture rows → derived People sheet, model-free
Model and search calls are REAL (the server's keys); total cost well under
$0.25. Transcription is local and needs the pinned Whisper base model in the
Hugging Face cache (scripts/e2e/fetch_whisper_model.py, or the in-app setup);
the keyless build does not transcribe.

``--keyless`` makes no provider call. Every model reply is a fixture
(STORIES_REPLIES, AUDIO_SUMMARY_REPLIES, TARIFF_SUMMARY_REPLIES,
TARIFF_SCORE_REPLIES) stored in the project's response cache under the keys
the run will look up (prime_replies.py) and replayed with the model routed to
the project's own key, so the runs succeed under
FRISKET_CACHE_MODE=replay_strict with no key at all. Web search has no
replay, so the keyless "Tariff impacts" imports TARIFF_SEARCH_RESULTS as its
search_results column instead of running research.web_search, and the
keyless "Council audio" writes the fixture AUDIO_TRANSCRIPT instead of
transcribing, so it needs no speech model.
<workspace> is the server's workspace directory.

Each project is built on its own: a run that ends with failed rows leaves its
project partial (the run records which rows failed), and a project that
cannot be built is missing; neither stops the others. The seed ends by
printing which projects are complete, partial, missing or already present,
and ``--status-file`` writes the same as JSON
({name: {"state": ..., "detail": ...}}). It exits 0 unless ``--strict`` is
given and a project is partial or missing.
"""

from __future__ import annotations

import argparse
import io
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from frisket.sample_content.audio import SAMPLE_COUNCIL_AUDIO, council_audio_bytes

STORIES_CSV = """snippet
"The mayor's office quietly awarded a $4M paving contract to his brother-in-law's firm without competitive bidding."
"Researchers announced the city's new light rail line carried two million riders in its first quarter, beating projections."
"County health inspectors found listeria at three packaged-salad facilities but the recall was delayed nine days."
"The school board voted 5-2 to adopt the new math curriculum after a heated public comment session."
"Leaked emails show the police chief ordered officers to stop logging use-of-force incidents in the public database."
"A new study finds the regional bus network's on-time rate fell to 61 percent after schedule cuts."
"State auditors flagged $1.2M in untracked overtime at the county jail."
"The city's three remaining pharmacies in the northern district will close by March."
"""

COUNTRIES = ["Canada", "Mexico", "China", "Germany", "Vietnam", "Brazil"]
COUNTRIES_CSV = "country\n" + "\n".join(COUNTRIES)

COUNCIL_AUDIO_PROJECT_NAME = "Council audio"
COUNCIL_AUDIO_SAMPLE = SAMPLE_COUNCIL_AUDIO[0]

MODEL = "gemini/gemini-3.5-flash-lite"

STORIES_CLASSIFY = {
    "model": MODEL,
    "engine": "llm",
    "source": ["snippet"],
    "context": "Each row is a one-sentence local news story summary.",
    "fields": [
        {
            "name": "beat",
            "type": "category",
            "labels": [
                "corruption",
                "transit",
                "public_health",
                "education",
                "other",
            ],
            "description": "Which news beat does this story belong to?",
        },
        {
            "name": "newsworthiness",
            "type": "score",
            "description": "0 = routine, 10 = drop-everything story",
        },
    ],
    "include_justification": True,
    "include_confidence": True,
}


# Fixture data for the keyless build; it stands in for provider output and
# measures nothing. The keyless "Council audio" writes AUDIO_TRANSCRIPT with
# map.python instead of transcribing, so no speech model runs; it and
# AUDIO_SUMMARY_REPLIES are for the one imported file.
AUDIO_TRANSCRIPT = (
    "Placeholder transcript standing in for the council committee excerpt."
)
AUDIO_SUMMARY_REPLIES = [
    {"summary": "A city council committee discusses items on its agenda."}
]
TARIFF_SEARCH_RESULTS = {
    country: [
        {
            "title": f"Fixture search result {n} on US tariffs and {country}",
            "url": f"https://example.org/fixture/{country.lower()}/{n}",
            "snippet": f"Placeholder text standing in for a search result about {country}.",
        }
        for n in (1, 2)
    ]
    for country in COUNTRIES
}
TARIFF_SUMMARY_REPLIES = [
    {"summary": f"Placeholder summary of US tariff impacts on {country}."}
    for country in COUNTRIES
]
TARIFF_SCORE_REPLIES = [
    {
        "tariff_impact": score,
        "tariff_impact_justification": f"Placeholder score for {country}.",
    }
    for country, score in zip(COUNTRIES, (7, 8, 9, 4, 6, 5), strict=True)
]


def _story_reply(beat: str, why: str, score: int, weight: str, confidence: float):
    return {
        "beat": beat,
        "beat_justification": why,
        "newsworthiness": score,
        "newsworthiness_justification": weight,
        "beat_confidence": confidence,
    }


# Fixture answers for the keyless "Local stories", one per STORIES_CSV row in
# order; they stand in for a model's classification and measure nothing.
STORIES_REPLIES = [
    _story_reply(
        "corruption",
        "A no-bid contract went to a relative of the mayor.",
        9,
        "Self-dealing by an elected official.",
        0.95,
    ),
    _story_reply(
        "transit",
        "Ridership figures for the new light rail line.",
        4,
        "Good news, but routine.",
        0.9,
    ),
    _story_reply(
        "public_health",
        "A delayed recall of contaminated food.",
        8,
        "An ongoing risk to the public.",
        0.9,
    ),
    _story_reply(
        "education",
        "A school board vote on the math curriculum.",
        4,
        "A scheduled decision.",
        0.85,
    ),
    _story_reply(
        "corruption",
        "The police chief suppressed public use-of-force records.",
        10,
        "Concealment of official misconduct.",
        0.9,
    ),
    _story_reply(
        "transit",
        "Bus punctuality fell after schedule cuts.",
        6,
        "A measurable service decline.",
        0.9,
    ),
    _story_reply(
        "corruption",
        "Auditors found untracked overtime spending.",
        7,
        "Unaccounted public money.",
        0.8,
    ),
    _story_reply(
        "public_health",
        "Pharmacy closures leave a district without access.",
        6,
        "A gap in local health services.",
        0.75,
    ),
]


def action_key(kind: str, params: dict, row_scope: dict | None = None) -> str:
    identity = {"kind": kind, "params": params}
    if row_scope is not None:
        identity["row_scope"] = row_scope
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"seed-demo-{kind}@sha256:{digest}"


def typed_action_request(
    action_id: str,
    *,
    sheet_id: int | None = None,
    params: dict,
    output_names: dict[str, str] | None = None,
    sheet_name: str | None = None,
) -> dict:
    scope = (
        {"kind": "sheet_rows", "sheet_id": sheet_id}
        if sheet_id is not None
        else {"kind": "project"}
    )
    placement = {"output_names": dict(output_names or {})}
    if sheet_name is not None:
        placement["sheet_name"] = sheet_name
    return {
        "action_id": action_id,
        "scope": scope,
        "params": dict(params),
        **placement,
        "idempotency_key": action_key(
            action_id, {"params": params, **placement}, scope
        ),
    }


class RunIncomplete(RuntimeError):
    """A run ended unsuccessfully or with failed rows; the run records which."""


def wait_run(client: httpx.Client, pid: str, run_id: int, label: str) -> None:
    for _ in range(600):
        response = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
        response.raise_for_status()
        body = response.json()
        s = body.get("run", {}).get("public_status", body)
        if s["status"] not in {"queued", "running"} and not s["live"]:
            cost = "cost unknown" if s["cost"] is None else f"${s['cost']:.4f}"
            summary = f"{s['status']} ({s['completed']}/{s['total']}, {cost})"
            if s["status"] != "completed" or s["failed"]:
                context = ([("failed_rows", s["failed"])] if s["failed"] else []) + [
                    (name, s.get(name))
                    for name in (
                        "error",
                        "stalled_reason",
                        "halted_code",
                        "halted_reason",
                    )
                    if s.get(name) not in {None, ""}
                ]
                queue = s.get("queue")
                if isinstance(queue, dict) and queue.get("error") not in {None, ""}:
                    context.append(("queue_error", queue["error"]))
                details = "; ".join(f"{name}={value}" for name, value in context)
                suffix = f"; {details}" if details else ""
                raise RunIncomplete(
                    f"{label}: run {run_id} ended unsuccessfully: {summary}{suffix}"
                )
            print(f"  {label}: {summary}")
            return
        time.sleep(1)
    raise TimeoutError(label)


def confirmation_hash(response: httpx.Response) -> str:
    """Read the exact consent token from a v1 action confirmation response."""

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "malformed HTTP 402 needs_confirmation response: expected JSON object"
        ) from exc
    if not isinstance(body, dict) or body.get("status") != "needs_confirmation":
        raise RuntimeError(
            "malformed HTTP 402 needs_confirmation response: "
            "expected status='needs_confirmation'"
        )
    errors = body.get("errors")
    first = errors[0] if isinstance(errors, list) and errors else None
    details = first.get("details") if isinstance(first, dict) else None
    promise_set_hash = (
        details.get("promise_set_hash") if isinstance(details, dict) else None
    )
    if not isinstance(promise_set_hash, str) or not promise_set_hash:
        raise RuntimeError(
            "malformed HTTP 402 needs_confirmation response: "
            "expected nonempty errors[0].details.promise_set_hash"
        )
    return promise_set_hash


def run(client: httpx.Client, pid: str, spec: dict, label: str) -> int | None:
    endpoint = f"/api/projects/{pid}/actions/v1/run"
    action = dict(spec)
    r = client.post(endpoint, json=action)
    if r.status_code == 402:
        promise_set_hash = confirmation_hash(r)
        action = {**action, "confirmation": promise_set_hash}
        r = client.post(endpoint, json=action)
    r.raise_for_status()
    result = r.json()
    raw_run_id = result.get("run_id")
    if raw_run_id is None:
        print(f"  {label}: {result['status']}")
        return None
    run_id = int(raw_run_id)
    wait_run(client, pid, run_id, label)
    return run_id


def create_project(client: httpx.Client, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    response.raise_for_status()
    return response.json()["id"]


def import_sheet(client: httpx.Client, pid: str, kind: str, files: dict) -> int:
    """Import ``files`` through ``/import/<kind>``; return the new sheet's id."""
    response = client.post(f"/api/projects/{pid}/import/{kind}", files=files)
    response.raise_for_status()
    return response.json()["sheet_id"]


def prime_replies(workspace: Path, pid: str, action: dict, replies: list[dict]) -> None:
    """Store fixture replies for ``action`` in the project's response cache."""
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("prime_replies.py")),
            str(workspace),
            pid,
        ],
        input=json.dumps({"action": action, "replies": replies}),
        text=True,
        check=True,
    )


def route_model_to_own_key(client: httpx.Client, pid: str) -> None:
    """Route MODEL to the project's own key so primed replies replay keyless."""
    client.patch(
        f"/api/projects/{pid}/model-routes",
        json={"model_id": MODEL, "route": "own"},
    ).raise_for_status()


def run_model_action(
    client: httpx.Client,
    pid: str,
    action: dict,
    label: str,
    keyless_workspace: Path | None,
    replies: list[dict],
) -> None:
    """Run a model action; keyless, its replies come from ``replies``."""
    if keyless_workspace is not None:
        prime_replies(keyless_workspace, pid, action, replies)
    run(client, pid, action, label)


def seed_stories(client: httpx.Client, keyless_workspace: Path | None = None) -> None:
    pid = create_project(client, "Local stories")
    sheet = import_sheet(
        client, pid, "csv", {"file": ("stories.csv", STORIES_CSV, "text/csv")}
    )
    action = typed_action_request(
        "map.classify", sheet_id=sheet, params=STORIES_CLASSIFY
    )
    if keyless_workspace is not None:
        route_model_to_own_key(client, pid)
    run_model_action(
        client, pid, action, "classify stories", keyless_workspace, STORIES_REPLIES
    )
    print(f"✓ Local stories → {pid}")


def import_fixture_search_results(client: httpx.Client, pid: str) -> int:
    """The keyless stand-in for research.web_search: countries with fixture
    search_results; returns the sheet's id."""
    run(
        client,
        pid,
        typed_action_request(
            "import.rows",
            sheet_name="countries",
            params={
                "columns": [
                    {"name": "country", "type": "text"},
                    {"name": "search_results", "type": "json"},
                ],
                "rows": [
                    {"country": country, "search_results": results}
                    for country, results in TARIFF_SEARCH_RESULTS.items()
                ],
                "source": {"kind": "inline", "label": "seed_demo search results"},
            },
        ),
        "import fixture search results",
    )
    response = client.get(f"/api/projects/{pid}/sheets")
    response.raise_for_status()
    return next(s["id"] for s in response.json() if s["name"] == "countries")


def seed_tariff(client: httpx.Client, keyless_workspace: Path | None = None) -> None:
    pid = create_project(client, "Tariff impacts")
    if keyless_workspace is not None:
        route_model_to_own_key(client, pid)
        sheet = import_fixture_search_results(client, pid)
    else:
        sheet = import_sheet(
            client, pid, "csv", {"file": ("countries.csv", COUNTRIES_CSV, "text/csv")}
        )
        run(
            client,
            pid,
            typed_action_request(
                "research.web_search",
                sheet_id=sheet,
                params={
                    "query": {"text": "US tariff impacts on {{country}} economy 2026"},
                    "model": MODEL,
                    "max_results": 8,
                },
                output_names={"search_results": "search_results"},
            ),
            "web search",
        )
    run_model_action(
        client,
        pid,
        typed_action_request(
            "map.summarize",
            sheet_id=sheet,
            params={
                "model": MODEL,
                "source": ["country", "search_results"],
                "instruction": "Summarize what these search results say about US "
                "tariff impacts on this country, one paragraph.",
            },
            output_names={"summary": "summary"},
        ),
        "summarize",
        keyless_workspace,
        TARIFF_SUMMARY_REPLIES,
    )
    run_model_action(
        client,
        pid,
        typed_action_request(
            "map.classify",
            sheet_id=sheet,
            params={
                "model": MODEL,
                "engine": "llm",
                "source": ["country", "summary"],
                "context": "Each row is a country and a summary of US tariff impacts.",
                "fields": [
                    {
                        "name": "tariff_impact",
                        "type": "score",
                        "description": "0 = none, 10 = severe economic impact",
                    }
                ],
                "include_justification": True,
            },
        ),
        "score impacts",
        keyless_workspace,
        TARIFF_SCORE_REPLIES,
    )
    print(f"✓ Tariff impacts → {pid}")


def seed_audio(client: httpx.Client, keyless_workspace: Path | None = None) -> None:
    pid = create_project(client, COUNCIL_AUDIO_PROJECT_NAME)
    if keyless_workspace is not None:
        route_model_to_own_key(client, pid)
    audio = council_audio_bytes(COUNCIL_AUDIO_SAMPLE.filename)
    sheet = import_sheet(
        client,
        pid,
        "files",
        {
            "files": (
                COUNCIL_AUDIO_SAMPLE.filename,
                io.BytesIO(audio),
                "audio/mpeg",
            )
        },
    )
    if keyless_workspace is not None:
        transcribe = typed_action_request(
            "map.python",
            sheet_id=sheet,
            params={
                "input_columns": ["media"],
                "code": f"result = {AUDIO_TRANSCRIPT!r}",
                "return_schema": {"type": "string"},
                "output_routes": [
                    {
                        "name": "transcript",
                        "path": "$",
                        "target": {"kind": "column", "type": "text"},
                    }
                ],
            },
            output_names={"transcript": "transcript"},
        )
    else:
        transcribe = typed_action_request(
            "media.transcribe",
            sheet_id=sheet,
            params={
                "source": "media",
                "engine": "faster_whisper",
                "model_size": "base",
            },
            output_names={
                "text": "transcript",
                "segments": "transcript_segments",
                "detected_language": "detected_language",
            },
        )
    run(client, pid, transcribe, "transcribe")
    run_model_action(
        client,
        pid,
        typed_action_request(
            "map.summarize",
            sheet_id=sheet,
            params={
                "model": MODEL,
                "source": ["transcript"],
                "preset": "one_line",
            },
            output_names={"summary": "one_line_summary"},
        ),
        "summarize transcript",
        keyless_workspace,
        AUDIO_SUMMARY_REPLIES,
    )
    print(f"✓ {COUNCIL_AUDIO_PROJECT_NAME} → {pid}")


# Failures that end one project's build; the next project is still built.
SEED_FAILURES = (
    httpx.HTTPError,
    RuntimeError,
    TimeoutError,
    subprocess.CalledProcessError,
    KeyError,
    StopIteration,
    ValueError,
)

OUTCOME_STATES = ("complete", "partial", "missing", "present")


def project_names(client: httpx.Client) -> set[str]:
    response = client.get("/api/projects")
    response.raise_for_status()
    return {project["name"] for project in response.json()}


def seed_one(
    client: httpx.Client, name: str, seed: Callable[[httpx.Client], None]
) -> dict:
    """Build one project; return its outcome, ``{"state", "detail"}``."""
    try:
        seed(client)
    except SEED_FAILURES as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"✗ {name}: {detail}")
        try:
            built = name in project_names(client)
        except httpx.HTTPError:
            built = False
        return {"state": "partial" if built else "missing", "detail": detail}
    return {"state": "complete", "detail": None}


def report(outcomes: dict[str, dict]) -> None:
    for state in OUTCOME_STATES:
        names = [
            name for name, outcome in outcomes.items() if outcome["state"] == state
        ]
        if names:
            print(f"{state}: {', '.join(names)}")
            for name in names:
                if outcomes[name]["detail"]:
                    print(f"  {name}: {outcomes[name]['detail']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", nargs="?", default="http://localhost:8000")
    parser.add_argument("session_cookie", nargs="?")
    parser.add_argument("basic_auth", nargs="?", help='"user:pass" for the Caddy fence')
    parser.add_argument("--keyless", type=Path, metavar="WORKSPACE")
    parser.add_argument(
        "--status-file",
        type=Path,
        help="write each project's outcome here as JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when a project is partial or missing",
    )
    args = parser.parse_args()
    cookies = {}
    if args.session_cookie:
        cookies["frisket_session"] = args.session_cookie
    auth = None
    if args.basic_auth:
        u, _, pw = args.basic_auth.partition(":")
        auth = httpx.BasicAuth(u, pw)
    seeders: dict[str, Callable[[httpx.Client], None]] = {
        "Local stories": lambda client: seed_stories(client, args.keyless),
        COUNCIL_AUDIO_PROJECT_NAME: lambda client: seed_audio(client, args.keyless),
        "Tariff impacts": lambda client: seed_tariff(client, args.keyless),
        "People mentioned": seed_people_derive,
    }
    outcomes: dict[str, dict] = {}
    with httpx.Client(
        base_url=args.base, cookies=cookies, timeout=180, auth=auth
    ) as client:
        health = client.get("/api/health").json()
        print(f"server OK: {health}")
        present = project_names(client)
        for name, seed in seeders.items():
            if name in present:
                outcomes[name] = {"state": "present", "detail": None}
                continue
            outcomes[name] = seed_one(client, name, seed)
    report(outcomes)
    if args.status_file is not None:
        args.status_file.write_text(json.dumps(outcomes, indent=1) + "\n")
    incomplete = any(o["state"] in {"partial", "missing"} for o in outcomes.values())
    if args.strict and incomplete:
        raise SystemExit(1)


PORTRAITS = [
    (
        "Abraham Lincoln (1865, Gardner)",
        "https://tile.loc.gov/storage-services/service/pnp/cwpb/04300/04326v.jpg",
    ),
    (
        "Frederick Douglass (c. 1879)",
        "https://tile.loc.gov/storage-services/service/pnp/cwpbh/03800/03898v.jpg",
    ),
    (
        "Susan B. Anthony (c. 1855)",
        "https://tile.loc.gov/storage-services/service/pnp/cph/3a00000/3a02000/3a02500/3a02558v.jpg",
    ),
]


def seed_people_derive(client: httpx.Client) -> None:
    """derive + lineage chips: stories (with a people json column) → People
    child sheet. Uses import.rows + derive.table_from_list's column source —
    the whole fixture is deterministic and model-free (the People lists are
    fixture data, not a measured classification)."""
    pid = create_project(client, "People mentioned")
    stories = [
        (
            "Mayor Linda Reyes met lobbyist Tom Quayle at the Capital Grille "
            "to discuss the stalled paving contract.",
            [
                {"name": "Linda Reyes", "role": "Mayor"},
                {"name": "Tom Quayle", "role": "lobbyist"},
            ],
        ),
        (
            "Council member Joe Park denied knowing developer Sandra Ochoa, "
            "despite three campaign donations.",
            [
                {"name": "Joe Park", "role": "Council member"},
                {"name": "Sandra Ochoa", "role": "developer"},
            ],
        ),
        (
            "Schools chief Maria Tran hired her former business partner "
            "Derek Holt as a $200k consultant.",
            [
                {"name": "Maria Tran", "role": "Schools chief"},
                {"name": "Derek Holt", "role": "consultant"},
            ],
        ),
    ]
    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=typed_action_request(
            "import.rows",
            sheet_name="stories",
            params={
                "columns": [
                    {"name": "story", "type": "text"},
                    {"name": "people", "type": "json"},
                ],
                "rows": [
                    {"story": story, "people": people} for story, people in stories
                ],
                "source": {"kind": "inline", "label": "seed_demo stories"},
            },
        ),
    )
    r.raise_for_status()
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    sheet = next(s["id"] for s in sheets if s["name"] == "stories")
    data = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data?offset=0&limit=0"
    ).json()
    people_col = next(c["id"] for c in data["columns"] if c["name"] == "people")
    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=typed_action_request(
            "derive.table_from_list",
            sheet_name="People",
            params={
                "source": {
                    "kind": "column",
                    "sheet_id": sheet,
                    "column_id": people_col,
                },
            },
        ),
    )
    r.raise_for_status()
    print(f"✓ People mentioned (derive + lineage) → {pid}")


def seed_feature_tour(client: httpx.Client) -> None:
    """One project, every column type of work: computed, judge, reduce."""
    pid = client.post("/api/projects", json={"name": "Feature tour"}).json()["id"]
    csv = """item,note
"Paving contract","Award of $4,200,000 went to Quayle Bros LLC on 2025-03-04"
"Jail overtime","Auditors flagged $1.2M in untracked overtime, fiscal year 2024"
"Bus shelters","The $380,000 shelter program finished under budget"
"Stadium study","Consultants billed $96,500 for a feasibility study"
"""
    sheet = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("ledger.csv", csv, "text/csv")},
    ).json()["sheet_id"]
    run(
        client,
        pid,
        typed_action_request(
            "map.regex_extract",
            sheet_id=sheet,
            params={
                "input_columns": ["note"],
                "pattern": r"\\$[\\d,.]+[MK]?",
            },
            output_names={"extracted": "amount_text"},
        ),
        "regex_extract (computed)",
    )
    run(
        client,
        pid,
        typed_action_request(
            "map.python",
            sheet_id=sheet,
            params={
                "input_columns": ["amount_text"],
                "code": (
                    "t = (row.get('amount_text') or '').replace('$','').replace(',','')\n"
                    "mult = 1_000_000 if t.endswith('M') else 1_000 if t.endswith('K') else 1\n"
                    "t = t.rstrip('MK')\n"
                    "result = float(t) * mult if t else None"
                ),
                "return_schema": {"type": "number"},
                "output_routes": [
                    {
                        "name": "amount_usd",
                        "path": "$",
                        "target": {
                            "kind": "column",
                            "type": "number",
                        },
                    }
                ],
            },
            output_names={"amount_usd": "amount_usd"},
        ),
        "python (computed)",
    )
    run(
        client,
        pid,
        typed_action_request(
            "map.classify",
            sheet_id=sheet,
            params={
                "model": MODEL,
                "engine": "llm",
                "source": ["item", "note"],
                "context": "City spending ledger lines.",
                "fields": [
                    {
                        "name": "follow_up",
                        "type": "category",
                        "labels": ["investigate", "routine"],
                        "description": "Does this spending line deserve scrutiny?",
                    }
                ],
                "include_justification": True,
            },
        ),
        "classify",
    )
    run(
        client,
        pid,
        typed_action_request(
            "map.judge",
            sheet_id=sheet,
            params={
                "model": MODEL,
                "source": [
                    "item",
                    "note",
                    "follow_up",
                    "follow_up_justification",
                ],
                "judged_column": "follow_up",
                "guidelines": "investigate is correct only when the note shows a "
                "concrete irregularity (no-bid, untracked, conflict); "
                "being merely large is not enough.",
            },
            output_names={"verdict": "verdict", "judge_note": "judge_note"},
        ),
        "judge",
    )
    run(
        client,
        pid,
        typed_action_request(
            "reduce.group_summary",
            sheet_id=sheet,
            sheet_name="By verdict",
            params={
                "model": MODEL,
                "source": ["item", "note", "follow_up"],
                "group_by": "follow_up",
                "instruction": "Summarize these ledger lines and what unites them.",
            },
        ),
        "reduce → By verdict",
    )
    print(f"✓ Feature tour (computed/classify/judge/reduce) → {pid}")


def seed_faces(client: httpx.Client) -> None:
    pid = client.post("/api/projects", json={"name": "Faces demo"}).json()["id"]
    files = []
    for name, url in PORTRAITS:
        data = httpx.get(url, follow_redirects=True, timeout=120).content
        files.append(
            (
                "files",
                (
                    name.split(" (")[0].replace(" ", "_") + ".jpg",
                    io.BytesIO(data),
                    "image/jpeg",
                ),
            )
        )
    sheet = client.post(f"/api/projects/{pid}/import/files", files=files).json()[
        "sheet_id"
    ]
    run(
        client,
        pid,
        typed_action_request(
            "media.extract_faces",
            sheet_id=sheet,
            params={"source": "media"},
            output_names={"faces": "faces"},
        ),
        "extract faces",
    )
    data = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data?offset=0&limit=0"
    ).json()
    faces_column = next(c["id"] for c in data["columns"] if c["name"] == "faces")
    run(
        client,
        pid,
        typed_action_request(
            "derive.table_from_list",
            params={
                "source": {
                    "kind": "column",
                    "sheet_id": sheet,
                    "column_id": faces_column,
                },
            },
            sheet_name="Faces",
        ),
        "derive Faces sheet",
    )
    print(f"✓ Faces demo → {pid}")


def seed_agent(client: httpx.Client) -> None:
    pid = client.post("/api/projects", json={"name": "Agent research"}).json()["id"]
    csv = "organization\nInvestigative Reporters and Editors\nThe Marshall Project\n"
    sheet = client.post(
        f"/api/projects/{pid}/import/csv", files={"file": ("orgs.csv", csv, "text/csv")}
    ).json()["sheet_id"]
    run(
        client,
        pid,
        typed_action_request(
            "research.answer",
            sheet_id=sheet,
            params={
                "model": MODEL,
                "source": ["organization"],
                "question": {
                    "text": "When was {{organization}} founded, and what is it best "
                    "known for? Cite a source URL.",
                },
            },
        ),
        "agent research",
    )
    print(f"✓ Agent research → {pid}")


def seed_showcase(base: str, session: str, basic: str | None = None) -> None:
    cookies = {"frisket_session": session}
    auth = None
    if basic:
        u, _, pw = basic.partition(":")
        auth = httpx.BasicAuth(u, pw)
    with httpx.Client(base_url=base, cookies=cookies, timeout=300, auth=auth) as client:
        seed_people_derive(client)
        seed_feature_tour(client)
        seed_faces(client)
        seed_agent(client)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "showcase":
    seed_showcase(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
