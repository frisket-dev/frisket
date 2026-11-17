// Open HTTP client for the frisket FastAPI server (src/frisket/server/app.py),
// reached through the vite dev proxy (/api → the local Frisket server; :7331
// by default). Instance-wide operations are module exports; everything scoped
// to one project is composed by createProjectApi from the domain factories.

import {
  getActionJobContract,
  getProjectContract,
  getReceiptContract,
  getRunRowsContract,
  getWorkbenchPluginRuntimeIndexContract,
  listActionJobsContract,
} from './httpContractRoutes';
import { deleteProject, updateProject } from './httpContractRoutes/projects';
import { getGlobalActionCatalog, getProjectActionCatalog } from './httpContractRoutes/catalog';
import { ActionCatalogCache } from './actionCatalog';
import { createProjectQAApi } from './projectQA';
import { emitActionCatalogInvalidated } from './catalogEvents';
import { emitRuntimeConfigChanged } from './runtimeConfigEvents';
import {
  createV1ActionSession,
  resolveRunActionInvocation,
  throwIfActionAborted,
} from './v1ActionSession';
import { createActionRunsApi } from './actionRuns';
import { createGoogleSheetsExportApi } from './googleSheetsExport';
import { createSheetGridApi } from './sheetGrid';
import { googleOAuthStartUrl } from './raw/browserAuth';
import { createMapPointsArrowApi } from './raw/mapPointsArrow';
import {
  embeddingExportArtifactUrl,
  projectExportUrl,
  sheetDatasetExportUrl,
  workLogExportUrl,
} from './raw/projectResources';
import { createActionEstimateValidationApi } from './actionEstimateValidation';
import { createActionPreviewApi } from './actionPreviewRuns';
import { createActionHistoryApi } from './actionHistory';
import { createHistoryReviewApi } from './historyReview';
import { createProjectEvidenceApi } from './projectEvidence';
import { createInstanceRuntimeApi } from './instanceRuntime';
import { createAdminOverviewApi } from './adminOverview';
import { createAdminBrowserApi } from './adminBrowser';
import { createIdentityProfileApi } from './identityProfile';
import { createErrorIntakeApi } from './errorIntake';
import { createEmbeddingsApi } from './embeddings';
import { createWorkbenchPluginsApi } from './workbenchPlugins';
import { createProjectCollaborationApi } from './projectCollaboration';
import { createColumnTypesApi } from './columnTypes';
import { createTranslateComparisonApi } from './translateComparison';
import { createProjectSearchApi } from './projectSearch';
import { createDocumentBrowsingApi } from './documentBrowsing';
import { createProjectDataManagementApi } from './projectDataManagement';
import { createProjectProviderKeysApi } from './projectProviderKeys';
import { createProjectSecretsApi } from './projectSecrets';
import { createMcpServersApi } from './mcpServers';
import { createResolvePreviewsApi } from './resolvePreviews';
import { createRunProvenanceApi } from './runProvenance';
import { createEntityMentionsApi } from './entityMentions';
import { createGraphLineageApi } from './graphLineage';
import { createRuntimeProjectionsApi } from './runtimeProjections';
import { createPreviewComparisonsApi } from './previewComparisons';
import { createOAuthConnectionsApi } from './oauthConnections';
import { createSpendApi } from './spend';
import { ApiError } from './contractErrors';
import { createApiTokensApi } from './apiTokens';
import { createLocalProvidersApi } from './localProviders';
import { createOrganizationProvidersApi } from './organizationProviders';
import { createOrganizationOperationsApi } from './organizationOperations';
import { createProjectAttemptsApi } from './projectAttempts';
import { createProjectSourcesApi } from './projectSources';
import { createTeamLocalModelsApi } from './teamLocalModels';
import { createViewsLensesApi } from './viewsLenses';
import { createWatchesApi } from './watches';
import { createNotificationsApi } from './notifications';
import type { RequestOptions } from './httpContract';
import type {
  ActionJob,
  ActionJobsPage,
  ColumnRunsInfo,
  OcrCompareScratchInput,
  PreviewStartResult,
  ProjectInfo,
  ProjectInvocationOptions,
  RunRowsPage,
  RuntimeConfig,
  TranscribeCompareScratchInput,
  V1Receipt,
  WorkbenchPluginRuntimeIndex,
} from './types';

export {
  V1ActionIdempotencyKeys,
  v1ActionIdempotencySignature,
} from './v1ActionSession';
export {
  ApiError,
  ConfirmationRequiredError,
  rowsEstimateFromDetail,
} from './contractErrors';
export { runEstimateFromV1Wire } from './actionEstimateValidation';
export { onRuntimeConfigChanged } from './runtimeConfigEvents';
export { signOut } from './browserAuth';
export {
  createProject,
  deleteProject,
  listProjects,
  updateProject,
} from './httpContractRoutes/projects';
export type { ActionProposal } from './types';

/** A saved/deleted provider key or secret can flip a hosted engine's
 *  availability: the server resolves keys (DEEPL_API_KEY,
 *  GOOGLE_TRANSLATE_API_KEY, …) when it assembles ui_hints.engines, and an
 *  installed artifact pair changes an engine's `models`. Every cached catalog
 *  (project and env-backed global) is dropped and mounted surfaces are told to
 *  refetch, so availability flips without a remount. */
const actionCatalogCache = new ActionCatalogCache({
  loadGlobal: () => getGlobalActionCatalog(),
  loadProject: (projectId) => getProjectActionCatalog(projectId),
  emitInvalidated: emitActionCatalogInvalidated,
});

function invalidatesActionCatalog<Args extends unknown[], Result>(
  write: (...args: Args) => Promise<Result>,
): (...args: Args) => Promise<Result> {
  return async (...args) => {
    const result = await write(...args);
    actionCatalogCache.invalidate();
    return result;
  };
}

const localProvidersApi = createLocalProvidersApi();
const instanceRuntimeApi = createInstanceRuntimeApi();
const organizationProvidersApi = createOrganizationProvidersApi();
const organizationOperationsApi = createOrganizationOperationsApi();
const teamLocalModelsApi = createTeamLocalModelsApi();

export const { listDocuments: listProjectDocuments } = createDocumentBrowsingApi();

let runtimeConfigSnapshot: RuntimeConfig | null = null;
let runtimeConfigRequest: Promise<RuntimeConfig> | null = null;

/** Served without a session, like /api/health. Runtime config is deployment
 * truth, so all browser consumers share one accepted snapshot instead of
 * independently probing the same public endpoint. A failed load is retryable. */
export function getRuntimeConfig(): Promise<RuntimeConfig> {
  if (runtimeConfigSnapshot !== null) return Promise.resolve(runtimeConfigSnapshot);
  if (runtimeConfigRequest !== null) return runtimeConfigRequest;
  runtimeConfigRequest = instanceRuntimeApi
    .getRuntimeConfig()
    .then((config) => {
      runtimeConfigSnapshot = config;
      return config;
    })
    .finally(() => {
      runtimeConfigRequest = null;
    });
  return runtimeConfigRequest;
}

export async function updateRuntimeConfig(
  cacheMode: RuntimeConfig['cache_mode'],
  confirmed: boolean,
): Promise<RuntimeConfig> {
  const config = await instanceRuntimeApi.updateRuntimeConfig(cacheMode, confirmed);
  runtimeConfigSnapshot = config;
  emitRuntimeConfigChanged(config);
  return config;
}

/** Save the unauthenticated local-installation amount through the existing
 * runtime config surface. Team accounts use updateProfile instead. */
export async function updateLocalCostPreapproval(
  amount: string,
): Promise<RuntimeConfig> {
  const config = await instanceRuntimeApi.updateRuntimeConfig(
    undefined,
    false,
    amount,
  );
  runtimeConfigSnapshot = config;
  emitRuntimeConfigChanged(config);
  return config;
}

/** Unauthenticated — used by AdminPage to tell "no admins configured yet"
 *  apart from "you're logged in but not an admin". */
export const { getHealth } = instanceRuntimeApi;

// Identity and account APIs. Availability is supplied by the active server
// composition; this shared client does not classify a commercial edition.
export const { getMe, updateProfile } = createIdentityProfileApi();
export const { getSpend } = createSpendApi();
export const { listOrgKeys, providerCatalog, validateOrgKey } = organizationProvidersApi;
export const setOrgKey = invalidatesActionCatalog(organizationProvidersApi.setOrgKey);
export const deleteOrgKey = invalidatesActionCatalog(organizationProvidersApi.deleteOrgKey);
export const {
  listOrgEnvVars,
  setOrgEnvVar,
  deleteOrgEnvVar,
  getMediaProxyStatus,
} = organizationOperationsApi;
export const { listApiKeys, createApiKey, revokeApiKey } = createApiTokensApi();

// Local-tier provider config for the model picker (GET /api/providers +
// UI key management + validate probe). The hosted tier manages keys through
// /org/keys and org_secrets.
export const {
  listProviders,
  providerStatus,
  validateProviderKey,
  listModelPulls,
  getModelPull,
  cancelModelPull,
  // Artifact-generic pull accepts qualified local model IDs plus artifact
  // schemes such as opus-mt: and hf:; one DTO/poll/progress path serves every
  // scheme. `unpinnedAcknowledged` gates a free-form unpinned hf: pull and is
  // ignored for pinned + ollama refs. Coded errors survive into
  // ApiError.details (400 invalid_model_ref, 400 unpinned_unacknowledged,
  // 409 pull_busy with `active`, 403 model_pull_disabled).
  startArtifactPull,
  uninstallArtifact,
} = localProvidersApi;
export const setProviderKey = invalidatesActionCatalog(localProvidersApi.setProviderKey);
export const deleteProviderKey = invalidatesActionCatalog(localProvidersApi.deleteProviderKey);
export const createLocalEndpoint = invalidatesActionCatalog(localProvidersApi.createLocalEndpoint);
export const discoverLocalEndpoints = invalidatesActionCatalog(
  localProvidersApi.discoverLocalEndpoints,
);
export const updateLocalEndpoint = invalidatesActionCatalog(localProvidersApi.updateLocalEndpoint);
export const deleteLocalEndpoint = invalidatesActionCatalog(localProvidersApi.deleteLocalEndpoint);

// Org (team-tier) local model catalog + pull. The dedicated generated port
// preserves its typed coded-error detail, including a busy pull's active DTO.
export const {
  listOrgLocalEndpoints,
  orgStartArtifactPull,
  orgListModelPulls,
  orgGetModelPull,
  orgCancelModelPull,
} = teamLocalModelsApi;

export const { getAdminOverview } = createAdminOverviewApi();
export const {
  getAdminHealth,
  getAdminUsers,
  inviteAdminUser,
  updateAdminUserRole,
  removeAdminUser,
  revokeAdminInvite,
  getAdminJobs,
  cancelAdminJob,
  getAdminErrors,
  getAdminAuditLog,
} = createAdminBrowserApi();
export const { createDiagnosticBundle } = createErrorIntakeApi();

export const {
  listProjectMembers,
  setProjectMember,
  removeProjectMember,
  listProjectInvites,
  createProjectInvite,
  revokeProjectInvite,
} = createProjectCollaborationApi();

/** Project-wide search (FTS5; snip carries <b>…</b> highlight markers). */
export function searchProject(
  projectId: string,
  request: import('./projectSearch').ProjectSearchRequestWire,
  options: RequestOptions = {},
): Promise<import('./projectSearch').ProjectSearchResponseWire> {
  return createProjectSearchApi(projectId).searchProject(request, options);
}

type KeysOf<Parts extends readonly object[]> =
  Parts extends readonly [infer Head, ...infer Rest extends readonly object[]]
    ? keyof Head | KeysOf<Rest>
    : never;
type SharedKeys<Parts extends readonly object[]> =
  Parts extends readonly [infer Head, ...infer Rest extends readonly object[]]
    ? (keyof Head & KeysOf<Rest>) | SharedKeys<Rest>
    : never;
type Merged<Parts extends readonly object[]> =
  Parts extends readonly [infer Head, ...infer Rest extends readonly object[]]
    ? Head & Merged<Rest>
    : unknown;

/** Merges domain APIs into one object. Member names must be disjoint: a name
 * two parts share is a compile error naming it (`duplicateMembers`), never a
 * silent override. */
function composeDisjoint<Parts extends readonly object[]>(
  ...parts: Parts & ([SharedKeys<Parts>] extends [never]
    ? unknown
    : { duplicateMembers: SharedKeys<Parts> })
): { [Key in keyof Merged<Parts>]: Merged<Parts>[Key] } {
  return Object.assign({}, ...parts);
}

function promptFromRunSpec(spec: Record<string, unknown>): string {
  const raw = spec.instruction ?? spec.prompt ?? spec.context;
  return typeof raw === 'string' ? raw : '';
}

/** Everything the workspace does against one project. One action session per
 * project API owns browser idempotency state; every domain that posts v1
 * actions shares it, so no domain mints parallel idempotency or run metadata. */
export function createProjectApi(projectId: string) {
  const v1ActionSession = createV1ActionSession(projectId);
  const actionRuns = createActionRunsApi(v1ActionSession, projectId);
  const {
    getColumnRuns: readColumnRuns,
    ...historyReview
  } = createHistoryReviewApi({ v1ActionSession }, projectId);
  const {
    registerStarted: registerStartedPreview,
    ...actionPreview
  } = createActionPreviewApi(projectId);
  const {
    compareOcrScratch: startOcrScratchComparison,
    compareTranscribeScratch: startTranscribeScratchComparison,
    ...scratchComparisons
  } = createPreviewComparisonsApi(projectId);
  const {
    setProjectProviderKey,
    deleteProjectProviderKey,
    ...providerKeyReads
  } = createProjectProviderKeysApi(projectId);
  const {
    setProjectSecret,
    deleteProjectSecret,
    ...secretReads
  } = createProjectSecretsApi(projectId);

  /** Fence a read to the invocation's project and abort signal: an aborted
   * invocation never resolves with a late response. */
  async function fenced<Result>(
    options: ProjectInvocationOptions | undefined,
    read: (projectId: string, signal: AbortSignal | undefined) => Promise<Result>,
  ): Promise<Result> {
    const invocation = resolveRunActionInvocation(options, projectId);
    throwIfActionAborted(invocation.signal);
    const result = await read(invocation.projectId, invocation.signal);
    throwIfActionAborted(invocation.signal);
    return result;
  }

  return composeDisjoint(
    createWatchesApi(projectId),
    createNotificationsApi(projectId),
    createViewsLensesApi(projectId),
    createColumnTypesApi(projectId),
    createProjectDataManagementApi(projectId),
    createProjectSourcesApi({ v1ActionSession }, projectId),
    createSheetGridApi({
      columnRunInfo: (id, sheetId, column) => actionRuns.columnRunInfo(id, sheetId, column),
      v1ActionSession,
    }, projectId),
    historyReview,
    createActionHistoryApi(projectId),
    createGoogleSheetsExportApi({ v1ActionSession }, projectId),
    scratchComparisons,
    createProjectEvidenceApi(projectId),
    createRuntimeProjectionsApi(projectId),
    // Arrow is a browser binary protocol, not a JSON contract: keep its error
    // identity on the public API while the raw owner keeps route and decoder
    // details.
    createMapPointsArrowApi((status, message) => new ApiError(status, message), projectId),
    createActionEstimateValidationApi(projectId),
    actionPreview,
    createEmbeddingsApi({ v1ActionSession }, projectId),
    createWorkbenchPluginsApi(projectId),
    createResolvePreviewsApi(projectId),
    createEntityMentionsApi(projectId),
    createGraphLineageApi(projectId),
    createRunProvenanceApi(projectId),
    createProjectAttemptsApi(projectId),
    createTranslateComparisonApi(projectId),
    createMcpServersApi(projectId),
    createOAuthConnectionsApi(),
    providerKeyReads,
    secretReads,
    {
      qa: createProjectQAApi(() => projectId),

      runAction: actionRuns.runAction.bind(actionRuns),
      runProposal: actionRuns.runProposal.bind(actionRuns),
      getRunProgress: actionRuns.getRunProgress.bind(actionRuns),
      cancelRun: actionRuns.cancelRun.bind(actionRuns),
      backfillColumn: actionRuns.backfillColumn.bind(actionRuns),

      setProjectProviderKey: invalidatesActionCatalog(setProjectProviderKey),
      deleteProjectProviderKey: invalidatesActionCatalog(deleteProjectProviderKey),
      setProjectSecret: invalidatesActionCatalog(setProjectSecret),
      deleteProjectSecret: invalidatesActionCatalog(deleteProjectSecret),

      /** Canonical action catalog. A scoped API defaults to its own project. */
      listActionCatalog(explicitProjectId?: string | null) {
        return actionCatalogCache.load(explicitProjectId ?? (projectId || null));
      },

      async getProject(): Promise<ProjectInfo> {
        return getProjectContract(projectId);
      },

      async updateCurrentProject(input: {
        name?: string;
        description?: string | null;
      }): Promise<ProjectInfo> {
        return updateProject(projectId, input);
      },

      async deleteProject(target: string | undefined, confirmName: string): Promise<void> {
        await deleteProject(target ?? projectId, confirmName);
      },

      /** Project-scoped workbench view of completed plugin.load manifest receipts. */
      async getWorkbenchPluginRuntimeIndex(): Promise<WorkbenchPluginRuntimeIndex> {
        return getWorkbenchPluginRuntimeIndexContract(projectId);
      },

      async listActionJobs(
        status?: string | null,
        limit = 50,
        options?: ProjectInvocationOptions,
      ): Promise<ActionJobsPage> {
        return fenced(options, (pid, signal) => (
          listActionJobsContract(pid, { status, limit }, { signal })
        ));
      },

      async getActionJob(
        jobId: number | string,
        options?: ProjectInvocationOptions,
      ): Promise<ActionJob> {
        return fenced(options, (pid, signal) => (
          getActionJobContract(pid, Number(jobId), { signal })
        ));
      },

      /** Canonical v1 receipt lookup shared by provenance, runs, results, and exports. */
      async getReceipt(
        receiptId: string,
        options?: ProjectInvocationOptions,
      ): Promise<V1Receipt> {
        return fenced(options, (pid, signal) => (
          getReceiptContract(pid, receiptId, { signal })
        ));
      },

      async getRunRows(
        runId: string,
        offset = 0,
        limit = 20,
        status?: 'error',
      ): Promise<RunRowsPage> {
        return getRunRowsContract(
          projectId,
          Number(runId),
          { offset, limit, ...(status ? { status } : {}) },
        );
      },

      /** Provenance for an AI column: a bounded newest-first run history page.
       *  The selected run's spec is remembered for the column drawer. */
      async getColumnRuns(columnId: string, offset = 0, limit = 20): Promise<ColumnRunsInfo> {
        const result = await readColumnRuns(columnId, offset, limit);
        const selectedRun = result.latestRun
          ?? result.currentRun
          ?? result.runs.find((run) => run.current)
          ?? result.runs[0];
        if (selectedRun) {
          actionRuns.rememberColumnRunInfo(projectId, columnId, {
            runId: selectedRun.runId,
            actionName: selectedRun.actionName,
            prompt: promptFromRunSpec(selectedRun.spec),
            model: selectedRun.model,
          });
        }
        return result;
      },

      async compareOcrScratch(
        file: File,
        input: OcrCompareScratchInput,
      ): Promise<PreviewStartResult> {
        return registerStartedPreview(await startOcrScratchComparison(file, input));
      },

      async compareTranscribeScratch(
        file: File,
        input: TranscribeCompareScratchInput,
      ): Promise<PreviewStartResult> {
        return registerStartedPreview(await startTranscribeScratchComparison(file, input));
      },

      projectExportUrl(includeMediaOrOptions?: Parameters<typeof projectExportUrl>[1]): string {
        return projectExportUrl(projectId, includeMediaOrOptions);
      },

      sheetDatasetExportUrl(options: Parameters<typeof sheetDatasetExportUrl>[1]): string {
        return sheetDatasetExportUrl(projectId, options);
      },

      workLogExportUrl(format?: Parameters<typeof workLogExportUrl>[1]): string {
        return workLogExportUrl(projectId, format);
      },

      /** Browser download URL for a previously-exported artifact of the given format. */
      embeddingExportArtifactUrl(indexId: string, format: string): string {
        return embeddingExportArtifactUrl(projectId, indexId, format);
      },

      googleOAuthStartUrl,
    },
  );
}
