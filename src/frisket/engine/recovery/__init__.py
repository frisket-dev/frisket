"""Production recovery mechanisms for the control-plane/run-queue durability
domain (fresh-eyes follow-on plan section 11.3).

The Postgres volume (control-plane, billing, audit, run-queue) is a separate
durability domain from the Litestream project replicas. This package owns the
two recovery *mechanisms* that domain still lacked:

- ``postgres_backup`` -- pinned off-box backup transport plus a disposable
  hermetic restore drill covering both databases and the run-queue migration
  ledger;
- ``split_store_reconciliation`` -- deterministic no-replay reconciliation of
  the restored Postgres and project stores with an idempotent recovery ledger.

Current-candidate off-box restore evidence through the real production topology
is a separate live-proof composite (``prod-postgres-current-topology-live-proof-v1``);
these modules prove the mechanism, not that live evidence.
"""

from __future__ import annotations
