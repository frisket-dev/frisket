import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import {
  getGlobalActionCatalog,
  getProjectActionCatalog,
} from '../../src/api/httpContractRoutes';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';

beforeAll(() => { servedActionCatalog(); }, 30_000);

function catalogWire(): Record<string, unknown> {
  const conditional = syntheticActionCatalogEntry('map.ner', {
    input_schema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        engine: { type: 'string', enum: ['spacy', 'llm'], default: 'spacy' },
      },
    },
    conditional_capabilities: [{
      capability: 'model:complete',
      when: { param: 'engine', value: 'llm' },
    }],
    row_scope_policy: {
      kind: 'sheet_rows',
      selectors: ['all_rows', 'exact_membership'],
    },
  });
  const withoutConditional: Record<string, unknown> = {
    ...conditional,
    kind: 'fixture.without_conditional',
  };
  delete withoutConditional.conditional_capabilities;
  delete withoutConditional.row_scope_policy;
  const projectScoped: Record<string, unknown> = {
    ...conditional,
    kind: 'source.create',
    row_scope_policy: { kind: 'project' },
    ui_hints: { form: 'source_create' },
  };

  return {
    schema_version: 'frisket.action_catalog.v2',
    action_schema: {},
    error_schema: {},
    result_schema: {},
    receipt_schema: {},
    validation_result_schema: {},
    actions: [
      {
        ...conditional,
        ui_hints: {
          ...conditional.ui_hints,
          commercial_overlay: {
            target: {
              target_id: 'hosted:fixture',
              capability: 'model:complete',
              terms_version: 'terms-2026-08-07',
              charge_authority: 'platform',
            },
            presentation: {
              venue_label: 'Fixture cloud',
              billing_label: 'Fixture credits',
              cost_source: 'composition_offering',
            },
            choices: [{ id: 'remote', labels: { short: 'Cloud', long: 'Cloud execution' } }],
          },
        },
      },
      withoutConditional,
      projectScoped,
    ],
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

class MappedCatalogError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped catalog error ${status}`);
    this.name = 'MappedCatalogError';
    this.status = status;
    this.payload = payload;
  }
}

function errorFactory(status: number, payload: unknown): Error {
  return new MappedCatalogError(status, payload);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('action catalog HTTP contracts', () => {
  it('uses exact global/project URLs and forwards exact optional headers and signals', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(catalogWire());
    }));
    const globalController = new AbortController();
    const projectController = new AbortController();
    const globalHeaders = new Headers({ Authorization: 'Bearer global', 'X-Trace-Id': 'global' });
    const projectHeaders = new Headers({ Authorization: 'Bearer project', 'X-Trace-Id': 'project' });

    await getGlobalActionCatalog(errorFactory, {
      signal: globalController.signal,
      headers: globalHeaders,
    });
    await getProjectActionCatalog('tenant/project +%', errorFactory, {
      signal: projectController.signal,
      headers: projectHeaders,
    });

    expect(requests.map((request) => request.input)).toEqual([
      '/api/actions/v1/catalog',
      '/api/projects/tenant%2Fproject%20%2B%25/actions/v1/catalog',
    ]);
    expect(requests[0]?.init?.method).toBe('GET');
    expect(requests[1]?.init?.method).toBe('GET');
    expect(requests[0]?.init?.signal).toBe(globalController.signal);
    expect(requests[1]?.init?.signal).toBe(projectController.signal);
    expect(requests[0]?.init?.headers).toBe(globalHeaders);
    expect(requests[1]?.init?.headers).toBe(projectHeaders);
  });

  it('preserves abort rejection identity', async () => {
    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    }));
    const pending = getGlobalActionCatalog(errorFactory, { signal: controller.signal });
    const abortError = new DOMException('catalog cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });

  it('maps conditional capabilities and leaves nested commercial/ui hints untouched', async () => {
    const wire = catalogWire();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(wire)));

    const catalog = await getProjectActionCatalog('project-1', errorFactory);

    expect(catalog.actions[0]?.conditional_capabilities).toEqual([
      {
        capability: 'model:complete',
        when: { param: 'engine', value: 'llm' },
      },
    ]);
    expect(catalog.actions[1]?.conditional_capabilities).toEqual([]);
    expect(catalog.actions[0]?.row_scope_policy).toEqual({
      kind: 'sheet_rows',
      selectors: ['all_rows', 'exact_membership'],
    });
    expect(catalog.actions[1]?.row_scope_policy).toBeNull();
    expect(catalog.actions[2]?.row_scope_policy).toEqual({ kind: 'project' });
    expect(catalog.actions[0]?.ui_hints).toEqual(
      (wire.actions as Array<Record<string, unknown>>)[0]?.ui_hints,
    );
    expect(catalog.actions[0]?.ui_hints.commercial_overlay).toEqual({
      target: {
        target_id: 'hosted:fixture',
        capability: 'model:complete',
        terms_version: 'terms-2026-08-07',
        charge_authority: 'platform',
      },
      presentation: {
        venue_label: 'Fixture cloud',
        billing_label: 'Fixture credits',
        cost_source: 'composition_offering',
      },
      choices: [{ id: 'remote', labels: { short: 'Cloud', long: 'Cloud execution' } }],
    });
  });

  it.each(['global', 'project'] as const)(
    'maps required v1 authoring-contract versions for the %s catalog',
    async (scope) => {
      const wire = catalogWire();
      const actions = wire.actions as Array<Record<string, unknown>>;
      if (!actions[0] || !actions[1]) throw new Error('two catalog actions are required');
      actions[0].authoring_contract_version = 1;
      actions[1].authoring_contract_version = 1;
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(wire)));

      const mapped = scope === 'global'
        ? await getGlobalActionCatalog(errorFactory)
        : await getProjectActionCatalog('project-1', errorFactory);

      expect(mapped.actions.map((action) => action.authoring_contract_version)).toEqual([1, 1, 1]);
    },
  );

  it.each(['global', 'project'] as const)(
    'fails closed when the %s catalog omits authoring_contract_version',
    async (scope) => {
      const wire = catalogWire();
      const first = (wire.actions as Array<Record<string, unknown>>)[0];
      if (!first) throw new Error('catalog action is required');
      delete first.authoring_contract_version;
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(wire)));

      const request = scope === 'global'
        ? getGlobalActionCatalog(errorFactory)
        : getProjectActionCatalog('project-1', errorFactory);
      await expect(request).rejects.toThrow(
        'action catalog entry map.ner has invalid authoring_contract_version',
      );
    },
  );

  it.each([
    ['global', 401],
    ['global', 500],
    ['project', 401],
    ['project', 403],
    ['project', 404],
    ['project', 409],
    ['project', 500],
  ] as const)('accepts the exact declared %s catalog error %s', async (scope, status) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: `error ${status}` }, status)));
    const request = scope === 'global'
      ? getGlobalActionCatalog(errorFactory)
      : getProjectActionCatalog('project-1', errorFactory);
    await expect(request).rejects.toMatchObject({
      name: 'MappedCatalogError',
      status,
      payload: { detail: `error ${status}` },
    });
  });

  it.each([
    ['global', 403],
    ['global', 418],
    ['project', 422],
    ['project', 418],
  ] as const)('maps additional %s catalog status %s', async (scope, status) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'undeclared' }, status)));
    const factory = vi.fn(errorFactory);
    const request = scope === 'global'
      ? getGlobalActionCatalog(factory)
      : getProjectActionCatalog('project-1', factory);
    await expect(request).rejects.toMatchObject({
      name: 'MappedCatalogError',
      status,
      payload: { detail: 'undeclared' },
    });
    expect(factory).toHaveBeenCalledOnce();
    expect(factory).toHaveBeenCalledWith(status, { detail: 'undeclared' });
  });
});
