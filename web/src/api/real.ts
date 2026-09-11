// Open HTTP adapter implementing FrisketApi against the real frisket FastAPI
// server (src/frisket/server/app.py), reached through the vite dev proxy
// (/api → the local Frisket server; :7331 by default).
//
// Interface mismatches handled here:
// - listSheets(): the backend splits sheet list and columns; we join them
//   (GET /sheets + GET /sheets/{id}/data?limit=0 per sheet).
// - getRunProgress(): GET /actions/runs/{id}/status, supplemented with run
//   context remembered from the runAction() call that started it.
// - stepTo(globalIndex): the backend has single-step operation controls, so the
//   history/review owner walks the cursor with operation.undo/operation.redo v1
//   actions; reviewItem() posts v1 review.decision beside the bundles reads.

import {
  cancelRunContract,
  copilotChatContract,
  createProjectContract,
  deleteProjectContract,
  getActionJobContract,
  getGlobalActionCatalog,
  getProjectActionCatalog,
  getProjectContract,
  getReceiptContract,
  getRunProgressContract,
  getRunRowsContract,
  getWorkbenchPluginRuntimeIndexContract,
  listActionJobsContract,
  listProjectsContract,
  updateProjectContract,
} from './httpContractRoutes';
import { ActionCatalogCache } from './actionCatalog';
import { emitActionCatalogInvalidated } from './catalogEvents';
import { emitRuntimeConfigChanged } from './runtimeConfigEvents';
import {
  createV1ActionSession,
  resolveRunActionInvocation,
  throwIfActionAborted,
} from './v1ActionSession';
import { createActionCompletionApi } from './actionCompletion';
import { createActionRunsApi } from './actionRuns';
import { createGoogleSheetsExportApi } from './googleSheetsExport';
import { createSheetGridDomainApi } from './sheetGrid';
export {
  V1ActionIdempotencyKeys,
  v1ActionIdempotencySignature,
} from './v1ActionSession';
import {
  googleOAuthStartUrl as browserGoogleOAuthStartUrl,
} from './raw/browserAuth';
import { signOut as browserSignOut } from './browserAuth';
import { createMapPointsArrowApi } from './raw/mapPointsArrow';
import {
  embeddingExportArtifactUrl as projectEmbeddingExportArtifactUrl,
  projectExportUrl as projectResourceExportUrl,
  sheetDatasetExportUrl as projectSheetDatasetExportUrl,
  workLogExportUrl as projectWorkLogExportUrl,
} from './raw/projectResources';
import {
  createActionEstimateValidationDomainApi,
} from './actionEstimateValidation';
import {
  createActionPreviewDomainApi,
} from './actionPreviewRuns';
import { createHistoryReviewDomainApi } from './historyReview';
import { createProjectEvidenceDomainApi } from './projectEvidence';
import { createInstanceRuntimeApi } from './instanceRuntime';
import { createAdminOverviewApi } from './adminOverview';
import {
  createAdminBrowserApi,
  type AdminInviteResult,
  type AdminRemoveUserResult,
  type AdminRevokeInviteResult,
  type AdminRoleUpdateResult,
} from './adminBrowser';
import { createIdentityProfileApi } from './identityProfile';
import {
  createErrorIntakeApi,
  type DiagnosticBundleRequest,
  type DiagnosticBundleResponse,
} from './errorIntake';
import { createEmbeddingsApi } from './embeddings';
import { createWorkbenchPluginsApi } from './workbenchPlugins';
import { createProjectCollaborationApi } from './projectCollaboration';
import { createColumnTypesDomainApi } from './columnTypes';
import { createTranslateComparisonApi } from './translateComparison';
import { createProjectSearchApi } from './projectSearch';
import { createProjectDataManagementApi } from './projectDataManagement';
import { createProjectProviderKeysApi } from './projectProviderKeys';
import { createProjectSecretsApi } from './projectSecrets';
import { createMcpServersApi } from './mcpServers';
import { createResolvePreviewsApi } from './resolvePreviews';
import { createRunProvenanceApi } from './runProvenance';
import { createEntityMentionsApi } from './entityMentions';
import { createGraphLineageApi } from './graphLineage';
import { createRuntimeProjectionsApi } from './runtimeProjections';
import type {
  ImportDraft,
  ImportDraftMapping,
  ImportResult,
} from './onboardingImports';
import { submitImportRowsDraft } from './onboardingImports';
import { createPreviewComparisonsApi } from './previewComparisons';
import { createOAuthConnectionsApi } from './oauthConnections';
import { createSpendApi } from './spend';
import {
  ApiError,
  apiErrorFromContract,
  workbenchPluginErrorFromContract,
  viewLensApiErrorFromContract,
  actionEstimateValidationErrorFromContract,
  translateComparisonErrorFromContract,
  resolvePreviewErrorFromContract,
  projectEvidenceErrorFromContract,
  actionPreviewRunErrorFromContract,
  embeddingErrorFromContract,
  runtimeProjectionErrorFromContract,
} from './contractErrors';
export {
  ApiError,
  ConfirmationRequiredError,
  rowsEstimateFromDetail,
} from './contractErrors';
export { runEstimateFromV1Wire } from './actionEstimateValidation';

export type { DiagnosticBundleRequest, DiagnosticBundleResponse } from './errorIntake';
import { createApiTokensApi } from './apiTokens';
import {
  createLocalProvidersApi,
  type LocalProvidersOptions,
} from './localProviders';
import { createOrganizationProvidersApi } from './organizationProviders';
import { createOrganizationOperationsApi } from './organizationOperations';
import { createProjectAttemptsApi } from './projectAttempts';
import {
  createProjectSourcesApi,
} from './projectSources';
import { createTeamLocalModelsApi } from './teamLocalModels';
import {
  createViewsLensesDomainApi,
} from './viewsLenses';
import {
  createWatchesDomainApi,
} from './watches';
import {
  createNotificationsDomainApi,
} from './notifications';
import type {
  AdminHealth,
  AdminJobActionResult,
  AdminJobs,
  AdminOrgRole,
  AdminOverview,
  AdminUsers,
  AdminErrors,
  HealthStatus,
  ApiTokenCreateResponse,
  ApiTokenInfo,
  MeInfo,
  OrgEnvInfo,
  OrgKeyInfo,
  ProjectNetworkPolicy,
  ProjectProviderKeys,
  ProjectRetentionPolicy,
  ProjectSettings,
  ProjectSecrets,
  ProviderCatalog,
  LocalProviderCatalog,
  ModelPullDto,
  LocalEndpointCatalog,
  ProviderValidateResult,
  ProjectInfo,
  ProjectInvite,
  ProjectInviteResponse,
  ProjectInviteRole,
  ProjectMember,
  ProjectMemberChange,
  ProjectRole,
  RemovalResult,
  RuntimeConfig,
  SearchHit,
  SearchOptions,
  SpendReport,
} from './types';
import type {
  ActionCatalogPayload,
  ActionJob,
  ActionJobsPage,
  BackfillResult,
  AdminAuditFilters,
  AdminAuditLog,
  CellEvidencePayload,
  ColumnEvidenceBatchPayload,
  CellEdit,
  AddColumnResult,
  CellValue,
  CreateEmbeddingIndexInput,
  DeleteRowsResult,
  DeleteSheetResult,
  EmbeddingExportResult,
  EmbeddingIndexAnalysisInput,
  EmbeddingIndexAnalysisResult,
  EmbeddingIndexSummary,
  EvidenceViewerPayload,
  UpdateEmbeddingIndexPolicyInput,
  EmbeddingProvider,
  EmbeddingComposedQuery,
  EmbeddingSimilarityResult,
  Lens,
  LensSaveInput,
  LensResolved,
  ColumnDef,
  ColumnPatch,
  ColumnRunsInfo,
  TextAnnotations,
  AttemptReceiptsPage,
  ColumnStats,
  ColumnTypeInfo,
  FrisketApi,
  GoogleSheetsExportInput,
  GoogleSheetsExportResult,
  SheetGraphOptions,
  SheetGraphResult,
  HistoryState,
  NotificationActorState,
  NotificationChannel,
  NotificationChannelInput,
  NotificationChannelsPage,
  NotificationDeliveryRequest,
  NotificationDeliveryRequestsPage,
  NotificationListParams,
  NotificationPage,
  NotificationRoute,
  NotificationRouteInput,
  NotificationRoutesPage,
  NotificationStateFilter,
  NotificationSummary,
  OcrCompareScratchInput,
  TranscribeCompareScratchInput,
  TopicSegmentationCompareScratchInput,
  TopicSegmentationCompareScratchResult,
  TranslateCompareScratchInput,
  TranslateCompareScratchResult,
  OAuthConnectionInfo,
  ProjectInvocationOptions,
  ProjectExportOptions,
  ProvenanceManifest,
  ReviewAction,
  ReviewBundlePage,
  MediaProxyStatus,
  ActionExecutionRequest,
  RunActionLaunchResult,
  RunActionInvocationOptions,
  RunProgress,
  RunRowsPage,
  PreviewStartResult,
  PreviewSampleResult,
  ActionParamResolution,
  RegisteredActionRequest,
  RunEstimate,
  RunTraceRowEvidence,
  SavedView,
  SavedViewCreateInput,
  SavedViewDefinitionReplaceInput,
  SavedViewRenameInput,
  SheetDatasetExportOptions,
  MapPointsOptions,
  MapPointsResult,
  RuntimeProjectionArtifactRequest,
  RuntimeProjectionBuildPlan,
  RuntimeProjectionBuildRequest,
  RuntimeProjectionStatus,
  RuntimeProjectionStatusRequest,
  TimelineProjectionArtifact,
  SheetDataOptions,
  SheetDataPage,
  SheetRowLocation,
  SheetMeta,
  SourceInput,
  LineageDag,
  SheetRefreshResult,
  V1Receipt,
  WatchInfo,
  WatchInput,
  WatchPatchInput,
  WatchRunEventsPage,
  WatchRunResult,
  WatchRunsPage,
  WorkbenchPluginActivation,
  WorkbenchPluginActivationRequest,
  WorkbenchPluginBackendActivation,
  WorkbenchPluginBackendActivationRequest,
  WorkbenchPluginSettings,
  WorkbenchPluginInstallStateChange,
  WorkbenchPluginLocalInstallExecution,
  WorkbenchPluginLocalInstallRequest,
  WorkbenchPluginRuntimeIndex,
  CopilotChatMessageInput,
  CopilotProposal,
  CopilotReply,
} from './types';

// Arrow is a browser binary protocol, not a JSON contract. Keep its error
// identity on the public API while the named raw owner retains route/metadata
// and decoder details.
const localProvidersApi = createLocalProvidersApi(apiErrorFromContract);
const translateComparisonApi = createTranslateComparisonApi(translateComparisonErrorFromContract);
const instanceRuntimeApi = createInstanceRuntimeApi(apiErrorFromContract);
const adminOverviewApi = createAdminOverviewApi(apiErrorFromContract);
const adminBrowserApi = createAdminBrowserApi(apiErrorFromContract);
const identityProfileApi = createIdentityProfileApi(apiErrorFromContract);
const errorIntakeApi = createErrorIntakeApi(apiErrorFromContract);
const workbenchPluginsApi = createWorkbenchPluginsApi(workbenchPluginErrorFromContract);
const projectCollaborationApi = createProjectCollaborationApi(apiErrorFromContract);
const apiTokensApi = createApiTokensApi(apiErrorFromContract);
const organizationProvidersApi = createOrganizationProvidersApi(apiErrorFromContract);
const organizationOperationsApi = createOrganizationOperationsApi(apiErrorFromContract);
const projectAttemptsApi = createProjectAttemptsApi(apiErrorFromContract);
const teamLocalModelsApi = createTeamLocalModelsApi(apiErrorFromContract);
const oauthConnectionsApi = createOAuthConnectionsApi(apiErrorFromContract);
const spendApi = createSpendApi(apiErrorFromContract);
// Watch lifecycle failures remain plain-string details, while Saved View
// source refusals use the shared typed {code, message} detail. The shared
// factory preserves both shapes, so no Watch-specific factory is needed.
// Notification errors are plain string details end to end (every
// NotificationRequestError detail is typed str, including the frozen raw
// Python int() leak), so the shared factory reproduces the untyped
// transport's ApiError exactly: status + message, code and details both
// undefined. No scoped factory is needed.
const resolvePreviewsApi = createResolvePreviewsApi(resolvePreviewErrorFromContract);
const runProvenanceApi = createRunProvenanceApi(apiErrorFromContract);
const entityMentionsApi = createEntityMentionsApi(resolvePreviewErrorFromContract);
const graphLineageApi = createGraphLineageApi(apiErrorFromContract);
const projectProviderKeysApi = createProjectProviderKeysApi(apiErrorFromContract);
const projectSecretsApi = createProjectSecretsApi(apiErrorFromContract);
const mcpServersApi = createMcpServersApi();

export function listProjects(): Promise<ProjectInfo[]> {
  return realApi.listProjects();
}

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

export { onRuntimeConfigChanged } from './runtimeConfigEvents';

export function getMediaProxyStatus(): Promise<MediaProxyStatus> {
  return organizationOperationsApi.getMediaProxyStatus();
}

// Identity and account APIs. Availability is supplied by the active server
// composition; this shared client does not classify a commercial edition.
export function getMe(): Promise<MeInfo> {
  return identityProfileApi.getMe();
}

export function updateProfile(input: {
  display_name?: string | null;
  cost_preapproval_usd?: string | null;
}): Promise<MeInfo> {
  return identityProfileApi.updateProfile(input);
}

export function getSpend(): Promise<SpendReport> {
  return spendApi.getSpend();
}

export function listOrgKeys(): Promise<OrgKeyInfo[]> {
  return organizationProvidersApi.listOrgKeys();
}

export function providerCatalog(): Promise<ProviderCatalog> {
  return organizationProvidersApi.providerCatalog();
}

// Local-tier provider config for the model picker (GET /api/providers +
// UI key management + validate probe). Real-backend only — the hosted tier
// manages keys through /org/keys and org_secrets.
export const listProviders = localProvidersApi.listProviders;
export const providerStatus = localProvidersApi.providerStatus;
export async function setProviderKey(
  provider: string,
  key: string,
  validationToken?: string | null,
  options?: LocalProvidersOptions,
): Promise<LocalProviderCatalog> {
  const result = await localProvidersApi.setProviderKey(
    provider,
    key,
    validationToken,
    options,
  );
  invalidateActionCatalog();
  return result;
}

export async function deleteProviderKey(
  provider: string,
  options?: LocalProvidersOptions,
): Promise<LocalProviderCatalog> {
  const result = await localProvidersApi.deleteProviderKey(provider, options);
  invalidateActionCatalog();
  return result;
}
export async function createLocalEndpoint(
  ...args: Parameters<typeof localProvidersApi.createLocalEndpoint>
): ReturnType<typeof localProvidersApi.createLocalEndpoint> {
  const result = await localProvidersApi.createLocalEndpoint(...args);
  invalidateActionCatalog();
  return result;
}

export async function discoverLocalEndpoints(
  ...args: Parameters<typeof localProvidersApi.discoverLocalEndpoints>
): ReturnType<typeof localProvidersApi.discoverLocalEndpoints> {
  const result = await localProvidersApi.discoverLocalEndpoints(...args);
  invalidateActionCatalog();
  return result;
}

export async function updateLocalEndpoint(
  ...args: Parameters<typeof localProvidersApi.updateLocalEndpoint>
): ReturnType<typeof localProvidersApi.updateLocalEndpoint> {
  const result = await localProvidersApi.updateLocalEndpoint(...args);
  invalidateActionCatalog();
  return result;
}

export async function deleteLocalEndpoint(
  ...args: Parameters<typeof localProvidersApi.deleteLocalEndpoint>
): ReturnType<typeof localProvidersApi.deleteLocalEndpoint> {
  const result = await localProvidersApi.deleteLocalEndpoint(...args);
  invalidateActionCatalog();
  return result;
}

export const listModelPulls = localProvidersApi.listModelPulls;
export const getModelPull = localProvidersApi.getModelPull;
export const cancelModelPull = localProvidersApi.cancelModelPull;

// Artifact-generic pull accepts qualified local model IDs plus artifact
// schemes such as opus-mt: and hf:. The
// The same DTO/poll/progress path serves every artifact scheme.
// `unpinnedAcknowledged` gates a free-form unpinned hf: pull and is ignored
// for pinned + ollama refs. Coded errors survive into ApiError.details (400
// invalid_model_ref, 400 unpinned_unacknowledged, 409 pull_busy with
// `active`, 403 model_pull_disabled).
export const startArtifactPull = localProvidersApi.startArtifactPull;
export const uninstallArtifact = localProvidersApi.uninstallArtifact;

/** Clear the cached action catalog and signal mounted surfaces to refetch —
 *  called after an inline pair install/uninstall so the translate form's
 *  engine `models` reflects the new install without a remount. */
export function invalidateActionCatalog(): void {
  realApi.invalidateActionCatalog();
}

// ---------------------------------------------------------------------------
// Org (team-tier) local model catalog + pull. The dedicated generated port
// preserves its typed coded-error detail, including a busy pull's active DTO.

export function listOrgLocalEndpoints(): Promise<LocalEndpointCatalog> {
  return teamLocalModelsApi.listOrgLocalEndpoints();
}

export const orgStartArtifactPull = teamLocalModelsApi.orgStartArtifactPull;

export function orgListModelPulls(): Promise<{ pulls: ModelPullDto[] }> {
  return teamLocalModelsApi.orgListModelPulls();
}

export function orgGetModelPull(id: number): Promise<ModelPullDto> {
  return teamLocalModelsApi.orgGetModelPull(id);
}

export function orgCancelModelPull(id: number): Promise<ModelPullDto> {
  return teamLocalModelsApi.orgCancelModelPull(id);
}

export const validateProviderKey = localProvidersApi.validateProviderKey;

// No spendCapUsd: POST /api/org/keys stores provider/encrypted/hint and has
// never persisted a cap (the control plane's org_keys table has no such
// column), so the field was sent and silently discarded. Project provider
// keys are where a real, enforced cap lives.
export async function setOrgKey(
  provider: string,
  key: string,
  validationToken?: string | null,
): Promise<void> {
  await organizationProvidersApi.setOrgKey(provider, key, validationToken);
  invalidateActionCatalog();
}

export function validateOrgKey(
  provider: string,
  key?: string,
): Promise<ProviderValidateResult> {
  return organizationProvidersApi.validateOrgKey(provider, key);
}

export async function deleteOrgKey(provider: string): Promise<void> {
  await organizationProvidersApi.deleteOrgKey(provider);
  invalidateActionCatalog();
}

export function listOrgEnvVars(): Promise<OrgEnvInfo[]> {
  return organizationOperationsApi.listOrgEnvVars();
}

export async function setOrgEnvVar(
  name: string,
  value: string,
): Promise<void> {
  await organizationOperationsApi.setOrgEnvVar(name, value);
}

export async function deleteOrgEnvVar(name: string): Promise<void> {
  await organizationOperationsApi.deleteOrgEnvVar(name);
}

export function listApiKeys(): Promise<ApiTokenInfo[]> {
  return apiTokensApi.listApiKeys();
}

export function createApiKey(name: string): Promise<ApiTokenCreateResponse> {
  return apiTokensApi.createApiKey(name);
}

export function revokeApiKey(tokenId: number | string): Promise<{
  ok: boolean;
  revoked: boolean;
}> {
  return apiTokensApi.revokeApiKey(tokenId);
}

/** /auth lives outside the /api prefix; ignore the response body. */
export async function signOut(): Promise<void> {
  await browserSignOut();
}



export function getAdminOverview(): Promise<AdminOverview> {
  return adminOverviewApi.getAdminOverview();
}

export function getAdminHealth(): Promise<AdminHealth> {
  return adminBrowserApi.getAdminHealth();
}

/** Unauthenticated — used by AdminPage to tell "no admins configured yet"
 *  apart from "you're logged in but not an admin" (onboard-selfhost-hardening-v1). */
export function getHealth(): Promise<HealthStatus> {
  return instanceRuntimeApi.getHealth();
}






export function getAdminUsers(): Promise<AdminUsers> {
  return adminBrowserApi.getAdminUsers();
}

export function inviteAdminUser(
  orgId: number,
  email: string,
): Promise<AdminInviteResult> {
  return adminBrowserApi.inviteAdminUser(orgId, email);
}

export function updateAdminUserRole(
  orgId: number,
  userId: number,
  role: AdminOrgRole,
): Promise<AdminRoleUpdateResult> {
  return adminBrowserApi.updateAdminUserRole(orgId, userId, role);
}

export function removeAdminUser(
  orgId: number,
  userId: number,
): Promise<AdminRemoveUserResult> {
  return adminBrowserApi.removeAdminUser(orgId, userId);
}

export function revokeAdminInvite(
  orgId: number,
  email: string,
): Promise<AdminRevokeInviteResult> {
  return adminBrowserApi.revokeAdminInvite(orgId, email);
}



export function getAdminJobs(): Promise<AdminJobs> {
  return adminBrowserApi.getAdminJobs();
}

export function cancelAdminJob(jobId: number): Promise<AdminJobActionResult> {
  return adminBrowserApi.cancelAdminJob(jobId);
}


export function getAdminErrors(limit = 50): Promise<AdminErrors> {
  return adminBrowserApi.getAdminErrors(limit);
}

export function getAdminAuditLog(filters: AdminAuditFilters = {}): Promise<AdminAuditLog> {
  return adminBrowserApi.getAdminAuditLog(filters);
}

export function createDiagnosticBundle(
  body: DiagnosticBundleRequest,
): Promise<DiagnosticBundleResponse> {
  return errorIntakeApi.createDiagnosticBundle(body);
}

// ---------------------------------------------------------------------------
// Project-wide search (FTS5; snip carries <b>…</b> highlight markers)



export function searchProject(
  projectId: string,
  q: string,
  options: SearchOptions | number = {},
): Promise<SearchHit[]> {
  return createProjectSearchApi(apiErrorFromContract, projectId).searchProject(q, options);
}

export function createProject(name: string): Promise<ProjectInfo> {
  return realApi.createProject(name);
}

/** Patch a project's shell metadata: the Home screen's Star/Archive flags and
 *  rename ride the same additive PATCH /api/projects/{id} endpoint. Archive
 *  sets a manifest flag; it never deletes the bundle. */
export function updateProject(
  projectId: string,
  patch: { name?: string; description?: string; starred?: boolean; archived?: boolean },
): Promise<ProjectInfo> {
  return realApi.updateProject(projectId, patch);
}

/** Standalone (project-implicit-free) delete, for callers that only have a
 * bare project id — e.g. the sample-project picker button cleaning up a
 * just-created project whose seed failed, so a failed attempt doesn't leave
 * an orphaned empty project behind. The caller supplies the project's name as
 * the server-side confirmation; this path only ever targets a project this
 * same click created seconds earlier. */
export function deleteProject(projectId: string, confirmName: string): Promise<void> {
  return realApi.deleteProject(projectId, confirmName);
}

export function listProjectMembers(projectId: string): Promise<ProjectMember[]> {
  return projectCollaborationApi.listMembers(projectId);
}

export function setProjectMember(
  projectId: string,
  email: string,
  role: ProjectRole,
): Promise<ProjectMemberChange> {
  return projectCollaborationApi.setMember(projectId, email, role);
}

export function removeProjectMember(projectId: string, email: string): Promise<RemovalResult> {
  return projectCollaborationApi.removeMember(projectId, email);
}

export async function listProjectInvites(projectId: string): Promise<ProjectInvite[]> {
  return projectCollaborationApi.listInvites(projectId);
}

export function createProjectInvite(
  projectId: string,
  email: string,
  role: ProjectInviteRole,
): Promise<ProjectInviteResponse> {
  return projectCollaborationApi.createInvite(projectId, email, role);
}

export function revokeProjectInvite(
  projectId: string,
  inviteId: number | string,
): Promise<{ ok: boolean; revoked: boolean }> {
  return projectCollaborationApi.revokeInvite(projectId, inviteId);
}

// Copilot: chat → runnable proposals, plus the needsImport decision. Types +
// wire mapping live with the generated-contract adapter (httpContractRoutes);
// the request flows through the declared POST /api/projects/{pid}/copilot
// contract, never raw transport.
export type { CopilotProposal, CopilotReply };

export async function confirmImportRowsDraft(
  projectId: string,
  draft: ImportDraft,
  mapping: ImportDraftMapping,
): Promise<ImportResult> {
  return new RealApi(projectId).confirmImportRowsDraft(draft, mapping);
}

// ---------------------------------------------------------------------------
// The adapter

function actionDisplayNameFromActionKind(actionKind: string): string {
  const normalized = actionKind.replace(/[._-]+/g, ' ').trim();
  if (!normalized) return 'AI run';
  return normalized
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function promptFromRunSpec(spec: Record<string, unknown>): string {
  const raw = spec.instruction ?? spec.prompt ?? spec.context;
  return typeof raw === 'string' ? raw : '';
}

const actionCatalogCache = new ActionCatalogCache({
  loadGlobal: () => getGlobalActionCatalog(apiErrorFromContract),
  loadProject: (projectId) =>
    getProjectActionCatalog(projectId, apiErrorFromContract),
  emitInvalidated: emitActionCatalogInvalidated,
});

class RealApi implements FrisketApi {
  private readonly projectId: string | null;
  private readonly mapPointsArrowApi: ReturnType<typeof createMapPointsArrowApi>;
  private readonly previewComparisonsApi: ReturnType<typeof createPreviewComparisonsApi>;
  private readonly projectEvidenceApi: ReturnType<typeof createProjectEvidenceDomainApi>;
  private readonly columnTypesApi: ReturnType<typeof createColumnTypesDomainApi>;
  private readonly viewsLensesApi: ReturnType<typeof createViewsLensesDomainApi>;
  private readonly watchesApi: ReturnType<typeof createWatchesDomainApi>;
  private readonly notificationsApi: ReturnType<typeof createNotificationsDomainApi>;
  private readonly runtimeProjectionsApi: ReturnType<typeof createRuntimeProjectionsApi>;
  private readonly projectDataManagementApi: ReturnType<typeof createProjectDataManagementApi>;
  private readonly actionCatalog = actionCatalogCache;
  /**
   * One session per adapter owns browser idempotency state.
   *
   * Catalog promises remain signal-free and owned by ActionCatalogCache; the
   * session observes them through each invocation's signal. The action-runs
   * owner receives this same session; it never creates parallel idempotency
   * or run-metadata state.
   */
  private readonly v1ActionSession: ReturnType<typeof createV1ActionSession>;
  private readonly actionEstimateValidation: ReturnType<typeof createActionEstimateValidationDomainApi>;
  // Preview owns its own translation, lifecycle binding, and wire mapping;
  // this adapter remains the sole owner of the shared browser action session.
  private readonly actionPreviewRuns: ReturnType<typeof createActionPreviewDomainApi>;
  private readonly embeddings: ReturnType<typeof createEmbeddingsApi>;
  // Source reads, mappings, and mutations share this adapter-owned session.
  // The extracted owner does not create a second idempotency-key registry.
  // This keeps all V1 action lifetimes scoped to one RealApi instance.
  private readonly projectSources: ReturnType<typeof createProjectSourcesApi>;
  private readonly actionCompletion = createActionCompletionApi(apiErrorFromContract);
  private readonly googleSheetsExport: ReturnType<typeof createGoogleSheetsExportApi>;
  private readonly actionRuns: ReturnType<typeof createActionRunsApi>;
  private readonly sheetGrid: ReturnType<typeof createSheetGridDomainApi>;
  // History and review own their reads plus the cursor and queue mutations,
  // including the invocation each one captures. This adapter keeps the shared
  // session and the column-run metadata it feeds back into action runs.
  private readonly historyReview: ReturnType<typeof createHistoryReviewDomainApi>;

  constructor(projectId: string | null = null) {
    this.projectId = projectId;
    const scopedProjectId = projectId ?? '';
    this.mapPointsArrowApi = createMapPointsArrowApi(
      (status, message) => new ApiError(status, message),
      scopedProjectId,
    );
    this.previewComparisonsApi = createPreviewComparisonsApi(
      apiErrorFromContract,
      scopedProjectId,
    );
    this.projectEvidenceApi = createProjectEvidenceDomainApi(
      projectEvidenceErrorFromContract,
      scopedProjectId,
    );
    this.columnTypesApi = createColumnTypesDomainApi(apiErrorFromContract, projectId);
    this.viewsLensesApi = createViewsLensesDomainApi(
      viewLensApiErrorFromContract,
      scopedProjectId,
    );
    this.watchesApi = createWatchesDomainApi(apiErrorFromContract, scopedProjectId);
    this.notificationsApi = createNotificationsDomainApi(
      apiErrorFromContract,
      scopedProjectId,
    );
    this.runtimeProjectionsApi = createRuntimeProjectionsApi(
      runtimeProjectionErrorFromContract,
      scopedProjectId,
    );
    this.projectDataManagementApi = createProjectDataManagementApi(
      apiErrorFromContract,
      scopedProjectId,
    );
    this.v1ActionSession = createV1ActionSession(scopedProjectId);
    this.actionRuns = createActionRunsApi({
      v1ActionSession: this.v1ActionSession,
      runAction: (req, options) => this.runAction(req, options),
      getRunProgress: (id, runId, options) =>
        getRunProgressContract(id, runId, apiErrorFromContract, options),
      cancelRun: (id, runId, options) =>
        cancelRunContract(id, runId, apiErrorFromContract, options),
      waitForV1ActionCompletion: (result, fallbackMessage, invocation) =>
        this.actionCompletion.waitForRunCompletion(result, fallbackMessage, invocation),
      actionDisplayNameFromActionKind,
    }, scopedProjectId);
    this.actionEstimateValidation = createActionEstimateValidationDomainApi({
      errorFactory: actionEstimateValidationErrorFromContract,
      projectId: scopedProjectId,
    });
    this.actionPreviewRuns = createActionPreviewDomainApi({
      errorFactory: actionPreviewRunErrorFromContract,
    }, scopedProjectId);
    this.embeddings = createEmbeddingsApi(embeddingErrorFromContract, {
      v1ActionSession: this.v1ActionSession,
    });
    this.projectSources = createProjectSourcesApi({
      errorFactory: apiErrorFromContract,
      v1ActionSession: this.v1ActionSession,
    }, scopedProjectId);
    this.googleSheetsExport = createGoogleSheetsExportApi(apiErrorFromContract, {
      v1ActionSession: this.v1ActionSession,
    }, scopedProjectId);
    this.sheetGrid = createSheetGridDomainApi(apiErrorFromContract, {
      columnRunInfo: (id, sheetId, column) => this.actionRuns.columnRunInfo(id, sheetId, column),
      v1ActionSession: this.v1ActionSession,
    }, scopedProjectId);
    this.historyReview = createHistoryReviewDomainApi(apiErrorFromContract, {
      v1ActionSession: this.v1ActionSession,
    }, scopedProjectId);
  }

  private requireProjectId(): string {
    if (!this.projectId) throw new ApiError(400, 'Project id is required');
    return this.projectId;
  }

  // ---- data ---------------------------------------------------------------

  confirmImportRowsDraft(
    draft: ImportDraft,
    mapping: ImportDraftMapping,
  ): Promise<ImportResult> {
    return submitImportRowsDraft(this.requireProjectId(), draft, mapping);
  }

  listActionCatalog(explicitProjectId?: string | null): Promise<ActionCatalogPayload> {
    const projectId =
      explicitProjectId ??
      (() => {
        try {
          return this.requireProjectId();
        } catch {
          return null;
        }
      })();
    return this.actionCatalog.load(projectId);
  }

  async embeddingProviderCatalog(opts: {
    modality?: string;
    sourceColumnType?: string;
  }): Promise<EmbeddingProvider[]> {
    return this.embeddings.embeddingProviderCatalog(this.requireProjectId(), opts);
  }

  async embeddingIndexes(sheetId: number): Promise<EmbeddingIndexSummary[]> {
    return this.embeddings.embeddingIndexes(this.requireProjectId(), sheetId);
  }

  async updateEmbeddingIndexPolicy(
    input: UpdateEmbeddingIndexPolicyInput,
  ): Promise<void> {
    const projectId = this.requireProjectId();
    return this.embeddings.updateEmbeddingIndexPolicy(projectId, input);
  }

  async createEmbeddingIndex(input: CreateEmbeddingIndexInput): Promise<string> {
    const projectId = this.requireProjectId();
    return this.embeddings.createEmbeddingIndex(projectId, input);
  }

  async refreshEmbeddingIndex(
    indexId: string,
    mode: 'incremental' | 'full',
  ): Promise<{ jobId: number | null; status: string }> {
    const projectId = this.requireProjectId();
    return this.embeddings.refreshEmbeddingIndex(projectId, indexId, mode);
  }

  async runEmbeddingIndexAnalysis(
    input: EmbeddingIndexAnalysisInput,
  ): Promise<EmbeddingIndexAnalysisResult> {
    const projectId = this.requireProjectId();
    return this.embeddings.runEmbeddingIndexAnalysis(projectId, input);
  }

  embeddingExportArtifactUrl(indexId: string, format: string): string {
    return projectEmbeddingExportArtifactUrl(this.requireProjectId(), indexId, format);
  }

  async exportEmbeddingIndex(
    indexId: string,
    opts?: { formats?: string[]; includeVectors?: boolean },
  ): Promise<EmbeddingExportResult> {
    return this.embeddings.exportEmbeddingIndex(this.requireProjectId(), indexId, opts);
  }

  async embeddingSimilarityPreview(
    indexId: string,
    query: string | EmbeddingComposedQuery,
    opts?: { limit?: number },
  ): Promise<EmbeddingSimilarityResult> {
    return this.embeddings.embeddingSimilarityPreview(
      this.requireProjectId(),
      indexId,
      query,
      opts,
    );
  }

  async embeddingHybridPreview(
    indexId: string,
    sheetId: number | null,
    text: string,
    opts?: { limit?: number },
  ): Promise<EmbeddingSimilarityResult> {
    return this.embeddings.embeddingHybridPreview(
      this.requireProjectId(),
      indexId,
      sheetId,
      text,
      opts,
    );
  }

  async listOAuthConnections(provider?: string): Promise<OAuthConnectionInfo[]> {
    return oauthConnectionsApi.listOAuthConnections(provider);
  }

  googleOAuthStartUrl(): string {
    return browserGoogleOAuthStartUrl();
  }

  async exportGoogleSheets(
    input: GoogleSheetsExportInput,
  ): Promise<GoogleSheetsExportResult> {
    return this.googleSheetsExport.exportGoogleSheets(input);
  }

  listViews(sheetId?: string): Promise<SavedView[]> {
    return this.viewsLensesApi.listViews(sheetId);
  }

  saveView(input: SavedViewCreateInput): Promise<SavedView> {
    return this.viewsLensesApi.saveView(input);
  }

  renameView(viewId: number, input: SavedViewRenameInput): Promise<SavedView> {
    return this.viewsLensesApi.renameView(viewId, input);
  }

  replaceViewDefinition(
    viewId: number,
    input: SavedViewDefinitionReplaceInput,
  ): Promise<SavedView> {
    return this.viewsLensesApi.replaceViewDefinition(viewId, input);
  }

  deleteView(viewId: number): Promise<void> {
    return this.viewsLensesApi.deleteView(viewId);
  }

  listLenses(sheetId?: number | string): Promise<Lens[]> {
    return this.viewsLensesApi.listLenses(sheetId);
  }

  saveLens(input: LensSaveInput): Promise<Lens> {
    return this.viewsLensesApi.saveLens(input);
  }

  async resolveLens(
    lensId: number,
    opts?: { limit?: number; offset?: number },
  ): Promise<LensResolved> {
    return this.viewsLensesApi.resolveLens(lensId, opts);
  }

  async listWatches(): Promise<WatchInfo[]> {
    return this.watchesApi.listWatches();
  }

  async createWatch(input: WatchInput): Promise<WatchInfo> {
    return this.watchesApi.createWatch(input);
  }

  async updateWatch(watchId: number, input: WatchPatchInput): Promise<WatchInfo> {
    return this.watchesApi.updateWatch(watchId, input);
  }

  async deleteWatch(watchId: number): Promise<void> {
    return this.watchesApi.deleteWatch(watchId);
  }

  async runWatch(watchId: number): Promise<WatchRunResult> {
    return this.watchesApi.runWatch(watchId);
  }

  async getWatchRuns(watchId: number, offset = 0, limit = 20): Promise<WatchRunsPage> {
    return this.watchesApi.getWatchRuns(watchId, offset, limit);
  }

  async getWatchRunEvents(
    watchId: number,
    runId: number,
    offset = 0,
    limit = 50,
    eventKind?: string,
  ): Promise<WatchRunEventsPage> {
    return this.watchesApi.getWatchRunEvents(watchId, runId, offset, limit, eventKind);
  }

  async listNotifications(params: NotificationListParams = {}): Promise<NotificationPage> {
    return this.notificationsApi.listNotifications(params);
  }

  async getNotificationsSummary(): Promise<NotificationSummary> {
    return this.notificationsApi.getNotificationsSummary();
  }

  async markNotificationsSeen(
    filter: NotificationStateFilter,
  ): Promise<{ seenCount: number }> {
    return this.notificationsApi.markNotificationsSeen(filter);
  }

  async markNotificationRead(notificationId: number): Promise<NotificationActorState> {
    return this.notificationsApi.markNotificationRead(notificationId);
  }

  async ackNotification(notificationId: number): Promise<NotificationActorState> {
    return this.notificationsApi.ackNotification(notificationId);
  }

  async unackNotification(notificationId: number): Promise<NotificationActorState> {
    return this.notificationsApi.unackNotification(notificationId);
  }

  async listNotificationChannels(): Promise<NotificationChannelsPage> {
    return this.notificationsApi.listNotificationChannels();
  }

  async createNotificationChannel(
    input: NotificationChannelInput,
  ): Promise<NotificationChannel> {
    return this.notificationsApi.createNotificationChannel(input);
  }

  async updateNotificationChannel(
    channelId: number,
    input: Partial<NotificationChannelInput>,
  ): Promise<NotificationChannel> {
    return this.notificationsApi.updateNotificationChannel(channelId, input);
  }

  async listNotificationRoutes(): Promise<NotificationRoutesPage> {
    return this.notificationsApi.listNotificationRoutes();
  }

  async createNotificationRoute(input: NotificationRouteInput): Promise<NotificationRoute> {
    return this.notificationsApi.createNotificationRoute(input);
  }

  async updateNotificationRoute(
    routeId: number,
    input: Partial<NotificationRouteInput>,
  ): Promise<NotificationRoute> {
    return this.notificationsApi.updateNotificationRoute(routeId, input);
  }

  async testNotificationRoute(routeId: number, ownerKind?: string): Promise<NotificationDeliveryRequest> {
    return this.notificationsApi.testNotificationRoute(routeId, ownerKind);
  }

  async listNotificationDeliveryRequests(params: {
    status?: NotificationDeliveryRequest['status'];
    routeId?: number;
    channelId?: number;
    notificationId?: number;
    offset?: number;
    limit?: number;
  } = {}): Promise<NotificationDeliveryRequestsPage> {
    return this.notificationsApi.listNotificationDeliveryRequests(params);
  }

  listColumnTypes(): Promise<ColumnTypeInfo[]> {
    return this.columnTypesApi.listColumnTypes();
  }

  projectExportUrl(includeMediaOrOptions: boolean | ProjectExportOptions = true): string {
    return projectResourceExportUrl(this.requireProjectId(), includeMediaOrOptions);
  }

  sheetDatasetExportUrl(options: SheetDatasetExportOptions): string {
    return projectSheetDatasetExportUrl(this.requireProjectId(), options);
  }

  workLogExportUrl(format: 'md' | 'html' | 'pdf' = 'md'): string {
    return projectWorkLogExportUrl(this.requireProjectId(), format);
  }

  listProjects(): Promise<ProjectInfo[]> {
    return listProjectsContract(apiErrorFromContract);
  }

  createProject(name: string): Promise<ProjectInfo> {
    return createProjectContract(name, apiErrorFromContract);
  }

  copilotChat(
    messages: CopilotChatMessageInput[],
    model?: string | null,
  ): Promise<CopilotReply> {
    return copilotChatContract(this.requireProjectId(), messages, apiErrorFromContract, model);
  }

  updateProject(
    projectId: string,
    patch: { name?: string; description?: string; starred?: boolean; archived?: boolean },
  ): Promise<ProjectInfo> {
    return updateProjectContract(projectId, patch, apiErrorFromContract);
  }

  async getProject(): Promise<ProjectInfo> {
    return getProjectContract(this.requireProjectId(), apiErrorFromContract);
  }

  async updateCurrentProject(input: {
    name?: string;
    description?: string | null;
  }): Promise<ProjectInfo> {
    return updateProjectContract(this.requireProjectId(), input, apiErrorFromContract);
  }

  async getProjectRetention(): Promise<ProjectRetentionPolicy> {
    return this.projectDataManagementApi.getProjectRetention();
  }

  async updateProjectRetention(
    input: Partial<ProjectRetentionPolicy>,
  ): Promise<ProjectRetentionPolicy> {
    return this.projectDataManagementApi.updateProjectRetention(input);
  }

  async getProjectNetworkPolicy(): Promise<ProjectNetworkPolicy> {
    return this.projectDataManagementApi.getProjectNetworkPolicy();
  }

  async updateProjectNetworkPolicy(input: {
    mode: string;
  }): Promise<ProjectNetworkPolicy> {
    return this.projectDataManagementApi.updateProjectNetworkPolicy(input);
  }

  async getMediaProxyStatus(): Promise<MediaProxyStatus> {
    return organizationOperationsApi.getMediaProxyStatus();
  }

  async getProjectSettings(): Promise<ProjectSettings> {
    return this.projectDataManagementApi.getProjectSettings();
  }

  async updateProjectSettings(
    input: Partial<ProjectSettings>,
  ): Promise<ProjectSettings> {
    return this.projectDataManagementApi.updateProjectSettings(input);
  }

  async compactProject(): Promise<Record<string, unknown>> {
    return this.projectDataManagementApi.compactProject();
  }

  async getProjectProviderKeys(): Promise<ProjectProviderKeys> {
    return projectProviderKeysApi.getProjectProviderKeys(this.requireProjectId());
  }

  async setProjectProviderKey(
    provider: string,
    key: string,
    spendCapUsd?: number | null,
    validationToken?: string | null,
  ): Promise<ProjectProviderKeys> {
    const result = await projectProviderKeysApi.setProjectProviderKey(
      this.requireProjectId(),
      provider,
      key,
      spendCapUsd,
      validationToken,
    );
    this.invalidateActionCatalogCache();
    return result;
  }

  async validateProjectProviderKey(
    provider: string,
    key?: string,
  ): Promise<ProviderValidateResult> {
    return projectProviderKeysApi.validateProjectProviderKey(
      this.requireProjectId(),
      provider,
      key,
    );
  }

  async deleteProjectProviderKey(provider: string): Promise<{
    ok: boolean;
    deleted: boolean;
    provider: string;
  }> {
    const result = await projectProviderKeysApi.deleteProjectProviderKey(
      this.requireProjectId(),
      provider,
    );
    this.invalidateActionCatalogCache();
    return result;
  }

  async getProjectSecrets(): Promise<ProjectSecrets> {
    return projectSecretsApi.getProjectSecrets(this.requireProjectId());
  }

  async setProjectSecret(name: string, value: string): Promise<ProjectSecrets> {
    const result = await projectSecretsApi.setProjectSecret(
      this.requireProjectId(),
      name,
      value,
    );
    this.invalidateActionCatalogCache();
    return result;
  }

  async deleteProjectSecret(name: string): Promise<{
    ok: boolean;
    deleted: boolean;
    name: string;
  }> {
    const result = await projectSecretsApi.deleteProjectSecret(
      this.requireProjectId(),
      name,
    );
    this.invalidateActionCatalogCache();
    return result;
  }

  async listMcpServers() { return mcpServersApi.list(this.requireProjectId()); }
  async createMcpServer(input: import('./mcpServers').McpServerDraft) { return mcpServersApi.create(this.requireProjectId(), input); }
  async updateMcpServer(id: string, patch: Partial<import('./mcpServers').McpServerDraft>) { return mcpServersApi.update(this.requireProjectId(), id, patch); }
  async deleteMcpServer(id: string) { return mcpServersApi.remove(this.requireProjectId(), id); }
  async testMcpServer(id: string) { return mcpServersApi.test(this.requireProjectId(), id); }

  /** A saved/deleted provider secret can flip a hosted-engine's availability —
   *  the server resolves keys (DEEPL_API_KEY / GOOGLE_TRANSLATE_API_KEY, …)
   *  through the project secrets store when it assembles ui_hints.engines, so
   *  a cached catalog would keep advertising the old availability until reload.
   *  Dropping every cached catalog makes the decided "keys flip availability"
   *  real: the next listActionCatalog refetches fresh. Clearing all keys (not
   *  just this project's) also covers the env-backed global catalog. Clearing
   *  the cache is only half the job: a mounted surface keeps its stale
   *  snapshot, so we also emit an invalidation signal that useWorkspaceModel
   *  listens to and re-fetches — the engine flips available without a
   *  remount. */
  private invalidateActionCatalogCache(): void {
    this.actionCatalog.invalidate();
  }

  /** Public entry: a completed artifact pull that installs a new Opus-MT pair
   *  changes the engine's `models`, so the mounted translate form must
   *  refetch — the SAME clear+emit path a provider-secret save uses, so the
   *  newly-installed pair flips to installed without a remount. */
  invalidateActionCatalog(): void {
    this.invalidateActionCatalogCache();
  }

  async deleteProject(projectId: string | undefined, confirmName: string): Promise<void> {
    await deleteProjectContract(projectId ?? this.requireProjectId(), confirmName, apiErrorFromContract);
  }

  // ---- live sources -------------------------------------------------------

  listSources() {
    return this.projectSources.listSources();
  }

  getSource(sourceId: number, runsOffset?: number | null, runsLimit = 50) {
    return this.projectSources.getSource(sourceId, runsOffset, runsLimit);
  }

  getSourceHealth(sourceId: number, runsOffset?: number | null, runsLimit = 20) {
    return this.projectSources.getSourceHealth(sourceId, runsOffset, runsLimit);
  }

  createSource(input: SourceInput) {
    return this.projectSources.createSource(input);
  }

  updateSource(sourceId: number, patch: Partial<SourceInput>) {
    return this.projectSources.updateSource(sourceId, patch);
  }

  deleteSource(sourceId: number) {
    return this.projectSources.deleteSource(sourceId);
  }

  fetchSource(sourceId: number) {
    return this.projectSources.fetchSource(sourceId);
  }

  async listSheets(): Promise<SheetMeta[]> {
    return this.sheetGrid.listSheets();
  }

  async setSheetTitleColumn(sheetId: string, titleColumnId: string | null): Promise<void> {
    return this.sheetGrid.setSheetTitleColumn(sheetId, titleColumnId);
  }

  async deleteSheet(sheetId: string): Promise<DeleteSheetResult> {
    return this.sheetGrid.deleteSheet(sheetId);
  }

  async getLineage(): Promise<LineageDag> {
    return graphLineageApi.getLineage(this.requireProjectId());
  }

  async refreshSheet(
    sheetId: string,
    confirmation?: string,
  ): Promise<SheetRefreshResult> {
    return this.sheetGrid.refreshSheet(sheetId, confirmation);
  }

  async getSheetData(
    sheetId: string,
    offset: number,
    limit: number,
    options: SheetDataOptions | null = {},
  ): Promise<SheetDataPage> {
    return this.sheetGrid.getSheetData(sheetId, offset, limit, options);
  }

  async getColumnStats(
    sheetId: string,
    columnId: string,
    opts: { force?: boolean } = {},
  ): Promise<ColumnStats> {
    return this.sheetGrid.getColumnStats(sheetId, columnId, opts);
  }

  async getMapPoints(
    sheetId: string,
    columnId: string,
    options: MapPointsOptions | null = {},
  ): Promise<MapPointsResult> {
    return this.mapPointsArrowApi.getMapPoints(sheetId, columnId, options);
  }

  async getMapPointsArrowBuffer(
    sheetId: string,
    columnId: string,
    options: MapPointsOptions | null = {},
  ): Promise<ArrayBuffer> {
    return this.mapPointsArrowApi.getMapPointsArrowBuffer(sheetId, columnId, options);
  }

  async getRuntimeProjectionStatus(
    input: RuntimeProjectionStatusRequest,
  ): Promise<RuntimeProjectionStatus> {
    return this.runtimeProjectionsApi.status(input);
  }

  async buildRuntimeProjection(
    input: RuntimeProjectionBuildRequest,
  ): Promise<RuntimeProjectionBuildPlan> {
    return this.runtimeProjectionsApi.build(input);
  }

  async readRuntimeProjectionArtifact(
    input: RuntimeProjectionArtifactRequest,
  ): Promise<TimelineProjectionArtifact> {
    return this.runtimeProjectionsApi.artifact(input);
  }

  async locateSheetRow(
    sheetId: string,
    rowId: string,
    options: SheetDataOptions | null = {},
    pageSize = 500,
  ): Promise<SheetRowLocation> {
    return this.sheetGrid.locateSheetRow(sheetId, rowId, options, pageSize);
  }

  async updateColumn(columnId: string, patch: ColumnPatch): Promise<ColumnDef> {
    return this.sheetGrid.updateColumn(columnId, patch);
  }

  async addRow(sheetId: string, cells: Record<string, CellValue> = {}): Promise<{ rowId: string; total: number }> {
    return this.sheetGrid.addRow(sheetId, cells);
  }

  async addColumn(
    sheetId: string,
    name: string,
    options: { type?: string; position?: number | null } = {},
  ): Promise<AddColumnResult> {
    return this.sheetGrid.addColumn(sheetId, name, options);
  }

  async deleteRows(sheetId: string, rowIds: string[]): Promise<DeleteRowsResult> {
    return this.sheetGrid.deleteRows(sheetId, rowIds);
  }

  async editCells(edits: CellEdit[]): Promise<void> {
    return this.sheetGrid.editCells(edits);
  }

  // Replay preserve+surface write endpoints. These route the shipped Project
  // helpers over the typed v1 action contract, the same way editCells routes
  // cell.edit.
  async acceptReplayValue(
    sheetId: string,
    columnId: string,
    rowId: string,
    generatedValueHash: string,
    runId: string,
  ): Promise<void> {
    return this.sheetGrid.acceptReplayValue(
      sheetId, columnId, rowId, generatedValueHash, runId,
    );
  }

  async acceptReplayValuesInColumn(sheetId: string, columnId: string): Promise<void> {
    return this.sheetGrid.acceptReplayValuesInColumn(sheetId, columnId);
  }

  async dismissReplayPending(
    sheetId: string,
    columnId: string,
    rowId: string,
    generatedValueHash: string,
    runId: string,
  ): Promise<void> {
    return this.sheetGrid.dismissReplayPending(
      sheetId, columnId, rowId, generatedValueHash, runId,
    );
  }

  // ---- runs ---------------------------------------------------------------

  runProposal(
    proposal: CopilotProposal,
    confirmed = false,
    consentedPromiseSetHash?: string,
    options?: RunActionInvocationOptions,
  ): Promise<{ run_id: number | null; output_sheet_id?: string | null }> {
    return this.actionRuns.runProposal(
      proposal,
      confirmed,
      consentedPromiseSetHash,
      options,
    );
  }

  async runAction(
    req: ActionExecutionRequest,
    options?: RunActionInvocationOptions,
  ): Promise<RunActionLaunchResult> {
    return this.actionRuns.runAction(req, options);
  }

  async listActionJobs(
    status?: string | null,
    limit = 50,
    options?: ProjectInvocationOptions,
  ): Promise<ActionJobsPage> {
    const invocation = resolveRunActionInvocation(options, this.requireProjectId());
    throwIfActionAborted(invocation.signal);
    const page = await listActionJobsContract(
      invocation.projectId,
      { status, limit },
      apiErrorFromContract,
      { signal: invocation.signal },
    );
    throwIfActionAborted(invocation.signal);
    return page;
  }

  async getActionJob(
    jobId: number | string,
    options?: ProjectInvocationOptions,
  ): Promise<ActionJob> {
    const invocation = resolveRunActionInvocation(options, this.requireProjectId());
    throwIfActionAborted(invocation.signal);
    const job = await getActionJobContract(
      invocation.projectId,
      Number(jobId),
      apiErrorFromContract,
      { signal: invocation.signal },
    );
    throwIfActionAborted(invocation.signal);
    return job;
  }

  /** Attempt receipts answer "did I approve it, and what did it cost".
   *  Omitting `runId` lists the whole project, which is what makes a
   *  compaction-orphaned attempt reachable at all. */
  async listAttemptReceipts(
    runId?: number | string | null,
    limit = 25,
  ): Promise<AttemptReceiptsPage> {
    return projectAttemptsApi.listAttemptReceipts(
      this.requireProjectId(),
      runId,
      limit,
    );
  }

  async getWorkbenchPluginRuntimeIndex(): Promise<WorkbenchPluginRuntimeIndex> {
    return getWorkbenchPluginRuntimeIndexContract(this.requireProjectId(), apiErrorFromContract);
  }

  async getWorkbenchPluginSettings(pluginId: string): Promise<WorkbenchPluginSettings> {
    return workbenchPluginsApi.getSettings(this.requireProjectId(), pluginId);
  }

  async patchWorkbenchPluginSettings(
    pluginId: string,
    values: Record<string, unknown>,
  ): Promise<WorkbenchPluginSettings> {
    return workbenchPluginsApi.patchSettings(this.requireProjectId(), pluginId, values);
  }

  async installLocalWorkbenchPlugin(
    input: WorkbenchPluginLocalInstallRequest,
  ): Promise<WorkbenchPluginLocalInstallExecution> {
    return workbenchPluginsApi.installLocal(this.requireProjectId(), input);
  }

  async activateWorkbenchPlugin(
    input: WorkbenchPluginActivationRequest,
  ): Promise<WorkbenchPluginActivation> {
    return workbenchPluginsApi.activate(this.requireProjectId(), input);
  }

  async activateWorkbenchPluginBackend(
    input: WorkbenchPluginBackendActivationRequest,
  ): Promise<WorkbenchPluginBackendActivation> {
    return workbenchPluginsApi.activateBackend(this.requireProjectId(), input);
  }

  async disableWorkbenchPlugin(pluginId: string): Promise<WorkbenchPluginInstallStateChange> {
    return workbenchPluginsApi.disable(this.requireProjectId(), pluginId);
  }

  async uninstallWorkbenchPlugin(pluginId: string): Promise<WorkbenchPluginInstallStateChange> {
    return workbenchPluginsApi.uninstall(this.requireProjectId(), pluginId);
  }

  estimateAction(req: ActionExecutionRequest): Promise<RunEstimate> {
    return this.actionEstimateValidation.estimate(req);
  }

  resolveActionParams(
    req: Pick<RegisteredActionRequest, 'action_id' | 'scope' | 'params'>,
  ): Promise<ActionParamResolution> {
    return this.actionEstimateValidation.resolveParams(req);
  }

  getRunProgress(
    runId: string,
    options?: ProjectInvocationOptions,
  ): Promise<RunProgress> {
    return this.actionRuns.getRunProgress(runId, options);
  }

  cancelRun(
    runId: string,
    options?: ProjectInvocationOptions,
  ): Promise<RunProgress> {
    return this.actionRuns.cancelRun(runId, options);
  }

  backfillColumn(
    sheetId: string,
    columnName: string,
    confirmed = false,
    rowIds?: number[],
    consentedPromiseSetHash?: string,
  ): Promise<BackfillResult> {
    return this.actionRuns.backfillColumn(
      sheetId,
      columnName,
      confirmed,
      rowIds,
      consentedPromiseSetHash,
    );
  }

  async clusterPreview(input: Parameters<typeof resolvePreviewsApi.cluster>[1]) {
    return resolvePreviewsApi.cluster(this.requireProjectId(), input);
  }

  async columnValuesPreview(input: Parameters<typeof resolvePreviewsApi.columnValues>[1]) {
    return resolvePreviewsApi.columnValues(this.requireProjectId(), input);
  }

  async entityMentionDocuments(
    input: Parameters<typeof entityMentionsApi.documents>[1],
  ) {
    return entityMentionsApi.documents(this.requireProjectId(), input);
  }

  async entityMentionOccurrences(
    input: Parameters<typeof entityMentionsApi.occurrences>[1],
  ) {
    return entityMentionsApi.occurrences(this.requireProjectId(), input);
  }

  async entityMentionsPreview(
    input: Parameters<typeof entityMentionsApi.preview>[1],
  ) {
    return entityMentionsApi.preview(this.requireProjectId(), input);
  }

  async replaceRulesPreview(input: Parameters<typeof resolvePreviewsApi.replaceRules>[1]) {
    return resolvePreviewsApi.replaceRules(this.requireProjectId(), input);
  }

  async getSheetGraph(options: SheetGraphOptions): Promise<SheetGraphResult> {
    return graphLineageApi.getSheetGraph(this.requireProjectId(), options);
  }

  async compareOcrScratch(
    file: File,
    input: OcrCompareScratchInput,
  ): Promise<PreviewStartResult> {
    return this.actionPreviewRuns.registerStarted(await this.previewComparisonsApi.compareOcrScratch(file, input));
  }

  estimateOcrScratch(file: File, input: OcrCompareScratchInput): Promise<RunEstimate> {
    return this.previewComparisonsApi.estimateOcrScratch(file, input);
  }

  async compareTranscribeScratch(
    file: File,
    input: TranscribeCompareScratchInput,
  ): Promise<PreviewStartResult> {
    return this.actionPreviewRuns.registerStarted(await this.previewComparisonsApi.compareTranscribeScratch(file, input));
  }

  estimateTranscribeScratch(file: File, input: TranscribeCompareScratchInput): Promise<RunEstimate> {
    return this.previewComparisonsApi.estimateTranscribeScratch(file, input);
  }

  async compareTopicSegmentationScratch(
    file: File,
    input: TopicSegmentationCompareScratchInput,
  ): Promise<TopicSegmentationCompareScratchResult> {
    return this.previewComparisonsApi.compareTopicSegmentationScratch(file, input);
  }

  async compareTranslateScratch(
    input: TranslateCompareScratchInput,
  ): Promise<TranslateCompareScratchResult> {
    // Text-source: a JSON body, unlike ocr/transcribe's multipart — nothing
    // uploads to the project (scratch bake-off, no blobs/sheets/rows). Billable
    // engines (llm/deepl/google_translate) are REFUSED server-side and are not
    // offered in the picker: compare is the free surface, so there is no
    // allow_remote to send and no `model` (it only ever fed the llm engine).
    return translateComparisonApi.compareTranslateScratch(this.requireProjectId(), input);
  }

  async getRunRows(
    runId: string,
    offset = 0,
    limit = 20,
    status?: 'error',
  ): Promise<RunRowsPage> {
    return getRunRowsContract(
      this.requireProjectId(),
      Number(runId),
      { offset, limit, ...(status ? { status } : {}) },
      apiErrorFromContract,
    );
  }

  getTextAnnotations(rowId: string, columnId: string): Promise<TextAnnotations> {
    return this.projectEvidenceApi.getTextAnnotations(rowId, columnId);
  }

  async getColumnRuns(columnId: string, offset = 0, limit = 20): Promise<ColumnRunsInfo> {
    const projectId = this.requireProjectId();
    const result = await this.historyReview.getColumnRuns(columnId, offset, limit);
    const selectedRun = result.latestRun
      ?? result.currentRun
      ?? result.runs.find((run) => run.current)
      ?? result.runs[0];
    if (selectedRun) {
      this.actionRuns.rememberColumnRunInfo(projectId, columnId, {
        actionName: selectedRun.actionName,
        prompt: promptFromRunSpec(selectedRun.spec),
        model: selectedRun.model,
      });
    }
    return result;
  }

  async getRunTraceRow(
    runId: string,
    rowId: string,
    columnId?: string | null,
  ): Promise<RunTraceRowEvidence> {
    const projectId = this.requireProjectId();
    return runProvenanceApi.getRunTraceRow(projectId, runId, rowId, columnId);
  }

  getCellEvidence(
    rowId: string,
    columnId: string,
    opts?: { includeStale?: boolean },
  ): Promise<CellEvidencePayload> {
    return this.projectEvidenceApi.getCellEvidence(rowId, columnId, opts?.includeStale);
  }

  getColumnEvidence(
    sheetId: string,
    columnId: string,
    opts?: { rowIds?: readonly string[] },
  ): Promise<ColumnEvidenceBatchPayload> {
    return this.projectEvidenceApi.getColumnEvidence(sheetId, columnId, opts?.rowIds);
  }

  getEvidenceViewer(evidenceLinkId: string | number): Promise<EvidenceViewerPayload> {
    return this.projectEvidenceApi.getEvidenceViewer(evidenceLinkId);
  }

  async getProvenanceManifest(
    runsOffset = 0,
    runsLimit = 25,
    receiptsOffset = 0,
    receiptsLimit = 25,
  ): Promise<ProvenanceManifest> {
    const projectId = this.requireProjectId();
    return runProvenanceApi.getProvenanceManifest(
      projectId,
      runsOffset,
      runsLimit,
      receiptsOffset,
      receiptsLimit,
    );
  }

  async getReceipt(
    receiptId: string,
    options?: ProjectInvocationOptions,
  ): Promise<V1Receipt> {
    const invocation = resolveRunActionInvocation(options, this.requireProjectId());
    throwIfActionAborted(invocation.signal);
    const receipt = await getReceiptContract(
      invocation.projectId,
      receiptId,
      apiErrorFromContract,
      { signal: invocation.signal },
    );
    throwIfActionAborted(invocation.signal);
    return receipt;
  }

  // ---- history ------------------------------------------------------------

  async getHistory(offset?: number | null, limit = 50): Promise<HistoryState> {
    return this.historyReview.getHistory(offset, limit);
  }

  async undo(): Promise<HistoryState> {
    return this.historyReview.undo();
  }

  async redo(): Promise<HistoryState> {
    return this.historyReview.redo();
  }

  async stepTo(opIndex: number): Promise<HistoryState> {
    return this.historyReview.stepTo(opIndex);
  }

  async startPreview(req: ActionExecutionRequest): Promise<PreviewStartResult> {
    const invocation = resolveRunActionInvocation(undefined, this.requireProjectId());
    return this.actionPreviewRuns.start(req, invocation);
  }

  async getPreview(previewId: string): Promise<PreviewSampleResult> {
    return this.actionPreviewRuns.get(previewId);
  }

  async cancelPreview(previewId: string): Promise<void> {
    // Idempotent 204 on the server; a failed DELETE (already-gone job) is a no-op.
    await this.actionPreviewRuns.cancel(previewId);
  }

  // ---- review queue ---------------------------------------------------------

  async getReviewCount(runId?: string): Promise<number> {
    return this.historyReview.getReviewCount(runId);
  }

  async getReviewBundles(
    offset = 0,
    limit = 25,
    runId?: string,
    includeReviewed = false,
  ): Promise<ReviewBundlePage> {
    return this.historyReview.getReviewBundles(offset, limit, runId, includeReviewed);
  }

  async reviewItem(
    itemId: string,
    action: ReviewAction,
    editedValue?: CellValue,
    note?: string | null,
  ): Promise<void> {
    return this.historyReview.reviewItem(itemId, action, editedValue, note);
  }
}

const realApi = new RealApi();
export const createProjectApi = (projectId: string): FrisketApi => new RealApi(projectId);
