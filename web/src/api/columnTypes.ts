import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { ColumnTypeInfo } from './types';

export interface ColumnTypesOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type ColumnTypesWire = HttpContractSuccessResponse<'tenant.list_column_types.get'>;
export type ProjectColumnTypesWire =
  HttpContractSuccessResponse<'tenant.list_project_column_types.get'>;

export interface ColumnTypesApi {
  listColumnTypes(options?: ColumnTypesOptions): Promise<ColumnTypesWire>;
  listProjectColumnTypes(
    projectId: string,
    options?: ColumnTypesOptions,
  ): Promise<ProjectColumnTypesWire>;
}

export interface ColumnTypesDomainApi {
  listColumnTypes(): Promise<ColumnTypeInfo[]>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export function createColumnTypesApi(errorFactory: ContractErrorFactory): ColumnTypesApi {
  return {
    listColumnTypes(options = {}) {
      return httpContract(
        'tenant.list_column_types.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listProjectColumnTypes(projectId, options = {}) {
      return httpContract(
        'tenant.list_project_column_types.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

function columnTypeInfoFromWire(
  wire: ColumnTypesWire[number],
): ColumnTypeInfo {
  return {
    name: wire.name,
    core: wire.core,
    plugin: wire.plugin ?? (wire.core ? 'core' : 'external'),
    presentation: wire.presentation ?? {},
    hasValidator: wire.has_validator,
    hasParser: wire.has_parser ?? false,
    description: wire.description ?? '',
  };
}

/** Owns column-type scope selection and wire-to-public mapping. Project scope
 * is selected synchronously: only no selected project uses the global route;
 * a project request failure remains that request's failure. */
export function createColumnTypesDomainApi(
  errorFactory: ContractErrorFactory,
  projectId?: string | null,
): ColumnTypesDomainApi {
  const columnTypesApi = createColumnTypesApi(errorFactory);

  return {
    async listColumnTypes() {
      const wire = projectId == null
        ? await columnTypesApi.listColumnTypes()
        : await columnTypesApi.listProjectColumnTypes(projectId);
      return wire.map(columnTypeInfoFromWire);
    },
  };
}
