import { httpContract } from './httpContract';
import type { LineageDag, SheetGraphOptions, SheetGraphResult } from './types';

export interface GraphLineageOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface GraphLineageApi {
  getLineage(projectId: string, options?: GraphLineageOptions): Promise<LineageDag>;
  getSheetGraph(
    projectId: string,
    input: SheetGraphOptions,
    options?: GraphLineageOptions,
  ): Promise<SheetGraphResult>;
}

export function createGraphLineageApi(errorFactory: ContractErrorFactory): GraphLineageApi {
  return {
    getLineage(projectId, options = {}) {
      return httpContract(
        'tenant.project_lineage.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getSheetGraph(projectId, input, options = {}) {
      const setColumn = (value: number | string | null | undefined) => (
        value !== null && value !== undefined && String(value).trim() !== ''
          ? Number(value)
          : undefined
      );
      return httpContract(
        'tenant.sheet_graph.get',
        {
          pathParams: { pid: projectId, sheet_id: Number(input.sheetId) },
          query: {
            direction: input.direction,
            node_label_column_id: setColumn(input.nodeLabelColumnId),
            node_color_column_id: setColumn(input.nodeColorColumnId),
            node_size_column_id: setColumn(input.nodeSizeColumnId),
            edge_label_column_id: setColumn(input.edgeLabelColumnId),
            limit_nodes: input.limitNodes ?? 200,
            limit_edges: input.limitEdges ?? 500,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
