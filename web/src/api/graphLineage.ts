import { httpContract, type RequestOptions } from './httpContract';
import type { LineageDag, SheetGraphOptions, SheetGraphResult } from './types';

export function createGraphLineageApi(projectId: string) {
  return {
    async getLineage(options: RequestOptions = {}): Promise<LineageDag> {
      return httpContract(
        'tenant.project_lineage.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    async getSheetGraph(
      input: SheetGraphOptions,
      options: RequestOptions = {},
    ): Promise<SheetGraphResult> {
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
        },
      );
    },
  };
}
