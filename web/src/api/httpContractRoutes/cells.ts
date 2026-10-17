import { httpContract, type HttpContractSuccessResponse } from '../httpContract';

export type CellTextWindow = HttpContractSuccessResponse<'tenant.cell_text_window.get'>;

/** Offsets are Unicode codepoints, and each continuation pins its revision. */
export function getCellTextWindowContract(
  projectId: string,
  rowId: string,
  columnId: string,
  options: { start: number; limit: number; revision?: number; signal?: AbortSignal },
): Promise<CellTextWindow> {
  return httpContract('tenant.cell_text_window.get', {
    pathParams: { pid: projectId, row_id: Number(rowId), column_id: Number(columnId) },
    query: { start: options.start, limit: options.limit, revision: options.revision },
    signal: options.signal,
  });
}
