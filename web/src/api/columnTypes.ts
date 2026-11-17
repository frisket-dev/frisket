import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { ColumnTypeInfo } from './types';

type ColumnTypesWire = HttpContractSuccessResponse<'tenant.list_column_types.get'>;

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
export function createColumnTypesApi(projectId?: string | null) {
  return {
    /** The column-type registry (core + plugin types, with presentation hints). */
    async listColumnTypes(): Promise<ColumnTypeInfo[]> {
      const wire = projectId == null
        ? await httpContract('tenant.list_column_types.get', {})
        : await httpContract('tenant.list_project_column_types.get', {
          pathParams: { pid: projectId },
        });
      return wire.map(columnTypeInfoFromWire);
    },
  };
}
