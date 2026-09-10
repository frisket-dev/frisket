// The two read-only preview endpoints back the resolve drawers. These tests pin
// the wire mapping both ways — camelCase inputs to snake_case request bodies,
// snake_case responses to the ColumnValuesPreview / ReplaceRulesPreview
// shapes — and the shared bare-action-error translation (ApiError with a
// branchable `.code`), all against a stubbed global fetch (no network).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


import { ApiError, createProjectApi } from '../../src/api/real';
import { createResolvePreviewsApi } from '../../src/api/resolvePreviews';

let api = createProjectApi('test-project');

function stubFetch(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  const fetchMock = vi.fn(() =>
    Promise.resolve({
      ok: init.ok ?? true,
      status: init.status ?? 200,
      statusText: '',
      json: () => Promise.resolve(body),
    } as Response),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function requestBody(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const init = fetchMock.mock.calls[0]![1] as RequestInit;
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

beforeEach(() => api = createProjectApi('p1'));
afterEach(() => vi.unstubAllGlobals());

const COLUMN_VALUES_WIRE = {
  schema_version: 'frisket.column_values_preview.v1',
  sheet_id: 7,
  column_id: 11,
  input_column: 'city',
  total_rows: 120,
  distinct: 14,
  missing: 3,
  values: [
    { value: 'NYC', count: 40 },
    { value: 'LA', count: 22 },
  ],
  offset: 0,
  limit: 500,
  truncated: false,
  value_hash: 'sha256:cv',
  search: null,
  distribution: {
    kind: 'number' as const,
    min: 1,
    max: 10,
    bins: [{ start: 1, end: 10, count: 117 }],
  },
};

describe('columnValuesPreview', () => {
  it('POSTs the snake_case request and maps the response to camelCase', async () => {
    const fetchMock = stubFetch(COLUMN_VALUES_WIRE);
    const out = await api.columnValuesPreview({
      sheetId: '7',
      inputColumn: 'city',
      search: 'ny',
      limit: 100,
      offset: 50,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/column-values/v1/preview',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(requestBody(fetchMock)).toEqual({
      sheet_id: 7,
      input_column: 'city',
      search: 'ny',
      limit: 100,
      offset: 50,
    });
    expect(out).toEqual({
      sheetId: '7',
      columnId: '11',
      inputColumn: 'city',
      totalRows: 120,
      distinct: 14,
      missing: 3,
      values: [
        { value: 'NYC', count: 40 },
        { value: 'LA', count: 22 },
      ],
      offset: 0,
      limit: 500,
      truncated: false,
      valueHash: 'sha256:cv',
      search: null,
      distribution: {
        kind: 'number',
        min: 1,
        max: 10,
        bins: [{ start: 1, end: 10, count: 117 }],
      },
    });
  });

  it('omits search/limit/offset when unset so server defaults apply', async () => {
    const fetchMock = stubFetch({ ...COLUMN_VALUES_WIRE, search: 'ny' });
    const out = await api.columnValuesPreview({ sheetId: '7', inputColumn: 'city' });
    expect(requestBody(fetchMock)).toEqual({ sheet_id: 7, input_column: 'city' });
    expect(out.search).toBe('ny');
  });

  it('maps a valid snake_case list facet to the browser shape', async () => {
    stubFetch({
      ...COLUMN_VALUES_WIRE,
      list_facet: {
        distinct: 1,
        offset: 0,
        limit: 500,
        truncated: false,
        search: null,
        choices: [{
          key: 'scalar:NYPD',
          label: 'NYPD',
          count: 3,
          selector: { kind: 'scalar', value: 'NYPD' },
        }],
      },
    });

    const out = await api.columnValuesPreview({ sheetId: '7', inputColumn: 'tags' });

    expect(out.listFacet).toEqual({
      distinct: 1,
      offset: 0,
      limit: 500,
      truncated: false,
      search: null,
      choices: [{
        key: 'scalar:NYPD',
        label: 'NYPD',
        count: 3,
        selector: { kind: 'scalar', value: 'NYPD' },
      }],
    });
  });

  it('omits the optional listFacet when the wire omits list_facet', async () => {
    stubFetch(COLUMN_VALUES_WIRE);

    const out = await api.columnValuesPreview({ sheetId: '7', inputColumn: 'city' });

    expect(out).not.toHaveProperty('listFacet');
  });

  it('rejects an entire list facet when one choice is malformed', async () => {
    stubFetch({
      ...COLUMN_VALUES_WIRE,
      list_facet: {
        distinct: 2,
        offset: 0,
        limit: 500,
        truncated: false,
        search: null,
        choices: [
          {
            key: 'scalar:NYPD',
            label: 'NYPD',
            count: 3,
            selector: { kind: 'scalar', value: 'NYPD' },
          },
          {
            key: 'invalid',
            label: 'invalid',
            count: 1,
            selector: { kind: 'scalar', value: ['not-a-scalar'] },
          },
        ],
      },
    });

    const out = await api.columnValuesPreview({ sheetId: '7', inputColumn: 'tags' });

    expect(out).toHaveProperty('listFacet', null);
  });

  it.each([null, [], 'not a choice'])(
    'rejects an entire list facet when a choice is %p',
    async (malformedChoice) => {
      stubFetch({
        ...COLUMN_VALUES_WIRE,
        list_facet: {
          distinct: 1,
          offset: 0,
          limit: 500,
          truncated: false,
          search: null,
          choices: [malformedChoice],
        },
      });

      const out = await api.columnValuesPreview({ sheetId: '7', inputColumn: 'tags' });

      expect(out).toHaveProperty('listFacet', null);
    },
  );

  it('keeps integer histogram endpoints as exact decimal strings', async () => {
    const fetchMock = stubFetch({
      ...COLUMN_VALUES_WIRE,
      distribution: {
        kind: 'integer',
        min: '-9223372036854775808',
        max: '9223372036854775807',
        bins: [{
          start: '-9223372036854775808',
          end: '9223372036854775807',
          count: 117,
        }],
      },
    });
    const out = await api.columnValuesPreview({ sheetId: '7', inputColumn: 'id' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(out.distribution).toEqual({
      kind: 'integer',
      min: '-9223372036854775808',
      max: '9223372036854775807',
      bins: [{
        start: '-9223372036854775808',
        end: '9223372036854775807',
        count: 117,
      }],
    });
  });

  it('translates a bare action-error envelope into ApiError with its code', async () => {
    stubFetch(
      {
        schema_version: 'frisket.action_error.v1',
        code: 'invalid_input_ref',
        message: 'no such column',
      },
      { ok: false, status: 404 },
    );
    const failure = api.columnValuesPreview({ sheetId: '7', inputColumn: 'gone' });
    await expect(failure).rejects.toThrow('no such column');
    await failure.catch((error: ApiError) => {
      expect(error).toBeInstanceOf(ApiError);
      expect(error.code).toBe('invalid_input_ref');
      expect(error.status).toBe(404);
    });
  });
});

describe('generated resolve preview transport', () => {
  it('reads the current raw pid for every call and lets the renderer encode it once', async () => {
    const fetchMock = stubFetch(COLUMN_VALUES_WIRE);
    const transport = createResolvePreviewsApi((status, payload) => new ApiError(status, String(payload)));
    api = createProjectApi('folder/child');

    await transport.columnValues('folder/child', { sheetId: '7', inputColumn: 'city' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/folder%2Fchild/column-values/v1/preview',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('uses RealApi’s current project for each invocation without double encoding', async () => {
    const fetchMock = stubFetch(COLUMN_VALUES_WIRE);
    api = createProjectApi('first/project');
    await api.columnValuesPreview({ sheetId: '7', inputColumn: 'city' });
    api = createProjectApi('second/project');
    await api.columnValuesPreview({ sheetId: '7', inputColumn: 'city' });

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/api/projects/first%2Fproject/column-values/v1/preview',
      '/api/projects/second%2Fproject/column-values/v1/preview',
    ]);
  });

  it('keeps ordinary HTTP failures and network identities intact', async () => {
    const ordinary = stubFetch({ detail: 'gateway denied' }, { ok: false, status: 502 });
    await expect(api.columnValuesPreview({ sheetId: '7', inputColumn: 'city' }))
      .rejects.toMatchObject({ status: 502, message: 'gateway denied' });
    expect(ordinary).toHaveBeenCalledTimes(1);

    const network = new Error('offline');
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(network)));
    await expect(api.columnValuesPreview({ sheetId: '7', inputColumn: 'city' }))
      .rejects.toBe(network);
  });
});

const REPLACE_RULES_WIRE = {
  schema_version: 'frisket.replace_rules_preview.v1',
  sheet_id: 7,
  column_id: 11,
  total_rows: 120,
  rule_counts: [
    { index: 0, matched_rows: 40, matched_values: 3 },
    { index: 1, matched_rows: 5, matched_values: 1 },
  ],
  unmatched_rows: 75,
  test_result: null,
  value_hash: 'sha256:rr',
};

const CLUSTER_WIRE = {
  schema_version: 'frisket.cluster_preview.v1',
  sheet_id: 7,
  sheet_name: 'Places',
  column_id: 11,
  column: 'city',
  column_type: 'text',
  method: 'fingerprint',
  min_size: 2,
  row_count: 4,
  value_hash: 'sha256:cluster',
  count: 1,
  clusters: [
    {
      key: 'new-york',
      canonical: 'New York',
      size: 2,
      values: [
        { value: 'New York', count: 2 },
        { value: 'new york', count: 1 },
      ],
      row_ids: [1, 2, 3],
    },
  ],
  semantic: false,
};

describe('clusterPreview', () => {
  it('maps the generated required cluster core directly to the browser shape', async () => {
    const fetchMock = stubFetch(CLUSTER_WIRE);
    const transport = createResolvePreviewsApi((status, payload) => new ApiError(status, String(payload)));

    const out = await transport.cluster('p1', {
      sheetId: '7',
      inputColumn: 'city',
      method: 'fingerprint',
    });

    expect(requestBody(fetchMock)).toEqual({
      sheet_id: 7,
      input_column: 'city',
      method: 'fingerprint',
      min_size: 2,
    });
    expect(out).toEqual({
      clusters: [
        {
          key: 'new-york',
          canonical: 'New York',
          size: 2,
          values: [
            { value: 'New York', count: 2 },
            { value: 'new york', count: 1 },
          ],
          rowIds: ['1', '2', '3'],
        },
      ],
      count: 1,
      valueHash: 'sha256:cluster',
      method: 'fingerprint',
      semantic: false,
    });
  });
});

describe('replaceRulesPreview', () => {
  it('POSTs wire-shaped rules (defaulting case_sensitive) and maps the counts', async () => {
    const fetchMock = stubFetch(REPLACE_RULES_WIRE);
    const out = await api.replaceRulesPreview({
      sheetId: '7',
      inputColumn: 'city',
      rules: [
        { match: 'contains', pattern: 'york', target: 'New York' },
        { match: 'exact', pattern: 'n/a', target: null, case_sensitive: true },
      ],
      unmatched: 'null',
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/replace-rules/v1/preview',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(requestBody(fetchMock)).toEqual({
      sheet_id: 7,
      input_column: 'city',
      rules: [
        { match: 'contains', pattern: 'york', target: 'New York', case_sensitive: false },
        { match: 'exact', pattern: 'n/a', target: null, case_sensitive: true },
      ],
      unmatched: 'null',
    });
    expect(out).toEqual({
      sheetId: '7',
      columnId: '11',
      totalRows: 120,
      ruleCounts: [
        { index: 0, matchedRows: 40, matchedValues: 3 },
        { index: 1, matchedRows: 5, matchedValues: 1 },
      ],
      unmatchedRows: 75,
      testResult: null,
      valueHash: 'sha256:rr',
    });
  });

  it('sends test_value through and maps a populated test_result', async () => {
    const fetchMock = stubFetch({
      ...REPLACE_RULES_WIRE,
      test_result: { matched_rule_index: 1, output: null },
    });
    const out = await api.replaceRulesPreview({
      sheetId: '7',
      inputColumn: 'city',
      rules: [{ match: 'exact', pattern: 'n/a', target: null }],
      testValue: 'n/a',
    });
    expect(requestBody(fetchMock)).toMatchObject({ test_value: 'n/a' });
    // unmatched omitted when unset (server default keep)
    expect(requestBody(fetchMock)).not.toHaveProperty('unmatched');
    expect(out.testResult).toEqual({ matchedRuleIndex: 1, output: null });
  });

  it('translates invalid_regex into ApiError with its code', async () => {
    stubFetch(
      {
        schema_version: 'frisket.action_error.v1',
        code: 'invalid_regex',
        message: 'rule 1: pattern does not compile',
      },
      { ok: false, status: 400 },
    );
    const failure = api.replaceRulesPreview({
      sheetId: '7',
      inputColumn: 'city',
      rules: [{ match: 'regex', pattern: '(', target: 'x' }],
    });
    await expect(failure).rejects.toThrow(/does not compile/);
    await failure.catch((error: ApiError) => {
      expect(error.code).toBe('invalid_regex');
    });
  });
});
