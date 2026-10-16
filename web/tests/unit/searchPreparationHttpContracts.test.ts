import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createSearchPreparation,
  decideSearchPreparation,
  getSearchPreparation,
  getSearchPreparationTextSamples,
  listSearchPreparations,
  stopSearchPreparation,
} from '../../src/api/searchPreparation';

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

const batch = {
  schemaVersion: 'frisket.search_preparation.v1',
  id: 'prep / 1',
  sheet_id: 7,
  plan_hash: 'sha256:plan',
  status: 'planning',
  groups: [],
  models: [],
  jobs: [],
  can_stop: false,
  can_setup: false,
  errors: [],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('search preparation generated HTTP contracts', () => {
  it('renders encoded paths, request bodies, and bounded text-sample queries', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      if (String(input).includes('/text-samples')) {
        return jsonResponse({
          schemaVersion: 'frisket.search_preparation_text_samples.v1',
          batch_id: batch.id,
          samples: [],
        });
      }
      if (String(input).endsWith('/search-preparation') && init?.method === 'GET') {
        return jsonResponse({ batches: [batch] });
      }
      return jsonResponse(batch);
    }));

    await createSearchPreparation('project / 1', {
      sheet_id: 7,
      row_ids: [42],
      force_ocr: true,
    });
    await listSearchPreparations('project / 1');
    await getSearchPreparation('project / 1', batch.id);
    await decideSearchPreparation('project / 1', batch.id, {
      plan_hash: batch.plan_hash,
      decision: 'prepare',
      selected_setup_refs: ['engine-setup:ocr.local@1'],
    });
    await stopSearchPreparation('project / 1', batch.id);
    await getSearchPreparationTextSamples('project / 1', batch.id, {
      rowId: 42,
      pageNumber: 3,
    });

    const base = '/api/projects/project%20%2F%201/search-preparation';
    const selected = `${base}/prep%20%2F%201`;
    expect(requests.map(({ input, init }) => [String(input), init?.method])).toEqual([
      [base, 'POST'],
      [base, 'GET'],
      [selected, 'GET'],
      [`${selected}/decision`, 'POST'],
      [`${selected}/stop`, 'POST'],
      [`${selected}/text-samples?row_id=42&page_number=3`, 'GET'],
    ]);
    expect(JSON.parse(String(requests[0].init?.body))).toEqual({
      sheet_id: 7,
      row_ids: [42],
      force_ocr: true,
    });
    expect(JSON.parse(String(requests[3].init?.body))).toEqual({
      plan_hash: 'sha256:plan',
      decision: 'prepare',
      selected_setup_refs: ['engine-setup:ocr.local@1'],
    });
    expect(requests[4].init?.body).toBeUndefined();
  });
});
