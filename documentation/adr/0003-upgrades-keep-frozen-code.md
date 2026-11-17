# Historical upgrades keep frozen copies of the code they depend on

Converters in `src/frisket/upgrades/project/` use pinned DDL and frozen copies of the runtime logic that defined an old format (`historical_schema.py`, `historical_current_cells.py`, the `*_migration_schema.py` files) instead of importing today's version, so a runtime change can never alter how an old project upgrades.

**Consequences:** the duplication is deliberate. The copies stay in `scripts/ci/duplicate_functions_baseline.json` and are not merged with runtime code; a runtime fix does not flow into them.
