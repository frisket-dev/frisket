import { afterEach, describe, expect, it, vi } from 'vitest';

import { createRuntimeProjectionsApi } from '../../src/api/runtimeProjections';
import { jsonResponse } from '../support/httpContractTestHelpers';

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
    const controller = new AbortController();
    await expect(createRuntimeProjectionsApi('project /%?☃').readRuntimeProjectionArtifact({
      projectionKind: 'timeline', artifactId: 'artifact-1', target: { sheetId: 'sheet-1' },
      params: { includeFuture: true },
    }, {
      signal: controller.signal,
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
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBe('application/json');
  });

  it('rejects a malformed successful timeline artifact', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ...timelineArtifact,
      metrics: { sourceRowCount: -1, timelineItemCount: 1 },
    })));
    await expect(createRuntimeProjectionsApi('project /%?☃').readRuntimeProjectionArtifact({
      projectionKind: 'timeline', artifactId: 'artifact-1', target: { sheetId: 'sheet-1' },
    })).rejects.toThrow('Invalid runtime projection artifact');
  });

});
