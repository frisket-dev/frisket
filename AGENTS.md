# frisket — agent instructions

Frisket is a local-first data-journalism workbench: import data,
transcribe audio, OCR documents, run AI actions over spreadsheet
columns — with cost estimates and explicit consent before anything
paid or external runs.

## Commands

- Python suite: `pytest -q -n 8 --dist loadfile` (~5 min; do not run
  the full suite serially). Single file: plain `pytest tests/<file>`.
- Web (from `web/`): `npx vitest run`; types: `npx tsc -b` (the root
  tsconfig uses project references; `tsc --noEmit` alone checks no files).
- Lint: `ruff check src tests` and `ruff format --check src tests`,
  plus the gates in `scripts/ci/lint_*.py` and
  `scripts/ci/check_import_boundaries.py --source-root src
  --config scripts/ci/import_boundaries.json`.
- Sidecar (from `sidecar/`): `uv run --no-sync pytest -q tests`.

## Conventions

- Tests self-skip when an optional extra is absent; a red test in an
  installed area is real. Test failures are named, not averaged.
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
  adds is worth a second look.
- See CONTRIBUTING.md for PR and testing expectations.
