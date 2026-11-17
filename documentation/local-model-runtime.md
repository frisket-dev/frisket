# Local model runtime recovery

The app starts its optional native model server in the background. A running
HTTP server and a loaded model are separate facts: `/capabilities` reports each
engine's `configured`, `available`, `loaded`, and `error` values. Passive checks
never retry a failed loader.

Explicit native Docling setup checks the installed environment and server,
then retries a cached Docling loader failure once. A cold, healthy model stays
lazy. Cancellation before recovery prevents a new load.

Operators of a standalone sidecar can deliberately retry a failed resident
loader with authenticated `POST /engines/{name}/retry?route={route}`. The route
must match that engine's registered adapter, for example
`/engines/docling/retry?route=%2Fto-markdown`. Send the same bearer authorization
used for inference. Recovery consumes the ordinary inference capacity slot;
capacity exhaustion returns 429, and a failed load returns 503. It does not
automatically repeat. A successfully loaded adapter is retained, including when
other requests are using it. Route adapters sharing a model recover together.

`LocalModelServer` owns its guarded child through stopped, starting, ready,
stopping, and cleanup-failed states. Cleanup failure retains the child handle
and blocks replacement. Its explicit `restart()` retries cleanup before
scheduling a replacement. Shutdown never treats an exited guardian alone as
proof that its process tree was cleaned up.

## Engine execution ownership

Every engine invocation enters `frisket.execution.purchases` through
`execute_purchase` or its synchronous wrapper. Both use the same lifecycle:
check cancellation, admit the actual request, invoke the engine, then record
returned usage before interpreting the result. Each provider retry enters
again; repairing accounting must not repeat a completed provider call.

Composition supplies the policy: local, user-key, included, or metered work.
Local inference follows the same boundary without creating billing records.
Cache hits do not invoke engines. Polling a submitted provider job stays
inside that purchase; it does not buy the job again.

Integrations expose a guarded facade and keep raw transports private. New
engines should implement the invocation and observation at that facade, not
add admission or settlement hooks to actions. The import-boundary check in
`scripts/ci/check_import_boundaries.py` restricts raw adapters and policy
construction to their owners. This catches accidental bypasses; it is not a
sandbox for arbitrary Python plugins.
