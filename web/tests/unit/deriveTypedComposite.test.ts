// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';
import { createProjectApi } from '../../src/api/real';
import type { RegisteredActionRequest, DeriveCompositeRequest } from '../../src/api/types';
import { actionExecutionId, isDeriveCompositeRequest } from '../../src/api/types';

beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());

const extractionRequest = (): RegisteredActionRequest => ({
  action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [] },
  params: { source: ['story'], fields: [{ name: 'items', type: 'list', items: { type: 'string' } }] },
  output_names: { items: 'found_items' }, idempotency_key: 'stable-extraction',
});

it('never reinterprets a canonical action envelope as a browser composite', () => {
  const request = { ...extractionRequest(), intent: 'derive_from_extraction' };
  expect(isDeriveCompositeRequest(request)).toBe(false);
  expect(actionExecutionId(request)).toBe('map.extract');
});

it.each([
  { sheet_name: '', itemField: 'items' },
  { sheet_name: 'Findings', itemField: 'missing' },
])('refuses invalid composite authoring before extraction: %j', async (authoring) => {
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  await expect(createProjectApi('typed-derive').runAction({
    intent: 'derive_from_extraction', extraction: extractionRequest(), ...authoring,
  })).rejects.toThrow('Choose one list field and enter a name for the new sheet.');
  expect(fetch).not.toHaveBeenCalled();
});

it('retains step identities across failure, success and new API sessions; changed destination is a new child intent', async () => {
  const request: DeriveCompositeRequest = {
    intent: 'derive_from_extraction', itemField: 'items', sheet_name: 'Findings',
    extraction: extractionRequest(), confirmation: 'quoted-promise', replace_existing: true,
  };
  const posts: RegisteredActionRequest[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as RegisteredActionRequest;
    posts.push(body);
    if (posts.length === 2) return new Response(JSON.stringify({ error: 'materialization failed' }), { status: 500 });
    const extract = body.action_id === 'map.extract';
    return new Response(JSON.stringify({
      schema_version: 'frisket.action_result.v1',
      action: { kind: body.action_id, action_id: 'action' },
      status: 'completed', run_id: extract ? 88 : null, receipt_id: 'receipt',
      errors: [], outputs: extract ? [{ kind: 'named_result', name: 'items',
        ref: { sheet_id: 7, column_id: 12, run_id: 88, route: 'items', schema: 'items_list',
          may_feed: ['derive.table_from_list'] } }] : [{ kind: 'sheet', sheet_id: 9 }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  const api = createProjectApi('typed-derive');
  await expect(api.runAction(request)).rejects.toThrow();
  await expect(api.runAction(request)).resolves.toMatchObject({ runId: '88', outputSheetId: '9' });
  expect(posts).toHaveLength(4);
  expect(posts[0]).toEqual({ ...request.extraction, confirmation: 'quoted-promise', replace_existing: true });
  expect(posts[2]).toEqual(posts[0]);
  expect(posts[3]).toEqual(posts[1]);
  expect(posts[1]).not.toHaveProperty('confirmation');
  expect(posts[1]).not.toHaveProperty('replace_existing');
  await createProjectApi('typed-derive').runAction(structuredClone(request));
  expect(posts).toHaveLength(6);
  expect(posts[4]).toEqual(posts[0]);
  expect(posts[5]).toEqual(posts[1]);
  await createProjectApi('typed-derive').runAction({ ...request, sheet_name: 'Other findings' });
  expect(posts).toHaveLength(8);
  expect(posts[6]).toEqual(posts[0]);
  expect(posts[7].sheet_name).toBe('Other findings');
  expect(posts[7].idempotency_key).not.toBe(posts[1].idempotency_key);
});

it('carries the confirmed extraction, scope, output mapping, and replacement into the ordinary chain', async () => {
  const extraction: RegisteredActionRequest = {
    action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 4] },
    params: { source: { text: '{{story}}' }, model: 'test/model',
      fields: [{ name: 'items', type: 'list', items: { type: 'string' } }] },
    output_names: { items: 'found_items' }, idempotency_key: 'extraction-identity',
  };
  const request: DeriveCompositeRequest = {
    intent: 'derive_from_extraction', itemField: 'items', sheet_name: 'Findings', extraction,
    confirmation: 'quoted-promise', replace_existing: true,
  };
  const posts: RegisteredActionRequest[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(input)).toBe('/api/projects/typed-derive/actions/v1/run');
    const body = JSON.parse(String(init?.body)) as RegisteredActionRequest;
    posts.push(body);
    const extract = body.action_id === 'map.extract';
    return new Response(JSON.stringify({
      schema_version: 'frisket.action_result.v1',
      action: { kind: body.action_id, action_id: `action-${posts.length}` },
      status: 'completed', run_id: extract ? 88 : null, receipt_id: `receipt-${posts.length}`,
      errors: [], outputs: extract ? [{ kind: 'named_result', name: 'items',
        ref: { sheet_id: 7, column_id: 12, run_id: 88, route: 'items', schema: 'items_list',
          may_feed: ['derive.table_from_list'] } }] : [{ kind: 'sheet', sheet_id: 9 }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  await expect(createProjectApi('typed-derive').runAction(request)).resolves.toMatchObject({ runId: '88' });
  expect(posts[0]).toEqual({ ...extraction, confirmation: 'quoted-promise', replace_existing: true });
  expect(posts[1]).toMatchObject({ action_id: 'derive.table_from_list', scope: { kind: 'project' },
    sheet_name: 'Findings', params: { source: { kind: 'named_result', sheet_id: 7, column_id: 12, run_id: 88 } } });
  expect(posts[1]).not.toHaveProperty('confirmation');
  expect(posts[1].idempotency_key).not.toBe(extraction.idempotency_key);
});
