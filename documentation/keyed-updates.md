# Keyed imports and source updates

An update identifies a source relationship, a target sheet, and explicitly mapped
key columns. Matching uses the current typed key values. Omitted fields remain
untouched; an explicit null is a value. `keep_changes` retains a local edit;
`incoming_wins` replaces it when the source changes. A first observation preserves
populated values under `keep_changes`. The observed baseline advances independently
of Undo, including when the local value wins.

Before publication, the entire incoming key domain is validated in a temporary
SQLite index. Identical records collapse; conflicting records for a key are all
skipped. A late duplicate therefore cannot overwrite an earlier published record.
The index is rebuilt from immutable staged input after interruption. The existing
import session cursor, observation updates, and immutable cell versions commit in
the same bounded transaction. No second publication checkpoint is needed.

The reviewed table UI and the typed `import.update_rows`, `import.update_csv`, and
`import.update_xlsx` actions use the same admission and publisher. HTTP requests
queue the existing table worker; embedded calls synchronously drain that worker's
page driver, committing bounded pages rather than one transaction for the entire
input. Typed record staging preserves stored value types and omitted fields.
Built-in request envelopes reference staged input rather than copying rows into
the queue and receipt. Original action identity and idempotency are retained.

A custom action with the sole `RowsUpdater` capability runs its real handler
outside the ordinary mutation transaction. Its capability call uses the same
publisher and must be returned directly. Custom queued action parameters still
use the ordinary action envelope, so very large plugin parameter bodies retain
that transport's existing memory cost; no separate request-file protocol exists.

Updates never infer deletion from a record absent in the incoming file. Reversible
row deletion and permanent purge are distinct: a retained hidden association can
be restored by a changed incoming record under `incoming_wins`; purge removes the
association. It does not suppress future source polling.

The older CSV/XLSX update-upload and update-preview HTTP routes are retired.
HTTP clients use `/actions/v1/run` or the reviewed `/import/table-imports` workflow.
A preview fingerprint is not a second update authority: publication checks the
frozen input plan and current target keys, then applies the selected source policy.
