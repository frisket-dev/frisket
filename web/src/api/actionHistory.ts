import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { GeneratedActionDraft } from './types';

export type ActionHistoryPage = HttpContractSuccessResponse<'tenant.action_history.get'>;
export type ActionHistoryItem = ActionHistoryPage['items'][number];
type HistoryDetailWire = HttpContractSuccessResponse<'tenant.action_history_detail.get'>;
export type ActionHistoryDetail = Omit<HistoryDetailWire, 'draft'> & { draft: GeneratedActionDraft | null };

export function createActionHistoryApi(errorFactory: (status: number, payload: unknown) => Error, projectId: string) {
  return {
    getActionHistory(actionId: string, offset = 0, limit = 20): Promise<ActionHistoryPage> {
      return httpContract('tenant.action_history.get', {
        pathParams: { pid: projectId }, query: { action_id: actionId, offset, limit }, errorFactory,
      });
    },
    async getActionHistoryEntry(historyId: string): Promise<ActionHistoryDetail> {
      const detail = await httpContract('tenant.action_history_detail.get', {
        pathParams: { pid: projectId, history_id: historyId }, query: {}, errorFactory,
      });
      const raw = detail.draft;
      return { ...detail, draft: raw === null ? null : {
        action_id: raw.action_id, params: raw.params, output_names: raw.output_names ?? {},
        scope: raw.scope.kind === 'project' ? { kind: 'project' }
          : { kind: 'sheet_rows', sheet_id: raw.scope.sheet_id,
            ...(raw.scope.row_ids == null ? {} : { row_ids: raw.scope.row_ids }) },
        ...(raw.sheet_name == null ? {} : { sheet_name: raw.sheet_name }),
      } };
    },
  };
}
