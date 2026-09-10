"""Read-only compare/preview services.

Family contract, machine-enforced by the ``preview-never-imports-execution``
boundary rule (scripts/ci/import_boundaries.json): every module here is
read-only — no receipts, no runs,
no job queue, no project writes. Results are ephemeral request-local payloads;
anything a preview returns that a commit later validates (e.g. the
``value_hash`` staleness anchor) is recomputed by the receipted action at
commit time.

Two sub-families share the package, not a runner (see ``common.py``'s scope
note): the scratch/bake-off compares
(``ocr``, ``transcribe``, ``translate``, ``topic_segmentation``) and the
whole-column store readers (``cluster``, ``column_values``, ``replace_rules``,
``query``).

Callers import from the specific service module, never from the package root.
"""

__all__: list[str] = []
