import { afterEach, describe, expect, it, vi } from 'vitest';
import { downloadEvaluation } from '../../src/api/raw/evaluationDownloads';
import type { EvaluationRequest } from '../../src/api/evaluation';

const request: EvaluationRequest = {
  basis: { kind: 'run', run_id: 4, column_id: 6 }, row_ids: [1], link_id: null,
  variants: [{ key: 'A', label: 'Current values', source: { kind: 'current' } }],
};
afterEach(() => vi.unstubAllGlobals());

describe('evaluation attachments', () => {
  it.each([
    ['download.csv', 'text/csv', 'id,answer,A_match\r\n1,"line one\nline two",yes\r\n'],
    ['settings.json', 'application/json', '{"variants":[{"key":"A"}]}'],
  ] as const)('downloads %s as bytes rather than the JSON response decoder', async (resource, mime, content) => {
    const transport = vi.fn().mockResolvedValue(new Response(content, { headers: { 'Content-Type': mime } }));
    vi.stubGlobal('fetch', transport);
    const result = await downloadEvaluation('project / one', resource, request, () => new Error('failed'));
    expect(await result.text()).toBe(content);
    expect(transport).toHaveBeenCalledWith(`/api/projects/project%20%2F%20one/evaluation/${resource}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
    });
  });

  it('surfaces permission errors instead of downloading an error document', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"Review access required"}', { status: 403 })));
    const error = new Error('Review access required');
    const errorFactory = vi.fn().mockReturnValue(error);
    await expect(downloadEvaluation('p', 'download.csv', request, errorFactory)).rejects.toBe(error);
    expect(errorFactory).toHaveBeenCalledWith(403, { detail: 'Review access required' });
  });
});
