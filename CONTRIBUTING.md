# Contributing to frisket

Thanks for your interest in contributing. This document is the contribution
policy for the **public `frisket` package** — the Apache-licensed local
single-user product and single-organization team server. It applies to the
public repository and to any change intended for the public artifacts (the
`frisket` wheel/sdist, the team image, and the local/team web bundles).

## Source checkout prerequisites

Editable installs (`uv sync`) compile the bundled SQLite Vec1 runtime used by
semantic search. Install a C compiler before syncing: Xcode Command Line Tools
on Apple Silicon macOS 14+, a C compiler such as GCC on Linux x86_64/aarch64, or
Visual Studio Build Tools with C++ support on Windows x86_64. On Windows, run
the sync from an MSVC developer prompt so `cl` is available. Source builds need
these tools even when working only on docs or the frontend; platform wheels
already include the native library.

For an existing editable installation cached before this build step was added,
build the library once from the same checkout and Python environment used to
run Frisket:

```sh
uv run --no-sync python scripts/build_vec1.py
```

Restart Frisket afterwards. Compilation does not fix a Python SQLite runtime
that disables loadable extensions; `frisket doctor embeddings` checks native
readiness separately from model availability.

## What is open to contribution

Contributions are welcome across the project store, runner, actions,
workbench, single-organization identity and membership, admin and audit controls,
OIDC/SSO, organization BYOK, telemetry controls, and the UI those APIs serve. Import
boundaries are enforced by `scripts/ci/check_import_boundaries.py` and its
configuration in `scripts/ci/import_boundaries.json`.

## Licensing: Apache-2.0 in, Apache-2.0 out

Everything in this repository is Apache-2.0 and stays that way. frisket is
licensed under the **Apache License 2.0** (see `LICENSE`).
Contributions are accepted under the same inbound = outbound terms: by
submitting a change you license it to the project under Apache-2.0, and it is
distributed to everyone else under Apache-2.0.

There is **no CLA**. This project deliberately does not require a Contributor
License Agreement and does not ask contributors for relicensing rights beyond
Apache-2.0. By submitting a contribution, you agree that it may be distributed
under the project's license.

## Practical expectations

- Keep commits scoped and reviewable; one logical change per commit.
- New behavior needs a focused test that proves the behavior it changes.
- Do not weaken existing checks to make a change pass — if a check looks
  wrong, say so in the PR instead.
- No secrets, no copyrighted media, and only public-domain or
  redistribution-clean fixture data.
- Before requesting human review, review the change in a separate session
  from the one that wrote it, with `.claude/skills/review-change/SKILL.md`
  (against its task and `CODING_STANDARDS.md`).
- Review follows the PR's merge danger. A one-way door or a wide blast radius
  always gets a person; a two-way, narrow, well-evidenced change can land on
  green checks plus the automated review. Changes to `src/frisket/upgrades/`,
  authentication and team security (`src/frisket/team/`), billing and consent
  in `src/frisket/execution/`, secrets, `.github/`, `scripts/release/`, gate
  baselines and allowlists in `scripts/ci/`, or `GLOSSARY.md` always get a
  person.

### Cleanup gates

CI runs three shrink-only ratchets from the repository root:
`python scripts/ci/ruff_ratchet.py` (per-file counts of the complexity,
argument-count and function-local-import rules configured in
`pyproject.toml`), `python scripts/ci/check_dead_symbols.py` (functions,
classes, methods and modules in `src/frisket` that production code never
reaches; tests do not count), and `python scripts/ci/check_duplicate_functions.py`
(near-identical function bodies and private helpers copied across modules).
A failure names the offender: restructure, call the existing code, or delete
the unreached code. When you remove offenders, run the same script with
`--update` to lower its baseline; `--update` refuses to raise any count or add
any entry, and hand edits that grow a baseline do not pass review. Renaming a
file or function means renaming its baseline key by hand. Locally,
`check_dead_symbols.py --downstream ../frisket-cloud` also counts the cloud
checkout as a consumer. From `web/`, `npm run knip` reports unused files,
exports and dependencies (not yet a CI gate).

The same shrink-only rule covers these gates too. `check_suppressions.py`
budgets suppression comments (`noqa`, `type: ignore`, `pragma: no cover`,
`eslint-disable`, `@ts-expect-error`, ...), `as any`/`as unknown as`/`as
never` casts and test skips that probe no environment; `.only` is never
allowed. `lint_comment_history.py` freezes comments that carry dates, ticket
slugs, review provenance or unfinished-work markers. `lint_glossary_avoid.py` freezes uses of the words each
`GLOSSARY.md` entry lists under `_Avoid_`, per file, in user-facing text
(web JSX text and labels, `ActionError`/`RouteError` messages, CLI help) and in
declared names; `--explain <term>` prints the entry. Plain avoids are checked;
qualified ones and words skipped because they are ordinary English in a scope
live in `glossary_avoid_scopes.json`. `type_ratchet.py` runs
pinned pyright (basic mode, `pyrightconfig.json`) over `src/frisket`; its
baseline is measured in the CI environment (`uv sync --dev --extra
entities`), and a pyright bump re-records it with `--update` in the bump's
commit. `web_tests_type_ratchet.py` type-checks `web/tests` through
`npm run tests:typecheck` (`web/tsconfig.tests.json`), which vitest and
Playwright never do. Fix these by satisfying the check, not by adding another
suppression. Finally, `check_test_collection.py --python`, `--sidecar` and
`--web` fail when a test file is collected by no pytest, vitest or Playwright
configuration; rename or move the file into a runner's pattern.

### Store transactions

Write transactions in `src/frisket/engine/store` and `src/frisket/engine/jobs`
go through `frisket.engine.store.transactions`. `write_transaction(db)` owns
one `BEGIN IMMEDIATE`: it commits on clean exit and rolls back on any
exception, interrupts included, and refuses to nest inside an open
transaction. `write_transaction(db, commit=False, what=...)` joins the
caller's open transaction and never ends it; `require_transaction(db, what)`
guards a write that must land inside its caller's transaction.
`scripts/ci/check_cell_write_ownership.py` refuses a new `"BEGIN IMMEDIATE"`
literal there; its `HAND_WRITTEN_TRANSACTIONS` list only shrinks.

### Shared UI controls

Use shared controls for ordinary inputs, segmented choices, icon buttons,
feedback and progress. Page CSS arranges them; it should not restyle them.
Express meaningful differences through component props (compact sizing,
full width, status), not page-specific selectors. Specialized editors can
remain specialized. Check changes in Storybook and the screenshot inventory.

### Action source inputs

Built-in actions declare source columns in their typed Params: `ColumnRef`,
column lists, or `Template`, with accepted types and validation on that same
definition. Register the handler with `action(...)`; the catalog, generated
TypeScript, form controls, and runtime reference discovery derive from it.
Keep selection, output names, and confirmation in `ActionRequest`, not Params.
Model-row handlers receive selected inputs with templates already rendered.
If a model-row action also references grounding-only columns, use
`model_rows(render, source_param="source")` to identify its prompt input.
All typed references still participate in validation and source snapshots;
grounding-only values do not enter the model prompt.

PDF file cells may include a 1-based `page` selector while retaining the original
PDF blob. Actions must process that page, include it in input identity checks,
and report citations using the original document's page numbers. Reuse
`engine.pdf_page_source.materialize_pdf_page` when a reader needs a
standalone PDF. Blob metadata and original-document viewing still describe the
whole PDF. Prepared native/OCR text resolves through the ordinary value readers;
consumers should not decode its internal storage reference themselves.

Search preparation designates one ordinary Text column for each File/Image
column. Native extraction and OCR write to that same destination through the
normal action hosts; explicitly selected alternative outputs remain separate.
Use `engine.store.document_text` to resolve a document's current text rather
than picking the newest evidence link. A configured but stale or unavailable
selection must not fall back to another output. Historical citations continue
to use their pinned prepared references, and manual text corrections do not
inherit page positions from the text they replace.

Plugin actions are ordinary actions: an installed `action(...)` declares its
source columns in its own typed Params, exactly as a built-in one does, and
runs on the same native hosts.

Do not hand-write `ui_hints.source_requirements`, add a parallel recipe method,
or repeat source-selection policy in runtime code. The action catalog and
runtime column resolution are projections of the typed Params. Operation code
still owns non-source domain rules, such as whether two output names collide.
A plugin's `plugin.json` never restates an action's inputs, writes, or
execution policy: the action definition is the sole authority, and the
manifest carries only its generated catalog entry.

Typed NDJSON import retries with the same request and idempotency key return
the original result without rereading the source. To import changed bytes, use
a new key and an available sheet name; recorded file hashes describe the
original read, not a continuing source-freshness guarantee.

### Import ownership

Imports use the ordinary typed action hosts. HTTP upload services and bulk
composition prepare action requests and admitted inputs; CLI, MCP and other
automation use those same action definitions. Installed table-producing jobs
retain their existing queued invocation. Do not introduce a second import
executor, receipt store or action catalog.

| Change | Owner |
| --- | --- |
| Borrowing admitted local inputs and closing reader wrappers | `engine/executor/import_sources.py` |
| Bounding decoded uploads, hashing borrowed spools and settling workers | `server/services/import_uploads.py` |
| Bounding raw import requests before body parsing | `server/import_bulk_request_limit.py` |
| Format-specific upload choices and HTTP response shape | `server/services/import_csv.py`, `import_xlsx.py`, `import_files.py` |
| CSV schema inference and bulk CSV request construction | `server/services/import_csv_analysis.py`, `import_csv_execute.py` |
| Shared tabular destination mapping and typed request construction | `server/services/import_tabular.py` |
| Pasted and uploaded table drafts, reviewed coercion and commit | `server/services/table_imports/` |
| Typed parser/producer behavior | `actions/imports.py`, `actions/import_xlsx.py`, `actions/import_media.py`, `actions/import_email.py`, `actions/import_geo.py` |
| Creating a table, its evidence and blobs | `engine/executor/table_action.py` |
| Append/update and exact update confirmation | `engine/executor/mutation_action.py`, `import_update.py` |
| Bulk staging, grouping and independent output commits | `server/services/import_bulk*.py` |
| Resumable attachment intake and progress | `server/services/import_sessions.py`, `engine/jobs/import_files.py` |
| Runtime providers and URL acquisition | `engine/executor/runtime_import_read.py`, `url_import_read.py` |
| FollowTheMoney atomic multi-table import | `engine/executor/entity_package.py` and the bundled FtM plugin |

A borrowed `BoundLocalFile` remains owned by its caller. Closing an admitted
reader must not close that source. A descriptor may instead provide an
`open_source` context manager: the reader opens it only for consumption and
closes it before moving to the next source. These two ownership forms are
mutually exclusive. An upload caller must retain its input until
the actual worker exits, including when its coroutine is cancelled. Completed
replay stays ahead of input access in the existing execution host.

HTTP routes admit the existing multipart spool as an `AdmittedUpload`; they do
not first read the whole file into bytes or make another persistent input copy.
Bulk keeps its staged plan files across requests and passes admitted source
descriptors to the same format services at execution. File and email groups
open and verify each staged source on demand, including after email sorting;
they do not hold the entire group's files open together. It commits each output
independently. A later fatal error stops further work and reports earlier
committed sheets together with failed and unattempted outputs.

Resumable attachments use a paged inventory of canonical blobs and the same typed
`import.files` producer. Pages atomically commit rows, provenance and a cursor to
a new or existing sheet under one operation/receipt. Completed rows are
browsable, but the destination is read-only until completion or Cancel followed
by Keep/Remove. Remove discards only the session's imported rows and columns;
it preserves existing destination data.
Queue claims own publication authority. Upload batches and worker claims are
bounded independently; neither carries the full corpus in a request or receipt.

Multi-PDF review stages files outside the project until final Import. Ordinary
text/OCR actions have their own Undo steps. Splitting documents or creating
page rows publishes through one protected import operation;
its automatic text preparation belongs to that operation, so one Undo removes
the split and its prepared text. Explicit preview OCR on existing originals and
later user-run OCR remain separate actions. Paid OCR is estimated against admitted
source pages and consented to before dispatch; each queued page must still satisfy
ordinary action admission and the consented allocation. A display label must
never alter the action identity or its queued execution parameters.

CSV, XLSX and paste build their existing typed Params and use
`run_tabular_action` for create, append and update. The browser sends raw paste
text and reviewed column choices; the server verifies the draft identity,
resolves destination types, coerces values and derives the request key. Paste
detection returns a bounded sample, not a second browser-owned execution input.

Adding inputs to an existing admitted map is distinct from replacing it with a
closed set. Preserve explicit replacement at authority boundaries; in
particular, bulk email admission requires the exact declared source set.
Format adapters and bulk grouping remain concrete functions; they are not
temporary migration bridges and need no universal format interface.

### Typed catalog examples

An action can declare `examples=(MyParams(...),)` alongside its handler. Use the
handler's exact Params class and illustrative values, never secrets or real
project data. Registration supplies the canonical request envelope and validates
each example without executing the handler. Omitted fields remain omitted;
explicit nulls remain explicit. Examples document the API; they do not populate
form defaults or serve as executable presets.

### Typed plugin editors

`Plugin(id=..., version=..., actions=[...])` owns typed action identities and
schemas. The default `action(..., form="generated")` needs no frontend or Node.
For a custom editor, set `form="CleanUI"` and export that exact, hook-free
factory from `frontend/plugin.tsx`. Run `frisket plugin build`: it generates
`frontend/generated/actionTypes.ts`, vendors `.frisket-sdk/action-ui.d.ts`,
and compiles the same trusted local module used by workbench components.
Do not hand-author a second action schema or `plugin.config.mjs`.

```tsx
import type { GeneratedActionParams } from './generated/actionTypes';
import type { ActionUI, ActionUIHost } from '../.frisket-sdk/action-ui';

export function CleanUI({ React }: ActionUIHost) {
  return {
    fields: {
      prefix: ({ value, onChange }) => <input value={value ?? ''}
        onChange={event => onChange(event.target.value)} />,
    },
    body: ({ Field }) => <><Field name="source" /><Field name="prefix" /></>,
  } satisfies ActionUI<GeneratedActionParams['acme.names.clean']>;
}
```

Use the host-provided React instance. Returned field/body components may use
hooks; the factory itself must not. Omit `body` to retain generated field
layout. Custom code edits Params only: the host retains validation, output
names, preview, execution, consent, and cancellation. Rebuild and reinstall
after changing code; stale package URLs and missing exports fail closed.

## Search indexing

Keyword search is a rebuildable sidecar, not a second data authority. Current-cell
projection changes and visibility/schema changes record dirty scopes in the same
source transaction. The existing job queue reconciles those scopes in bounded
batches; a lost enqueue is recovered by the next search, while merely opening a
project remains read-only with respect to the job queue. Search reads never rebuild
the corpus. Interactive responses include `indexing` and may contain
validated partial results; action/agent consumers require a complete matching
source snapshot and otherwise return `search_index_not_ready`.

Without a worker, library callers can explicitly run
`frisket.search.drain_index(project)` before searching. Background alert evaluation
also completes its required index before evaluating, so indexing does not drop
an automatic notification. Do not add ad-hoc invalidation calls to individual
actions: use the current-cell writers and existing metadata mutations.

## Cell storage writes

Base cells, generated results, and manual edits have distinct storage owners.
Use `engine.store.cell_writes` for base/edit writes and `RunResultStore` for
generated results; never mutate their physical tables from an action. Base
writes require a producer bound to the operation, edits require its `op_id`,
and result writes retain their admitted run/claim authority. Streaming imports
bind one staged producer at publication. `Project.add_rows` supplies one
operation automatically for a standalone append.

The storage owners encode values with `engine.store.value_codec` and maintain
`current_cells` in the same transaction. That projection holds the winning
authority and validity, plus bounded copies of numeric, boolean, null, and short
text/category/date values (up to 512 UTF-8 bytes per cell). Larger text and
structured values remain in their authority tables. The shared projection writer maintains these copies atomically; direct
SQL changes that bypass the storage owners require a projection rebuild. Read
decoded values via
`Project.get_values` / `get_values_with_refs`; use `apply_edits=False` only for
the generated candidate beneath an edit. SQL queries use `current_cell_values`
and its native `value` plus `value_kind`; JSON functions apply only to complex
JSON values. Historical result readers use `RunResultStore.decoded_result_rows`.
Historical source references stay unchanged, and missing historical producer
links mean unknown—not inferred provenance.

Column IDs are permanent history identities; names are unique only among active
columns on a sheet. Undoing creation deactivates the column and frees its name;
new creation gets a fresh ID. Hidden internal outputs remain active and reserve
their names. Resolve current names with `active=1` (including hidden outputs);
read historical values by ID, without substituting a same-name successor.

## Testing

- Each test should own a distinct behavior, security, or durability risk.
  Prefer runtime and public-boundary checks. Do not inspect source text or AST
  shape unless the test executes the artifact or compares independently
  maintained artifacts; `scripts/ci/lint_test_tokens.py` enforces this.
  Mark a legitimate exception inline with `# rule19: <reason>`.
- Keep the behavior under test real. Fake external services when needed, but
  avoid replacing our executor or store in tests meant to prove execution or
  persistence. Derive expected results independently of the implementation;
  test counts and coverage alone do not establish correctness.
- Run focused checks for routine changes. The Python suite excludes `gap`,
  `network`, and `plugin_contract` tests by default; web unit tests run with
  `npm run test:unit --prefix web`.
- Editable installs build the vendored Vec1 runtime for semantic retrieval.
  For a cached installation missing it, use the source-checkout repair above
  before running semantic retrieval tests.
- Keyless `gap` probes must be green when relevant:
  `uv run --no-sync pytest -q -n auto --dist loadfile -m "gap and not gap_env" tests/`. Run `gap_env`
  only in the prepared environment it names.
- Routine tests must not make live network, paid-model, or secret-bearing
  calls. Delete tests for removed behavior and move any genuine coverage to
  its strongest remaining home.
- Model-backed tests replay cached real responses instead of hand-written
  semantic mocks. To refresh the committed fixture intentionally, run the
  target with `FRISKET_CACHE_REFRESH=1 FRISKET_CACHE_MODE=replay`, then review
  and commit `tests/cache/llm_cache.db`.

### Browser suite

The complete browser suite (`web/tests/e2e`, about 700 tests) boots its own
backend and Vite server. CI runs only `deploy-smoke` and
`e2e-cassette-core` on pull requests and in the queue; the rest runs nightly
and on developers' machines. From `web/`:

```sh
FRISKET_TEST_PYTHON=../.venv/bin/python FRISKET_CACHE_MODE=replay_strict \
  npx playwright test --config playwright/playwright.config.ts
```

- `FRISKET_TEST_PYTHON` is the interpreter specs (and vitest's catalog
  helpers) shell out to; it defaults to the repository `.venv`, so a worktree
  must set it. A worktree also needs `UV_PROJECT_ENVIRONMENT=<main>/.venv`,
  `UV_NO_SYNC=1` and `PYTHONPATH=<worktree>/src` for the backend.
- `FRISKET_CACHE_MODE=replay_strict` keeps the run keyless: model calls
  replay from the committed response cache or fail. Without it some specs make
  cheap real calls.
- About 50 tests read four demo projects (`scripts/e2e/seed_demo.py`) from a
  cache in `~/.frisket/e2e-ws`. The `seed` setup opens each one first and
  records which are absent or unopenable (a cache from an older schema); the
  specs that need them then fail at once with that reason. Building the cache
  makes real model calls (about $0.25): delete the directory and run once with
  model keys and without `replay_strict`.
- A full run takes 50 to 80 minutes at the default two workers
  (`FRISKET_E2E_WORKERS`). Add `--reporter=line,json,./playwright/playwright.checklog-reporter.mjs`
  with `PLAYWRIGHT_JSON_OUTPUT_NAME=<file>` to judge it as below.

`nightly-e2e.yml` runs the suite on `main` at 08:41 UTC in four shards on
`ubuntu-latest`, keyless (`replay_strict`, no secrets), and skips a night on
which `main` has not moved since its last green run. Its `verdict` job merges
the shard reports and runs `scripts/ci/e2e_known_failures.py`, which writes
the job summary (totals, new failures, listed tests now passing, counts by
reason) and, when the verdict fails on `main`, opens or comments on one issue,
"Nightly browser suite: new failures" (label `nightly-e2e`); the next passing
night closes it. Expect roughly 100 to 130 runner-minutes a night.

`scripts/ci/e2e_known_failures.json` lists the tests known to fail: spec file
(relative to `web/tests/e2e`) to test title (describe titles and the test
title joined by ` › `) to a reason class, `seed`, `retired-request`,
`stale-expectation`, `environment` or `unclassified`. The list only shrinks.
The script fails on a failure the list does not name, on a listed test that
now passes (delete it, or run with `--update`, which only removes entries and
refuses while a new failure is unlisted), and on an entry whose spec file or
title no longer exists (rename it by hand when the test was renamed). A test
that passes on retry counts as passing and is reported as flaky. Adding an
entry is a hand edit naming its reason; fix the test instead when you can.
`seed` entries are judged only when the seed setup found none of the demo
projects, as on CI; with a seeded cache they are reported, not judged.

The list was first generated on macOS, so the nightly starts in report-only
mode (`E2E_KNOWN_FAILURES_ENFORCE: "false"` in `nightly-e2e.yml`): it prints
the differences and passes. To reconcile it with Linux and turn enforcement
on:

1. `gh workflow run nightly-e2e.yml --ref main`, and wait for it.
2. `gh run download <run id> -p 'e2e-report-*' -D reports`.
3. `python scripts/ci/e2e_known_failures.py reports/*.json`; add each new
   failure it names to the list by hand with its reason (usually
   `environment` for Linux-only ones), then rerun with `--update` to drop the
   listed tests that pass on Linux, and once more without flags until it
   passes.
4. Land the list and `E2E_KNOWN_FAILURES_ENFORCE: "true"` in one PR.

### Working in worktrees

Parallel work happens in git worktrees that share the main checkout's
`.venv`. Use the tools in `scripts/dev/` rather than hand-rolled shell:

- `scripts/dev/worktree.py create <name> [--base <ref>]` adds
  `../wt-<name>` on branch `wt/<name>` from the main checkout, prints the
  base commit and provisions it: the native Vec1 library is copied, web and
  SDK `node_modules` are symlinked, `sdk/src/contracts.gen.ts` is staged,
  and `import frisket` is proven to resolve to the worktree. It refuses when
  the main venv cannot import the project and prints the additive repair
  (`uv sync --inexact --frozen`). The commands it prints are also written to
  `.worktree-env`. `provision [<path>]` repeats the setup for an existing
  worktree; `remove <name>` refuses while work is uncommitted or unpushed.
- `scripts/dev/suite.py` is the one way to run the full Python suite. It holds
  a machine-wide lock (a second full run refuses, or queues with `--wait`),
  sizes `-n` from free CPU, and classifies failures as new, known, or fixed
  against `scripts/dev/known_local_failures.txt` when that file exists.
  Targeted runs, `suite.py -- tests/engine -k name`, skip the lock and use at
  most four workers.
- `scripts/dev/land.py <pr>` lands one PR: required checks green on the
  current head, mergeable, squash-merge, confirmed MERGED, main checkout
  fast-forwarded, then the lane worktree and branches removed. `--dry-run`
  prints the plan. With a merge queue the merge command enqueues the PR
  (`--auto`) and a behind branch is left alone; the script exits 4 with the
  queue position. Pending checks stop the script (exit 1), except that
  `--wait` requests auto-merge anyway and polls until the PR is MERGED (then
  it fast-forwards and cleans up), a required check fails, the PR conflicts
  with its base, or it leaves the queue (it names the failing check, and
  says when its log shows a dependency download or network timeout; exit 1).
  Without a queue a behind branch is updated and the script exits 3.
  Re-running on an already-queued or already-merged PR resumes.

#### Smell reservoir

`scripts/dev/smell.py add` is a cheap, append-only place to record a code smell
noticed outside the task at hand, so it is neither fixed as a drive-by nor lost
in a report: `--file`, optional `--symbol` and `--line`, `--kind`, and a
one-line `--note` (240 characters at most). Kinds: `duplicate`, `dead`,
`forwarder`, `loose-type`, `missing-guard`, `suspicious-behaviour`,
`misleading-name`, `stale-comment`, `test-smell`, `other`. Recording the same
file, symbol and kind again raises the entry's `seen` count rather than adding
a second entry. `list`, `prune` and `resolve` (as `promoted`, `fixed` or
`dismissed`) serve the periodic audit that triages the reservoir. The store is
outside the repository and shared by all worktrees: `--store`, else
`$FRISKET_SMELL_STORE`, else `~/.cache/frisket/smells/`; it holds one JSON file
per entry, so pointing it at a private notes repository merges cleanly.

### Merge queue

When `main` requires the merge queue, a PR no longer has to be up to date
with `main` to land, and updating the branch only costs a CI run.
`scripts/dev/land.py <pr> [--wait]` (or `gh pr merge <pr> --squash --auto`, or
"Merge when ready" in the UI) enqueues it once its PR checks pass. The queue tests each
entry on a temporary `gh-readonly-queue/main/...` commit that combines it with
`main` and every entry ahead of it (the `merge_group` event), and
squash-merges up to five entries together once all of their runs pass. A
failure in the queue ejects that PR: it leaves the queue unmerged, the
entries behind it are retested without it, and it must be fixed and
enqueued again.

Because the queue gates what lands, the PR run is fast feedback only. Every
required check (`deterministic-source`, `ci-pass`, `docker-ci`,
`desktop-macos-ci`, `desktop-windows-ci`, `macos-portability`,
`windows-portability`) still reports on every PR and queue entry, passing when
its heavy jobs are deliberately skipped. Its job summary says why: the diff
changes one of the job's inputs, the job is "deferred to the nightly platform
run on main", or the diff does "not affect" it at all.

| | Pull request (fast tier) | Merge queue | Nightly on `main` |
| --- | --- | --- | --- |
| Lint, formatting, `scripts/ci` gates and ratchets, pyright, `tsc -b`, web tests type ratchet | yes, by layer | yes | no |
| Web lint, vitest, build | when `web/`, `sdk/`, `scripts/`, `.github/`, `pyproject.toml` or `uv.lock` changed | yes | no |
| Python tests | the test directories of the changed source areas plus the root tests; the complete suite when dependencies, test configuration, shared test helpers, package data, a top-level module, `sidecar/` or a wheel build input changed | complete suite, including the `gap` subset | no |
| Sidecar, Postgres, GPU-topology checks | by layer | yes | no |
| `deploy-smoke`, `e2e-cassette-core` | when the e2e specs, Playwright configs, `scripts/e2e/`, `web/package*.json` or `ci.yml` changed | yes | no |
| Full browser suite (`nightly-e2e`), judged by `scripts/ci/e2e_known_failures.py` | no | no | yes, when `main` moved |
| Docker build and Compose smoke | when an image input changed | when an image input changed | no (weekly, and every release) |
| macOS and Windows installed-app proofs, macOS and Windows portability smokes | when the job's own inputs changed (below) | when the job's own inputs changed | always, in full |
| macOS managed-Python slice (not required) | when `src/frisket/`, `tests/`, `pyproject.toml`, `uv.lock`, `.python-version`, the vec1 build or its workflow changed | same | always |

The four platform jobs' own inputs, stated in full beside each workflow's
`changes` filter, are:

- packaging: `pyproject.toml`, `uv.lock`, `.python-version`, `hatch_build.py`,
  `scripts/build_vec1.py`, `vendor/sqlite-vec1/`, the workflow itself, and for
  the installed app `scripts/release/build_frontend.py`;
- platform-specific Python, the same for all four: `src/frisket/runtime/`,
  `engine/sandbox/`, `engine/_workers/`, `cli/`, `data/model-server/`, and the
  modules that branch on the OS for process trees, file locking, reserved names
  and app paths (`recipe_fence_posture`, `retrieval/native`,
  `operability/diagnostics`, `cloud_connection`, `project_identity`,
  `server/workspace`, `team/security/secrets`, `ops/ytdlp`,
  `ops/media_metadata`, `ops/integrations/mcp_stdio`);
- any added or removed line under `src/` that branches on the OS or starts,
  signals or confines a process (`sys.platform`, `os.name`, `subprocess`,
  `signal`, `creationflags`, `fcntl`, `msvcrt`, `multiprocessing`, `ctypes`
  and similar);
- installed app: `desktop/`, `src/frisket/server/desktop.py`, and an added,
  removed or renamed file under `src/frisket/data/` or a bundled plugin (the
  desktop wheel must carry them); on Windows also the guardian test;
- portability smokes: the tests each smoke runs, `conftest.py`, and the
  modules those tests prove on that OS (store leases and blob paths, bulk
  import; on Windows also the job queue and worker, project lifecycle, the
  recipe registry and team bootstrap).

A diff whose base cannot be resolved runs everything.

For a risky change, add the `ci:full` label: the PR reruns with the complete
`ci` suite and every platform proof, whatever the diff touches.
`gh workflow run ci.yml --ref <branch>` also runs the complete `ci` suite on
the branch head.

The `ci`, `docker` and portability push runs on `main` ask
`queue-tested.yml` whether that exact commit already passed in the queue; if
so their test jobs skip and the run still succeeds, otherwise (a direct push,
or the queue disabled) they run the complete suite as before.

#### Nightly platform proofs

`nightly-platform.yml` runs at 07:23 UTC and proves the head of `main` on
every platform, whatever changed: it calls `desktop-macos.yml` and
`desktop-windows.yml` (the unsigned installed-app proofs) and the two
portability workflows. A night on which `main` has not moved since the last
green run is skipped. `gh workflow run nightly-platform.yml --ref main`
proves the current head now.

A red night opens one issue, "Nightly platform proof failed", labelled
`nightly-platform` (the workflow creates the label), or comments on it if it
is already open; the next green night closes it. The issue links the failing
jobs and lists the merges since the last green night: one of them broke the
platform, unless the list is empty (then suspect the runner or a network
dependency, and rerun the failed jobs). When it opens:

1. Read the failing job's log and evidence artifacts; the culprit is usually
   evident from them and the listed merges.
2. Otherwise bisect: push a branch at a commit in the range and run
   `gh workflow run nightly-platform.yml --ref <that branch>`.
3. Land the fix (or a revert) through the queue as usual, adding `ci:full` so
   the PR proves every platform, then dispatch the nightly on `main` to close
   the issue rather than waiting for the night.

Releases require two passes on the exact released commit: its latest `ci`
push or dispatch run, and the `platform-proofs` job of its latest
`nightly-platform` run that got that far (a skipped night does not count). Before tagging or dispatching
a release, check that the commit has a green nightly; if it has none, run
`gh workflow run nightly-platform.yml --ref <main at that commit, or the tag>`
and rerun the release once it is green. The desktop release additionally
builds, signs and proves both installers on that commit itself.

### Reviewing model and pricing updates

Pricing refresh and model discovery are separate workflows. Both run on a
schedule and can be started manually from GitHub Actions. Pricing keeps one
rolling PR, regenerates it from current `main`, and enables auto-merge after
required checks. Model discovery leaves an open proposal untouched for review.

- `update_pricing.py --mode pricing` (the default) refreshes rates for the approved
  catalog. It never changes model names, ordering, capabilities, or OpenDocRouter
  model membership. Missing upstream prices retain the previous approved rates
  with a warning; an incomplete OpenDocRouter response retains its whole previous
  snapshot. Invalid rates or changes to the approved inventory fail validation.
  Rate changes have no percentage threshold.
- `update_pricing.py --mode models` proposes supported model additions, removals,
  and capability changes, with matching prices. Price-only changes are a no-op.
  Existing curated labels and ordering are preserved; new entries use supplied
  names where available and append deterministically. This is a discovery draft,
  not a ranking of model strength.

Before merging a discovery PR, check provider documentation for names and
capabilities. Start each provider with a current balanced model for fresh forms,
then its other current models, followed by older models. Keep names concise;
defaults and recent-use badges must not silently reshuffle the list. Regenerate
`web/src/actions/modelCatalog.generated.ts` and the OCR catalog with the sync scripts after edits.

Refresh discovery PRs against current `main` before merging: pricing and model PRs
both touch `pricing_data.json`, and an older proposal must not restore stale
prices or OpenDocRouter membership. Rerun the appropriate updater and inspect the
final diff after rebasing; for discovery, retain the reviewed names and ordering.

Pricing automation requires repository auto-merge enabled, a repository variable
`PRICING_APP_ID`, and a repository secret `PRICING_APP_PRIVATE_KEY`. Install that
GitHub App on this repository with Contents and Pull requests read/write access.
Its token triggers normal PR checks; no branch-protection bypass is used.
`deterministic-source` validates that the rolling PR changes only
`pricing_data.json` and preserves the approved model metadata. Run the same
offline validation with `python3 scripts/dev/update_pricing.py --validate BEFORE AFTER`.
