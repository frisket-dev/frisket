// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { guideSamplesApi } from '../../src/api/guideSamples';

afterEach(() => vi.unstubAllGlobals());

it('reuses the operation for project creation and preparation on retry', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'p', name: 'Sample' }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'retry' }), { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'p', name: 'Sample' }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ project_id: 'p', destination: { kind: 'sheet', sheet_id: 1, guide_id: 'g', step_id: 's' } }), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  await expect(guideSamplesApi.launch('council-recordings', 1, 'operation-1', 'Sample')).rejects.toThrow();
  await guideSamplesApi.launch('council-recordings', 1, 'operation-1', 'Sample');
  expect(fetch).toHaveBeenCalledTimes(4);
  for (const index of [0, 2]) {
    const [url, init] = fetch.mock.calls[index];
    expect(url).toBe('/api/projects');
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('operation-1');
  }
  for (const index of [1, 3]) {
    const [url, init] = fetch.mock.calls[index];
    expect(url).toBe('/api/projects/p/import/guide-samples/council-recordings/prepare');
    expect(JSON.parse(init.body)).toEqual({ revision: 1, operation_id: 'operation-1' });
  }
});
