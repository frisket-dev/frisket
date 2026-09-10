import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { createProjectApi } from '../../src/api/real';
import type { CopilotProposal, RegisteredActionRequest } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import {
  completeMappedActionCatalog,
  syntheticActionCatalogEntry,
} from '../support/actionCatalogFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';

beforeAll(() => { servedActionCatalog(); }, 30_000);

const registeredCatalog = () => completeMappedActionCatalog({
  stockEntries: [
    syntheticActionCatalogEntry('map.template'),
    syntheticActionCatalogEntry('map.clean_dates'),
    syntheticActionCatalogEntry('map.to_geo_point'),
  ],
});

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const request: RegisteredActionRequest = {
  action_id: 'map.template',
  scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 3] },
  params: { template: { text: 'Hello {{name}}' } },
  output_names: { rendered: 'greeting' },
  idempotency_key: 'web-map.template:test',
};

afterEach(() => vi.unstubAllGlobals());

describe('registered action transport', () => {
  it('uses one canonical launcher identity without the retired aliases', () => {
    const templates = actionTemplatesFromCatalog(registeredCatalog());
    for (const [canonical, alias] of [
      ['map.template', 'template'],
      ['map.clean_dates', 'clean_dates'],
      ['map.to_geo_point', 'to_geo_point'],
    ] as const) {
      expect(templates.filter((entry) => entry.kind === canonical)).toHaveLength(1);
      expect(templates.some((entry) => String(entry.kind) === alias)).toBe(false);
    }
  });

  it('posts a run request unchanged', async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init ?? {});
      return response({
        schema_version: 'frisket.action_result.v1',
        status: 'completed',
        run_id: 11,
        errors: [],
      });
    }));

    await createProjectApi('project-one').runAction(request);

    expect(JSON.parse(String(calls[0]?.body))).toEqual(request);
  });

  it('posts a preview request unchanged', async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init ?? {});
      return response({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'preview-one',
        total: 2,
      }, 202);
    }));

    await createProjectApi('project-one').startPreview(request);

    expect(JSON.parse(String(calls[0]?.body))).toEqual(request);
  });

  it('runs a Copilot registered draft directly with one stable execution key', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'copilot-request-id' });
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'GET') {
        throw new Error(`Registered proposal unexpectedly loaded ${String(input)}`);
      }
      bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return response({
        schema_version: 'frisket.action_result.v1',
        status: 'completed',
        run_id: bodies.length + 20,
        errors: [],
      });
    }));
    const proposal: CopilotProposal = {
      kind: 'map',
      title: 'Template greeting',
      spec: {
        action_id: 'map.template',
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 3] },
        params: { template: { text: 'Hello {{name}}' } },
        output_names: { rendered: 'greeting' },
      },
    };
    const api = createProjectApi('project-one');

    await api.runProposal(proposal);
    await api.runProposal(proposal, true, 'confirmation-hash');

    expect(bodies).toEqual([
      {
        ...proposal.spec,
        idempotency_key: 'copilot-map.template:copilot-request-id',
      },
      {
        ...proposal.spec,
        idempotency_key: 'copilot-map.template:copilot-request-id',
        confirmation: 'confirmation-hash',
      },
    ]);
    expect(bodies[0]).not.toHaveProperty('action_kind');
    expect(bodies[0]).not.toHaveProperty('authoring_contract_version');
  });
});
