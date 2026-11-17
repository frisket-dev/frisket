# Runtime reaches historical upgrades only through `upgrade_project`

Runtime code calls `frisket.upgrades.project.upgrade_project`; individual converters are private to the upgrades package (rule `project-upgrades-have-one-entry` in `scripts/ci/import_boundaries.json`). A current project never loads a converter, and deleting an old converter never means untangling runtime readers or writers.

**Consequences:** runtime code that needs an old project's data reads it after the upgrade, never by calling a converter.
