---
name: frisket
description: Inspect Frisket projects and run or repair typed actions through an available Frisket MCP or CLI connection. Use for listing projects, reading or searching sheets, running actions, polling runs, or backfilling partial AI columns; not for developing Frisket itself.
---

# Use Frisket

Work through the Frisket surface already available to the caller. Prefer MCP for
interactive project work because it supplies list, read, search, run, status,
and backfill tools. Use the CLI when it is the available surface or when a
local action catalog or request needs inspection. Do not start the web app just
to use local MCP; `frisket mcp [workspace-dir]` binds to a local workspace over
stdio. Hosted MCP uses the connection's configured `FRISKET_BASE_URL` and
`FRISKET_PAT` with `frisket mcp --hosted`.

## Discover before acting

- Call `list_projects`, then `list_sheets(project_id)` to discover sheet ids.
- Read sheets in pages. Continue until `offset + len(rows) >= total`; the
  server caps a page at 1,000 rows.
- Use the schemas and descriptions exposed by the connected tools. For a local
  CLI, `frisket action schema` is the current action catalog, including each
  action's params, scope, outputs, cost policy, and execution mode.
- Build registered requests with `action_id`, `scope`, and `params`, plus only
  the applicable `output_names`, `sheet_name`, `replace_existing`, and
  `idempotency_key`. Do not invent capabilities, params, action ids, or legacy
  request shapes. Validate a CLI request with `frisket action validate FILE`
  before `frisket action run --project PROJECT.frisket FILE` when practical.

Stay within the user's requested data, operation, and spending constraints.
Use the narrowest row scope that satisfies the task. Frisket's configured
preapproval covers under-limit cost and declared external egress; do not add
a redundant generic prompt. It does not authorize unrelated actions or waive
the user's restrictions. Never bypass a server refusal or cost challenge.

## Run and observe

Call `run_action(project_id=..., action=...)` with the prepared request as
`action`. A synchronous action may
return a terminal receipt immediately. For a started run, keep its `run_id` and
poll `get_run_status` until `completed`, `failed`, or `cancelled`. Report named
errors and partial outputs: a failed, cancelled, or partial result can still
have durable effects and is not an automatic rollback.

Treat `status=needs_confirmation` as a human decision, not an execution error:

1. Surface the returned estimate, complete estimate details, scope/claims, and
   message. `estimate: null` or `estimate_known: false` means the price is
   unknown, not free.
2. Ask the human to approve those exact terms. Do not infer approval from the
   original run request or from a spending ceiling when the returned price is
   unknown or outside that ceiling.
3. If approved, retry without changing the action. With MCP, set
   `confirmed=true` and `consented_promise_set_hash` to the exact returned
   `promise_set_hash`. With `frisket action run`, copy the exact hash from the
   result's confirmation error details into the request's `confirmation`.

Never fabricate, shorten, reuse across changed terms, or pre-populate a
confirmation hash. A changed action or scope must be submitted without the old
confirmation and may produce a fresh challenge.

## Repair partial columns

Use `backfill_run` for a partial AI-generated column instead of launching an
unrelated second action. Omit `row_ids` for the automatic sweep of rows without
a terminal result; pass explicit row ids only when the user intends to rerun
exactly those rows. Preserve the same confirmation procedure if backfill is
gated. If Frisket refuses with a named condition such as a mixed origin or
missing source generation, report it rather than improvising a replacement
run.
