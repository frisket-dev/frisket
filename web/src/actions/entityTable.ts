import type { GeneratedActionDraft } from '../api/types';

/** The receipt already contains the user's committed clustering choices. */
export function entityTableDraft(receiptId: string): GeneratedActionDraft {
  return {
    action_id: 'resolve.entities',
    scope: { kind: 'project' },
    params: { source: { kind: 'cluster_values', receipt_id: receiptId } },
    output_names: {},
    sheet_name: 'Entities',
  };
}
