# Tests do not count as usage

Production code that only tests reach is deleted, or moved under `tests/` when it is a parity oracle or reference implementation. `scripts/ci/check_dead_symbols.py` counts `src/frisket` and the listed `CONSUMERS` as production, never `tests/`, so a symbol cannot stay alive because a test calls it.

**Consequences:** a test that arranges or observes state by calling an internal function directly keeps it alive only if production calls it too; otherwise the test moves to the production entry point.
