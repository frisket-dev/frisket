import { describe, expect, it, vi } from 'vitest';

const generated = vi.hoisted(() => ({
  artifact: {
    schemaVersion: 'fixture.http_contract_artifact.v4',
    endpoints: [
      {
        id: 'fixture.transport_mechanics.delete',
        method: 'DELETE',
        path: '/fixture/mechanics/{segment}',
        request: { required: true, mediaType: 'application/json' },
      },
      {
        id: 'fixture.status_family.post',
        method: 'POST',
        path: '/fixture/status-family',
        request: null,
      },
      {
        id: 'tenant.import_csv.post',
        method: 'POST',
        path: '/api/projects/{pid}/import/csv',
        request: { required: true, mediaType: 'multipart/form-data' },
      },
    ],
  },
}));

vi.mock('../../src/generated/openHttpContracts', () => ({
  HTTP_CONTRACT_ARTIFACT: generated.artifact,
}));

import {
  httpContract,
  type HttpContractInvokeOptions,
} from '../../src/api/httpContract';

type FixtureOptions = {
  pathParams: Record<string, unknown>;
  query: Record<string, unknown>;
  body?: unknown;
  fetch?: typeof globalThis.fetch;
  signal?: AbortSignal;
  headers?: HeadersInit;
  errorFactory?: (status: number, payload: unknown) => Error;
};

const invokeFixture = httpContract as unknown as <DomainResponse>(
  endpointId: string,
  options: FixtureOptions,
  mapResponse: (wire: unknown) => DomainResponse,
) => Promise<DomainResponse>;
const invokeFixtureUnmapped = httpContract as unknown as (
  endpointId: string,
  options: FixtureOptions,
) => Promise<unknown>;

const requiredBodyOptions: HttpContractInvokeOptions<'tenant.create_project.post'> = {
  pathParams: {},
  query: {},
  body: { name: 'typed', sensitive: false },
};
// @ts-expect-error create_project requires a request body
const missingRequiredBody: HttpContractInvokeOptions<'tenant.create_project.post'> = {
  pathParams: {},
  query: {},
};
const bodylessOptions: HttpContractInvokeOptions<'tenant.list_projects.get'> = {
  pathParams: {},
  query: {},
};
const invalidBodylessOptions: HttpContractInvokeOptions<'tenant.list_projects.get'> = {
  pathParams: {},
  query: {},
  // @ts-expect-error list_projects is bodyless
  body: {},
};
void requiredBodyOptions;
void missingRequiredBody;
void bodylessOptions;
void invalidBodylessOptions;

const multipartBodyOptions: HttpContractInvokeOptions<'tenant.import_csv.post'> = {
  pathParams: { pid: 'project-id' },
  query: {},
  body: new FormData(),
};
const invalidMultipartBodyOptions: HttpContractInvokeOptions<'tenant.import_csv.post'> = {
  pathParams: { pid: 'project-id' },
  query: {},
  // @ts-expect-error CSV imports accept a native FormData body
  body: { file: 'rows.csv' },
};
void multipartBodyOptions;
void invalidMultipartBodyOptions;

function jsonResponse(
  payload: unknown,
  status = 200,
  mediaType = 'application/json',
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': mediaType },
  });
}

describe('generated HTTP contract transport', () => {
  it('returns generated success payloads directly when no domain mapper is needed', async () => {
    const payload = { shape: 'generated-contract-owned' };
    const fetch = vi.fn(async () => jsonResponse(payload));

    await expect(invokeFixtureUnmapped(
      'fixture.status_family.post',
      { pathParams: {}, query: {}, fetch },
    )).resolves.toEqual(payload);
  });

  it('lets type-bypassed path, query, and body values reach the server boundary', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({ server: 'accepted' });
    });

    await expect(invokeFixture(
      'fixture.transport_mechanics.delete',
      {
        pathParams: { segment: 7 },
        query: { nullable: false },
        body: { value: 7 },
        fetch,
      },
      (wire) => wire,
    )).resolves.toEqual({ server: 'accepted' });

    expect(requests[0]?.input).toBe('/fixture/mechanics/7?nullable=false');
    expect(requests[0]?.init?.body).toBe('{"value":7}');
  });

  it('encodes paths, preserves null, omits undefined, repeats arrays, and sends DELETE JSON', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({ status: 'ok' });
    });

    await expect(invokeFixture(
      'fixture.transport_mechanics.delete',
      {
        pathParams: { segment: 'a b+%' },
        query: {
          omitted: undefined,
          nullable: null,
          tags: ['first value', 'second/value'],
        },
        body: { value: 'body', omitted: undefined, nullable: null },
        headers: { 'X-Trace-Id': 'trace-mechanics' },
        fetch,
      },
      (wire) => wire,
    )).resolves.toEqual({ status: 'ok' });

    expect(requests[0]?.input).toBe(
      '/fixture/mechanics/a%20b%2B%25?nullable=null&tags=first+value&tags=second%2Fvalue',
    );
    expect(requests[0]?.init?.method).toBe('DELETE');
    expect(requests[0]?.init?.body).toBe('{"value":"body","nullable":null}');
    const headers = new Headers(requests[0]?.init?.headers);
    expect(headers.get('x-trace-id')).toBe('trace-mechanics');
    expect(headers.get('content-type')).toBe('application/json');
  });

  it('keeps only missing-placeholder and request-body construction guards', async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));

    await expect(invokeFixture(
      'fixture.transport_mechanics.delete',
      { pathParams: {}, query: {}, body: { value: 'body' }, fetch },
      (wire) => wire,
    )).rejects.toThrow('missing rendered path parameter segment');
    await expect(invokeFixture(
      'fixture.transport_mechanics.delete',
      { pathParams: { segment: 'valid' }, query: {}, fetch },
      (wire) => wire,
    )).rejects.toThrow('requires a JSON request body');
    await expect(invokeFixture(
      'fixture.status_family.post',
      { pathParams: {}, query: {}, body: {}, fetch },
      (wire) => wire,
    )).rejects.toThrow('does not declare a JSON request body');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('passes CSV FormData through unchanged and leaves its boundary header to fetch', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({ sheet_id: 8, rows: 1, columns: ['name'], encoding: 'utf-8' });
    });
    const body = new FormData();
    body.append('file', new Blob(['name\nAda\n'], { type: 'text/csv' }), 'rows.csv');

    await expect(invokeFixture(
      'tenant.import_csv.post',
      { pathParams: { pid: 'project id' }, query: {}, body, fetch },
      (wire) => wire,
    )).resolves.toEqual({ sheet_id: 8, rows: 1, columns: ['name'], encoding: 'utf-8' });

    expect(requests[0]?.input).toBe('/api/projects/project%20id/import/csv');
    expect(requests[0]?.init?.body).toBe(body);
    expect(new Headers(requests[0]?.init?.headers).has('content-type')).toBe(false);
  });

  it('forwards exact headers and signal and preserves abort rejection identity', async () => {
    const controller = new AbortController();
    const headers = new Headers({ Authorization: 'Bearer token', 'X-Trace-Id': 'trace-abort' });
    let forwardedHeaders: HeadersInit | undefined;
    let forwardedSignal: AbortSignal | null | undefined;
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedHeaders = init?.headers;
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    });
    const pending = invokeFixture(
      'fixture.status_family.post',
      { pathParams: {}, query: {}, headers, signal: controller.signal, fetch },
      (wire) => wire,
    );
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    expect(forwardedHeaders).toBe(headers);
    expect(forwardedSignal).toBe(controller.signal);
  });

  it.each([
    [200, { shape: 'server-owned' }, 'text/plain'],
    [206, { shape: 'previously-undeclared' }, 'application/json'],
    [204, undefined, undefined],
    [205, undefined, undefined],
  ] as const)(
    'maps successful status %s without response-schema metadata',
    async (status, payload, mediaType) => {
      const mapResponse = vi.fn((wire: unknown) => wire);
      const fetch = async (): Promise<Response> => (
        payload === undefined
          ? new Response(null, { status })
          : jsonResponse(payload, status, mediaType)
      );

      await expect(invokeFixture(
        'fixture.status_family.post',
        { pathParams: {}, query: {}, fetch },
        mapResponse,
      )).resolves.toEqual(payload);
      expect(mapResponse).toHaveBeenCalledWith(payload);
    },
  );

  it.each([400, 418, 503])('maps any non-2xx status %s with its raw JSON payload', async (status) => {
    const payload = { detail: `server error ${status}`, extra: ['preserved'] };
    const errorFactory = vi.fn(
      (mappedStatus: number, mappedPayload: unknown) => Object.assign(
        new Error('mapped'),
        { status: mappedStatus, payload: mappedPayload },
      ),
    );
    const mapResponse = vi.fn((wire: unknown) => wire);

    await expect(invokeFixture(
      'fixture.status_family.post',
      {
        pathParams: {},
        query: {},
        fetch: async () => jsonResponse(payload, status),
        errorFactory,
      },
      mapResponse,
    )).rejects.toMatchObject({ status, payload });
    expect(errorFactory).toHaveBeenCalledWith(status, payload);
    expect(mapResponse).not.toHaveBeenCalled();
  });

  it.each([
    ['plain-text', 'upstream unavailable'],
    ['malformed JSON', '{"broken":'],
  ])('uses the non-JSON fallback for a %s error response', async (_kind, body) => {
    const mapResponse = vi.fn((wire: unknown) => wire);
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => Object.assign(new Error('factory error'), { status, payload }),
    );

    await expect(invokeFixture(
      'fixture.status_family.post',
      {
        pathParams: {},
        query: {},
        fetch: async () => new Response(body, {
          status: 502,
          statusText: 'Gateway unavailable',
        }),
        errorFactory,
      },
      mapResponse,
    )).rejects.toMatchObject({
      message: 'factory error',
      status: 502,
      payload: { detail: 'Gateway unavailable' },
    });
    expect(errorFactory).toHaveBeenCalledWith(502, { detail: 'Gateway unavailable' });
    expect(mapResponse).not.toHaveBeenCalled();
  });

  it.each([
    ['empty', null],
    ['blank', ' \n\t '],
  ] as const)('uses the status fallback for a %s error body without a status text', async (_kind, body) => {
    const mapResponse = vi.fn((wire: unknown) => wire);

    await expect(invokeFixture(
      'fixture.status_family.post',
      {
        pathParams: {},
        query: {},
        fetch: async () => new Response(body, { status: 503 }),
      },
      mapResponse,
    )).rejects.toMatchObject({
      name: 'HttpContractResponseError',
      message: 'Request failed (HTTP 503)',
      status: 503,
      payload: { detail: 'Request failed (HTTP 503)' },
    });
    expect(mapResponse).not.toHaveBeenCalled();
  });

  it.each([
    ['AbortError', new DOMException('body cancelled', 'AbortError')],
    ['TypeError', new TypeError('body stream failed')],
  ])('preserves %s identity from response.json()', async (_kind, bodyError) => {
    const mapResponse = vi.fn((wire: unknown) => wire);
    const errorFactory = vi.fn(() => new Error('must not map'));
    const response = {
      status: 502,
      statusText: 'Bad Gateway',
      json: vi.fn(async () => {
        throw bodyError;
      }),
    } as unknown as Response;

    await expect(invokeFixture(
      'fixture.status_family.post',
      {
        pathParams: {},
        query: {},
        fetch: async () => response,
        errorFactory,
      },
      mapResponse,
    )).rejects.toBe(bodyError);
    expect(errorFactory).not.toHaveBeenCalled();
    expect(mapResponse).not.toHaveBeenCalled();
  });

  it('preserves a network rejection identity', async () => {
    const mapResponse = vi.fn((wire: unknown) => wire);
    const networkError = new TypeError('network disconnected');

    await expect(invokeFixture(
      'fixture.status_family.post',
      {
        pathParams: {},
        query: {},
        fetch: async () => {
          throw networkError;
        },
      },
      mapResponse,
    )).rejects.toBe(networkError);
    expect(mapResponse).not.toHaveBeenCalled();
  });
});
