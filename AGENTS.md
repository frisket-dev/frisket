# frisket — agent instructions

Frisket is a local-first data-journalism workbench: import data,
transcribe audio, OCR documents, run AI actions over spreadsheet
columns — with cost estimates and explicit consent before anything
paid or external runs.

## Commands

- Python suite: `scripts/dev/suite.py` (~5 min; one full run per machine,
  `--wait` queues behind another; never start `pytest -n` over all of
  `tests/` by hand). Targeted: `scripts/dev/suite.py -- tests/<path>`, or
  plain `pytest tests/<file>`.
- Worktrees: `scripts/dev/worktree.py create <name>` (then follow the
  commands it prints); land a PR with `scripts/dev/land.py <pr>`, never a
  chained `gh pr merge && git branch -D ...`.
- Web (from `web/`): `npx vitest run`; types: `npx tsc -b` (the root
  tsconfig uses project references; `tsc --noEmit` alone checks no files).
- Lint: `ruff check src tests` and `ruff format --check src tests`,
  plus the gates in `scripts/ci/lint_*.py`,
  `scripts/ci/check_import_boundaries.py --source-root src
  --config scripts/ci/import_boundaries.json`, the shrink-only
  ratchets `scripts/ci/ruff_ratchet.py`,
  `scripts/ci/check_dead_symbols.py`,
  `scripts/ci/check_duplicate_functions.py`,
  `scripts/ci/check_suppressions.py`, `scripts/ci/type_ratchet.py`
  (pyright) and `scripts/ci/web_tests_type_ratchet.py`, and
  `scripts/ci/check_test_collection.py --python|--sidecar|--web`.
  Web: `npm run knip`.
- Sidecar (from `sidecar/`): `uv run --no-sync pytest -q tests`.

## Conventions

- Tests self-skip when an optional extra is absent; a red test in an
  installed area is real. Test failures are named, not averaged. (gate:
  check_suppressions.py budgets skips that probe no environment)
- Every test file is collected by a runner (gate: check_test_collection.py).
- Silence a check only where the budget allows; suppression comments,
  casts and type errors only shrink (gates: check_suppressions.py,
  type_ratchet.py, web_tests_type_ratchet.py).
- Comments state constraints, never history: no dates, ticket slugs,
  review provenance or for-now markers (gate: lint_comment_history.py).
- No live network in tests; provider calls replay recorded transports.
- Secrets never appear in reprs, errors, or logs. Malformed config
  refuses and names the offending knob.
- Money/consent code carries facts by value end to end; a bare
  `confirmed` boolean is never consent. Changes on that seam ship a
  regression that fails when a consumer drops one of those facts.
- After this pre-public follow-up round, schema changes require data-preserving
  migrations; reset or reseed only during explicitly authorized maintenance,
  never as a routine upgrade.
- Prefer deleting dead code over commenting it out; a change that only
  adds is worth a second look. (gate: check_dead_symbols.py)
- A path that supersedes another deletes the old one in the same change;
  no shims, aliases or compatibility wrappers.
- Code reached only by tests is not shipped. Parity oracles and reference
  implementations live under `tests/`. (gate: check_dead_symbols.py)
- No function whose body only forwards its own parameters; no ABC or
  Protocol with a single implementation.
- One owner per lifecycle primitive: call the shared driver, never copy a
  sibling's skeleton and edit it. (gate: check_duplicate_functions.py)
- State a fact once and derive the rest: no hand-written twin of a
  generated type, no table keyed by action id outside the registry.
- Validate at the boundary into a typed model, then trust it: no
  `dict[str, Any]` across layers, no re-validating internal values.
- Ratchet baselines and allowlists only shrink. Never regenerate one
  upward to make a change pass; restructure the change instead. (gate:
  each ratchet's `--update` refuses growth)
- Don't edit `README.md`; Jonathan maintains it. If docs should change,
  suggest the wording in your PR description instead.
- See CONTRIBUTING.md for PR and testing expectations.
