# Glossary

The words Frisket uses for its own domain, in the product, in conversation and in code. One word per thing. Terms are added when they are settled and only when something being built needs them; the maintainer decides them.

This file holds terms and nothing else: no implementation details, no general programming vocabulary.

Under `_Avoid_`, a plain word or phrase is checked by `scripts/ci/lint_glossary_avoid.py` in user-facing text and in new names. A word followed by a parenthesis or a qualifier ("job" in user-facing text) is guidance for people and agents; the script reads only the exceptions listed in `scripts/ci/glossary_avoid_scopes.json`.

## Data

**Source** — the original, imported material that results are derived from: a source column, a source document.
_Avoid_: source (for something checked on a schedule; that is a feed).

**Provenance** — where a source came from: its URL or file, and when it was fetched.
_Avoid_: provenance (for how a result was produced; that is a receipt).

**Project** — one investigation's sheets, files and history.

**Workspace** — the folder on a machine or server where projects live.

**Sheet** — one grid of data in a project.
_Avoid_: table (except a table found inside a document).

**Column** — a column of a sheet, as in a spreadsheet.
_Avoid_: field (for a column; a field is only one named value an action extracts).

**Row** — a row of a sheet, as in a spreadsheet.
_Avoid_: record.

**Source column** — a column that came from imported data. A run never replaces one.
_Avoid_: base.

**Generated column** — a column whose values a run produced. A run may replace one.
_Avoid_: output column, AI column, derived.

**Document** — one unit of source material that becomes a row.

**View** — a saved way of looking at a sheet: filters, sort, visible columns.
_Avoid_: lens, saved view.

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

**Engine** — the thing a person picks to do an action's work: a named way of doing it, local or remote.

**Provider** — the outside company or service an engine sends data to, if any.

**Model** — the specific model an engine runs.

**Preview** — running an action on a few rows to see results before running it on everything. Nothing is written to the sheet.
_Avoid_: scratch, sample, dry run.

**Comparison** — a preview that runs several variants (engines, models, settings) side by side.

## Checking results

**Citation** — the exact place in the source a result points to: a passage, a page region or a timestamp. It says where a result came from; it does not claim proof.
_Avoid_: evidence, quote, excerpt.

**Receipt** — the record of what a run did: action, model, settings, cost.
_Avoid_: provenance (for this meaning).

**Review** — a person checking a run's results against their citations. A reviewed result is **Accepted** or **Rejected**, or corrected.
_Avoid_: approve (it is confusable with consent), verified.

## Money and data leaving the machine

**Consent** — a person's agreement to one specific estimate: what will run, what it will cost, and what will leave their machine. Consent is to those facts, not a yes in general.
_Avoid_: approval, preapproval; "confirmation" for anything but the click itself.

**Estimate** — what a run is expected to do before it starts: rows, engine and provider, what leaves the machine, and the expected cost. It is what a person consents to.
_Avoid_: quote.

## Bringing material in

**Import** — bringing material into a project as rows.

**Feed** — a place Frisket checks on a schedule for new items, which it imports as rows: an RSS feed, a YouTube channel or playlist, a court docket, an API listing.
_Avoid_: source (for this meaning), poller.

**Packet** — one file that holds several documents. Importing a packet splits it into its documents, one row each. A packet contains documents, not data: a meeting-packet PDF, a zip of PDFs, a PDF imported page by page, a mailbox file. A CSV or a workbook is not a packet. The same file can be imported as one document or as a packet.

## Staying informed

**Alert** — a saved condition on a sheet. When new or changed rows match it, Frisket sends a notification. An alert is always the thing a person sets up.
_Avoid_: watch, watchlist, monitor.

**Notification** — the message Frisket sends, from an alert or a finished run.

## Where Frisket runs

**Edition** — one of the ways Frisket runs: Local, Team or Cloud.

**Local** — on one person's machine, for a single user. Desktop is its packaged app, not a separate edition.

**Team** — one organization's own server.

**Cloud** — the service Frisket runs for many organizations.
_Avoid_: hosted.

**Organization** — a group of people who share projects on a team or cloud server.
_Avoid_: team (for a group of people), org, tenant.
