import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  CreateEmbeddingIndexInput,
  EmbeddingComposedQuery,
  EmbeddingExportResult,
  EmbeddingIndexFreshness,
  EmbeddingIndexAnalysisInput,
  EmbeddingIndexAnalysisResult,
  EmbeddingIndexSummary,
  EmbeddingProvider,
  EmbeddingSimilarityResult,
  UpdateEmbeddingIndexPolicyInput,
  JsonValue,
} from './types';
import {
  ApiError,
  ConfirmationRequiredError,
  v1ErrorMessage,
} from './contractErrors';
import type { V1ActionSession, V1ActionResult } from './v1ActionSession';

export interface EmbeddingsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface EmbeddingsApi {
  embeddingProviderCatalog(
    projectId: string,
    opts: { modality?: string; sourceColumnType?: string },
    options?: EmbeddingsOptions,
  ): Promise<EmbeddingProvider[]>;
  embeddingIndexes(
    projectId: string,
    sheetId: number,
    options?: EmbeddingsOptions,
  ): Promise<EmbeddingIndexSummary[]>;
  updateEmbeddingIndexPolicy(
    projectId: string,
    input: UpdateEmbeddingIndexPolicyInput,
  ): Promise<void>;
  createEmbeddingIndex(projectId: string, input: CreateEmbeddingIndexInput): Promise<string>;
  refreshEmbeddingIndex(
    projectId: string,
    indexId: string,
    mode: 'incremental' | 'full',
  ): Promise<{ jobId: number | null; status: string }>;
  runEmbeddingIndexAnalysis(
    projectId: string,
    input: EmbeddingIndexAnalysisInput,
  ): Promise<EmbeddingIndexAnalysisResult>;
  exportEmbeddingIndex(
    projectId: string,
    indexId: string,
    opts?: { formats?: string[]; includeVectors?: boolean },
    options?: EmbeddingsOptions,
  ): Promise<EmbeddingExportResult>;
  embeddingSimilarityPreview(
    projectId: string,
    indexId: string,
    query: string | EmbeddingComposedQuery,
    opts?: { limit?: number },
    options?: EmbeddingsOptions,
  ): Promise<EmbeddingSimilarityResult>;
  embeddingHybridPreview(
    projectId: string,
    indexId: string,
    sheetId: number | null,
    text: string,
    opts?: { limit?: number },
    options?: EmbeddingsOptions,
  ): Promise<EmbeddingSimilarityResult>;
}

type EmbeddingsV1ActionSession = Pick<
  V1ActionSession,
  | 'v1ActionSpec'
  | 'registeredProjectActionSpec'
  | 'postV1ActionSpec'
  | 'clearV1ActionIdempotencyKey'
  | 'releaseV1ActionIdempotencyKey'
>;

export interface EmbeddingsApiDependencies {
  v1ActionSession: EmbeddingsV1ActionSession;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type ProviderCatalogWire = HttpContractSuccessResponse<
  'tenant.embedding_provider_catalog.get'
>;
type IndexesWire = HttpContractSuccessResponse<'tenant.embedding_indexes.get'>;
type ExportWire = HttpContractSuccessResponse<'tenant.embedding_index_export.post'>;
type SimilarityWire = HttpContractSuccessResponse<
  'tenant.embedding_similarity_preview.post'
>;
type HybridWire = HttpContractSuccessResponse<'tenant.embedding_hybrid_preview.post'>;

type MaintenanceMode = 'manual' | 'on_source_append' | 'scheduled';

const MAINTENANCE_MODES = new Set<MaintenanceMode>([
  'manual',
  'on_source_append',
  'scheduled',
]);

function isMaintenanceMode(value: unknown): value is MaintenanceMode {
  return typeof value === 'string' && MAINTENANCE_MODES.has(value as MaintenanceMode);
}

function normalizeIndexPolicy(index: IndexesWire['indexes'][number]): Pick<
  EmbeddingIndexSummary,
  | 'allowRemote'
  | 'allowRemoteAutomaticRefresh'
  | 'maxCostUsdPerRefresh'
  | 'maintenanceMode'
  | 'schedule'
  | 'policyNeedsRepair'
> {
  const provider = index.provider_policy;
  const maintenance = index.maintenance;
  const allowRemoteRaw = provider.allow_remote;
  const allowAutoRaw = provider.allow_remote_automatic_refresh;
  const costRaw = provider.max_cost_usd_per_refresh;
  const modeRaw = maintenance.mode;
  const scheduleRaw = maintenance.schedule;
  let policyNeedsRepair =
    (allowRemoteRaw !== undefined && typeof allowRemoteRaw !== 'boolean') ||
    (allowAutoRaw !== undefined && typeof allowAutoRaw !== 'boolean') ||
    (costRaw !== undefined && costRaw !== null &&
      (typeof costRaw !== 'number' || !Number.isFinite(costRaw) || costRaw <= 0)) ||
    (modeRaw !== undefined &&
      !isMaintenanceMode(modeRaw)) ||
    (scheduleRaw !== undefined && scheduleRaw !== null && typeof scheduleRaw !== 'string');
  const allowRemote = allowRemoteRaw === true;
  let allowRemoteAutomaticRefresh = allowAutoRaw === true;
  const maxCostUsdPerRefresh =
    typeof costRaw === 'number' && Number.isFinite(costRaw) && costRaw > 0 ? costRaw : null;
  let maintenanceMode: MaintenanceMode = isMaintenanceMode(modeRaw) ? modeRaw : 'manual';
  let schedule = typeof scheduleRaw === 'string' ? scheduleRaw : null;

  if (!allowRemote && allowRemoteAutomaticRefresh) {
    allowRemoteAutomaticRefresh = false;
    policyNeedsRepair = true;
  }
  if (maintenanceMode === 'scheduled' && !schedule?.trim()) {
    maintenanceMode = 'manual';
    schedule = null;
    policyNeedsRepair = true;
  }

  return {
    allowRemote,
    allowRemoteAutomaticRefresh,
    maxCostUsdPerRefresh,
    maintenanceMode,
    schedule,
    policyNeedsRepair,
  };
}

function mapProviderCatalog(wire: ProviderCatalogWire): EmbeddingProvider[] {
  return wire.providers.map((provider) => ({
    providerId: provider.provider_id,
    providerKind: provider.provider_kind,
    modelId: provider.model_id,
    label: provider.label,
    modalities: provider.modalities ?? [],
    dimensions: provider.dimensions ?? null,
    local: provider.local,
    available: provider.available,
    disabledReason: provider.disabled_reason,
    egress: (provider.privacy as { egress?: string }).egress ?? null,
    recommended: Boolean(provider.recommended),
    sizeGb: provider.size_gb ?? null,
    maxInputTokens: provider.max_input_tokens ?? null,
    pricing: provider.pricing
      ? {
          policy: typeof provider.pricing.policy === 'string'
            ? provider.pricing.policy
            : 'unknown_unit_price',
          inputUsdPerMillionTokens:
            typeof provider.pricing.input_usd_per_million_tokens === 'number'
              ? provider.pricing.input_usd_per_million_tokens
              : null,
          sourceUrl: typeof provider.pricing.source_url === 'string'
            ? provider.pricing.source_url
            : null,
          updated: typeof provider.pricing.updated === 'string'
            ? provider.pricing.updated
            : null,
        }
      : null,
    modalityCompatible: provider.modality_compatible !== false,
    dimensionDiscoveryRequired: Boolean(provider.dimension_discovery_required),
  }));
}

function mapFreshness(
  freshness: IndexesWire['indexes'][number]['freshness'],
): EmbeddingIndexFreshness {
  return {
    reason: freshness.reason,
    current: freshness.current,
    missing: freshness.missing,
    stale: freshness.stale,
    error: freshness.error,
    total: freshness.total,
    lastRefreshJobId: freshness.last_refresh_job_id,
    lastRefreshReceiptId: freshness.last_refresh_receipt_id,
    pendingRefreshJobId: freshness.pending_refresh_job_id,
  };
}

function mapIndexes(wire: IndexesWire): EmbeddingIndexSummary[] {
  return wire.indexes.map((index) => {
    const policy = normalizeIndexPolicy(index);
    return {
      indexId: index.index_id,
      name: index.name,
      sheetId: index.sheet_id,
      modality: index.modality,
      providerId: index.provider_id,
      modelId: index.model_id,
      sourceColumns: index.source_columns,
      status: index.status,
      totalItems: index.total_items,
      readyItems: index.ready_items,
      staleItems: index.stale_items,
      missingItems: index.missing_source_items,
      errorItems: index.error_items,
      refreshNeeded: index.refresh_needed,
      lastRefreshedAt: index.last_refreshed_at,
      remote: index.remote,
      allowRemote: policy.allowRemote,
      allowRemoteAutomaticRefresh: policy.allowRemoteAutomaticRefresh,
      providerKind: index.provider_kind,
      maxCostUsdPerRefresh: policy.maxCostUsdPerRefresh,
      maintenanceMode: policy.maintenanceMode,
      schedule: policy.schedule,
      policyNeedsRepair: policy.policyNeedsRepair,
      freshness: mapFreshness(index.freshness),
      spaceId: index.space_id,
      dimension: index.dimension,
      distanceMetric: index.distance_metric,
    };
  });
}

function mapExport(wire: ExportWire): EmbeddingExportResult {
  return {
    receiptId: wire.receipt_id,
    artifacts: wire.artifacts.map((artifact) => ({
      format: artifact.format,
      path: artifact.path,
      byteCount: artifact.byte_count,
      sha256: artifact.sha256,
      rowCount: artifact.row_count,
    })),
  };
}

function mapSimilarity(wire: SimilarityWire): EmbeddingSimilarityResult {
  return {
    indexId: wire.index_id,
    distanceMetric: wire.distance_metric,
    hits: wire.hits.map((hit) => ({
      rowId: hit.row_id,
      distance: hit.distance,
      score: hit.score,
      values: hit.values,
    })),
  };
}

function mapHybrid(wire: HybridWire): EmbeddingSimilarityResult {
  return {
    indexId: wire.index_id,
    distanceMetric: wire.distance_metric,
    hits: wire.hits.map((hit) => ({
      rowId: hit.row_id,
      distance: hit.distance,
      score: hit.score,
      values: hit.values ?? {},
    })),
  };
}

function retainsIdempotencyAfterPreacceptFailure(error: unknown): boolean {
  return error instanceof ConfirmationRequiredError
    || (error instanceof ApiError && error.status === 402);
}

async function runEmbeddingAction(
  v1ActionSession: EmbeddingsV1ActionSession,
  projectId: string,
  spec: Record<string, unknown>,
  options: { allowQueued?: boolean } = {},
): Promise<V1ActionResult> {
  let out: V1ActionResult;
  try {
    out = await v1ActionSession.postV1ActionSpec(spec, { projectId });
  } catch (error) {
    if (!retainsIdempotencyAfterPreacceptFailure(error)) {
      v1ActionSession.clearV1ActionIdempotencyKey(spec, projectId);
    }
    throw error;
  }

  const queued = out.status === 'queued' || out.status === 'running';
  if (out.status === 'completed' || (options.allowQueued && queued)) {
    // Release before the caller interprets outputs, so malformed completed
    // payloads cannot turn a completed command into a permanently replayed key.
    v1ActionSession.releaseV1ActionIdempotencyKey(spec, projectId);
    return out;
  }

  v1ActionSession.clearV1ActionIdempotencyKey(spec, projectId);
  const err = out.errors?.[0];
  throw new ApiError(400, v1ErrorMessage(out, 'embedding action failed'), err?.code);
}

export function createEmbeddingsApi(
  errorFactory: ContractErrorFactory,
  { v1ActionSession }: EmbeddingsApiDependencies,
): EmbeddingsApi {
  return {
    embeddingProviderCatalog(projectId, opts, options = {}) {
      return httpContract(
        'tenant.embedding_provider_catalog.get',
        {
          pathParams: { pid: projectId },
          query: {
            ...(opts.modality ? { modality: opts.modality } : {}),
            ...(opts.sourceColumnType ? { source_column_type: opts.sourceColumnType } : {}),
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapProviderCatalog,
      );
    },

    embeddingIndexes(projectId, sheetId, options = {}) {
      return httpContract(
        'tenant.embedding_indexes.get',
        {
          pathParams: { pid: projectId },
          query: { sheet_id: sheetId },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapIndexes,
      );
    },

    async updateEmbeddingIndexPolicy(projectId, input) {
      const params: Record<string, JsonValue> = { index_id: input.indexId };
      if (input.maintenancePolicy) {
        params.maintenance_policy = {
          mode: input.maintenancePolicy.mode,
          ...(input.maintenancePolicy.schedule != null
            ? { schedule: input.maintenancePolicy.schedule }
            : {}),
        };
      }
      if (input.providerPolicy) {
        const pp = input.providerPolicy;
        params.provider_policy = {
          ...(pp.allowRemote != null ? { allow_remote: pp.allowRemote } : {}),
          ...(pp.allowRemoteAutomaticRefresh != null
            ? { allow_remote_automatic_refresh: pp.allowRemoteAutomaticRefresh }
            : {}),
          ...(pp.maxCostUsdPerRefresh !== undefined
            ? { max_cost_usd_per_refresh: pp.maxCostUsdPerRefresh }
            : {}),
        };
      }
      const spec = v1ActionSession.registeredProjectActionSpec(
        'embedding.index_update_policy',
        params,
        projectId,
      );
      await runEmbeddingAction(v1ActionSession, projectId, spec);
    },

    async createEmbeddingIndex(projectId, input) {
      if (input.modality !== 'text') {
        throw new ApiError(
          400,
          'The embeddings UI currently supports text columns only.',
          'embedding_modality_unsupported',
        );
      }
      const providerPolicy: Record<string, JsonValue> = {
        allow_remote: input.allowRemote,
        allow_remote_automatic_refresh: input.allowRemoteAutomaticRefresh,
      };
      if (input.maxCostUsdPerRefresh != null) {
        providerPolicy.max_cost_usd_per_refresh = input.maxCostUsdPerRefresh;
      }
      const params: Record<string, JsonValue> = {
        sheet_id: input.sheetId,
        source_columns: input.sourceColumns,
        modality: input.modality,
        provider: input.provider,
        source_policy: { kind: 'text_cell' },
        provider_policy: providerPolicy,
      };
      if (input.model) params.model = input.model;
      const spec = v1ActionSession.registeredProjectActionSpec(
        'embedding.index_create',
        params,
        projectId,
      );
      const out = await runEmbeddingAction(v1ActionSession, projectId, spec);
      const ref = out.outputs?.[0]?.ref as { index_id?: string } | undefined;
      if (!ref?.index_id) throw new ApiError(500, 'index_create returned no index id');
      return ref.index_id;
    },

    async refreshEmbeddingIndex(projectId, indexId, mode) {
      const spec = v1ActionSession.registeredProjectActionSpec(
        'embedding.index_refresh',
        { index_id: indexId, mode },
        projectId,
      );
      const out = await runEmbeddingAction(v1ActionSession, projectId, spec, {
        allowQueued: true,
      });
      return { jobId: out.job_id ?? null, status: out.status };
    },

    async runEmbeddingIndexAnalysis(projectId, input) {
      const spec = v1ActionSession.registeredProjectActionSpec(
        input.action_id,
        input.params,
        projectId,
        undefined,
        { sheet_name: input.sheet_name, output_names: input.output_names },
      );
      const out = await runEmbeddingAction(v1ActionSession, projectId, spec);
      const output = out.outputs?.[0];
      const refSheetId = (output?.ref as { sheet_id?: number | null } | undefined)?.sheet_id;
      const sheetId = output?.sheet_id ?? refSheetId ?? null;
      if (sheetId == null) {
        throw new ApiError(500, `${input.action_id} returned no child sheet id`);
      }
      return { sheetId };
    },

    exportEmbeddingIndex(projectId, indexId, opts, options = {}) {
      return httpContract(
        'tenant.embedding_index_export.post',
        {
          pathParams: { pid: projectId, index_id: indexId },
          query: {},
          body: {
            ...(opts?.formats !== undefined ? { formats: opts.formats } : {}),
            ...(opts?.includeVectors !== undefined
              ? { include_vectors: opts.includeVectors }
              : {}),
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapExport,
      );
    },

    embeddingSimilarityPreview(projectId, indexId, query, opts, options = {}) {
      const anchor =
        typeof query === 'string'
          ? { kind: 'manual_text_query', text: query }
          : {
              kind: 'manual_text_query',
              terms: query.terms,
              ...(query.exclude && query.exclude.length ? { exclude: query.exclude } : {}),
            };
      return httpContract(
        'tenant.embedding_similarity_preview.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            query: {
              kind: 'embedding_similarity',
              embedding_index_id: indexId,
              anchor: anchor as never,
              ...(opts?.limit ? { limit: opts.limit } : {}),
            },
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapSimilarity,
      );
    },

    embeddingHybridPreview(projectId, indexId, sheetId, text, opts, options = {}) {
      return httpContract(
        'tenant.embedding_hybrid_preview.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            query: {
              kind: 'embedding_hybrid',
              embedding_index_id: indexId,
              sheet_id: sheetId,
              text,
              ...(opts?.limit ? { limit: opts.limit } : {}),
            },
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapHybrid,
      );
    },
  };
}
