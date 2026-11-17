# Coding standards

What a reviewer checks a diff against: judgments no gate makes. Where
AGENTS.md states a principle, this file says how to spot a breach of it.

## One owner per mechanism

- A new function whose steps mirror an existing one (the same claim, write,
  finish and rollback sequence under other names) is a second
  implementation. Call the existing owner and name it in the finding.
- Map an exception to an error once: in the executor through `ActionCtx`
  and `refusal_error` (`engine/executor/action_support.py`), in the server
  through `register_typed_error` or `RouteError`. A local `except` that
  rebuilds an error another site already maps is a finding.
- A boolean parameter that chooses between two sequences of steps hides two
  pipelines. Instead: two named functions sharing the common steps, or one
  pipeline with the difference as data.
- A new store function takes no `commit` flag and never commits or rolls
  back a transaction it did not open: it calls `require_transaction`, or
  owns one with `write_transaction`.
- Secret-bearing text goes through `frisket.redaction`; a local regex or
  string replace is a second, weaker redactor.

## Facts and types across layers

- A literal that already has a definition (a status, action id, error code,
  limit, column type) is imported or derived from its registry, not retyped.
- `Any` or `dict[str, Any]` in a new signature that crosses a module
  boundary is a finding unless the value is opaque user JSON. Instead: the
  typed model the boundary already validated.
- An `isinstance`, `model_validate` or shape check on a value that already
  has a typed model is dead re-validation; delete it.
- An untrusted URL, path or identifier (from a request, a cell, a model
  answer, a plugin) reaches a sink (filesystem, subprocess, fetch,
  `href`/`src`) only through that sink's one checked helper, such as
  `web/src/core/safeUrl.ts`. With no helper, the fix is one helper with
  every site routed through it, not a check at this site.

## Names and interfaces

- An optional parameter that no production caller passes is deleted; add
  it when a production caller needs it.
- Names say what the code does now. A glossary word in a name carries the
  glossary's meaning: a `replace_*` path that can write a source column is
  a finding. A new concept with no glossary term is raised in the PR, not
  settled by the code.

## Tests

- Arrange state through the functions production uses to create it, not raw
  SQL into a store's tables; tests of upgrades and crash recovery are the
  exception. A state no production function reaches needs no test. A shared
  setup helper belongs under `tests/`, not in the module.
- A bug fix carries a test that fails without the fix.

## Comments and follow-through

- A comment that narrates (`now`, `previously`, `new`, `instead of the old`)
  is rewritten as the constraint that shapes the code, or deleted.
- Deleting or renaming a symbol updates every docstring, guide and
  CONTRIBUTING.md passage that names it.

Rules the implementing agent must follow while writing are in AGENTS.md;
this file is for review.
