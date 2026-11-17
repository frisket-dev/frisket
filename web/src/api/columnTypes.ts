import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type { ColumnTypeInfo } from './types';

type ColumnTypesWire = HttpContractSuccessResponse<'tenant.list_column_types.get'>;
type ProjectColumnTypesWire =
  HttpContractSuccessResponse<'tenant.list_project_column_types.get'>;

interface ColumnTypesApi {
  listColumnTypes(options?: RequestOptions): Promise<ColumnTypesWire>;
  listProjectColumnTypes(
    projectId: string,
    options?: RequestOptions,
  ): Promise<ProjectColumnTypesWire>;
}

interface ColumnTypesDomainApi {
  listColumnTypes(): Promise<ColumnTypeInfo[]>;
}

export function createColumnTypesApi(): ColumnTypesApi {
  return {
    listColumnTypes(options = {}) {
      return httpContract(
        'tenant.list_column_types.get',
        {
          signal: options.signal,
        },
      );
    },

    listProjectColumnTypes(projectId, options = {}) {
      return httpContract(
        'tenant.list_project_column_types.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
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
  projectId?: string | null,
): ColumnTypesDomainApi {
  const columnTypesApi = createColumnTypesApi();

  return {
    async listColumnTypes() {
      const wire = projectId == null
        ? await columnTypesApi.listColumnTypes()
        : await columnTypesApi.listProjectColumnTypes(projectId);
      return wire.map(columnTypeInfoFromWire);
    },
  };
}
