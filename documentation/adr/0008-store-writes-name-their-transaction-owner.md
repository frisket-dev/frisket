# Store writes name their transaction owner

A project-store write either owns a transaction or joins its caller's, and says which through `engine/store/transactions.write_transaction`: `commit=True` opens its own `BEGIN IMMEDIATE`, commits on clean exit, and rolls back on any exception including `KeyboardInterrupt`, `SystemExit` and `CancelledError`; `commit=False` refuses to run without an open transaction and never begins, commits or rolls back. A function never commits a transaction it did not open, so an owning call inside an open transaction fails at its `BEGIN` and leaves the caller's work untouched.

**Considered:** committing whatever transaction is open at the end of each write. Callers need not say who owns the transaction, but an interrupted or nested call can publish someone else's half-finished work.

**Consequences:** every write call states its ownership. `scripts/ci/check_cell_write_ownership.py` keeps the `BEGIN IMMEDIATE` literal in `engine/store` and `engine/jobs` to `transactions.py` plus an exact, shrink-only list of hand-written boundaries; writes that still end with a bare `db.commit()` under a `commit` flag do not yet meet this rule.
