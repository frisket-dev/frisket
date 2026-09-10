import { describe, expect, it, vi } from 'vitest';

import { fetchDiagnostics } from '../../src/api/diagnostics';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('diagnostics generated HTTP contracts', () => {
  it.each([
    [undefined, '/api/diagnose'],
    ['project id%', '/api/projects/project%20id%25/diagnose'],
  ])('uses the generated Diagnose operation for %s', async (projectId, path) => {
    const payload = {
      healthy: true,
      core: { store: { ok: true } },
      info: { future_probe: { nested: [true, null, 3.5] } },
    };
    const fetch = vi.fn(async () => jsonResponse(payload));

    await expect(fetchDiagnostics(projectId, { fetch })).resolves.toEqual(payload);

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe(path);
    expect(fetch.mock.calls[0]?.[1]).toEqual({ method: 'GET' });
  });

  it('preserves the legacy status-only error for a failed Diagnose response', async () => {
    await expect(fetchDiagnostics(undefined, {
      fetch: async () => jsonResponse({ detail: 'hidden' }, 503),
    })).rejects.toThrow('diagnose returned 503');
  });
});
