import { afterEach, describe, expect, it, vi } from 'vitest';

import { createRuntimeProjectionsApi } from '../../src/api/runtimeProjections';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const timelineArtifact = {
  schemaVersion: 'frisket.timeline_projection_artifact.v1',
  generation: 'generation-1',
  artifactId: 'artifact-1',
  projectionKind: 'timeline',
  target: {
    sheetId: 17, dateColumnId: 23, titleColumnId: 'title', caseColumnId: 29, futureTarget: true,
  },
  params: { includeFuture: true },
  columns: { dateColumnId: 23, titleColumnId: 'title', caseColumnId: 29, futureColumn: 'kept' },
  metrics: { sourceRowCount: 1, timelineItemCount: 1, futureMetric: 9 },
  items: [{
    sourceRowId: 42, date: '2026-08-13', title: 'Review', caseId: 99, futureItem: true,
  }],
  pluginPayload: { future: [1] },
};

afterEach(() => vi.unstubAllGlobals());

describe('runtime projection artifact HTTP contract', () => {
  it('uses the generated operation once with aliases and caller options', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(timelineArtifact);
    }));
    const errorFactory = vi.fn((status: number) => new Error(`status ${status}`));
    const controller = new AbortController();
    await expect(createRuntimeProjectionsApi(errorFactory, 'project /%?☃').artifact({
      projectionKind: 'timeline', artifactId: 'artifact-1', target: { sheetId: 'sheet-1' },
      params: { includeFuture: true },
    }, {
      signal: controller.signal,
      headers: { Authorization: 'Bearer token' },
    })).resolves.toMatchObject({
      artifactId: 'artifact-1',
      target: {
        sheetId: '17', dateColumnId: '23', titleColumnId: 'title', caseColumnId: '29',
        futureTarget: true,
      },
      columns: {
        dateColumnId: '23', titleColumnId: 'title', caseColumnId: '29', futureColumn: 'kept',
      },
      items: [{
        sourceRowId: 42, date: '2026-08-13', title: 'Review', caseId: '99', futureItem: true,
      }],
      pluginPayload: { future: [1] },
    });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe(
      '/api/projects/project%20%2F%25%3F%E2%98%83/projections/runtime/artifact',
    );
    expect(requests[0]?.init?.method).toBe('POST');
    expect(requests[0]?.init?.signal).toBe(controller.signal);
    expect(requests[0]?.init?.body).toBe(
      '{"projectionKind":"timeline","artifactId":"artifact-1","target":{"sheetId":"sheet-1"},"params":{"includeFuture":true}}',
    );
    const headers = new Headers(requests[0]?.init?.headers);
    expect(headers.get('authorization')).toBe('Bearer token');
    expect(headers.get('content-type')).toBe('application/json');
  });

  it.each([400, 404, 422, 500, 502])('maps declared HTTP %s errors without retry', async (status) => {
    const fetch = vi.fn(async () => jsonResponse({ detail: `failure ${status}` }, status));
    vi.stubGlobal('fetch', fetch);
    const errorFactory = vi.fn((received: number) => new Error(`status ${received}`));
    await expect(createRuntimeProjectionsApi(errorFactory, 'project /%?☃').artifact({
      projectionKind: 'timeline', artifactId: 'artifact-1', target: { sheetId: 'sheet-1' },
    })).rejects.toThrow(`status ${status}`);

    expect(errorFactory).toHaveBeenCalledWith(status, { detail: `failure ${status}` });
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('rejects a malformed successful timeline artifact', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ...timelineArtifact,
      metrics: { sourceRowCount: -1, timelineItemCount: 1 },
    })));
    await expect(createRuntimeProjectionsApi(
      () => new Error('HTTP error'),
      'project /%?☃',
    ).artifact({
      projectionKind: 'timeline', artifactId: 'artifact-1', target: { sheetId: 'sheet-1' },
    })).rejects.toThrow('Invalid runtime projection artifact');
  });

});
