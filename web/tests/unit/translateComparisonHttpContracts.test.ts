import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


import { ApiError, createProjectApi } from '../../src/api/real';
import { createTranslateComparisonApi } from '../../src/api/translateComparison';

let api = createProjectApi('test-project');

const RESPONSE = {
  schema_version: 'frisket.translate_compare_preview.v1',
  source: { scratch: true, text_length: 11 },
  target_language: 'French',
  engines: ['opus_mt'],
  results: [{
    engine: 'opus_mt',
    translation: 'bonjour monde',
    detected_language: 'English',
    runtime_ms: 12,
    errors: [],
  }],
  warnings: [{ code: 'engine_degraded', nested: { retry_after: 3 } }],
  errors: [{ code: 'partial', detail: { engine: 'other' } }],
};

function stubFetch(body: unknown, init: { status?: number; statusText?: string } = {}) {
  const fetchMock = vi.fn(() => Promise.resolve({
    status: init.status ?? 200,
    statusText: init.statusText ?? '',
    json: () => Promise.resolve(body),
  } as Response));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function requestBody(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const init = fetchMock.mock.calls[0]![1] as RequestInit;
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

beforeEach(() => api = createProjectApi('tenant/a % hostile'));
afterEach(() => vi.unstubAllGlobals());

describe('translate comparison HTTP contract', () => {
  it('renders a project path exactly once and preserves the response envelope', async () => {
    const fetchMock = stubFetch(RESPONSE);

    const result = await api.compareTranslateScratch({
      engines: ['opus_mt'],
      text: 'hello world',
      targetLanguage: ' French ',
      language: ' English ',
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/tenant%2Fa%20%25%20hostile/translate/compare-scratch',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(requestBody(fetchMock)).toEqual({
      engines: ['opus_mt'],
      text: 'hello world',
      target_language: 'French',
      language: ['English'],
    });
    expect(requestBody(fetchMock)).not.toHaveProperty('allow_remote');
    expect(requestBody(fetchMock)).not.toHaveProperty('model');
    expect(result).toEqual(RESPONSE);
  });

  it('uses English and auto-detect when language inputs are blank', async () => {
    const fetchMock = stubFetch(RESPONSE);

    await api.compareTranslateScratch({
      engines: ['opus_mt'],
      text: 'hello world',
      targetLanguage: ' ',
      language: ' ',
    });

    expect(requestBody(fetchMock)).toMatchObject({
      target_language: 'English',
      language: null,
    });
  });

  it('keeps the route-specific bare action error, including empty details', async () => {
    stubFetch({
      schema_version: 'frisket.action_error.v1',
      code: 'invalid_request',
      message: 'select at least one engine',
      details: {},
    }, { status: 400 });

    const failure = api.compareTranslateScratch({
      engines: [], text: 'hello world', targetLanguage: 'French',
    });

    await expect(failure).rejects.toThrow('select at least one engine');
    await failure.catch((error: ApiError) => {
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(400);
      expect(error.code).toBe('invalid_request');
      expect(error.details).toEqual({});
    });
  });

  it('defaults missing bare action-error details to an empty object', async () => {
    stubFetch({
      schema_version: 'frisket.action_error.v1',
      code: 'invalid_request',
      message: 'select at least one engine',
    }, { status: 400 });

    const failure = api.compareTranslateScratch({
      engines: [], text: 'hello world', targetLanguage: 'French',
    });

    await failure.catch((error: ApiError) => {
      expect(error.details).toEqual({});
    });
  });

  it('keeps legacy detail-wrapped error behavior for other errors', async () => {
    stubFetch({
      detail: { code: 'project_missing', message: 'project is gone', field: 'pid' },
    }, { status: 404 });

    const failure = api.compareTranslateScratch({
      engines: ['opus_mt'], text: 'hello world', targetLanguage: 'French',
    });

    await expect(failure).rejects.toThrow('project is gone');
    await failure.catch((error: ApiError) => {
      expect(error.status).toBe(404);
      expect(error.code).toBe('project_missing');
      expect(error.details).toEqual({ field: 'pid' });
    });
  });

  it('forwards adapter cancellation and headers without widening RealApi', async () => {
    const fetchMock = stubFetch(RESPONSE);
    const controller = new AbortController();
    const adapter = createTranslateComparisonApi((status, payload) => new Error(`${status}:${payload}`));

    await adapter.compareTranslateScratch(
      'tenant/a % hostile',
      { engines: ['opus_mt'], text: 'hello world', targetLanguage: 'French' },
      { signal: controller.signal, headers: { 'X-Request-ID': 'compare-1' } },
    );

    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect(init.signal).toBe(controller.signal);
    expect(new Headers(init.headers).get('X-Request-ID')).toBe('compare-1');
  });
});
