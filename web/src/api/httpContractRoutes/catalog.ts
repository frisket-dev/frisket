import {
  httpContract,
  type HttpContractSuccessResponse,
} from '../httpContract';
import type { ActionCatalogPayload } from '../types';

type GlobalActionCatalogWire =
  HttpContractSuccessResponse<'tenant.v1_action_catalog.get'>;
type ProjectActionCatalogWire =
  HttpContractSuccessResponse<'tenant.project_v1_action_catalog.get'>;
type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface ActionCatalogContractOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

function mapActionCatalog(
  wire: GlobalActionCatalogWire | ProjectActionCatalogWire,
): ActionCatalogPayload {
  return {
    schema_version: wire.schema_version ?? 'frisket.action_catalog.v2',
    action_schema: wire.action_schema,
    error_schema: wire.error_schema,
    result_schema: wire.result_schema,
    receipt_schema: wire.receipt_schema,
    validation_result_schema: wire.validation_result_schema,
    actions: wire.actions.map((action) => {
      const authoringContractVersion = action.authoring_contract_version;
      if (authoringContractVersion !== 1) {
        throw new Error(
          `action catalog entry ${action.kind} has invalid authoring_contract_version`,
        );
      }
      return {
        kind: action.kind,
        authoring_contract_version: authoringContractVersion,
        title: action.title,
        description: action.description,
        input_schema: action.input_schema,
        output_schema: action.output_schema,
        errors: action.errors,
        side_effects: action.side_effects,
        required_capabilities: action.required_capabilities,
        conditional_capabilities: action.conditional_capabilities ?? [],
        required_credentials: action.required_credentials ?? [],
        cost_policy: action.cost_policy,
        idempotency: action.idempotency,
        retry_policy: action.retry_policy,
        execution_mode: action.execution_mode,
        async_mode: action.async_mode,
        writes_project: action.writes_project,
        examples: action.examples ?? [],
        ui_hints: action.ui_hints ?? {},
        receipt_policy: action.receipt_policy ?? 'deferred_until_receipts_table',
        row_scope_policy: action.row_scope_policy == null
          ? null
          : action.row_scope_policy.kind === 'project'
            ? { kind: 'project' }
            : {
              kind: action.row_scope_policy.kind ?? 'sheet_rows',
              selectors: action.row_scope_policy.selectors ?? [],
            },
      };
    }),
  };
}

export function getGlobalActionCatalog(
  errorFactory: ContractErrorFactory,
  options: ActionCatalogContractOptions = {},
): Promise<ActionCatalogPayload> {
  return httpContract('tenant.v1_action_catalog.get', {
    pathParams: {},
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapActionCatalog);
}

export function getProjectActionCatalog(
  projectId: string,
  errorFactory: ContractErrorFactory,
  options: ActionCatalogContractOptions = {},
): Promise<ActionCatalogPayload> {
  return httpContract('tenant.project_v1_action_catalog.get', {
    pathParams: { pid: projectId },
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapActionCatalog);
}
