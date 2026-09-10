"""Generate an ActionCatalogEntry from an `@op` declaration.

DERIVED by the generator (no author input): input/output JSON schema (from the
declared models), the ui-hint `form` name (the kind suffix), and the structural
defaults (async_mode, writes_project, receipt_policy). DECLARED prose passes
through unchanged; plugin operations must declare their own errors.
"""

from __future__ import annotations

from frisket.contracts.action import (
    CURRENT_ACTION_AUTHORING_CONTRACT_VERSION,
    ActionCatalogEntry,
    ActionErrorSpec,
)
from frisket.sdk.declaration import Op


def build_catalog(decl: Op) -> ActionCatalogEntry:
    ui_hints = {
        "form": decl.form or decl.slug,
        "primary_fields": list(decl.primary_fields),
        **decl.extra_ui_hints,
    }
    if decl.inputs is not None:
        if "source_requirements" in decl.extra_ui_hints:
            raise ValueError(
                f"{decl.kind} declares source_requirements both in inputs and ui_hints"
            )
        ui_hints["source_requirements"] = decl.inputs.catalog_requirements()

    return ActionCatalogEntry(
        kind=decl.kind,
        authoring_contract_version=CURRENT_ACTION_AUTHORING_CONTRACT_VERSION,
        title=decl.title,
        description=decl.description,
        # DERIVED: schemas come straight from the declared models.
        input_schema=decl.params_model.model_json_schema(),
        output_schema=decl.output_model.model_json_schema(),
        errors=_catalog_errors(decl),
        side_effects=list(decl.side_effects),
        required_capabilities=_required_capabilities(decl),
        conditional_capabilities=list(decl.conditional_capabilities),
        cost_policy=decl.cost,
        idempotency=decl.idempotency,
        retry_policy=decl.retry,
        execution_mode=decl.execution_mode,
        async_mode=decl.async_mode,
        writes_project=decl.writes_project,
        examples=list(decl.examples),
        row_scope_policy=decl.row_scope_policy,
        # DERIVED: form name is the kind suffix and source requirements come
        # directly from the typed input descriptor.
        ui_hints=ui_hints,
        receipt_policy=decl.receipt_policy,
    )


def _catalog_errors(decl: Op) -> list[ActionErrorSpec]:
    if decl.errors is not None:
        return list(decl.errors)

    raise LookupError(f"action errors must be declared for {decl.kind!r}")


def _required_capabilities(decl: Op) -> list[str]:
    # DERIVED from policy: project-writing ops need project:write; model-backed ops
    # need model:complete. Matches the hand-written ops.
    caps: list[str] = []
    if decl.writes_project:
        caps.append("project:write")
    conditional_names = {
        item.get("capability") for item in decl.conditional_capabilities
    }
    if decl.model_backed and "model:complete" not in conditional_names:
        caps.append("model:complete")
    if decl.external_capability:
        caps.append(f"external:{decl.external_capability}")
    # op-declared extras (e.g. map.python's unsafe:local_code) that the policy flags
    # don't derive.
    caps.extend(decl.extra_capabilities)
    return caps
