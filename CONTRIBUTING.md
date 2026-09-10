# Contributing to frisket

Thanks for your interest in contributing. This document is the contribution
policy for the **public `frisket` package** — the Apache-licensed local
single-user product and single-organization team server. It applies to the
public repository and to any change intended for the public artifacts (the
`frisket` wheel/sdist, the team image, and the local/team web bundles).

## What is open to contribution

Contributions are welcome across the project store, runner, actions,
workbench, single-org identity and membership, admin and audit controls,
OIDC/SSO, org BYOK, telemetry controls, and the UI those APIs serve. Import
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

Plugin actions are ordinary actions: an installed `action(...)` declares its
source columns in its own typed Params, exactly as a built-in one does, and
runs on the same native hosts. Internal `frisket.sdk` operations still use
descriptors from `frisket.sdk.inputs` on their `Op`; choose the smallest
shape that matches the source (`Column`, `Columns`, `Template`, `Fields`,
`OneOf`, `FixedColumn`, or `Computed`).

Do not hand-write `ui_hints.source_requirements`, add a parallel recipe method,
or repeat source-selection policy in runtime code. The action catalog and
runtime column resolution are projections of the descriptor. Operation code
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
| Format-specific upload choices and HTTP response shape | `server/services/import_csv.py`, `import_xlsx.py`, `import_files.py`, `import_pdf.py`, `import_urls.py` |
| CSV schema inference and bulk CSV request construction | `server/services/import_csv_analysis.py`, `import_csv_execute.py` |
| Shared tabular destination mapping and typed request construction | `server/services/import_tabular.py` |
| Paste detection, reviewed coercion and confirmation | `server/services/import_drafts.py` |
| Typed parser/producer behavior | `actions/imports.py`, `actions/import_xlsx.py`, `actions/import_media.py`, `actions/import_email.py`, `actions/import_geo.py` |
| Creating a table, its evidence and blobs | `engine/executor/table_action.py` |
| Append/update and exact update confirmation | `engine/executor/mutation_action.py`, `import_update.py` |
| Bulk staging, grouping and independent output commits | `server/services/import_bulk*.py` |
| Runtime providers and URL acquisition | `engine/executor/runtime_import_read.py`, `url_import_read.py` |
| FollowTheMoney atomic multi-table import | `engine/executor/entity_package.py` and the bundled FtM plugin |

A borrowed `BoundLocalFile` remains owned by its caller. Closing an admitted
reader must not close that source. An upload caller must retain its input until
the actual worker exits, including when its coroutine is cancelled. Completed
replay stays ahead of input access in the existing execution host.

HTTP routes admit the existing multipart spool as an `AdmittedUpload`; they do
not first read the whole file into bytes or make another persistent input copy.
Bulk keeps its staged plan files across requests and passes verified open
sources to the same format services at execution. It commits each output
independently. A later fatal error stops further work and reports earlier
committed sheets together with failed and unattempted outputs.

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

## Cell storage writes

Base cells, generated results, and manual edits have distinct storage owners.
Use `engine.store.cell_writes` for base/edit writes and `RunResultStore` for
generated results; never mutate their physical tables from an action. Base
writes require a producer bound to the operation, edits require its `op_id`,
and result writes retain their admitted run/claim authority. Streaming imports
bind one staged producer at publication. `Project.add_rows` supplies one
operation automatically for a standalone append.

The storage owners maintain `current_cells` in the same transaction. Read via
`Project.get_values` / `get_values_with_refs`; use `apply_edits=False` only for
the generated candidate beneath an edit. The projection is rebuildable; it is
not a second writable authority. Historical source references stay unchanged,
and missing historical producer links mean unknown—not inferred provenance.

## Testing

- Each test should own a distinct behavior, security, or durability risk.
  Prefer runtime and public-boundary checks. Do not inspect source text or AST
  shape unless the test executes the artifact or compares independently
  maintained artifacts; `scripts/ci/lint_test_source_reads.py` enforces this.
  Mark a legitimate exception inline with `# rule19: <reason>`.
- Run focused checks for routine changes. The Python suite excludes `gap`,
  `network`, and `plugin_contract` tests by default; web unit tests run with
  `npm run test:unit --prefix web`.
- Keyless `gap` probes must be green when relevant:
  `uv run --no-sync pytest -q -m "gap and not gap_env" tests/`. Run `gap_env`
  only in the prepared environment it names.
- Routine tests must not make live network, paid-model, or secret-bearing
  calls. Delete tests for removed behavior and move any genuine coverage to
  its strongest remaining home.
- Model-backed tests replay cached real responses instead of hand-written
  semantic mocks. To refresh the committed fixture intentionally, run the
  target with `FRISKET_CACHE_REFRESH=1 FRISKET_CACHE_MODE=replay`, then review
  and commit `tests/cache/llm_cache.db`.
