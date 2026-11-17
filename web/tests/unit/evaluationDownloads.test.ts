import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../src/api/contractErrors';
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
    const result = await downloadEvaluation('project / one', resource, request);
    expect(await result.text()).toBe(content);
    expect(transport).toHaveBeenCalledWith(`/api/projects/project%20%2F%20one/evaluation/${resource}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
    });
  });

  it('surfaces permission errors instead of downloading an error document', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"Review access required"}', { status: 403 })));
    const rejection = downloadEvaluation('p', 'download.csv', request);
    await expect(rejection).rejects.toBeInstanceOf(ApiError);
    await expect(rejection).rejects.toMatchObject({
      name: 'ApiError', status: 403, message: 'Review access required', code: undefined, details: undefined,
    });
  });

  it('falls back to the HTTP status when a failed download has no JSON body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>gateway</html>', { status: 502 })));
    await expect(downloadEvaluation('p', 'settings.json', request)).rejects.toMatchObject({
      name: 'ApiError', status: 502, message: 'Request failed (HTTP 502)',
    });
  });
});
