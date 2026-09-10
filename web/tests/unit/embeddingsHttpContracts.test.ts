import { afterEach, describe, expect, it, vi } from 'vitest';

import { createEmbeddingsApi } from '../../src/api/embeddings';
import type { V1ActionSession } from '../../src/api/v1ActionSession';

const catalog = {
  schema_version: 'frisket.embedding_provider_catalog.v1',
  modality: 'text',
  source_column_type: 'markdown',
  providers: [{
    provider_id: 'local',
    provider_kind: 'local',
    model_id: 'tiny',
    label: 'Tiny local model',
    modalities: ['text'],
    dimensions: [384],
    local: true,
    available: true,
    disabled_reason: null,
    privacy: { egress: 'local' },
    recommended: true,
    size_gb: 0.5,
    max_input_tokens: 1024,
    modality_compatible: true,
    dimension_discovery_required: false,
  }],
};

const indexes = {
  schema_version: 'frisket.embedding_index_list.v1',
  sheet_id: 3,
  indexes: [{
    index_id: 'idx_1',
    name: 'People',
    sheet_id: 3,
    modality: 'text',
    provider_id: 'local',
    provider_kind: 'local',
    model_id: 'tiny',
    source_columns: ['bio'],
    status: 'ready',
    total_items: 12,
    ready_items: 12,
    stale_items: 0,
    missing_source_items: 0,
    error_items: 0,
    refresh_needed: false,
    last_refreshed_at: '2026-08-11T12:00:00Z',
    remote: false,
    provider_policy: { allow_remote: false },
    maintenance: { mode: 'manual', schedule: null },
    freshness: {
      reason: 'fresh',
      current: 12,
      missing: 0,
      stale: 0,
      error: 0,
      total: 12,
      last_refresh_job_id: 'job_12',
      last_refresh_receipt_id: null,
      pending_refresh_job_id: null,
      ready: 12,
      last_refreshed_at: '2026-08-11T12:00:00Z',
      maintenance_mode: 'manual',
      refresh_needed: false,
      scope_resolved: true,
    },
    space_id: 'space_1',
    dimension: 384,
    distance_metric: 'cosine',
    stale_source_items: 0,
  }],
};

const exported = {
  schema_version: 'frisket.embedding_index_export_result.v1',
  index_id: 'idx_1',
  receipt_id: 'receipt_1',
  artifacts: [{
    kind: 'export_artifact',
    export_kind: 'embedding_index_export',
    format: 'jsonl',
    path: 'exports/idx_1.jsonl',
    byte_count: 12,
    sha256: `sha256:${'a'.repeat(64)}`,
    row_count: 1,
  }],
};

const similarity = {
  schema_version: 'frisket.embedding_similarity_preview.v1',
  index_id: 'idx_1',
  space_id: 'space_1',
  sheet_id: 3,
  distance_metric: 'cosine',
  limit: 10,
  anchor: { kind: 'manual_text_query', text: 'needle' },
  hits: [{ row_id: 9, sheet_id: 3, distance: 0.12, score: null, values: { name: 'Ada' } }],
};

const hybrid = {
  schema_version: 'frisket.embedding_hybrid_preview.v1',
  index_id: 'idx_1',
  sheet_id: 3,
  distance_metric: 'rrf',
  hits: [{
    row_id: 9,
    sheet_id: 3,
    distance: null,
    score: 0.03,
    vector_rank: 1,
    keyword_rank: null,
    values: undefined,
  }],
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function actionResult(kind: string, status = 'completed'): Record<string, unknown> {
  return {
    schema_version: 'frisket.action_result.v1',
    action: { kind, action_id: `embedding-${kind}` },
    status,
    project_id: 'embedding-command-project',
    run_id: null,
    receipt_id: null,
    errors: [],
    outputs: [],
  };
}

function requestBody(init: RequestInit | undefined): Record<string, unknown> {
  return JSON.parse(String(init?.body)) as Record<string, unknown>;
}

const readOnlyV1ActionSession = {
  v1ActionSpec: vi.fn(),
  registeredProjectActionSpec: vi.fn(),
  postV1ActionSpec: vi.fn(),
  clearV1ActionIdempotencyKey: vi.fn(),
  releaseV1ActionIdempotencyKey: vi.fn(),
} as Pick<
  V1ActionSession,
  | 'v1ActionSpec'
  | 'registeredProjectActionSpec'
  | 'postV1ActionSpec'
  | 'clearV1ActionIdempotencyKey'
  | 'releaseV1ActionIdempotencyKey'
>;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('embeddings generated HTTP contracts', () => {
  it('creates and queues refresh with typed intent and durable provider policies', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = requestBody(init);
      bodies.push(body);
      return jsonResponse({
        ...actionResult(String(body.action_id)),
        ...(body.action_id === 'embedding.index_refresh'
          ? { status: 'queued', job_id: 42, receipt_id: 'refresh-receipt' }
          : { outputs: [{ kind: 'embedding_index', ref: { index_id: 'idx_1' } }] }),
      });
    }));
    const { createProjectApi } = await import('../../src/api/real');
    const api = createProjectApi('embedding-lifecycle');
    await expect(api.createEmbeddingIndex({
      sheetId: 3, sourceColumns: ['body'], modality: 'text', provider: 'openrouter',
      model: 'custom/model', allowRemote: true, allowRemoteAutomaticRefresh: true,
      maxCostUsdPerRefresh: 0.5,
    })).resolves.toBe('idx_1');
    await expect(api.refreshEmbeddingIndex('idx_1', 'full')).resolves.toEqual({
      jobId: 42, status: 'queued',
    });
    expect(bodies).toEqual([
      {
        action_id: 'embedding.index_create', scope: { kind: 'project' },
        output_names: {}, idempotency_key: expect.any(String),
        params: {
          sheet_id: 3, source_columns: ['body'], modality: 'text', provider: 'openrouter',
          model: 'custom/model', source_policy: { kind: 'text_cell' },
          provider_policy: { allow_remote: true, allow_remote_automatic_refresh: true,
            max_cost_usd_per_refresh: 0.5 },
        },
      },
      {
        action_id: 'embedding.index_refresh', scope: { kind: 'project' },
        output_names: {}, idempotency_key: expect.any(String),
        params: { index_id: 'idx_1', mode: 'full' },
      },
    ]);
  });

  it('runs both analyses as typed project requests and opens their materialized sheet', async () => {
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const body = requestBody(init);
      requests.push({ url: String(url), body });
      return jsonResponse({
        ...actionResult(String(body.action_id)),
        outputs: [{ kind: 'sheet', sheet_id: 17, ref: { kind: 'materialized_sheet', sheet_id: 17 } }],
      });
    }));
    const { createProjectApi } = await import('../../src/api/real');
    const api = createProjectApi('embedding-command-project');

    await expect(api.runEmbeddingIndexAnalysis({
      action_id: 'embedding.index_project',
      sheet_name: 'PCA of People',
      params: { index_id: 'idx_1', method: 'pca', dimensions: 2 },
      output_names: { dim_0: 'horizontal', dim_1: 'vertical' },
    })).resolves.toEqual({ sheetId: 17 });
    await expect(api.runEmbeddingIndexAnalysis({
      action_id: 'embedding.index_cluster',
      sheet_name: 'Clusters of People',
      params: { index_id: 'idx_1', method: 'kmeans', k: 4, seed: 7 },
    })).resolves.toEqual({ sheetId: 17 });
    expect(requests).toEqual([
      {
        url: '/api/projects/embedding-command-project/actions/v1/run',
        body: {
          action_id: 'embedding.index_project', scope: { kind: 'project' },
          sheet_name: 'PCA of People',
          params: { index_id: 'idx_1', method: 'pca', dimensions: 2 },
          output_names: { dim_0: 'horizontal', dim_1: 'vertical' },
          idempotency_key: expect.any(String),
        },
      },
      {
        url: '/api/projects/embedding-command-project/actions/v1/run',
        body: {
          action_id: 'embedding.index_cluster', scope: { kind: 'project' },
          sheet_name: 'Clusters of People',
          params: { index_id: 'idx_1', method: 'kmeans', k: 4, seed: 7 },
          output_names: {}, idempotency_key: expect.any(String),
        },
      },
    ]);
  });

  it('preserves requests, encoding, optional catalog query, defaults, headers, and abort', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [catalog, catalog, indexes, exported, similarity, hybrid];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer session', 'X-Trace-Id': 'embeddings' },
    };
    const api = createEmbeddingsApi(
      (status, payload) => new Error(`${status}:${String(payload)}`),
      { v1ActionSession: readOnlyV1ActionSession },
    );

    await api.embeddingProviderCatalog('project one', {}, options);
    const providers = await api.embeddingProviderCatalog(
      'project one',
      { modality: 'text', sourceColumnType: 'markdown' },
      options,
    );
    const listed = await api.embeddingIndexes('project one', 3, options);
    const exportResult = await api.exportEmbeddingIndex('project one', 'idx /☃', undefined, options);
    const similar = await api.embeddingSimilarityPreview(
      'project one',
      'idx_1',
      { terms: [{ text: 'needle', weight: 1 }], exclude: [{ text: 'hay', threshold: 0.5 }] },
      { limit: 10 },
      options,
    );
    const hybridResult = await api.embeddingHybridPreview(
      'project one', 'idx_1', 3, 'needle', { limit: 10 }, options,
    );

    expect(providers[0]?.egress).toBe('local');
    expect(listed[0]?.freshness.lastRefreshJobId).toBe('job_12');
    expect(exportResult).toEqual({
      receiptId: 'receipt_1',
      artifacts: [{ format: 'jsonl', path: 'exports/idx_1.jsonl', byteCount: 12, sha256: `sha256:${'a'.repeat(64)}`, rowCount: 1 }],
    });
    expect(similar.hits[0]?.values).toEqual({ name: 'Ada' });
    expect(hybridResult.hits[0]).toMatchObject({ distance: null, values: {} });
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      ['/api/projects/project%20one/embeddings/v1/provider-catalog', 'GET', undefined],
      ['/api/projects/project%20one/embeddings/v1/provider-catalog?modality=text&source_column_type=markdown', 'GET', undefined],
      ['/api/projects/project%20one/embeddings/v1/indexes?sheet_id=3', 'GET', undefined],
      ['/api/projects/project%20one/embeddings/v1/indexes/idx%20%2F%E2%98%83/export', 'POST', '{}'],
      ['/api/projects/project%20one/embeddings/v1/similarity-preview', 'POST', JSON.stringify({
        query: {
          kind: 'embedding_similarity', embedding_index_id: 'idx_1',
          anchor: { kind: 'manual_text_query', terms: [{ text: 'needle', weight: 1 }], exclude: [{ text: 'hay', threshold: 0.5 }] },
          limit: 10,
        },
      })],
      ['/api/projects/project%20one/embeddings/v1/hybrid-preview', 'POST', JSON.stringify({
        query: { kind: 'embedding_hybrid', embedding_index_id: 'idx_1', sheet_id: 3, text: 'needle', limit: 10 },
      })],
    ]);
    for (const { init } of requests) {
      expect(init?.signal).toBe(controller.signal);
      const headers = new Headers(init?.headers);
      expect(headers.get('authorization')).toBe('Bearer session');
      expect(headers.get('x-trace-id')).toBe('embeddings');
    }
    for (const request of requests.slice(3)) {
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
  });

  it('forwards non-2xx payloads to the scoped error factory', async () => {
    const errorFactory = vi.fn((status: number, payload: unknown) =>
      Object.assign(new Error('embedding failed'), { status, payload }),
    );
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: { code: 'embedding_source_stale', message: 'refresh required', extra: 'private' },
    }, 409)));
    const api = createEmbeddingsApi(errorFactory, { v1ActionSession: readOnlyV1ActionSession });

    await expect(api.embeddingIndexes('project', 3)).rejects.toMatchObject({
      message: 'embedding failed', status: 409,
    });
    expect(errorFactory).toHaveBeenCalledWith(409, {
      detail: { code: 'embedding_source_stale', message: 'refresh required', extra: 'private' },
    });
  });

  it('normalizes corrupt stored policy leaves for a safe repair', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ...indexes,
      indexes: [{
        ...indexes.indexes[0],
        remote: true,
        provider_policy: {
          allow_remote: 'false',
          allow_remote_automatic_refresh: 1,
          max_cost_usd_per_refresh: 'unbounded',
          ignored: true,
        },
        maintenance: { mode: 'scheduled', schedule: 12, ignored: true },
      }],
    })));
    const api = createEmbeddingsApi(
      (status, payload) => new Error(`${status}:${String(payload)}`),
      { v1ActionSession: readOnlyV1ActionSession },
    );

    const [index] = await api.embeddingIndexes('project', 3);

    expect(index).toMatchObject({
      allowRemote: false,
      allowRemoteAutomaticRefresh: false,
      maxCostUsdPerRefresh: null,
      maintenanceMode: 'manual',
      schedule: null,
      policyNeedsRepair: true,
    });
  });

  it.each([0, -1])('marks non-positive stored cap %s for repair', async (costCap) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ...indexes,
      indexes: [{
        ...indexes.indexes[0],
        remote: true,
        provider_policy: {
          allow_remote: true,
          allow_remote_automatic_refresh: true,
          max_cost_usd_per_refresh: costCap,
        },
      }],
    })));
    const api = createEmbeddingsApi(
      (status, payload) => new Error(`${status}:${String(payload)}`),
      { v1ActionSession: readOnlyV1ActionSession },
    );

    await expect(api.embeddingIndexes('project', 3)).resolves.toMatchObject({
      0: { maxCostUsdPerRefresh: null, policyNeedsRepair: true },
    });
  });

  it('normalizes the encoded project scope once before all RealApi embeddings contracts', async () => {
    const requests: string[] = [];
    const responses = [catalog, indexes, exported, similarity, hybrid];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return jsonResponse(responses.shift());
    }));
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('project one/alpha');

    await projectApi.embeddingProviderCatalog({ modality: 'text' });
    await projectApi.embeddingIndexes(3);
    await projectApi.exportEmbeddingIndex('idx_1');
    await projectApi.embeddingSimilarityPreview('idx_1', 'needle');
    await projectApi.embeddingHybridPreview('idx_1', 3, 'needle');

    expect(requests).toEqual([
      '/api/projects/project%20one%2Falpha/embeddings/v1/provider-catalog?modality=text',
      '/api/projects/project%20one%2Falpha/embeddings/v1/indexes?sheet_id=3',
      '/api/projects/project%20one%2Falpha/embeddings/v1/indexes/idx_1/export',
      '/api/projects/project%20one%2Falpha/embeddings/v1/similarity-preview',
      '/api/projects/project%20one%2Falpha/embeddings/v1/hybrid-preview',
    ]);
  });

  it('uses the RealApi embeddings error mapper without exposing detail extras', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: { code: 'embedding_source_stale', message: 'refresh required', extra: 'private' },
    }, 409)));
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('embedding-error');

    await expect(projectApi.embeddingIndexes(3)).rejects.toMatchObject({
      name: 'ApiError',
      status: 409,
      message: 'refresh required',
      code: 'embedding_source_stale',
      details: undefined,
    });
  });

  it('shares one semantic idempotency key across genuinely concurrent policy updates', async () => {
    const bodies: Record<string, unknown>[] = [];
    let resolveBothPosts!: () => void;
    const bothPosts = new Promise<void>((resolve) => {
      resolveBothPosts = resolve;
    });
    let releasePosts!: () => void;
    const deferredPosts = new Promise<void>((resolve) => {
      releasePosts = resolve;
    });
    vi.stubGlobal('fetch', vi.fn(async (_raw: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(requestBody(init));
      if (bodies.length === 2) resolveBothPosts();
      await deferredPosts;
      return jsonResponse(actionResult('embedding.index_update_policy'));
    }));
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('embedding-command-project');

    const first = projectApi.updateEmbeddingIndexPolicy({ indexId: 'idx-concurrent', providerPolicy: {} });
    const second = projectApi.updateEmbeddingIndexPolicy({ indexId: 'idx-concurrent', providerPolicy: {} });
    await bothPosts;

    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toEqual({
      action_id: 'embedding.index_update_policy',
      scope: { kind: 'project' },
      params: { index_id: 'idx-concurrent', provider_policy: {} },
      output_names: {},
      idempotency_key: expect.any(String),
    });
    expect(bodies[0]?.idempotency_key).toEqual(expect.any(String));
    expect(bodies[0]?.idempotency_key).not.toBe('');
    expect(bodies[1]?.idempotency_key).toBe(bodies[0]?.idempotency_key);

    releasePosts();
    await expect(Promise.all([first, second])).resolves.toEqual([undefined, undefined]);
  });

  it('bounds embedding command idempotency across admission, terminal, and replay outcomes', async () => {
    vi.useFakeTimers();
    const bodies: Record<string, unknown>[] = [];
    const attempts = new Map<string, number>();
    vi.stubGlobal('fetch', vi.fn(async (_raw: RequestInfo | URL, init?: RequestInit) => {
      const body = requestBody(init);
      bodies.push(body);
      const params = body.params as { index_id: string };
      const attempt = (attempts.get(params.index_id) ?? 0) + 1;
      attempts.set(params.index_id, attempt);
      if (params.index_id === 'idx-402' && attempt === 1) {
        return jsonResponse({ detail: 'confirmation required' }, 402);
      }
      if (params.index_id === 'idx-preaccept' && attempt === 1) {
        return jsonResponse({ detail: 'temporarily unavailable' }, 503);
      }
      if (params.index_id === 'idx-terminal' && attempt === 1) {
        return jsonResponse({
          ...actionResult(String(body.action_id), 'failed'),
          errors: [{ code: 'embedding_terminal', message: 'provider rejected update' }],
        });
      }
      return jsonResponse(actionResult(String(body.action_id)));
    }));
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('embedding-command-lifecycle');
    const update = (indexId: string) => projectApi.updateEmbeddingIndexPolicy({ indexId, providerPolicy: {} });

    await expect(update('idx-402')).rejects.toMatchObject({ status: 402 });
    await update('idx-402');
    await expect(update('idx-preaccept')).rejects.toMatchObject({ status: 503 });
    await update('idx-preaccept');
    await expect(update('idx-terminal')).rejects.toThrow('provider rejected update');
    await update('idx-terminal');
    await update('idx-replay');
    await update('idx-replay');
    await vi.advanceTimersByTimeAsync(5001);
    await update('idx-replay');

    expect(bodies[1]?.idempotency_key).toBe(bodies[0]?.idempotency_key);
    expect(bodies[3]?.idempotency_key).not.toBe(bodies[2]?.idempotency_key);
    expect(bodies[5]?.idempotency_key).not.toBe(bodies[4]?.idempotency_key);
    expect(bodies[7]?.idempotency_key).toBe(bodies[6]?.idempotency_key);
    expect(bodies[8]?.idempotency_key).not.toBe(bodies[6]?.idempotency_key);
  });
});
