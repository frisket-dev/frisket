"""Execution seam: targets, routes, resolution, and the promise algebra.

This package deliberately exports NOTHING of its own. Every consumer —
production and test — imports from the submodule that owns the name, so there
is exactly one answer to "where does this live":

- ``targets`` — ``ExecutionTarget`` and the target/engine-support value types.
- ``definitions`` — the static target table and its activation reads.
- ``resolver`` — request -> ``Resolution`` | ``Refusal``, and the route-row
  facts projection.
- ``provider`` — the ``ExecutionTargetProvider`` port.
- ``promises`` — the promise/claim algebra, registry, and evaluation.
- ``promise_compiler`` — the capability-generic claim compiler and cost-basis
  sum type.
- ``resolve_for_action`` — the action-level composition: resolve, compile,
  gate coverage, and the sole route writer.
- ``runtime_binding`` — dispatch-time binding material from a persisted route.
- ``action_identity`` / ``claim_labels`` — identity hashing and display text.

There is intentionally no ``__all__`` re-export façade: production imports
names from their owning submodules, and a partial package-root façade would
create a second, misleading answer to "what is this package's API?"
"""
