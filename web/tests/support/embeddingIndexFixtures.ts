import type { HttpContractSuccessResponse } from '../../src/api/httpContract';

type EmbeddingIndexListFixture = HttpContractSuccessResponse<'tenant.embedding_indexes.get'>;
export type EmbeddingIndexFixture = EmbeddingIndexListFixture['indexes'][number];

type Identity = Pick<EmbeddingIndexFixture, 'index_id' | 'name' | 'sheet_id' | 'space_id'>;
type EmbeddingIndexFixtureInput = Identity & Partial<Omit<EmbeddingIndexFixture, keyof Identity>>;

export function embeddingIndexFixture(
  input: EmbeddingIndexFixtureInput,
): EmbeddingIndexFixture {
  const readyItems = input.ready_items ?? 0;
  const staleItems = input.stale_items ?? 0;
  const errorItems = input.error_items ?? 0;
  const totalItems = input.total_items ?? readyItems + staleItems + errorItems;
  const missingSourceItems = input.missing_source_items
    ?? Math.max(0, totalItems - readyItems - staleItems - errorItems);
  const refreshNeeded = input.refresh_needed ?? (missingSourceItems > 0 || staleItems > 0);
  const maintenance = input.maintenance ?? { mode: 'manual', schedule: null };
  const maintenanceMode = typeof maintenance.mode === 'string'
    ? maintenance.mode
    : 'manual';
  const freshness = {
    current: readyItems,
    error: errorItems,
    last_refresh_job_id: null,
    last_refresh_receipt_id: null,
    last_refreshed_at: input.last_refreshed_at ?? null,
    maintenance_mode: maintenanceMode,
    missing: missingSourceItems,
    pending_refresh_job_id: null,
    ready: readyItems,
    reason: refreshNeeded ? 'missing_rows' : 'current',
    refresh_needed: refreshNeeded,
    scope_resolved: true,
    stale: staleItems,
    total: totalItems,
    ...input.freshness,
  } satisfies EmbeddingIndexFixture['freshness'];

  const fixture: EmbeddingIndexFixture = {
    dimension: null,
    distance_metric: null,
    error_items: errorItems,
    last_refreshed_at: null,
    maintenance,
    missing_source_items: missingSourceItems,
    modality: 'text',
    model_id: null,
    provider_id: null,
    provider_kind: null,
    provider_policy: {
      allow_remote: false,
      allow_remote_automatic_refresh: false,
      max_cost_usd_per_refresh: null,
    },
    ready_items: readyItems,
    refresh_needed: refreshNeeded,
    remote: false,
    source_columns: [],
    stale_items: staleItems,
    stale_source_items: staleItems,
    status: 'idle',
    total_items: totalItems,
    freshness,
    ...input,
  };

  return {
    ...fixture,
    freshness,
    maintenance,
    missing_source_items: missingSourceItems,
    ready_items: readyItems,
    refresh_needed: refreshNeeded,
    stale_items: staleItems,
    total_items: totalItems,
  };
}

export function embeddingIndexListFixture(
  sheetId: number | null,
  indexes: EmbeddingIndexFixture[],
): EmbeddingIndexListFixture {
  return {
    schema_version: 'frisket.embedding_index_list.v1',
    sheet_id: sheetId,
    indexes,
  };
}
