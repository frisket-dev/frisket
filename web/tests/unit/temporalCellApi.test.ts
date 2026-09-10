import { afterEach, describe, expect, it, vi } from 'vitest';

import { getSheetDataContract } from '../../src/api/httpContractRoutes';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('temporal sheet-data values', () => {
  it('keeps canonical temporal objects structured at the frontend boundary', async () => {
    const temporalValue = {
      schema_version: 'frisket.timeline_points.v1',
      timeline: {
        artifact_stable_id: 'source_artifact:interview-video',
        fingerprint: `sha256:${'a'.repeat(64)}`,
        duration_ms: 60_000,
      },
      items: [{ id: 'cut-1', at_ms: 10_000, metadata: { frame: 240 } }],
    };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      columns: [{
        id: 7,
        name: 'cuts',
        type: 'timeline_points',
        ai_generated: true,
        format: null,
        current_run_id: 9,
        transcript_status: null,
        media_download_candidate: null,
      }],
      rows: [{
        id: 11,
        cells: { '7': temporalValue },
        meta: {
          '7': {
            current_value_ref: {
              kind: 'run_result',
              op_id: 8,
              row_id: 11,
              column_id: 7,
              run_id: 9,
            },
          },
        },
        parent_row_id: null,
        child_count: 0,
      }],
      total: 1,
    }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })));

    const page = await getSheetDataContract(
      'project-id',
      1,
      { offset: 0, limit: 1 },
      (status) => new Error(`HTTP ${status}`),
    );

    expect(page.rows[0]?.cells['7']).toEqual(temporalValue);
    expect(typeof page.rows[0]?.cells['7']).toBe('object');
  });
});
