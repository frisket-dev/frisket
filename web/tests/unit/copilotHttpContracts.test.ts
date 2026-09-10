import { afterEach, describe, expect, it, vi } from 'vitest';

import { copilotChatContract } from '../../src/api/httpContractRoutes';

const messages = [{ role: 'user' as const, content: 'Help me plan this' }];

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function contractError(status: number, payload: unknown): Error {
  return new Error(`unexpected contract error ${status}: ${JSON.stringify(payload)}`);
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped contract error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

async function expectMapped(request: Promise<unknown>, status: number): Promise<void> {
  await expect(request).rejects.toMatchObject({
    name: 'MappedContractError',
    status,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('copilot HTTP contracts', () => {
  it.each([
    { action_id: 'enrich.geocode', params: {
      source: { text: '{{street}}, {{city}}' }, engine: 'nominatim', include_lat_lon: true,
    }, output_names: { geo_point: 'location', formatted_address: 'address', latitude: 'lat', longitude: 'lon' } },
    { action_id: 'enrich.census_demographics', params: {
      source: 'point', geography: 'block_group', include_moe: true,
    }, output_names: Object.fromEntries([
      'demo_population', 'demo_median_age', 'demo_median_household_income', 'demo_poverty_rate',
      'demo_bachelors_plus_rate', 'demo_white_non_hispanic_pct', 'demo_black_pct', 'demo_hispanic_pct',
      'demo_asian_pct', 'demo_owner_occupied_pct', 'demo_provider', 'demo_dataset', 'demo_vintage',
      'demo_area_level', 'demo_area_id', 'us_census_state_fips', 'us_census_county_fips',
      'us_census_tract_geoid', 'us_census_block_group_geoid', 'demo_population_moe',
      'demo_median_age_moe', 'demo_median_household_income_moe',
    ].map((key) => [key, `saved_${key}`])) },
  ])('preserves the typed enrichment draft for $action_id', async ({ action_id, params, output_names }) => {
    const draft = { action_id, scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 3] }, params, output_names };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.copilot_reply.v1', reply: 'Enrich these places.',
      proposals: [{ kind: 'enrich', title: 'Enrich places', spec: draft }],
      needs_import: false, cost_usd: 0,
    })));
    const result = await copilotChatContract('project-1', messages, contractError);
    expect(result.proposals).toEqual([{ kind: 'enrich', title: 'Enrich places', spec: draft }]);
    expect(result.proposals[0]?.spec).not.toHaveProperty('action_kind');
  });

  it.each([undefined, { name: 'Person' }])('preserves project scope, destination and optional renames on a typed list-table proposal', async (outputNames) => {
    const draft = {
      action_id: 'derive.table_from_list',
      scope: { kind: 'project' },
      sheet_name: 'People',
      params: { source: { kind: 'column', sheet_id: 7, column_id: 12 } },
      ...(outputNames === undefined ? {} : { output_names: outputNames }),
    };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.copilot_reply.v1',
      reply: 'Materialize these items.',
      proposals: [{ kind: 'derive', title: 'People', spec: draft }],
      needs_import: false, cost_usd: 0,
    })));
    const result = await copilotChatContract('project-1', messages, contractError);
    expect(result.proposals[0]?.spec).toEqual({ ...draft, output_names: outputNames ?? {} });
    expect(result.proposals[0]?.kind).toBe('derive');
  });

  it('preserves a registered action draft instead of translating it to a legacy spec', async () => {
    const draft = {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 3] },
      params: { template: { text: 'Hello {{name}}' } },
      output_names: { rendered: 'greeting' },
    } as const;
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.copilot_reply.v1',
      reply: 'Try this.',
      proposals: [{ kind: 'map', title: 'Template', spec: draft }],
      needs_import: false,
      cost_usd: 0.01,
    })));

    const result = await copilotChatContract('project-1', messages, contractError);

    expect(result.proposals[0]?.spec).toEqual(draft);
    expect(result.proposals[0]?.spec).not.toHaveProperty('action_kind');
  });

  it('accepts the middleware 403, upstream 502, and route-body spend-cap 409', async () => {
    const upstreamError = { detail: 'Anthropic is unavailable; check AI provider settings' };
    const spendCapError = {
      detail: {
        code: 'provider_spend_cap_exceeded',
        message: 'Raise the project spend cap before trying again',
        details: { setting: 'spend_cap_usd' },
      },
    };
    const responses = [
      jsonResponse({ detail: 'editor role required' }, 403),
      jsonResponse(upstreamError, 502),
      jsonResponse(spendCapError, 409),
    ];
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return responses.shift()!;
    }));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );
    const options = {
      headers: new Headers({
        Authorization: 'Bearer token',
        'X-Trace-Id': 'trace-copilot',
      }),
    };

    await expectMapped(
      copilotChatContract('project/one', messages, errorFactory, null, options),
      403,
    );
    await expectMapped(
      copilotChatContract('project/one', messages, errorFactory, null, options),
      502,
    );
    await expectMapped(
      copilotChatContract('project/one', messages, errorFactory, null, options),
      409,
    );

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/copilot',
      '/api/projects/project%2Fone/copilot',
      '/api/projects/project%2Fone/copilot',
    ]);
    expect(errorFactory).toHaveBeenNthCalledWith(2, 502, upstreamError);
    expect(errorFactory).toHaveBeenNthCalledWith(3, 409, spendCapError);
    for (const request of requests) {
      expect(request.init?.method).toBe('POST');
      expect(request.init?.body).toBe(JSON.stringify({ messages }));
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer token');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-trace-id')).toBe('trace-copilot');
    }
  });

  it('maps an additional non-2xx status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'teapot' }, 418)));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      copilotChatContract('project-1', messages, errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 418,
      payload: { detail: 'teapot' },
    });
    expect(errorFactory).toHaveBeenCalledOnce();
    expect(errorFactory).toHaveBeenCalledWith(418, { detail: 'teapot' });
  });

  it('forwards the exact signal and preserves abort rejection', async () => {
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

    const request = copilotChatContract('project-1', messages, contractError, null, {
      signal: controller.signal,
    });
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(request).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});
