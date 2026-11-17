# Glossary

The words Frisket uses for its own domain, in the product, in conversation and in code. One word per thing. Terms are added when they are settled and only when something being built needs them; the maintainer decides them.

This file holds terms and nothing else: no implementation details, no general programming vocabulary.

## Doing work

**Action** — something Frisket can do to a sheet: classify, extract, transcribe, import, export.
_Avoid_: recipe, op, operation.

**Run** — one execution of an action over a set of rows. A run is what a person starts, watches, cancels and reviews.
_Avoid_: job (for this meaning), execution.

**Job** — a queued unit of background work. Jobs are how the system schedules work; people see runs.
_Avoid_: task; "job" in user-facing text.

**Attempt** — one billed try at a provider call within a run.
_Avoid_: retry (for a first try), call.

**Replace** — a run writes its results into an existing generated column in place of the old results. Only a generated column can be replaced; a source column never is.
_Avoid_: overwrite, regenerate.

## Money and data leaving the machine

**Consent** — a person's agreement to one specific estimate: what will run, what it will cost, and what will leave their machine. Consent is to those facts, not a yes in general.
_Avoid_: approval, preapproval; "confirmation" for anything but the click itself.
