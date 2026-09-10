import { vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { RunEstimate } from '../../src/api/types';

const ACTION_FORM_PROJECT = 'action-form-test';
export const actionFormProjectApi = createProjectApi(ACTION_FORM_PROJECT);

export function mockActionApiDefaults(estimate: Partial<RunEstimate> = {}): void {
  vi.spyOn(actionFormProjectApi, 'estimateAction').mockResolvedValue({
    cost: 0,
    rows: 1,
    llm: true,
    ...estimate,
  });
  vi.spyOn(actionFormProjectApi, 'listSheets').mockResolvedValue([]);
}
