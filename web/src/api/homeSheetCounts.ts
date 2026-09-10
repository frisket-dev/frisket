import { listSheetsContract } from './httpContractRoutes';

class SheetCountsHttpError extends Error {}

/** Home-card count adapter. HTTP errors degrade to zero; transport failures
 * reject so the caller retains its existing no-state-update failure path. */
export function fetchProjectSheetCounts(projectId: string): Promise<{ sheets: number; rows: number }> {
  return listSheetsContract(projectId, () => new SheetCountsHttpError())
    .catch((error: unknown) => {
      if (error instanceof SheetCountsHttpError) return [];
      throw error;
    })
    .then((sheets) => ({
      sheets: sheets.length,
      rows: sheets.reduce((sum, sheet) => sum + (Number(sheet.rowCount) || 0), 0),
    }));
}
