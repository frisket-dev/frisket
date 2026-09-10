# Workbench Portability V1 Fixtures

This fixture package admits `workbench-portability-contract-sketch-v1`.
It is intentionally product-completion shaped: it sketches descriptor,
placement, route, host-context, layout/profile/runtime, missing-placeholder,
localStorage migration, and old-path-removal contracts before renderer
implementation starts.

The older `tests/fixtures/workbench_plugin/` package remains the V1
descriptor-host proof. This package is stricter about product portability:
every placement includes `slot` and `placementId`, negative slot resolution is
explicit, and implementation cases carry old hardcoded path removal
expectations.
