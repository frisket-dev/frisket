import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import {
  createProjectEvidenceApi,
  createProjectEvidenceDomainApi,
} from '../../src/api/projectEvidence';

const linkSummary = {
  id: 11,
  stable_id: 'evidence-link-11',
  export_ref: 'evidence-link:11',
  status: 'active',
  role: 'support',
  evidence_kind: 'source',
  span_count: 1,
  artifact_count: 1,
  snippet: 'Ada',
  viewer_href: '/api/projects/project/evidence/links/evidence-link-11/viewer',
};

const cellFixture = {
  schema_version: 'frisket.cell_evidence.v1',
  sheet_id: 3,
  row_id: 7,
  column_id: 5,
  current_value_ref: {
    kind: 'run_result', row_id: 7, column_id: 5, run_id: 9, op_id: 10,
  },
  links: [linkSummary],
  stale_count: 0,
};

const scalarCellValueFixture = {
  ...cellFixture,
  current_value_ref: 'retained-provider-value',
};

const annotationsFixture = {
  sheet_id: 3,
  row_id: 7,
  column_id: 5,
  content_hash: 'sha256:current',
  offset_unit: 'utf16_code_unit',
  text: 'Ada',
  layers: [{
    toggle_key: '3:5:entities',
    layer_family: 'entities',
    producer: { kind: 'entities', engine: null },
    output_column: { id: 5, name: 'Entities' },
    positioned: true,
    counts: { shown: 1, total: 1, invalid: 0 },
    spans: [{
      occurrence_id: 'span:1',
      start: 0,
      end: 3,
      quote: 'Ada',
      metadata: { entity_type: 'PERSON', entity_fingerprint: 'person:ada' },
    }],
  }],
};

const columnFixture = {
  schema_version: 'frisket.column_evidence_batch.v1',
  sheet_id: 3,
  column_id: 5,
  rows: [{ row_id: 7, links: [linkSummary] }],
};

const viewerFixture = {
  schema_version: 'frisket.evidence_viewer.v1',
  link: {
    id: 11,
    stable_id: 'evidence-link-11',
    export_ref: 'evidence-link:11',
    subject_kind: 'cell',
    subject_ref: { row_id: 7, column_id: 5 },
    sheet_id: 3,
    row_id: 7,
    column_id: 5,
    run_id: 9,
    op_id: 10,
    receipt_id: null,
    role: 'support',
    status: 'active',
    confidence: 0.9,
    pinned: false,
    producer: { action_kind: 'extract' },
    stale_reason: null,
    stale_at: null,
    created_at: '2026-08-11T00:00:00Z',
    text_layer_hash_mismatch: false,
  },
  artifacts: [],
  warnings: [],
};

const viewerJsonLeavesFixture = {
  ...viewerFixture,
  link: {
    ...viewerFixture.link,
    subject_ref: null,
    producer: { action_kind: null, model: null, grounding_method: null },
  },
  artifacts: [{
    id: 12,
    stable_id: 'source_artifact:12',
    export_ref: 'source_artifact:12',
    artifact_kind: 'row',
    media_type: 'application/vnd.frisket.row+json',
    title: null,
    filename: null,
    page_count: null,
    duration_ms: null,
    source_url: null,
    canonical_url: null,
    source_cell: null,
    external_ref: false,
    artifact_ref: {
      kind: 'source_artifact',
      stable_id: 'source_artifact:12',
      artifact_kind: 'row',
      media_type: 'application/vnd.frisket.row+json',
      blob: null,
      source_url: null,
      external_ref: null,
    },
    metadata: null,
    spans: [{
      id: 13,
      stable_id: 'evidence_span:13',
      export_ref: 'evidence_span:13',
      span_kind: 'text',
      rank: 0,
      span_role: 'support',
      required: true,
      note: null,
      status: 'active',
      selector: 0,
      quote: null,
      snippet: null,
      text_layer_hash: null,
      preview: false,
      raw: null,
      warnings: [],
      deep_link_url: null,
      clip_url: null,
      run_index: null,
    }],
    pages: [],
    runs: [],
  }],
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('project evidence generated HTTP reads', () => {
  it('preserves request bytes for four product-used JSON routes', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [cellFixture, annotationsFixture, columnFixture, viewerFixture];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const signal = new AbortController().signal;
    const options = { signal, headers: { Authorization: 'Bearer evidence' } };
    const api = createProjectEvidenceApi(
      (status) => new Error(`evidence ${status}`),
      'evidence/project',
    );

    await expect(api.getCellEvidence('row/a', 'column/b', true, options))
      .resolves.toEqual(cellFixture);
    await expect(api.getTextAnnotations('row/a', 'column/b', options))
      .resolves.toEqual(annotationsFixture);
    await expect(api.getColumnEvidence('sheet/a', 'column/b', ['7', '8'], options))
      .resolves.toEqual(columnFixture);
    await expect(api.getEvidenceViewer('link/a', options)).resolves.toEqual(viewerFixture);

    expect(requests.map((request) => [request.input, request.init?.method])).toEqual([
      ['/api/projects/evidence%2Fproject/cells/row%2Fa/column%2Fb/evidence?include_stale=1', 'GET'],
      ['/api/projects/evidence%2Fproject/cells/row%2Fa/column%2Fb/annotations', 'GET'],
      ['/api/projects/evidence%2Fproject/sheets/sheet%2Fa/columns/column%2Fb/evidence?row_ids=7%2C8', 'GET'],
      ['/api/projects/evidence%2Fproject/evidence/links/link%2Fa/viewer', 'GET'],
    ]);
    requests.forEach((request) => {
      expect(request.init?.signal).toBe(signal);
      expect(new Headers(request.init?.headers).get('authorization')).toBe('Bearer evidence');
    });
  });

  it('returns evidence-viewer JSON leaves unchanged', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(viewerJsonLeavesFixture)));
    const api = createProjectEvidenceApi(
      (status) => new Error(`evidence ${status}`),
      'evidence-json-leaves',
    );

    await expect(api.getEvidenceViewer('json-leaves')).resolves.toEqual(viewerJsonLeavesFixture);
  });

  it('maps annotations at the project-evidence domain boundary', async () => {
    const domainAnnotations = {
      ...annotationsFixture,
      sheet_id: undefined,
      row_id: undefined,
      column_id: undefined,
      text: null,
      content_hash: null,
      layers: [
        {
          ...annotationsFixture.layers[0],
          counts: undefined,
          spans: [annotationsFixture.layers[0].spans[0]],
        },
        {
          toggle_key: '3:5:invalid-geometry',
          layer_family: 'entities',
          producer: {},
          output_column: {},
          positioned: true,
          spans: [{ start: 3, end: 3 }],
        },
        {
          toggle_key: '3:5:unpositioned',
          layer_family: 'entities',
          producer: {},
          output_column: {},
          positioned: false,
          unpositioned: { reason: 'unknown', total: undefined },
        },
        { layer_family: 'dropped' },
      ],
    };
    const responses = [
      domainAnnotations,
      scalarCellValueFixture,
      columnFixture,
      viewerJsonLeavesFixture,
    ];
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(responses.shift())));
    const api = createProjectEvidenceDomainApi(
      (status) => new Error(`evidence ${status}`),
      'evidence-domain-factory',
    );

    await expect(api.getTextAnnotations('row-a', 'column-b')).resolves.toEqual({
      sheetId: '',
      rowId: 'row-a',
      columnId: 'column-b',
      text: null,
      contentHash: null,
      layers: [
        {
          toggleKey: '3:5:entities',
          layerFamily: 'entities',
          producerKind: 'entities',
          producerEngine: null,
          outputColumn: { id: '5', name: 'Entities' },
          positioned: true,
          spans: [{
            occurrenceId: 'span:1',
            start: 0,
            end: 3,
            quote: 'Ada',
            entityType: 'PERSON',
            entityFingerprint: 'person:ada',
          }],
          counts: { shown: 1, total: 1, invalid: 0 },
        },
        {
          toggleKey: '3:5:invalid-geometry',
          layerFamily: 'entities',
          producerKind: null,
          producerEngine: null,
          outputColumn: { id: '', name: null },
          positioned: true,
          spans: [],
          counts: { shown: 0, total: 0, invalid: 0 },
        },
        {
          toggleKey: '3:5:unpositioned',
          layerFamily: 'entities',
          producerKind: null,
          producerEngine: null,
          outputColumn: { id: '', name: null },
          positioned: false,
          unpositioned: { reason: 'invalid_geometry', total: 0 },
        },
      ],
    });
    await expect(api.getCellEvidence('7', '5')).resolves.toEqual(scalarCellValueFixture);
    await expect(api.getColumnEvidence('3', '5', ['7'])).resolves.toEqual(columnFixture);
    await expect(api.getEvidenceViewer('json-leaves')).resolves.toEqual(viewerJsonLeavesFixture);
  });

  it('preserves scalar cell-evidence JSON leaves through RealApi', async () => {
    const real = await import('../../src/api/real');
    const projectApi = real.createProjectApi('evidence-scalar-leaf');
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(scalarCellValueFixture)));
    await expect(projectApi.getCellEvidence('7', '5'))
      .resolves.toEqual(scalarCellValueFixture);
  });

  it('keeps RealApi domain mapping and bare ActionError details stable', async () => {
    const real = await import('../../src/api/real');
    const projectApi = real.createProjectApi('evidence-domain');
    const responses = [annotationsFixture, cellFixture, columnFixture, viewerFixture];
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(responses.shift())));
    await expect(projectApi.getTextAnnotations('7', '5')).resolves.toEqual({
      sheetId: '3',
      rowId: '7',
      columnId: '5',
      text: 'Ada',
      contentHash: 'sha256:current',
      layers: [{
        toggleKey: '3:5:entities',
        layerFamily: 'entities',
        producerKind: 'entities',
        producerEngine: null,
        outputColumn: { id: '5', name: 'Entities' },
        positioned: true,
        spans: [{
          occurrenceId: 'span:1',
          start: 0,
          end: 3,
          quote: 'Ada',
          entityType: 'PERSON',
          entityFingerprint: 'person:ada',
        }],
        counts: { shown: 1, total: 1, invalid: 0 },
      }],
    });
    await expect(projectApi.getCellEvidence('7', '5', { includeStale: true }))
      .resolves.toEqual(cellFixture);
    await expect(projectApi.getColumnEvidence('3', '5', { rowIds: ['7'] }))
      .resolves.toEqual(columnFixture);
    await expect(projectApi.getEvidenceViewer('evidence-link-11'))
      .resolves.toEqual(viewerFixture);

    const errorPayload = {
      schema_version: 'frisket.action_error.v1',
      code: 'evidence_not_found',
      message: 'Evidence link is missing',
      field: null,
      details: { evidence_link_id: 'missing' },
    };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(errorPayload, 404)));
    await expect(projectApi.getEvidenceViewer('missing')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'Evidence link is missing',
      code: 'evidence_not_found',
      details: { evidence_link_id: 'missing' },
    });

    const projectError = {
      detail: {
        code: 'project_not_found',
        message: 'Project is missing',
        field: 'pid',
      },
    };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(projectError, 404)));
    await expect(projectApi.getCellEvidence('7', '5')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'Project is missing',
      code: 'project_not_found',
      details: undefined,
    });
  });
});
