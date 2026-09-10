# Current-epoch corpus for execution route records

`tests/execution/test_route_corpus.py` round-trips every fixture accepted in
the current reset epoch through the current readers.

## Reset-epoch discipline

- The two named promise-set fixtures were reseeded for the GD-07 reset.
  Their rows contain exactly the current six hashed keys; retired
  enforcement keys and the retired credential row have no decoder.
- Pinned `promise_set_hash` values are the current canonical identities
  (sha256 over canonical JSON with the `frisket.promise_set.v1` domain).
  The store validates those hashes on read and write; a stale pin refuses.
- Future row/document additions remain decode-preserved. This is extension
  tolerance for current rows, not a bridge for retired representations.
- `hash_families_gen1.json` pins EVERY hash family in
  `frisket.execution.promises.HASH_FAMILIES`, including
  `frisket.action_identity.v1`. The retired target-definition family is
  absent.
- `registry_tables_gen1.json` pins every versioned side table
  (`egress.v1`, `gpu_throughput.v1`) any recorded promise may reference.
  A table version, once published, is immutable: a semantic correction is
  a NEW version (`gpu_throughput.v2`) published alongside, plus a new
  fixture entry — editing a published table would silently rewrite the
  meaning of every recorded promise that pinned it.
- `promise_set_future_unknowns.json` is deliberately from an imaginary
  NEWER writer (unknown op, unknown field, unknown row/document keys): it
  proves current-row extension preservation. Unknown vocabulary is
  recorded as unevaluable.

This mirrors the alias-replay-corpus discipline used elsewhere in the
test suite.
