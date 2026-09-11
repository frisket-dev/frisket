
import type { GeneratedActionParams } from '../generated/actionTypes';
import type {
  HttpActionPreviewStatusResponse,
  HttpCellEvidenceResponse,
  HttpEvidenceViewerResponse,
  HttpLocalEndpointDiscoveryResponse,
  HttpReceipt,
} from '../generated/openHttpContracts';



export interface ProjectInfo {
  id: string;
  name: string;
  description?: string | null;
  sensitive?: boolean;
  role?: ProjectRole | null;
  /** ISO timestamp of the project bundle's last write (server: project.db
   *  mtime). Nullable for older/degenerate bundles missing project.db. */
  updated_at?: string | null;
  /** Cached pending-review bundle count (accept/reject/edit still owed).
   *  Absent/0 means nothing pending — the Home screen only shows a badge when
   *  this is truthy. */
  pending_review_count?: number;
  /** Shell/Home project flags: additive manifest booleans that scope the Home
   *  project list (Starred / Archive). Archive is a FLAG, never a deletion.
   *  Default false on older bundles. */
  starred?: boolean;
  archived?: boolean;
}

// Instance identity: a third-party operator's own display name/welcome
// message/support contact, so a self-hosted or hosted-for-others deployment
// isn't stuck with our hardcoded branding/copy.
export interface InstanceIdentity {
  display_name: string;
  support_contact?: string | null;
}

/**
 * The shared, open identity payload for GET /api/me: who you are and which
 * instance you are on. It carries NO balance/credits field — balances are an
 * external managed contribution over this seam, denominated in abstract
 * credits, and the open bundle does not know about them.
 */
export interface MeInfo {
  email: string;
  display_name?: string | null;
  /** Null marks a newly signed-in person who has not answered the initial
   * cost pre-approval prompt. */
  cost_preapproval_usd?: string | null;
  avatar_seed?: string | null;
  instance?: {
    display_name: string;
    welcome_message?: string | null;
    support_contact?: string | null;
  };
}

/** GET /api/config: the active router cache-mode posture, read by the
 * replay-mode top bar. Same on local and hosted — both expose it without a
 * signed-in session (frisket.llm.router.CacheMode).
 *
 * Also carries the outbound-email sender identity (address + name), read by
 * SignIn.tsx instead of a hardcoded address. Both null on the local tier,
 * which never sends email. */
export interface RuntimeConfig {
  cache_mode: 'replay' | 'fresh' | 'replay_strict' | 'off';
  live_calls_possible: boolean;
  /** True only when this server composition exposes the local persisted
   *  runtime-setting mutation. Team/managed deployments own this posture
   *  outside the personal browser settings surface. */
  cache_mode_editable: boolean;
  /** Local-installation amount for an unauthenticated workspace. */
  cost_preapproval_usd?: string;
  /** True only when this local installation owns the setting. */
  cost_preapproval_editable?: boolean;
  /** Whether the server runs inside a container. Managed deployments may
   *  source `cache_mode` from their environment; local editable instances
   *  persist it through Preferences. Hosted deployments may omit it. */
  in_container?: boolean;
  /** What a `map.python` code recipe is confined by on the server, minted by
   *  frisket.engine.recipe_fence_posture from the fence's own platform matrix
   *  (src/frisket/engine/sandbox/fence.py):
   *
   *  - `enforced`  a kernel fence goes up in the recipe child before recipe
   *                code runs, or the run refuses. No network at all, and no
   *                writes or data reads outside its own scratch directory.
   *  - `partial`   an OS network wall but NO filesystem confinement (macOS).
   *  - `none`      nothing below Python (Windows).
   *  - `unknown`   the server did not say, or said something this client does
   *                not recognise.
   *
   *  Optional on the wire: a server that predates the field leaves it
   *  undefined, which every consumer must read as `unknown` and WARN about —
   *  never as "probably fine". */
  recipe_fence_posture?: RecipeFencePosture;
  email_from_address: string | null;
  email_from_name: string | null;
  auth_methods: {
    password: boolean;
    magic_link: boolean;
    oidc: Array<{ id: string; label: string }>;
  };
  /** Whether this server composition exposes any workbench plugin
   *  contributions. Runtime layout loading consumes this field. */
  plugins_available: boolean;
  /** Whether this composition exposes nonbundled plugin lifecycle and
   *  configuration controls. Required and fail-closed. */
  plugin_management_available: boolean;
  /** Whether this deployment has a configured product-telemetry destination. */
  product_telemetry_available: boolean;
}

/** @see RuntimeConfig.recipe_fence_posture */
export type RecipeFencePosture = 'enforced' | 'partial' | 'none' | 'unknown';

export interface SpendRow {
  project: string;
  model: string | null;
  month: string | null;
  runs: number;
  rows: number;
  cost: number;
}

export interface SpendReport {
  rows: SpendRow[];
  total_cost: number;
  has_unknown_costs: boolean;
  unknown_cost_models: string[];
}

/**
 * What GET /api/org/keys actually returns — provider and hint, nothing more.
 * It previously also declared spend_cap_usd / spent_usd / over_cap, none of
 * which the endpoint has ever sent: the control plane's `org_keys` table has
 * no cap or accrual column (metered caps against an org key are private
 * commerce). The settings table rendered all three as `undefined`, i.e. "-".
 * Project-level provider keys carry the real, enforced cap.
 */
export interface OrgKeyInfo {
  provider: string;
  hint: string;
}

export interface OrgEnvInfo {
  name: string;
  hint: string;
  created_at?: string | null;
}

export interface ProviderCatalogItem {
  id: string;
  label: string;
  secret_name: string;
  kind: string;
  policy_fields: string[];
}

export interface ProviderCatalog {
  schemaVersion: 'frisket.provider_catalog.v1';
  providers: ProviderCatalogItem[];
}

export interface ApiTokenInfo {
  id: number;
  user_id: number;
  created_by: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked: boolean;
}

export interface ApiTokenCreateResponse {
  id: number;
  token: string;
  name: string;
  prefix: string;
}

export interface RemovalResult {
  ok: boolean;
  removed: boolean;
}

export type ProjectRole = 'viewer' | 'reviewer' | 'editor' | 'owner';

export type ProjectInviteRole = Extract<ProjectRole, 'viewer' | 'editor'>;

export interface ProjectMember {
  user_id: number;
  email: string;
  role: ProjectRole;
}

export interface ProjectMemberChange {
  ok: boolean;
  email: string;
  role: ProjectRole;
}

export interface ProjectInvite {
  id: number;
  email: string;
  role: ProjectInviteRole;
  created_at: string | null;
  expires_at: string | null;
  accepted_at: string | null;
  revoked_at: string | null;
}

export interface ProjectInviteResponse {
  sent: boolean;
  invite: ProjectInvite;
}

export interface ProjectRetentionPolicy {
  schemaVersion?: 'frisket.project_retention_policy.v1' | string;
  default_evidence: string;
  pin_evidence_by_default: boolean;
  no_compact: boolean;
  supported_default_evidence?: string[];
  [key: string]: unknown;
}

export interface ProjectNetworkPolicy {
  schemaVersion?: 'frisket.project_network_policy.v1' | string;
  /** inherit = follow the org default (local edition: on). */
  mode: 'inherit' | 'on' | 'off' | string;
  /** Org-wide default materialized into the project; null = built-in "on". */
  org_default?: 'on' | 'off' | null;
  /** The resolved two-state value the dispatch gate reads. */
  effective: 'on' | 'off' | string;
  [key: string]: unknown;
}

export interface ProjectSettings {
  /**
   * EFFECTIVE value: whether media downloads may reach loopback and RFC1918
   * addresses, after the server default (FRISKET_MEDIA_PRIVATE_HOSTS) and any
   * project override are resolved. Link-local and cloud instance-metadata
   * addresses are refused at every level. App-level filter on what dispatch
   * accepts, not a network guarantee: direct downloads re-check each redirect
   * hop, but the yt-dlp path is checked on entry only.
   */
  media_allow_private_hosts: boolean;
  /**
   * True when the server sets FRISKET_MEDIA_PRIVATE_HOSTS=deny-locked: the
   * project override is refused (PATCHing it returns 400) and the toggle is
   * hidden.
   */
  media_allow_private_hosts_locked: boolean;
}

export interface ProjectProviderKeyInfo extends ProviderCatalogItem {
  configured: boolean;
  hint: string | null;
  spend_cap_usd: number | null;
  /**
   * Accrued spend on this key, summed from model-call facts whose
   * credential_source is `project_key`. Only counts calls we could price —
   * see `unmetered_calls`.
   */
  spent_usd: number;
  /**
   * Live calls on this key with no published price. When non-zero,
   * `spent_usd` is a lower bound, not the total.
   */
  unmetered_calls: number;
  updated_at: string | null;
}

export interface ProjectProviderKeys {
  schemaVersion: 'frisket.project_provider_keys.v1';
  projectId: string;
  providers: ProjectProviderKeyInfo[];
}

export interface ProjectSecretConsumer {
  kind: string;
  id: string;
}

export interface ProjectSecretInfo {
  name: string;
  hint: string | null;
  configured: boolean;
  updatedAt: string | null;
  consumers: ProjectSecretConsumer[];
}

export interface ProjectSecretMigrationConflict {
  plugin_id: string;
  name: string;
  hint: string | null;
  status: string;
  created_at: string | null;
}

export interface ProjectSecrets {
  schemaVersion: 'frisket.project_secrets.v1';
  projectId: string;
  secrets: ProjectSecretInfo[];
  conflicts: ProjectSecretMigrationConflict[];
}

export interface AdminOverview {
  totals: {
    orgs?: number;
    users?: number;
    projects?: number;
    pending_invites?: number;
  };
  /** A downstream admin-overview renderer may consume composition-owned
   * fields without making them part of the public team contract. */
  [key: string]: unknown;
}

/** GET /api/health — unauthenticated. `posture` is the self-host security
 *  posture; absent on the local tier. */
export interface HealthStatus {
  ok: boolean;
  tier?: string;
  posture?: {
    default_secrets_master_key: boolean;
    open_signup: boolean;
    admin_configured: boolean;
  };
}

// Compatibility-only aliases. The browser-admin wire contract is derived from
// the generated OpenAPI artifact in adminBrowser.ts, not handwritten here.
export type {
  AdminAuditCategory,
  AdminAuditEvent,
  AdminAuditFilters,
  AdminAuditLog,
  AdminErrorEvent,
  AdminErrors,
  AdminHealth,
  AdminInvite,
  AdminJob,
  AdminJobActionResult,
  AdminJobs,
  AdminOrgRole,
  AdminUser,
  AdminUsers,
  AdminUsersOrg,
} from './adminBrowser';

export interface SearchHit {
  sheet_id: number | string;
  row_id: number | string;
  column_id: number | string;
  column_name: string;
  ai_generated?: boolean;
  snip: string;
}

export interface SearchOptions {
  limit?: number;
  mode?: 'keyword' | 'semantic';
  rerank?: boolean;
}

/** The core column types. The palette is OPEN: plugins register more over
 *  the backend registry (GET /api/column-types), so a column's type is any
 *  registered name — `(string & {})` keeps literal autocomplete working. */
export type KnownColumnType =
  | 'text'
  | 'number'
  | 'integer'
  | 'boolean'
  | 'category'
  | 'json'
  | 'date'
  | 'image'
  | 'audio'
  | 'video'
  | 'file'
  | 'link'
  | 'timestamped_transcript'
  | 'timeline_point'
  | 'timeline_points'
  | 'timeline_range'
  | 'timeline_ranges';

export type ColumnType = KnownColumnType | (string & {});

/** One entry of the column-type registry (GET /api/column-types). The grid
 *  resolves its renderer from `presentation.renderer` — no hardcoded enum. */
export interface ColumnTypeInfo {
  name: string;
  core: boolean;
  plugin?: string;
  presentation: { renderer?: string; [k: string]: unknown };
  hasValidator: boolean;
  hasParser: boolean;
  description: string;
}

export type ProjectExportMode = 'bundle' | 'db';

export interface ProjectExportOptions {
  mode?: ProjectExportMode;
  includeMedia?: boolean;
  includeTraces?: boolean;
}

/** One prior run of the op that owns an AI column (run-based versioning). */
export interface RunVersion {
  runId: string;
  version: number;
  model: string;
  startedAt: string; // ISO
  rowCount: number;
  cost: number; // USD
  status: 'complete' | 'partial' | 'superseded';
}

/** Op metadata owned by an AI column header ("column headers own op metadata"). */
export interface ColumnAiMeta {
  actionName: string;
  prompt: string;
  model: string;
  costSoFar: number; // USD
  versions: RunVersion[];
}

export interface ColumnDef {
  id: string;
  name: string;
  type: ColumnType;
  width?: number;
  /** Display hint from the backend: 'markdown' | 'filesize' | 'currency' | 'percent' | null. */
  format?: string | null;
  /** Explicit contract marker (`columns.semantic_type`) — what the cell
   *  contents MEAN, as opposed to `type`, which says how they are stored.
   *  Currently only `'entity_mentions'` (written by map.ner onto its output
   *  column). null/undefined means the column declares no contract; it is
   *  NEVER inferred from cell contents or a column name. The Mentions panel's
   *  eligibility test is this marker plus `type === 'json'`. */
  semanticType?: string | null;
  /** Host-authored presentation default. The column remains available to
   * Detail, actions, and exports; the grid starts it collapsed. */
  defaultHidden?: boolean;
  /** Present iff this column is AI-generated. */
  ai?: ColumnAiMeta;
  /** Legacy scalar value authority. Always null for generation-managed columns;
   * their per-cell currentValueRef is authoritative. */
  currentRunId?: string | null;
  /** Newest applicable output family for grouping and in-flight presentation.
   * Never use this as cell-value or mutation provenance. */
  latestRunId?: string | null;
  /** True when exact per-cell generation heads, rather than the legacy scalar,
   * own value identity and replacement compatibility. */
  generationManaged?: boolean;
  /** Dynamic exact-head signal; true when active cells originate from at least
   * two runs. No maintained column summary backs this field. */
  mixedOrigins?: boolean;
  /** Backend-computed transcribe coverage (audio/video columns only) — row-scoped
   * success over non-empty media rows, from the newest transcribe run targeting
   * this column. Undefined for non-media columns. */
  transcriptStatus?: 'missing' | 'partial' | 'complete_visible' | 'complete_hidden' | null;
  /** Backend-computed (link/text columns only): sampled values classify as a
   * downloadable single media URL and the sheet has no audio/video/file
   * column yet — the launcher the "Download media?" prompt should route to.
   * null once a media column exists or the column doesn't classify. */
  mediaDownloadCandidate?: 'media.ytdlp_download' | 'media.fetch_url' | null;
  /** Replay preserve+surface: full-column count of edited generated cells
   * whose fresh current-run value differs from the human edit — feeds the "N
   * updated values · Review" header chip. 0/undefined when nothing is
   * pending. This count is the full-column promise, not the loaded page. */
  replayPendingCount?: number;
}

export type ColumnDisplayFormat = 'markdown' | 'filesize' | 'currency' | 'percent';

export interface ColumnPatch {
  type?: ColumnType;
  format?: ColumnDisplayFormat | null;
}

export interface ColumnStatsTopValue {
  value: string;
  count: number;
}

export interface ColumnStatsHistogramBin {
  min: number;
  max: number;
  count: number;
}

export interface ColumnStatsNumeric {
  count: number;
  mean: number;
  median: number;
  min: number;
  max: number;
  histogram: ColumnStatsHistogramBin[];
}

export interface ColumnStatsText {
  count: number;
  shortest: string;
  shortestLength: number;
  longest: string;
  longestLength: number;
  meanLength: number;
  medianLength: number;
  lengthHistogram: ColumnStatsHistogramBin[];
}

export interface ColumnStatsDate {
  count: number;
  min: string;
  median: string;
  max: string;
}

export interface ColumnStatsJsonType {
  type: string;
  count: number;
}

export interface ColumnStatsFile {
  count: number;
  minSize: number;
  maxSize: number;
}

export interface ColumnStats {
  schemaVersion: 'frisket.column_stats.v1';
  sheetId: string;
  column: { id: string; name: string; type: ColumnType; format?: ColumnDisplayFormat | null };
  rowCount: number;
  threshold: number;
  computed: boolean;
  requiresManualAnalyze: boolean;
  missing?: number;
  invalid?: number;
  present?: number;
  distinct?: number;
  topValues?: ColumnStatsTopValue[];
  numeric?: ColumnStatsNumeric | null;
  text?: ColumnStatsText | null;
  date?: ColumnStatsDate | null;
  file?: ColumnStatsFile | null;
  jsonTypes?: ColumnStatsJsonType[];
}

export interface SheetMeta {
  id: string;
  name: string;
  rowCount: number;
  columns: ColumnDef[];
  /** Lineage: set when this sheet was derived from another. */
  parent?: {
    sheetId: string;
    sheetName: string;
    viaAction: string; // real op kind/label, e.g. "derive.table_from_list"
  };
  /**
   * Live derived-sheet sync (derived sheets only; roots leave this
   * undefined). Read straight from the API's lazy resolver; never faked.
   * 'stale' means an ancestor changed since this sheet was materialized.
   */
  syncState?: 'synced' | 'stale';
  staleReason?: string | null;
  /**
   * Sheet-shape graph-availability signal. Present only when this sheet IS a
   * materialized edge/join table (its rows carry two-sided membership);
   * absent for plain imported/base sheets. Gates the graph work-view segment
   * (`graphAvailable`).
   */
  materializedKind?: 'edge' | 'join';
  /**
   * The sheet-level row-title override, set via the column '...' menu's "Use
   * as row title" (server-persisted). `null`/absent means "no explicit
   * override" — every reader falls back to web/src/workbench/rowTitle.ts's
   * default (first column per the grid's current drag order, falling back to
   * canonical column order).
   */
  titleColumnId?: string | null;
  /**
   * Column ids that carry at least one ACTIVE evidence link on this sheet,
   * DISTINCT, ascending. ALWAYS present (an empty array for sheets with zero
   * evidence links — unlike `materializedKind`'s conditional-absent shape
   * above), so the Grounded Answers work-view's availability rule
   * (core/selectors/views/answers.view.ts) can read it unconditionally.
   * Mirrors the graph-view materializedKind precedent: a server-computed,
   * sheet-shape signal the work-view switcher consumes synchronously — no
   * async availability probe. Staleness-until-reload accepted: a column's
   * FIRST citation does not retroactively unlock the view until the sheet
   * metadata reloads.
   */
  citedColumnIds: string[];
  /**
   * SOURCE text column ids on this sheet that are reachable through at least
   * one valid annotation coordinate surface, DISTINCT, ascending. ALWAYS
   * present (`[]` when nothing on the sheet is annotated), same contract as
   * `citedColumnIds` above and consumed the same way — synchronously, by the
   * work-view availability rule (core/selectors/views/document.view.ts).
   *
   * NOT interchangeable with `citedColumnIds`: that one names NER's OUTPUT
   * column and every other evidence kind, so gating the annotated-text reader
   * on it would offer the Document view on every sheet that has any evidence
   * at all (annotated-text-layers R13). This one names the column whose TEXT
   * the marks are drawn over.
   *
   * Freshness is deliberately NOT required: a layer whose stored surface no
   * longer matches the current cell still keeps its source column listed, so
   * the reader can show the `[!]` unpositioned state instead of the column
   * silently disappearing.
   */
  annotatedTextColumnIds: string[];
  /** Visible materialized sheets that consume this sheet. Deletion is blocked
   *  until these dependents are removed first. */
  dependentSheetIds?: string[];
}

export interface DeleteSheetResult {
  deletedSheetId: string;
  deletedSheetName: string;
}

/* ------------------------------------------------------------------ *
 * annotated-text-layers: the per-cell annotation payload the reader   *
 * draws marks from (GET /cells/{row}/{col}/annotations).              *
 * ------------------------------------------------------------------ */

/** One drawable occurrence. `start`/`end` are UTF-16 code-unit offsets into
 *  `TextAnnotations.text` — the SAME index unit a JavaScript string uses, so
 *  `text.slice(start, end)` is the mark, with no conversion and no re-location
 *  by quote search (the reader's D1: flat text + server offsets). */
export interface TextAnnotationSpan {
  /** Stable within one response: `${spanId}:${linkId}`. Identifies the
   *  occurrence across the fragments a single mark may be split into. */
  occurrenceId: string;
  start: number;
  end: number;
  /** The slice of the CURRENT text, re-derived server-side — never the stored
   *  quote, so a drifted cell can never render text it does not contain. */
  quote: string;
  entityType: string | null;
  /** The identity the Mentions panel groups by, resolved server-side from the
   *  link's own entity cell. Null for the UNFINGERPRINTED types (date, time,
   *  percent, quantity, ordinal, cardinal, money), which group by exact
   *  spelling — so a null here means "address this one by its text", never
   *  "identity unknown". */
  entityFingerprint: string | null;
}

/** Why a layer's marks cannot be drawn. The reader keeps the layer's
 *  last-known count and shows a `[!]` status control instead of guessing at
 *  positions. */
export type TextAnnotationUnpositionedReason =
  | 'content_hash_mismatch'
  | 'unsupported_offset_unit'
  | 'invalid_geometry';

/** One producer run's annotation layer over this cell. Positioned and
 *  unpositioned are DISJOINT arms, not one nullable shape — an unpositioned
 *  layer has no spans at all, and that is the point. */
export type TextAnnotationLayer = {
  /** `${sheetId}:${outputColumnId}:${family}` — the persistence key for the
   *  per-family toggle. Server-minted so the client never composes it. */
  toggleKey: string;
  layerFamily: string;
  producerKind: string | null;
  producerEngine: string | null;
  outputColumn: { id: string; name: string | null };
} & (
  | {
      positioned: true;
      spans: TextAnnotationSpan[];
      counts: { shown: number; total: number; invalid: number };
    }
  | {
      positioned: false;
      unpositioned: { reason: TextAnnotationUnpositionedReason; total: number };
    }
);

export interface TextAnnotations {
  sheetId: string;
  rowId: string;
  columnId: string;
  /** The exact cell string the offsets index, or null when there is nothing to
   *  render (hidden/absent cell, or a non-text value). */
  text: string | null;
  contentHash: string | null;
  layers: TextAnnotationLayer[];
}

/** One document (row) a normalized mention appears in — the mention-detail
 *  panel's Level 1. */
export interface EntityMentionDocument {
  rowId: string;
  /** From the sheet's title column; null when that column is empty for this
   *  row. `rowId` — not the title — is what the click-through addresses. */
  title: string | null;
  occurrenceCount: number;
}

export interface EntityMentionDocumentsPage {
  sheetId: string;
  columnId: string;
  type: string;
  /** Echoed back so the reader and the grid cross-link to the SAME filter
   *  without reconstructing it. */
  selector: EntityMentionSelector;
  /** "N mentions across M documents" — `mentions` is COUNT(*) and `documents`
   *  is COUNT(DISTINCT row), the SAME two numbers the Mentions panel reports
   *  for the group, from the same stream. */
  totals: { mentions: number; documents: number };
  documents: EntityMentionDocument[];
  /** Offset for the next page, or null at the end. */
  nextOffset: number | null;
}

/** One occurrence of a mention inside one document, with the reading context
 *  around it. `start`/`end` are UTF-16 offsets into the WHOLE cell (what the
 *  reader scrolls to); `snippet.markStart`/`markEnd` are UTF-16 offsets into
 *  the snippet (what the panel highlights). Two coordinate spaces because they
 *  answer two questions — mixing them puts the highlight in the wrong place. */
export interface EntityMentionOccurrence {
  occurrenceId: string;
  start: number;
  end: number;
  quote: string;
  snippet: {
    text: string;
    markStart: number;
    markEnd: number;
    /** Text was dropped at this edge, so an ellipsis is earned rather than
     *  assumed. */
    truncatedStart: boolean;
    truncatedEnd: boolean;
  };
}

export interface EntityMentionOccurrencesPage {
  sheetId: string;
  rowId: string;
  columnId: string;
  type: string;
  selector: EntityMentionSelector;
  /** The source text column the layer is drawn over, resolved server-side from
   *  the entity column. Null when no annotation layer reaches this row. */
  textColumn: { id: string; name: string | null } | null;
  totals: { occurrences: number };
  occurrences: EntityMentionOccurrence[];
  nextOffset: number | null;
  /** Set when the layer's coordinates no longer apply to the current text. The
   *  occurrence list is then EMPTY on purpose — the panel says the text moved,
   *  it does not say the mention is absent. */
  unpositioned: { reason: TextAnnotationUnpositionedReason; total: number } | null;
}

/** A node in the Monitor Lineage DAG. */
export interface LineageNode {
  id: string;
  kind: 'source' | 'sheet' | 'ai_column';
  name: string;
  sheet_id?: number | null;
  column_id?: number;
  derived?: boolean;
  syncState?: 'synced' | 'stale' | null;
  stale_reason?: string | null;
  op_kind?: string;
  op_label?: string | null;
  model?: string | null;
  source_kind?: string;
}

export interface LineageEdge {
  from: string;
  to: string;
  kind: 'source' | 'derive' | 'ai_column';
  stale: boolean;
}

export interface LineageDag {
  project_id: string;
  op_cursor: number;
  nodes: LineageNode[];
  edges: LineageEdge[];
}

export interface SheetRefreshResult {
  status: string;
  sheetId: string;
  needsConfirmation: boolean;
  estimate?: unknown;
}

/** Values carried by the sheet-data and cell-edit APIs.
 *
 * Most existing structured columns are adapted to JSON strings for their
 * legacy grid renderers. Source-bound temporal columns intentionally retain
 * their canonical object value so an edit cannot accidentally turn a typed
 * value into text on its way back to the server.
 */
export type CellValue = string | number | boolean | null | Record<string, unknown>;

export type CellValueRefKind =
  | 'manual_edit'
  | 'run_result'
  | 'source_cell'
  | 'missing'
  | 'compacted';

export interface CellValueRef {
  kind: CellValueRefKind;
  rowId: string;
  columnId: string;
  runId: string | null;
  opId: string | null;
}

/** Per-cell provenance for AI-generated values ("row drawers own provenance"). */
export interface CellProvenance {
  currentValueRef: CellValueRef;
  model: string;
  actionName: string;
  cost: number; // USD, this cell's share
  confidence: number | null; // 0–1 self-reported, null if not requested
  justification: string;
  runId: string;
}

export interface CellEvidenceLinkSummary {
  id: number;
  stable_id: string;
  export_ref: string;
  status: 'active' | 'stale' | 'replaced' | 'rejected' | (string & {});
  role: string;
  evidence_kind: string;
  span_count: number;
  artifact_count: number;
  snippet: string | null;
  viewer_href: string;
}

/**
 * Generated contract authority for cell-evidence reads. `current_value_ref`
 * can retain valid scalar, list, or provider/plugin JSON leaves, so callers
 * must not narrow it into a client-owned object shape.
 */
export type CellEvidencePayload = HttpCellEvidenceResponse;

/**
 * answers-view-column-evidence-batch-v1: the Grounded Answers reading view's
 * middle-pane batch. Per row that has ACTIVE evidence on the column, its
 * row_id + the SAME `CellEvidenceLinkSummary` chip shape `CellEvidencePayload`
 * carries — a chip here is field-identical to a chip from the per-cell
 * endpoint. A row with no active evidence is simply absent from `rows`.
 */
export interface ColumnEvidenceBatchRow {
  row_id: number;
  links: CellEvidenceLinkSummary[];
}

export interface ColumnEvidenceBatchPayload {
  schema_version: 'frisket.column_evidence_batch.v1';
  sheet_id: number;
  column_id: number;
  rows: ColumnEvidenceBatchRow[];
}

/** Browser aliases intentionally track the generated evidence-viewer wire
 * response. Its JSON leaves are open values, so consumers must narrow only
 * the individual leaves they dereference rather than narrowing the payload. */
export type EvidenceViewerPayload = HttpEvidenceViewerResponse;
export type EvidenceArtifact = EvidenceViewerPayload['artifacts'][number];
export type EvidenceArtifactRef = EvidenceArtifact['artifact_ref'];
export type EvidenceBlobRef = NonNullable<EvidenceArtifactRef['blob']>;
export type EvidencePage = EvidenceArtifact['pages'][number];
export type EvidenceRegion = EvidencePage['regions'][number];
export type EvidenceSpan = EvidenceArtifact['spans'][number];
export type EvidenceCitationRun = EvidenceArtifact['runs'][number];

export interface Row {
  id: string;
  index: number;
  cells: Record<string, CellValue>; // columnId -> value
  provenance: Record<string, CellProvenance>; // columnId -> current value refs/provenance
  /** Backend per-cell state from /data meta: complete/error/incomplete. */
  cellStates?: Record<string, string>;
  /** Per-cell error text from /data meta (columnId -> message). A failed run
   *  row lands here so the grid can render the error inline instead of an
   *  invisible empty dash. */
  cellErrors?: Record<string, string>;
  /** Per-cell result-outcome bucket from /data meta, failed cells only
   *  (columnId -> outcome). 'empty_output' marks the terminal-failure bucket
   *  behind the row-level "Retry anyway" affordance. */
  cellOutcomes?: Record<string, string>;
  /** Typed-column cells whose preserved raw value does not match the type. */
  invalidCells?: Record<string, true>;
  /** For derived sheets: the parent-sheet row this row came from (lineage). */
  parentRowId?: string | null;
  /** Rows in child sheets that derive from this row (the "3 faces" count). */
  childCount?: number;
  /** Replay preserve+surface: for each edited generated cell on this
   *  (loaded) row whose fresh current-run value differs from the human edit,
   *  the fresh value + its run/value-identity — the per-cell badge + accept
   *  popover read this. Absent columns have no pending value. Page-local
   *  (badges); the column chip count is the full-column promise. */
  replayPending?: Record<string, ReplayPendingValue>;
}

export interface ReplayPendingValue {
  freshValue: CellValue;
  runId: string;
  generatedValueHash: string;
}

export interface CellEdit {
  rowId: string;
  columnId: string;
  value: CellValue;
}

export interface AddRowResult {
  rowId: string;
  total: number;
}

export interface AddColumnResult {
  columnId: string;
  name: string;
  type: string;
  position: number;
}

export interface DeleteRowsResult {
  rowIds: string[];
  deleted: number;
  total: number;
}

// ---------------------------------------------------------------------------
// Actions & runs

// Action identities come directly from the served catalog.
export type ActionKind = string;

export interface OutputJsonSchema {
  type: 'string' | 'number' | 'integer' | 'boolean' | 'object' | 'array' | string;
  description?: string;
  properties?: Record<string, OutputJsonSchema>;
  required?: string[];
  items?: OutputJsonSchema;
  enum?: string[];
}

export interface OutputField {
  name: string;
  type: ColumnType;
  description: string;
  /** Allowed labels for category fields (rendered as an enum in the schema). */
  labels?: string[];
  /** Classify only: optional semantic descriptions embedded for each label. */
  labelDescriptions?: Record<string, string>;
  /** JSON Schema for list items or json object shape. */
  items?: OutputJsonSchema;
  properties?: Record<string, OutputJsonSchema>;
  /** Extract only: the model MUST find this field — a null/missing value is
   *  withheld with a per-cell flag rather than silently empty. Optional
   *  (default false) and only serialized when true. */
  required?: boolean;
}

/** One op-specific parameter rendered in the action form (testid
 *  `field-<name>`); `name` is the backend spec key it feeds. */
export interface ActionParamUnitOption {
  /** What the person selects and sees alongside the display value. */
  value: string;
  /** Number of canonical units represented by one selected unit. */
  multiplier: number;
}

/** Declarative display-unit metadata for a positive integer canonical value.
 * The action draft still owns the canonical value; this only tells the shared
 * parameter renderer how to express it locally. */
export interface ActionParamUnitPresentation {
  defaultUnit: string;
  units: [ActionParamUnitOption, ...ActionParamUnitOption[]];
}

export interface ActionParam {
  name: string;
  label: string;
  input: 'text' | 'textarea' | 'column' | 'column-optional' | 'columns' | 'select' | 'checkbox';
  choices?: string[];
  /** Friendly display for select choices (popover label + one-line
   *  description); each `value` must appear in `choices`. */
  choiceOptions?: { value: string; label: string; description?: string }[];
  columnTypes?: ColumnType[];
  /** Restrict a column picker to model-produced columns. */
  aiGeneratedOnly?: boolean;
  placeholder?: string;
  defaultValue?: string;
  /** Optional local display units for a canonical positive integer value. */
  unit?: ActionParamUnitPresentation;
  required?: boolean;
  /** Hard character cap forwarded to the rendered input's maxLength —
   *  mirror of a backend contract bound, not a UI invention. */
  maxLength?: number;
  hint?: string;
  /** Render with a monospace font (code/regex/templates). */
  monospace?: boolean;
  /** Host-owned server-validator key (e.g. 'python_regex') from the catalog's
   *  `ui_hints.param_validators`. When set, ActionForm debounce-preflights this
   *  field against /actions/v1/validate-params and gates Run on the verdict. */
  validator?: string;
  /** Hide low-frequency controls behind the action form's advanced disclosure. */
  advanced?: boolean;
  /** Group this control (and any others sharing the same string) inside a
   *  named, collapsed `<details>` disclosure labeled with the group name
   *  (e.g. 'Retrieval settings'). Distinct from `advanced`, which files
   *  params into the single generic "Advanced options" disclosure. */
  advancedGroup?: string;
  /** Only render this control when another param has a matching value. */
  visibleWhen?: { param: string; value: string };
  /** Compact-layout hint. 'grid' renders this checkbox as a bare compact
   *  control inside a CSS-grid section (clean_column's Cleanings) rather than a
   *  tall toggle-row card. */
  layout?: 'grid';
  /** Section label for a `layout:'grid'` run — rendered as the grid fieldset's
   *  legend (e.g. 'Cleanings'). */
  group?: string;
  /** Cluster this control with others sharing the same key into one compact
   *  auto-fit CSS grid (`.dense-grid`). Unlike `layout:'grid'` (bare checkboxes
   *  only) this works for any input type. */
  denseGroup?: string;
  /** Grid tracks a dense-grid item occupies. Omitted → inferred from `input`
   *  (checkbox/text/number → 1, select/column pickers → 2). */
  denseSpan?: 1 | 2;
}

interface ActionSourceRequirementBase {
  id: string;
  label: string;
  one_of_group?: string;
  one_of_default?: boolean;
  role?: string;
  min?: number;
  max?: number;
  accepted_column_types?: ColumnType[];
  template_accepted_column_types?: ColumnType[];
  /** Require columns whose current values are generation-managed outputs. */
  ai_generated_only?: boolean;
  accepted_cell_kinds?: string[];
  template_param?: string;
  template_columns?: 'exact' | 'union';
  message?: string;
  next_steps?: string[];
  unset_fallback_includes_ai_generated?: boolean;
  source_union_params?: string[];
  sheet_param?: string;
}

export type ActionSourceRequirement = ActionSourceRequirementBase & (
  | {
      mode: 'column' | 'columns' | 'column_or_template' | 'columns_or_template' | 'template' | 'field' | 'computed';
      param: string;
      column_name?: never;
    }
  | {
      mode: 'fixed_column';
      column_name: string;
      param?: never;
    }
);

/** Where a launcher kind lives in the shared Act navigation model consumed
 *  by both surface densities. `tab` identifies its navigation tab; `group`
 *  is that tab's group caption and `order` sorts items within the (tab,
 *  group) pair. `primary` marks the group's single prominent item (at most
 *  one per group — resolveGroup enforces exactly one).
 *  Populated centrally by ACTION_PLACEMENTS (web/src/actions/model.ts) — an
 *  unplaced built-in routes to Misc/Other with one warning; an unplaced plugin
 *  action retains the generic Plugins fallback. */
export interface ActionPlacement {
  tab: string;
  group: string;
  order: number;
  primary?: boolean;
}

export interface ActionTemplate {
  kind: ActionKind;
  /** Placement in the shared Act navigation model, sourced from
   *  ACTION_PLACEMENTS. Undefined means the built-in Misc/Other fallback or,
   *  for plugin actions, Plugins. */
  placement?: ActionPlacement;
  /** Canonical v1 action kind from /api/actions/v1/catalog when this form is catalog-backed. */
  actionKind?: string;
  /** Catalog-owned authoring contract version. Undefined only on static
   * pre-catalog declarations; every resolved template carries the exact
   * ActionCatalogEntry value. */
  authoringContractVersion?: 1;
  actionTitle?: string;
  actionDescription?: string;
  /** Catalog-native actions use this backend-owned category for default
   * discovery placement and iconography. Product UI may override either. */
  actionCategory?: string;
  /** The drawer renders this action directly from its validated catalog form. */
  generatedAction?: true;
  name: string;
  description: string;
  /** Extra search terms the command-palette ACTIONS/BEST-MATCH sections also
   *  match against `name` (WorkbenchCommandPalette.tsx matchesQuery) — e.g. a
   *  retired display name kept findable after a rename. Never rendered. */
  keywords?: string[];
  defaultPrompt: string;
  defaultFields: OutputField[];
  /** Keep a newly typed output name's display spelling on submit instead of
   *  applying the generic lowercase/underscore normalization. */
  preserveOutputName?: boolean;
  pricing?: ExternalPricingEntry;
  pricingOptions?: Record<string, ExternalPricingEntry>;
  /** Server classification that is deliberately not a price row. */
  costSource?: string;
  costSourceOptions?: Record<string, string>;
  /** Canonical v1 catalog policy: the user must explicitly confirm before run. */
  requiresConfirmation?: boolean;
  /** True when the v1 catalog's cost_policy.kind is "external_metered" — a
   *  zero-platform-cost action that still performs a live external network
   *  call (e.g. research.web_search's provider adapter), as distinct from a
   *  genuinely local/no-network computation. The cost line must not claim
   *  "runs locally" for these, even though the estimate is $0.00. */
  externalMetered?: boolean;
  /** Engine-selectable actions (transcribe/ocr/convert): backend choices
   *  surfaced by the v1 action catalog. */
  engines?: EngineOption[];
  /** Op-specific parameters (item_field, code, pattern, …). */
  params?: ActionParam[];
  /** False for deterministic computed ops: no model picker, $0 cost. */
  llm?: boolean;
  /** What the op writes: a column on this sheet (default) or a new
   *  child sheet (derive/reduce). */
  produces?: 'column' | 'sheet';
  /** Hide the generic prompt textarea (the op's text lives in a param). */
  noPrompt?: boolean;
  /** Prompt textarea still renders but is not required to run — the
   *  primary instruction lives elsewhere (e.g. classify's option labels,
   *  auto-composed by the op) and the prompt is genuinely additional. */
  promptOptional?: boolean;
  /** Hide the output-fields builder (fixed or schema-less outputs). */
  noFields?: boolean;
  /** Typed source requirements surfaced by the v1 action catalog. */
  sourceRequirements?: ActionSourceRequirement[];
  /** Credential names (e.g. "CENSUS_API_KEY") this action needs that are NOT
   *  currently configured for the project — project-aware
   *  (server/action_catalog_hints.py, the same env-var-then-project-secret
   *  resolution the queue-time gate uses). Undefined/empty = runnable.
   *  ActionPanel shows a needs-credential banner + Settings deep link and
   *  blocks Run while this is non-empty. */
  missingCredentials?: string[];
  /** True for catalog actions whose output columns are DECLARED by the action
   *  itself (plugin @plugin.op writes, core recipe output contracts), not
   *  defined by the user. The output-fields builder renders them read-only:
   *  no add/remove/rename, no extraction placeholder copy. Only genuinely
   *  field-defining actions (extract/classify) keep the editable builder. */
  outputFieldsReadOnly?: boolean;
  /** Fail-closed marker: the action's output shape could not be resolved from
   *  its declaration (no declared writes/outputs, and it is not a field-
   *  defining action). ActionForm renders an explicit misconfigured-form error
   *  card naming the kind + what was missing, and blocks Run — it never borrows
   *  the generic/extraction template as a silent bridge. */
  outputShapeError?: { actionKind: string; missing: string };
}

// Engine-declared language capability. The action form renders the matching
// control: nothing (auto_only/fixed) / one Auto-first picker (single) / an
// inline multi-select (multi). OCR/translate consume the identical shape
// later.
export interface LanguageChoice {
  value: string; // ISO-639 code (never 'auto')
  label: string;
}
export interface LanguageDeclaration {
  mode: 'auto_only' | 'fixed' | 'single' | 'multi';
  default: string; // 'auto' or an ISO code
  fixed_language?: string | null;
  choices?: LanguageChoice[] | null; // null for auto_only / fixed
  detects: boolean; // gates the detected_language output column
  // False only for a single-mode engine that REQUIRES an explicit language and
  // cannot auto-detect (Opus-MT: pair-based, no LID). Absent/true elsewhere; the
  // single picker drops its Auto-first option when this is false.
  allows_auto?: boolean;
}
/** One execution target's own facts for a collapsed multi-target
 *  engine (parakeet-tdt on local-onnx/modal; faster_whisper on local/the
 *  models gateway). The engine entry's top-level `available` reflects the
 *  resolver's PREFERRED target; each row here carries one target's probe. */
export interface EngineTargetAvailability {
  target: string; // target family id ('local', 'local-onnx', 'models-gateway', 'modal')
  /** Exact request-composition target identity. `target` retains its durable
   * family spelling for older clients. */
  target_id?: string;
  available: boolean;
  error?: string;
  models?: string[];
  sizes?: string[];
  billable?: boolean;
  pricing?: ExternalPricingEntry;
  diarization?: DiarizationDeclaration;
}

export interface DiarizationDeclaration {
  supported: boolean;
  mode?: 'none' | 'optional' | 'intrinsic';
  /** Engine-specific default when diarization is request-driven. */
  default?: boolean;
  // Released-checkpoint speaker cap (Sortformer on parakeet_modal = 4). Present
  // only when supported; the form warns before a >max request the contract
  // would reject (diarization_speaker_cap).
  max_speakers?: number | null;
  // DECLARATION-DRIVEN speaker-count control (schemas/media.py
  // transcribe_speaker_hint): "none" when the engine's checkpoint takes no
  // count/range input (Sortformer's fixed 4-channel offline pass) — the form
  // renders NO count field. "count" when the engine accepts an
  // exact-or-min/max hint — the form renders it. Present only when
  // `supported` is true. An advisory (non-enforced) count field on a "none"
  // engine would be a one-line backend declaration flip, not a shape change
  // here.
  speaker_hint?: 'none' | 'count';
}

/** Independently declared transcription request knobs. No engine inherits
 * Whisper's controls: an absent declaration fails closed except for the
 * version-skewed built-in roster, whose explicit web fallback is merged by id.
 * `language` answers whether a caller hint is accepted; the richer top-level
 * EngineOption.language declaration still drives the picker and output copy. */
export interface TranscriptionOptionDeclaration {
  language: boolean;
  vad: boolean;
  model_size: boolean;
  /** Hotword/context prompt: a short string of names/jargon the
   * engine biases recognition toward. */
  context: boolean;
  /** Readability-oriented transcript without fillers or false starts. */
  clean?: boolean;
}

// Only a FLAGGED (restricted/gated/custom) engine license carries this —
// permissive engines (Apache-2.0, MIT, ...) attach no `license` field at
// all, so EnginePicker's "render a hint only when present" check stays
// meaningful. Mirrors the backend passthrough shape
// (action_catalog_hints.py `_engine()`'s `license_hint` param). The pull
// manifest carries the SAME license vocabulary at download time — one
// vocabulary, two surfaces (catalog-time disclosure here, pull-time
// disclosure there).
export interface EngineLicense {
  name: string;
  url: string;
  note?: string;
  restricted: boolean;
}

/** Three-tier engine remoteness vocabulary: 'local' = this process/box;
 *  'sidecar' = the operator's frisket-models service, which may be another
 *  machine — never labelled local; 'hosted' = leaves the operator's
 *  infrastructure. */
export type EngineTier = 'local' | 'sidecar' | 'hosted';

export interface EngineOption {
  id: string; // spec.engine value (e.g. 'faster_whisper', 'parakeet', 'remote')
  label: string;
  description?: string;
  version?: string;
  recommended?: boolean;
  tier: EngineTier;
  /** True when the engine bills real money per call/page/token — drives the
   *  allow-remote consent gate and the billable UI badges. */
  billable?: boolean;
  available?: boolean;
  error?: string | null;
  models?: string[];
  pricing?: ExternalPricingEntry;
  /** Exact preferred target when a project composition supplies the option. */
  target_id?: string;
  language?: LanguageDeclaration;
  diarization?: DiarizationDeclaration;
  transcription_options?: TranscriptionOptionDeclaration;
  /** Per-target availability rows for a collapsed multi-target
   *  engine; absent for single-target engines. */
  targets?: EngineTargetAvailability[];
  license?: EngineLicense;
  /** The downloadable Opus-MT pair roster from the pinned manifest, for the
   *  translate form's inline pair picker. Only on the `opus_mt` engine;
   *  `models` carries the already-INSTALLED pairs. */
  downloadable_pairs?: DownloadablePair[];
  /** The single downloadable Hy-MT2 GGUF model artifact, so the form can
   *  honestly surface the ~1.1GB first-run download. Only on the `hy_mt2`
   *  engine. */
  downloadable_model?: DownloadableModel;
  /** Pinned artifacts needed by this engine, pullable through
   *  POST /api/providers/models/pull. Used by Parakeet/faster-whisper and the
   *  runtime-managed spaCy pipeline. */
  downloadable_models?: DownloadableArtifact[];
}

/** A revision-pinned, pullable hf-snapshot artifact from the pinned manifest.
 *  `size` is approximate (snapshot entries pin a revision + file list, not
 *  byte sizes) and may be null. */
export interface DownloadableArtifact {
  ref: string;
  display_name: string;
  revision: string;
  size: number | null;
  license: string;
}

/** A single downloadable model artifact (the Hy-MT2 GGUF). */
export interface DownloadableModel {
  display_name: string;
  size: number;
  license: string;
  installed: boolean;
}

export interface ExternalPricingEntry {
  key: string;
  label: string;
  provider: string;
  unit: string;
  unit_price_usd: number | null;
  unit_price_usd_string?: string | null;
  billable: boolean;
  external_api: boolean;
  cost_source: string;
  description?: string;
  env_var?: string | null;
  /** Opaque downstream commercial-offering metadata; absent on provider-direct
   * catalog entries. */
  terms_version?: string;
  charge_authority?: string;
  venue_label?: string;
  billing_label?: string;
}

export interface ActionCatalogFormParam {
  name: string;
  type?: string;
  label?: string;
  choices?: string[];
  /** Friendly per-choice labels for a category param (value → label). */
  choice_labels?: Record<string, string>;
  default?: string | number | boolean | null;
  required?: boolean;
  column_types?: ColumnType[];
  ai_generated_only?: boolean;
  cardinality?: string;
  description?: string;
  /** Compact-layout hint. 'grid' tags a boolean for the checkbox grid
   *  (clean_column's Cleanings section) instead of a stacked toggle-row. */
  layout?: string;
  /** Optional grouping label carried for future grouped layouts. */
  group?: string;
  /** Row-clustering key for the compact dense-grid layout (any input type).
   *  Passed through untouched from a recipe's `params()` dict. */
  dense_group?: string;
  /** Grid tracks a dense-grid item occupies (1 or 2). */
  dense_span?: 1 | 2;
  /** Only render this control when another param has a matching value. */
  visible_when?: { param: string; value: string };
}

export interface JsonSchemaLike {
  type?: string;
  description?: string;
  properties?: Record<string, JsonSchemaLike>;
  required?: string[];
  items?: JsonSchemaLike;
  enum?: unknown[];
  [key: string]: unknown;
}

export interface ActionCatalogErrorSpec {
  code: string;
  message: string;
}

export interface ActionCatalogPolicy {
  kind?: string;
  supported?: boolean;
  [key: string]: unknown;
}

export interface SheetRowScope {
  sheet_id: number;
  selector:
    | { kind: 'all_rows' }
    | { kind: 'exact_membership'; membership: { row_ids: number[] } };
}

export interface SheetRowScopePolicy {
  kind: 'sheet_rows';
  selectors: Array<'all_rows' | 'exact_membership'>;
}

export interface ProjectScopePolicy {
  kind: 'project';
}

export type ActionScopePolicy = SheetRowScopePolicy | ProjectScopePolicy;

export interface ExportTargetConnectionRequirement {
  provider: string;
  scopes?: string[];
}

export interface ExportTargetHint {
  surface: string;
  label: string;
  destination_kind?: string;
  form?: string;
  source_modes?: string[];
  destination_modes?: string[];
  requires_connection?: ExportTargetConnectionRequirement;
}

export interface ActionCatalogUiHints {
  action_ui?: PluginActionUIBinding;
  typed_action?: { creates_sheet?: boolean; [key: string]: unknown };
  form?: string;
  category?: string;
  primary_fields?: string[];
  uses_model?: boolean;
  form_params?: ActionCatalogFormParam[];
  source_requirements?: ActionSourceRequirement[];
  engines?: EngineOption[];
  pricing?: ExternalPricingEntry;
  pricing_options?: Record<string, ExternalPricingEntry>;
  /** Cost provenance without a tariff (for example, a free public API). */
  cost_source?: string;
  cost_source_options?: Record<string, string>;
  default_output?: {
    name?: string;
    type?: string;
    description?: string;
  };
  export_target?: ExportTargetHint;
  /** Project-aware — set only when `kind`'s required_credentials are
   *  declared AND at least one is unconfigured for the current project
   *  (server/action_catalog_hints.py). */
  missing_credentials?: string[];
  /** Structural edition/composition gate, not a per-project secret (e.g.
   *  export.google_sheets on the local single-user tier, which has no OAuth
   *  flow to connect a Google account at all — server/action_catalog_hints.py
   *  _apply_connected_account_hints). Read directly off the raw catalog by
   *  the one current consumer (components/TopNav.tsx's
   *  ExportGoogleSheetsModal) rather than through ActionTemplate: this kind
   *  isn't reachable through the generic ActionForm launcher at all. */
  unavailable_reason?: string;
  /** Host-owned param-validator declarations: param name -> validator key
   *  (server/param_validation.py). Server-truthful preflight, never JS RegExp. */
  param_validators?: Record<string, string>;
  [key: string]: unknown;
}

export interface ActionCatalogEntry {
  kind: string;
  authoring_contract_version: 1;
  title: string;
  description: string;
  input_schema: JsonSchemaLike;
  output_schema: JsonSchemaLike;
  errors: ActionCatalogErrorSpec[];
  side_effects: string[];
  required_capabilities: string[];
  /** Capabilities added when an assembled canonical param (or its schema
   *  default) matches the server-authored condition. */
  conditional_capabilities?: Array<{
    capability: string;
    when: { param: string; value: string };
  }>;
  row_scope_policy?: ActionScopePolicy | null;
  /** Credential names (env-var style) this action needs to run
   *  (contracts/actions/schemas/_base.py). Empty for nearly every action;
   *  `enrich.census_demographics` is the pinned concrete case. */
  required_credentials: string[];
  cost_policy: ActionCatalogPolicy;
  idempotency: ActionCatalogPolicy;
  retry_policy: ActionCatalogPolicy;
  execution_mode: string;
  async_mode: string;
  writes_project: boolean;
  examples: Array<Record<string, unknown>>;
  ui_hints: ActionCatalogUiHints;
  receipt_policy: 'writes_receipt' | 'deferred_until_receipts_table';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export type GeneratedSemanticControl =
  | 'column'
  | 'columns'
  | 'template'
  | 'rich_source'
  | 'transcript_selection'
  | 'column_or_template'
  | 'engine'
  | 'model';
export type GeneratedActionRequest = RegisteredActionRequest;
export type GeneratedActionDraft = CopilotRegisteredActionDraft;
export interface PluginActionUIBinding {
  plugin_id: string;
  export_name: string;
  package_sha256: string;
}

/** Only a host-admitted typed plugin binding can customize the generated shell. */
export function hasGeneratedActionForm(entry: ActionCatalogEntry): boolean {
  if (entry.ui_hints.form === 'generated') return true;
  const ui = entry.ui_hints.action_ui;
  return isRecord(ui) && typeof ui.plugin_id === 'string'
    && /^[a-z0-9][a-z0-9_.-]*$/.test(ui.plugin_id)
    && entry.kind.startsWith(`${ui.plugin_id}.`)
    && typeof ui.export_name === 'string' && ui.export_name.length > 0
    && ui.export_name === entry.ui_hints.form
    && typeof ui.package_sha256 === 'string' && /^sha256:[a-f0-9]{64}$/.test(ui.package_sha256);
}
export type GeneratedActionCatalogEntry = Omit<ActionCatalogEntry, 'ui_hints'> & {
  ui_hints: ActionCatalogUiHints & {
    form: string;
    action_ui?: PluginActionUIBinding;
    semantic_controls: Record<string, GeneratedSemanticControl>;
    logical_outputs: Array<{
      key: string;
      column_type: string;
      existing_column_policy?: 'generated' | 'compatible';
    }>;
    dynamic_outputs?: true;
  };
};

export function isGeneratedActionCatalogEntry(
  entry: ActionCatalogEntry,
): entry is GeneratedActionCatalogEntry {
  const controls = entry.ui_hints.semantic_controls;
  const outputs = entry.ui_hints.logical_outputs;
  const outputKeys = Array.isArray(outputs)
    ? outputs.flatMap((value) => isRecord(value) && typeof value.key === 'string'
      ? [value.key] : [])
    : [];
  return hasGeneratedActionForm(entry)
    && isRecord(entry.input_schema.properties)
    && isRecord(controls)
    && Object.values(controls).every((value) => (
      ['column', 'columns', 'template', 'rich_source', 'transcript_selection', 'column_or_template', 'engine', 'model'].includes(String(value))
    ))
    && Array.isArray(outputs)
    && outputs.every((value) => isRecord(value)
      && typeof value.key === 'string' && value.key.trim().length > 0
      && value.key === value.key.trim()
      && typeof value.column_type === 'string')
    && new Set(outputKeys).size === outputs.length;
}

export function freshActionRequestKey(actionId: string): string {
  return `web-${actionId}:${globalThis.crypto.randomUUID()}`;
}

export interface ActionCatalogPayload {
  schema_version: 'frisket.action_catalog.v2';
  actions: ActionCatalogEntry[];
  action_schema: JsonSchemaLike;
  error_schema: JsonSchemaLike;
  result_schema: JsonSchemaLike;
  receipt_schema: JsonSchemaLike;
  validation_result_schema: JsonSchemaLike;
}

export interface OAuthConnectionInfo {
  id: string;
  connection_id?: string;
  provider: string;
  external_subject?: string | null;
  external_email?: string | null;
  scopes?: string[];
  token_type?: string | null;
  refresh_token_hint?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  revoked_at?: string | null;
}

export type GoogleSheetsExportSourceKind = 'current_sheet' | 'current_view' | 'all_sheets';
export type GoogleSheetsExportDestinationKind = 'new_spreadsheet' | 'update_existing';

export interface GoogleSheetsExportInput {
  connectionId: string;
  sourceKind: GoogleSheetsExportSourceKind;
  sheetId?: string | null;
  currentView?: SheetViewExportOptions | null;
  destinationKind: GoogleSheetsExportDestinationKind;
  spreadsheetTitle?: string;
  spreadsheetId?: string;
  /** Exact promise-set hash returned by the server for this confirmed retry. */
  confirmation?: string;
}

export interface GoogleSheetsExportResult {
  spreadsheetId: string;
  spreadsheetUrl?: string | null;
  updatedTabs: Array<Record<string, unknown>>;
  receiptId?: string | null;
}

/** A model the picker can offer. Carries no price: the client does not estimate
 *  cost. The panel shows the SERVER's estimate (/actions/v1/estimate) and the
 *  server's 402 is the only thing that decides a run needs confirming. */
export interface ModelOption {
  id: string;
  label: string;
}

/** Local-tier provider catalog for the Braintrust-style model picker
 * (GET /api/providers). Distinct from the hosted-org ProviderCatalog above. */
export interface LocalProviderModel {
  id: string; // e.g. "anthropic/claude-haiku-4-5"
  label: string;
  /** $/M tokens from the live pricing table; null when unknown (never faked). */
  price: { input: number; output: number } | null;
  local?: boolean;
}

export interface PlatformLocalProviderEntry {
  id: string;
  label: string;
  kind: 'platform_api';
  models: LocalProviderModel[];
  configured: boolean;
  source: 'env' | 'local_file' | null;
  hint: string | null;
}

export interface LocalHttpEndpointEntry {
  endpoint_id: string;
  label: string;
  kind: 'local_http';
  read_only: boolean;
  models: LocalProviderModel[];
  reachable: boolean;
  origin: string;
  authority: 'instance' | 'organization';
  source: 'stored' | 'environment';
  detail: string | null;
  installed_models?: string[];
  /** Capability facts, reported separately by the server probe — never
   *  inferred client-side. `ollama_native` means /api/tags answered with its
   *  schema-valid shape (an EMPTY model list still qualifies: a fresh
   *  install is native with nothing pulled). */
  protocol: 'ollama_native' | 'openai_compatible' | 'unknown';
  /** `unauthorized` = the server answered 401/403 (e.g. behind an
   *  auth-checking front door) — a distinct state, not "no models".
   *  `unenforced` = a token is configured but a deliberately tokenless
   *  probe SUCCEEDED: the front door the operator thinks exists is not
   *  actually checking — a loud misconfiguration, not a healthy state. */
  auth_status: 'ok' | 'unauthorized' | 'unenforced' | 'unknown';
  /** Whether the effective local endpoint configuration includes a bearer
   *  token. The token itself is never returned. */
  token_configured: boolean;
  /** Whether model-download requests use a separately configured token. */
  provisioning_token_configured: boolean;
  /** Whether the configured front door is expected to enforce bearer auth. */
  edge_auth: boolean;
  /** In-app pull opt-in. Gates the Download affordance in
   *  LocalServerGuidance and the Settings toggle. */
  pull_enabled: boolean;
}

export type LocalProviderEntry = PlatformLocalProviderEntry | LocalHttpEndpointEntry;

export interface LocalProviderCatalog {
  schemaVersion: string;
  tier: 'local';
  /** Present when a project's effective network policy filtered remote
   *  providers from this otherwise instance-scoped catalog. */
  network?: 'off';
  providers: LocalProviderEntry[];
}

export interface LocalEndpointCatalog {
  schemaVersion: 'frisket.local_endpoints.v1';
  endpoints: LocalHttpEndpointEntry[];
}

export type LocalEndpointDiscoveryResponse = HttpLocalEndpointDiscoveryResponse;
export type LocalEndpointDiscoveryCandidate =
  LocalEndpointDiscoveryResponse['candidates'][number];

/** A single in-app model pull (frisket.model_pull.v3). Field names match the
 *  wire shape as-is — no camelCase translation layer, since this DTO is
 *  only ever read back (never built client-side into a request body).
 *  Ollama reports byte progress per layer digest, so
 *  `total_bytes`/`completed_bytes` are the aggregate across known digests
 *  and stay `null` until the first layer starts streaming (render an
 *  indeterminate bar until then). */
export interface ModelPullDto {
  /** Wire version carrying immutable local-endpoint provenance. */
  schemaVersion: 'frisket.model_pull.v3';
  id: number;
  model: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled' | 'uninstalled';
  phase: string | null;
  total_bytes: number | null;
  completed_bytes: number | null;
  error: { code: string | null; message: string | null } | null;
  resolved_digest: string | null;
  resolved_size: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested: boolean;
  /** Stable endpoint identity for local pulls; null for artifact-only pulls. */
  endpoint_id: string | null;
  /** Credential-free origin used for an Ollama pull; null for artifact-only
   *  pulls. */
  endpoint_origin: string | null;
  /** Hosted actor provenance where applicable; null for local pulls. */
  initiated_by: string | null;
  /** Pinned-artifact provenance for an opus-mt:/hf: pull
   *  (frisket.model_pull.v3); `null` for a local-server pull.
   *  `license`/`manifest_version` are null for a free-form unpinned hf pull. */
  artifact: {
    kind: string;
    source_url: string | null;
    license: string | null;
    manifest_version: string | null;
  } | null;
}

/** A downloadable Opus-MT language pair from the pinned manifest, advertised
 *  on the translate `opus_mt` engine's `downloadable_pairs` for the form's
 *  inline pair picker. */
export interface DownloadablePair {
  pair: string;
  display_name: string;
  size: number;
  license: string;
}

/** POST /providers/models/pull response (202). `deduplicated` is true when
 *  the request joined an already-running pull for the same model instead of
 *  starting a new one (atomic active-operation uniqueness). */
export interface ModelPullStartResult {
  pull: ModelPullDto;
  deduplicated: boolean;
}

export interface ProviderValidateResult {
  provider?: string;
  ok: boolean;
  reachable: boolean;
  status: number | null;
  detail?: string | null;
  validation_token?: string | null;
}

/** The composite Derive launcher is the only browser action that intentionally
 * dispatches more than one catalog action. Keep its authored routing typed so
 * column names never pass through a comma/newline encoded scalar bag. */
export interface DeriveCompositeRequest {
  intent: 'derive_from_extraction';
  itemField: string;
  sheet_name: string;
  /** Exact first step, authored and resolved by the generated extraction form. */
  extraction: RegisteredActionRequest;
  confirmation?: string;
  replace_existing?: boolean;
}

export function isDeriveCompositeRequest(
  request: ActionExecutionRequest,
): request is DeriveCompositeRequest {
  return !('action_id' in request)
    && 'intent' in request && request.intent === 'derive_from_extraction';
}

/** The methods supported by the generated clustering form. */
export type ClusterValuesMethod = 'fingerprint' | 'ngram_fingerprint' | 'semantic';

export type RegisteredSheetRowsScope = Record<string, JsonValue> & {
  kind: 'sheet_rows';
  sheet_id: number;
  row_ids?: number[];
};

export type RegisteredProjectScope = Record<string, JsonValue> & {
  kind: 'project';
};

export type RegisteredActionScope = RegisteredSheetRowsScope | RegisteredProjectScope;

/** Shared catalog-authored action draft. Execution adds only retry identity. */
export interface RegisteredActionDraft<
  ParamValue = JsonValue,
  Scope extends RegisteredActionScope = RegisteredActionScope,
> extends Record<string, unknown> {
  action_id: string;
  scope: Scope;
  params: Record<string, ParamValue>;
  output_names: Record<string, string>;
  /** Destination name for an action that creates one new sheet. */
  sheet_name?: string;
}

/** A catalog-registered action request is already the server's execution
 * contract. It must cross the browser transport unchanged. */
export interface RegisteredActionRequest extends RegisteredActionDraft {
  idempotency_key: string;
  confirmation?: string;
  /** One-run consent to replace compatible generated output columns. Saved
   * action drafts deliberately never carry this destructive invocation fact. */
  replace_existing?: boolean;
}

function hasRegisteredActionDraftShape(candidate: unknown): candidate is RegisteredActionDraft {
  if (!isRecord(candidate) || !isRecord(candidate.scope)) return false;
  const scope = candidate.scope;
  const validScope = scope.kind === 'project'
    || (
      scope.kind === 'sheet_rows'
      && typeof scope.sheet_id === 'number'
      && (scope.row_ids === undefined || Array.isArray(scope.row_ids))
    );
  return typeof candidate.action_id === 'string'
    && validScope
    && isRecord(candidate.params)
    && isRecord(candidate.output_names);
}

export type ActionExecutionRequest = RegisteredActionRequest | DeriveCompositeRequest;

export function isRegisteredActionRequest(
  request: ActionExecutionRequest,
): request is RegisteredActionRequest {
  return hasRegisteredActionDraftShape(request)
    && typeof request.idempotency_key === 'string';
}

export function actionExecutionId(request: ActionExecutionRequest): string {
  if (isDeriveCompositeRequest(request)) return 'derive.table_from_list';
  return request.action_id;
}

export function actionExecutionSheetId(request: ActionExecutionRequest): string {
  if (isDeriveCompositeRequest(request)) return actionExecutionSheetId(request.extraction);
  return request.scope.kind === 'sheet_rows' ? String(request.scope.sheet_id) : '';
}

export function actionExecutionRowIds(request: ActionExecutionRequest): string[] | undefined {
  if (isDeriveCompositeRequest(request)) return actionExecutionRowIds(request.extraction);
  return request.scope.kind === 'sheet_rows' ? request.scope.row_ids?.map(String) : undefined;
}

export function actionExecutionPrimaryOutput(request: ActionExecutionRequest): string {
  if (isDeriveCompositeRequest(request)) return request.extraction.output_names[request.itemField] ?? request.itemField;
  return Object.values(request.output_names)[0] ?? '';
}

export function actionExecutionName(request: ActionExecutionRequest): string {
  if (isDeriveCompositeRequest(request)) return 'Derive rows';
  return request.action_id;
}

/** Browser-local invocation controls for one project-bound request. Neither
 * field is serialized into a request body. */
export interface ProjectInvocationOptions {
  /** Immutable project identity captured before the launch's first await. */
  readonly projectId?: string;
  /** Cancels subsequent reads/effects and retired frontend publication. */
  readonly signal?: AbortSignal;
}

/** Action-launch name for the shared project invocation controls. */
export type RunActionInvocationOptions = ProjectInvocationOptions;

/** The 202 body of POST .../actions/v1/preview: the in-memory sample job's id
 *  plus the total rows it will compute (the sample size, capped server-side). */
export interface PreviewStartResult {
  previewId: string;
  total: number | null;
}

export type PreviewStatus = 'running' | 'done' | 'error' | 'cancelled';

/** A virtual output column the sample produced. `overwritesColumnId` is set when
 *  the output name matches an existing column (the overlay replaces that column's
 *  sampled cells in place); null means a brand-new trailing preview column. */
export interface PreviewOverlayColumn {
  name: string;
  columnType: string;
  format: string | null;
  hidden: boolean;
  overwritesColumnId: string | null;
}

/** One sampled cell, mirroring the runner's per-field cell shape. A per-cell
 *  error is surfaced so the overlay can render it like a real error cell. */
export interface PreviewOverlayCell {
  value: CellValue;
  error?: string | null;
  confidence?: number | null;
  justification?: string | null;
  outcome?: string | null;
}

/** The sampled cell selected for read-only inspection, never a committed value. */
export interface PreviewCellDetail {
  column: PreviewOverlayColumn;
  cell: PreviewOverlayCell;
}

/** GET .../actions/v1/preview/{id}: the polled job status and, once done, the
 *  in-memory sample (never persisted — the frontend overlays it on the grid). */
interface PreviewSampleCommon {
  accounting?: HttpActionPreviewStatusResponse['accounting'];
  previewId: string;
  status: PreviewStatus;
  progress: { done: number; total: number | null };
  columns: PreviewOverlayColumn[];
  sampled: number;
  total: number | null;
  error: { code: string; message: string } | null;
}

export interface PreviewRowSampleResult extends PreviewSampleCommon {
  kind: 'row_overlay';
  sheetId: string | null;
  /** rowId (string) -> output field name -> sampled cell. */
  rows: Record<string, Record<string, PreviewOverlayCell>>;
  rowIds: number[];
}

export interface PreviewTableSampleResult extends PreviewSampleCommon {
  kind: 'table';
  rows: Record<string, PreviewOverlayCell>[];
  warnings: string[];
}

export type PreviewSampleResult = PreviewRowSampleResult | PreviewTableSampleResult;

/** One R3a claims-gate line: a user claim the run makes (egress/cost trust
 *  label copy rendered server-side). Additive — pre-claims servers never send
 *  these. */
export interface RunEstimateClaim {
  field: string;
  display: string;
}

export interface RunEstimate {
  /** The PROVIDER's cost, in USD. This field means that and only that,
   *  permanently: a deployment that bills cost-plus reports its marked-up
   *  figure in `billed_cost` below, never here. */
  cost: number | null;
  rows: number;
  /** Stable provider/list-price identity bound into server-side consent. */
  pricing_key?: string;
  /** Selected execution engine bound into server-side consent. */
  engine?: string;
  /** Remote effect class the approval covers. */
  remote_capability?: string;
  requires_confirmation?: boolean;
  /** What this deployment will BILL, in integer micro-dollars, as rated by
   *  its pricing policy (server: `execution/pricing_policy.py`). Present and
   *  non-null is the figure the user is agreeing to pay, so the modal shows
   *  THAT.
   *
   *  `null` alongside a `policy_id` means the policy DECLINED to price this
   *  run — an honest cannot-say, which renders UNKNOWN and demands the typed
   *  confirm. An absent field is an incomplete old-shape verdict and also
   *  renders UNKNOWN: every current quote producer rates before crossing this
   *  boundary, so provider `cost` is never a fallback. Read the two together,
   *  never `billed_cost ?? cost`.
   *
   *  Micro-dollars, not USD, because it is money the server computed and the
   *  client must not re-round: divide by 1e6 only to display. */
  billed_cost?: number | null;
  /** Identity of the pricing policy that produced `billed_cost` — part of the
   *  consent hash server-side, so a tariff change invalidates old approvals.
   *  Its PRESENCE plus `billed_cost`'s presence distinguishes an explicit
   *  Unpriceable verdict from an incomplete/unrated envelope. */
  policy_id?: string;
  avg_input_tokens?: number;
  /** R3a claims gate (additive): claim lines to render in the cost gate. */
  claims?: RunEstimateClaim[];
  /** R3a claims gate (additive): the promise-set hash the confirm retry must
   *  echo back as params.consented_promise_set_hash. */
  promise_set_hash?: string;
  /** Seconds of audio the estimate is quoted over. Present for transcription,
   *  which is priced per audio second rather than per row — the quantity the
   *  cost line names so a sub-cent figure is readable as a rate, not a guess. */
  audio_seconds?: number;
  /** Request-scoped presentation from the selected execution offering. The
   * client carries this copy; it never infers a billing venue from funding. */
  billing_label?: string;
  venue_label?: string;
  /** Provenance classification from the server's selected cost fact. This is
   * presentation input, not a client-side pricing rule: in particular,
   * `free_public_api` lets the panel say that no provider/API charge exists
   * without inventing a dollar price for a free public service. */
  cost_source?: string;
  /** Why the server could not price the run, in the server's own words (e.g.
   *  missing duration metadata). Surfaced instead of a bare "UNKNOWN". */
  warning?: string;
}

/** One param's server verdict from /actions/v1/validate-params. `position` is
 *  a 0-based offset into the submitted value when the validator can point at
 *  the error (regex compile errors carry it). */
export interface ParamDiagnostic {
  ok: boolean;
  message?: string;
  position?: number;
}

/** Server diagnostics keyed by the top-level param name. Typed Pydantic model
 *  errors that do not belong to one field use `__all__`; legacy actions still
 *  return only diagnostics for explicitly declared validators. */
export type ParamValidationResult = Record<string, ParamDiagnostic>;

export interface ActionParamResolution {
  diagnostics: ParamValidationResult;
  logical_outputs: Array<{
    key: string;
    column_type: string;
    existing_column_policy?: 'generated' | 'compatible';
  }>;
  /** Actual prepared materialization, when it depends on semantic Params. */
  creates_sheet?: boolean;
}

export interface RunActionLaunchResult {
  runId: string | null;
  jobId?: number | null;
  receiptId?: string | null;
  status?: string;
  /** A sheet materialized synchronously by this action, when present. */
  outputSheetId?: string | null;
}

export type GridFilterOperator =
  | 'eq'
  | 'in'
  | 'neq'
  | 'contains'
  | 'gte'
  | 'lte'
  | 'between'
  | 'date_relative'
  | 'date_this_year'
  | 'date_ytd'
  | 'date_year'
  | 'date_month'
  | 'date_weekday'
  | 'date_invalid'
  | 'bbox'
  | 'failed'
  | 'entity_eq'
  /** At least one top-level member of a JSON list matches one selector. */
  | 'list_contains_any';

export interface GridFilterRangeValue {
  start: string;
  end: string;
}

export interface GridFilterRelativeDateValue {
  amount: number;
  unit: 'days' | 'weeks' | 'months';
}

/** Geographic bounding-box filter on a geo_point column (map "filter to
 *  viewport"). Set programmatically by the map, not via the manual filter UI. */
export interface GridFilterBboxValue {
  min_lon: number;
  min_lat: number;
  max_lon: number;
  max_lat: number;
}

/** Structured `entity_eq` filter payload on a marked entity-mentions JSON
 *  column. Exactly one of three closed shapes:
 *  `{type}` (every mention of a canonical type), `{type, text}` (one exact
 *  raw spelling, case-sensitive), or `{type, fingerprint}` (every raw
 *  spelling that shares one stored fingerprint — the "grouped forms" filter).
 *  Never both `text` and `fingerprint`, never any other key. Set
 *  PROGRAMMATICALLY by the Mentions panel; the generic scalar filter editor
 *  can neither author nor edit it. */
export interface GridFilterEntityValue {
  type: string;
  text?: string;
  fingerprint?: string;
}

/** A server-authored selector for one member of a list-valued JSON cell.
 *
 * This deliberately has no generic object form.  Ordinary extracted lists can
 * facet scalar members, while entity-mentions columns carry the one object
 * shape whose comparison semantics Frisket knows. */
export type GridFilterListSelector =
  | { kind: 'scalar'; value: string | number | boolean }
  | { kind: 'entity'; type: string; text: string };

export type GridFilterValue =
  | string
  | string[]
  | GridFilterRangeValue
  | GridFilterRelativeDateValue
  | GridFilterBboxValue
  | GridFilterEntityValue
  | GridFilterListSelector[];

export type GridFilterSpec = Record<string, Partial<Record<GridFilterOperator, GridFilterValue>>>;

export type GridSortDirection = 'asc' | 'desc';

export interface GridSortRule {
  column: string;
  dir: GridSortDirection;
}

export type GridSortSpec = GridSortRule[];

export interface SheetDataOptions {
  parentRowId?: string | null;
  filter?: GridFilterSpec | null;
  sort?: GridSortSpec | null;
  /** Lens-view scope: page EXACTLY these row ids, in THIS ranked order
   *  (distance/score order from the resolver). When set, the grid ignores
   *  column filter/sort — the lens already defines the row-set + order. */
  rowIds?: number[] | null;
}

export type SheetViewExportOptions = Pick<SheetDataOptions, 'filter' | 'sort'>;

export interface SheetDatasetExportOptions {
  format: 'csv' | 'xlsx';
  sheetIds: string[];
  /** Current-view filter/sort is valid only for one selected sheet. */
  currentView?: SheetViewExportOptions | null;
}

/** Options for the binary map-points endpoint (deck.gl map view). bbox is
 *  [minLon, minLat, maxLon, maxLat]; filter/sort reuse the grid semantics. */
export interface MapPointsOptions {
  bbox?: [number, number, number, number] | null;
  filter?: GridFilterSpec | null;
  sort?: GridSortSpec | null;
  /** Column ids whose live values ride along as `attr:<id>` columns for
   *  color/size-by-column. Resolved read-only at query time. */
  attrs?: string[];
}

/** Decoded map-points payload. `positions` is interleaved lon,lat float32 fed
 *  straight into deck.gl binary attributes; `rowIds[i]` is the stable row id for
 *  point i (used to open the row drawer on pick). */
export interface MapPointsResult {
  count: number;
  positions: Float32Array;
  rowIds: string[];
  /** Extra column values for color/size-by-column, keyed by column id; each
   *  array is index-aligned with the points. */
  attributes: Record<string, Array<number | string | null>>;
  generation: string;
  transient: boolean;
  validPoints: number;
  schema: string;
  backend: string;
}

export type RuntimeProjectionBuildMode = 'refresh' | 'rebuild';

export interface RuntimeProjectionArtifactRef {
  kind: 'projection_artifact' | string;
  projectionKind: string;
  artifactId: string;
  [key: string]: unknown;
}

export interface RuntimeProjectionTarget {
  sheetId: string;
  dateColumnId?: string;
  titleColumnId?: string;
  caseColumnId?: string;
  [key: string]: unknown;
}

export interface RuntimeProjectionRequestBase {
  projectionKind: string;
  target: RuntimeProjectionTarget;
  params?: Record<string, unknown>;
}

export type RuntimeProjectionStatusRequest = RuntimeProjectionRequestBase;

export interface RuntimeProjectionBuildRequest extends RuntimeProjectionRequestBase {
  mode: RuntimeProjectionBuildMode;
}

export interface RuntimeProjectionArtifactRequest extends RuntimeProjectionRequestBase {
  artifactId: string;
}

export interface RuntimeProjectionStatus {
  schemaVersion: 'frisket.runtime_projection_status.v1';
  status: 'ready' | 'stale' | 'missing' | 'building' | 'failed' | string;
  freshness: {
    state: 'fresh' | 'stale' | 'missing' | 'transient' | 'failed' | string;
    generation?: string | null;
    transient?: boolean;
    [key: string]: unknown;
  };
  outputs: {
    artifactRefs: RuntimeProjectionArtifactRef[];
    metrics?: Record<string, unknown>;
    [key: string]: unknown;
  };
  warnings?: string[];
  [key: string]: unknown;
}

export interface RuntimeProjectionBuildPlan {
  schemaVersion: 'frisket.runtime_projection_build_plan.v1';
  status: 'accepted' | 'noop' | 'failed' | string;
  build: {
    operation: 'refresh' | 'rebuild' | 'noop' | string;
    idempotencyKey: string;
    [key: string]: unknown;
  };
  outputs: {
    artifactRefs: RuntimeProjectionArtifactRef[];
    metrics?: Record<string, unknown>;
    [key: string]: unknown;
  };
  warnings?: string[];
  [key: string]: unknown;
}

export interface TimelineProjectionItem {
  sourceRowId: string | number;
  date: string;
  title: string;
  caseId?: string | null;
  [key: string]: unknown;
}

export interface TimelineProjectionArtifact {
  schemaVersion: 'frisket.timeline_projection_artifact.v1';
  projectionKind: string;
  artifactId: string;
  generation: string;
  target: RuntimeProjectionTarget;
  params: Record<string, unknown>;
  columns: {
    dateColumnId: string;
    titleColumnId: string;
    caseColumnId?: string | null;
  };
  metrics: {
    sourceRowCount: number;
    timelineItemCount: number;
    [key: string]: unknown;
  };
  items: TimelineProjectionItem[];
  [key: string]: unknown;
}

export interface GraphRowRef {
  sheet_id: number;
  row_id: number;
}

// --- Generic sheet-graph view -----------------------------------------------
// GET /projects/:pid/sheets/:sheetId/graph — derived from the materialized
// edge/join substrate, NOT the FtM neighborhood service.

export interface SheetGraphNode {
  id: string;
  label: string;
  sheet_id: number;
  row_id: number;
  row_ref: GraphRowRef;
  degree: number;
  color_value?: unknown;
  size_value?: unknown;
}

export interface SheetGraphEdge {
  id: string;
  source: string;
  target: string;
  direction: 'directed' | 'undirected';
  label: string;
  sheet_id: number;
  row_id: number;
  row_ref: GraphRowRef;
}

export interface SheetGraphResult {
  schema_version: 'frisket.sheet_graph.v1' | string;
  sheet_id: number;
  materialized_kind: 'edge' | 'join' | null;
  direction: 'directed' | 'undirected';
  nodes: SheetGraphNode[];
  edges: SheetGraphEdge[];
  truncated: boolean;
  limits: {
    nodes: number;
    edges: number;
    returned_nodes: number;
    returned_edges: number;
  };
  diagnostics: Array<{ code: string; message: string; [key: string]: unknown }>;
}

export interface SheetGraphOptions {
  sheetId: number | string;
  direction?: 'directed' | 'undirected';
  nodeLabelColumnId?: number | string | null;
  nodeColorColumnId?: number | string | null;
  nodeSizeColumnId?: number | string | null;
  edgeLabelColumnId?: number | string | null;
  limitNodes?: number;
  limitEdges?: number;
}

export interface OcrComparePreviewBlock {
  text: string;
  bbox?: Record<string, unknown>;
  score?: number;
  raw?: Record<string, unknown>;
}

export interface OcrComparePreviewPageResult {
  page: number;
  engine: string;
  text: string;
  blocks: OcrComparePreviewBlock[];
  warnings: string[];
  errors: string[];
  runtime_ms: number | null;
  [key: string]: unknown;
}

export interface OcrComparePreviewMessage {
  code?: string;
  engine?: string;
  message?: string;
  details?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface OcrCompareScratchInput {
  pages: number[];
  engine: string;
  language?: string | null;
  dpi?: number;
  searchable_pdf?: boolean;
  confirmation?: string;
}

// Transcription Compare bake-off — the transcribe/compare-scratch shape.
// Each engine result carries the joined transcript plus the timestamped
// segments the diff aligns on.
export interface TranscribeCompareSegment {
  segment_index: number;
  start: number;
  end: number;
  text: string;
  speaker?: string;
  speaker_confidence?: string;
  words?: Array<{ word: string; start: number; end: number }>;
}

export interface TranscribeCompareEngineResult {
  engine: string;
  text: string;
  segments: TranscribeCompareSegment[];
  detected_language: string | null;
  runtime_ms: number | null;
  warnings?: string[];
  errors?: string[];
  [key: string]: unknown;
}

export interface TranscribeCompareScratchInput {
  engine: string;
  language?: string | null;
  model_size?: string | null;
  /** Voice-activity detection (Silero). Omitted means the selected engine did
   * not declare this knob; the client must not synthesize true in that case. */
  vad?: boolean;
  diarize?: boolean;
  time_limit_seconds?: number;
  confirmation?: string;
}

// Topic-segmentation Compare — an ephemeral TXT/SRT/VTT bake-off.  Unlike
// Transcribe Compare, the uploaded file is already a transcript and each
// result describes boundaries/sections over the immutable source-unit order.
export type TopicSegmentationDetail = 'fewer' | 'balanced' | 'more';

export interface TopicSegmentationCompareVariantInput {
  id: string;
  engine: string;
  settings: { detail: TopicSegmentationDetail };
}

export interface TopicSegmentationCompareScratchInput {
  variants: TopicSegmentationCompareVariantInput[];
  language?: string | null;
}

export interface TopicSegmentationCompareUnit {
  id: string;
  ordinal: number;
  text: string;
  speaker: string | null;
  start_ms: number | null;
  end_ms: number | null;
}

export interface TopicSegmentationCompareBoundary {
  id: string;
  locator: Record<string, unknown>;
  canonical_key: number;
  locking_kind: 'point' | 'span';
  strength: number | null;
  label: string | null;
  diagnostics: Record<string, unknown>;
}

export interface TopicSegmentationCompareCanonicalBoundary {
  /** Ordinal of the source unit where the following section begins. */
  key: number;
  /** Presentation detail only; boundary identity is `key`. */
  kind: 'point' | 'span';
  candidate_ids: string[];
}

export interface TopicSegmentationCompareSection {
  index: number;
  unit_ids: string[];
}

export interface TopicSegmentationCompareEngineResult {
  variant_id: string;
  engine: string;
  engine_version: string | null;
  settings: { detail?: TopicSegmentationDetail; [key: string]: unknown };
  status: 'completed' | 'failed';
  runtime_ms: number;
  boundaries: TopicSegmentationCompareBoundary[];
  canonical_boundaries: TopicSegmentationCompareCanonicalBoundary[];
  sections: TopicSegmentationCompareSection[];
  unit_membership: Array<{ unit_id: string; section_indexes: number[] }>;
  diagnostics: Record<string, unknown>;
  warnings: OcrComparePreviewMessage[];
  errors: OcrComparePreviewMessage[];
}

export interface TopicSegmentationCompareScratchResult {
  schema_version: 'frisket.topic_segmentation_compare.v1' | string;
  source: {
    scratch: true;
    filename: string;
    mime: string | null;
    size: number;
    source_kind: 'untimed_transcript' | 'timestamped_transcript';
    snapshot_hash: string;
    language: string | null;
  };
  engines: EngineOption[];
  units: TopicSegmentationCompareUnit[];
  results: TopicSegmentationCompareEngineResult[];
  warnings: OcrComparePreviewMessage[];
  errors: OcrComparePreviewMessage[];
}

// Translate compare: a text-source bake-off — one translation per engine
// over a pasted sample. No cost object (the backend records per-row spend on
// the durable action; the bake-off is a preview).
export interface TranslateCompareEngineResult {
  engine: string;
  translation: string;
  detected_language: string | null;
  runtime_ms: number | null;
  errors?: string[];
  [key: string]: unknown;
}

export interface TranslateCompareScratchInput {
  engines: string[];
  text: string;
  targetLanguage: string;
  /** Single source-language hint ('' / 'auto' = auto-detect). */
  language?: string | null;
}

export interface TranslateCompareScratchResult {
  schema_version: 'frisket.translate_compare_preview.v1' | string;
  source: { scratch: true; text_length: number };
  target_language: string;
  engines: string[];
  results: TranslateCompareEngineResult[];
  warnings: OcrComparePreviewMessage[];
  errors: OcrComparePreviewMessage[];
}

export interface SheetDataPage {
  rows: Row[];
  total: number;
  /** The resulting sheet's columns (incl.
   * transcriptStatus/mediaDownloadCandidate) — the same payload getSheetData
   * already fetches internally, exposed to callers that need the full column
   * list right after a write (e.g. ImportCsv.tsx's finishImport classifying
   * a just-imported sheet) without a second round trip. */
  columns: ColumnDef[];
}

export interface SheetRowLocation {
  found: boolean;
  rowId: string;
  index: number | null;
  pageOffset: number | null;
  pageSize: number;
}

export interface SavedView {
  id: number;
  name: string;
  /** Saved Views are structurally scoped to one sheet. */
  sheet_id: number;
  /** The persisted definition always carries an explicit filter object. */
  spec: Record<string, unknown> & { filter: Record<string, unknown> };
  op_id: number | null;
}

export interface SavedViewColumnGroup {
  run_id: number;
  label: string;
  columns: string[];
  show_confidence: boolean;
  show_justification: boolean;
}

/** The complete mutable definition of a Saved View.  It deliberately omits
 * its immutable identity, name, and sheet ownership. */
export interface SavedViewDefinitionInput {
  filter: GridFilterSpec;
  sort: GridSortSpec | null;
  columns: string[] | null;
  column_groups: SavedViewColumnGroup[] | null;
}

/** Closed create payload: a name, fixed sheet, and complete definition. */
export interface SavedViewCreateInput extends SavedViewDefinitionInput {
  name: string;
  sheetId: string | number;
}

/** Closed rename payload. Definition fields cannot cross this mutation. */
export interface SavedViewRenameInput {
  name: string;
}

/** Closed definition-replacement payload. Name and sheet cannot move here. */
export type SavedViewDefinitionReplaceInput = SavedViewDefinitionInput;

export type WatchScopeKind = 'project' | 'sheet';

interface WatchCreateBase {
  name: string;
  enabled?: boolean;
}

/** A direct Watch owns its snapshot query at creation time. */
export interface DirectWatchInput extends WatchCreateBase {
  scope?: { kind: WatchScopeKind; sheet_id?: number | string | null };
  query: Record<string, unknown>;
}

export type WatchInput = DirectWatchInput;

/** The only mutable Watch fields. Query and binding authority are fixed when
 * the Watch is created, so lifecycle PATCHes cannot carry either field. */
export interface WatchPatchInput {
  name?: string;
  enabled?: boolean;
}

export interface WatchHit {
  runId?: number;
  sheetId: number;
  rowId: number;
  columnId: number | null;
  rank: number;
  snippet: string | null;
  isNew: boolean;
}

export interface WatchRun {
  id: number;
  watchId: number;
  status: string;
  opCursorBefore: number;
  opCursorAfter: number;
  matchedRows: number;
  newRows: number;
  error: string | null;
  errorCode: string | null;
  resolvedQueryHash: string | null;
  resolvedQuery: Record<string, unknown>;
  startedAt: string;
  finishedAt: string | null;
  hits?: WatchHit[];
}

export interface WatchInfo {
  id: number;
  name: string;
  scope: WatchScopeKind | string;
  sheetId: number | null;
  query: Record<string, unknown>;
  queryVersion: string | null;
  queryHash: string | null;
  enabled: boolean;
  lastEvaluatedOp: number;
  lastRunId: number | null;
  lastStatus: string | null;
  createdAt: string;
  updatedAt: string;
  latestRun: WatchRun | null;
}

export interface WatchRunResult {
  schemaVersion: 'frisket.watch_run.v1';
  watch: WatchInfo;
  run: WatchRun;
  hits: WatchHit[];
}

export interface WatchRunsPage extends PageMeta {
  schemaVersion: 'frisket.watch_runs_page.v1';
  order: 'desc';
  runs: WatchRun[];
}

export interface WatchRunEvent {
  id: number;
  runId: number;
  watchId: number;
  eventKind: string;
  subjectKind: string;
  subjectRef: Record<string, unknown>;
  beforeJson: Record<string, unknown> | null;
  afterJson: Record<string, unknown> | null;
  deltaJson: Record<string, unknown> | null;
  severity: string;
  rank: number;
  snippet: string | null;
  createdAt: string;
}

export interface WatchRunEventsPage extends PageMeta {
  schemaVersion: 'frisket.watch_run_events_page.v1';
  order: 'asc';
  events: WatchRunEvent[];
}

export type NotificationState = 'unseen' | 'seen' | 'read' | 'acknowledged';
export type NotificationSeverity = 'info' | 'warning' | 'critical' | string;

export interface NotificationItem {
  id: number;
  sourceKind: string;
  sourceRef: Record<string, unknown>;
  sourceEventIds: number[];
  eventCount: number;
  eventKinds: string[];
  title: string;
  summary: string;
  severity: NotificationSeverity;
  deepLink: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  state: NotificationState;
  seenAt: string | null;
  readAt: string | null;
  acknowledgedAt: string | null;
  acknowledgedBy: string | null;
}

export interface NotificationPage extends PageMeta {
  schemaVersion: 'frisket.notifications_page.v1';
  order: 'desc';
  notifications: NotificationItem[];
}

export interface NotificationSummarySourceRef {
  sourceKind: string;
  sourceRef: Record<string, unknown>;
  unseen: number;
}

export interface NotificationSummary {
  schemaVersion: 'frisket.notifications_summary.v1';
  total: number;
  unseen: number;
  seen: number;
  read: number;
  acknowledged: number;
  bySeverity: Record<string, number>;
  bySourceKind: Record<string, number>;
  bySourceRef: NotificationSummarySourceRef[];
}

export interface NotificationActorState {
  notificationId: number;
  actorId: string;
  state: NotificationState;
  seenAt: string | null;
  readAt: string | null;
  acknowledgedAt: string | null;
  acknowledgedBy: string | null;
  updatedAt: string;
}

export interface NotificationListParams {
  state?: NotificationState | 'all';
  sourceKind?: string;
  sourceRef?: Record<string, unknown>;
  severity?: string;
  offset?: number;
  limit?: number;
}

export interface NotificationStateFilter {
  notificationIds?: number[];
  sourceKind?: string;
  sourceRef?: Record<string, unknown>;
  beforeCreatedAt?: string;
}

export type NotificationChannelKind = 'in_app' | 'email' | 'slack' | 'webhook';
export type NotificationDeliveryMode = 'immediate' | 'digest';
export type NotificationDigestCadence = 'hourly' | 'daily' | 'manual';
export type NotificationDeliveryStatus = 'queued' | 'processing' | 'sent' | 'failed' | 'skipped' | 'cancelled' | 'reconciliation_required';

export interface NotificationChannel {
  id: number;
  kind: NotificationChannelKind;
  name: string;
  enabled: boolean;
  ownerKind: 'project' | 'user' | string;
  config: Record<string, unknown>;
  hasSecret: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface NotificationChannelsPage {
  schemaVersion: 'frisket.notification_channels.v1';
  channels: NotificationChannel[];
}

export interface NotificationChannelInput {
  kind: Exclude<NotificationChannelKind, 'in_app'>;
  name: string;
  ownerKind?: string;
  enabled?: boolean;
  to?: string;
  from?: string;
  channelLabel?: string;
  webhookHost?: string;
  webhookUrlSecretRef?: string;
  signingSecretRef?: string;
  signatureHeader?: string;
  maxRequestBytes?: number;
  maxResponseBytes?: number;
  maxRedirects?: number;
  secretRef?: string;
  webhookSecretRef?: string;
}

export interface NotificationRoute {
  id: number;
  name: string;
  enabled: boolean;
  ownerKind: 'project' | 'system' | string;
  ownerRef: string;
  recipientActorId: string | null;
  channelId: number;
  sourceKind: string | null;
  sourceRefMatch: Record<string, unknown>;
  eventKinds: string[];
  severityMin: string;
  deliveryMode: NotificationDeliveryMode;
  digestCadence: NotificationDigestCadence | null;
  digestTimezone: string;
  digestAnchorTime: string | null;
  templateKey: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface NotificationRoutesPage {
  schemaVersion: 'frisket.notification_routes.v1';
  routes: NotificationRoute[];
}

export interface NotificationRouteInput {
  name: string;
  ownerKind?: string;
  enabled?: boolean;
  channelId: number;
  sourceKind?: string | null;
  sourceRefMatch?: Record<string, unknown>;
  eventKinds?: string[];
  severityMin?: string;
  deliveryMode?: NotificationDeliveryMode;
  digestCadence?: NotificationDigestCadence | null;
  digestTimezone?: string;
  digestAnchorTime?: string | null;
}

export interface NotificationDeliveryRequest {
  id: number;
  routeId: number | null;
  channelId: number;
  notificationId: number | null;
  digestRunId: number | null;
  deliveryKind: string;
  dedupeKey: string;
  status: NotificationDeliveryStatus;
  jobId: number | null;
  availableAt: string;
  lastError: string | null;
  providerRef: string | null;
  createdAt: string;
  updatedAt: string;
  sentAt: string | null;
}

export interface NotificationDeliveryRequestsPage extends PageMeta {
  schemaVersion: 'frisket.notification_delivery_requests.v1';
  order: 'desc';
  deliveryRequests: NotificationDeliveryRequest[];
}

/**
 * One distinct failure MESSAGE across the run's failed rows, with its count
 * and up to a handful of example row ids (e.g. "11x: media has no
 * values..."). Server shape: server/run_payloads.py's row_error_summary.
 */
export interface RunRowErrorGroup {
  message: string;
  count: number;
  code: string | null;
  /** Result-outcome taxonomy bucket the backend classified this failure into
   *  (model_error / invalid_output / empty_output) — triage groups by this,
   *  never by parsing `message`. */
  outcome: string | null;
  /** True for terminal failure buckets (empty_output): automation never
   *  retries them; only a deliberate user retry re-runs those rows. */
  terminal: boolean;
  /** EXAMPLE row ids only (server-capped) — never the full failing set. */
  rowIds: string[];
}

export interface RunRowErrorSummary {
  totalFailedRows: number;
  groups: RunRowErrorGroup[];
}

/** Server-side media egress proxy status (GET /api/org/media-proxy/status),
 *  polled by MediaProxyRemediationCard for the youtube_provider_blocked
 *  triage card. `connected` is null exactly when `configured` is false; the
 *  local tier (no org identity) 404s instead of returning this shape. */
export interface MediaProxyStatus {
  configured: boolean;
  connected: boolean | null;
  canConfigure: boolean;
}

export interface RunProgress {
  runId: string;
  actionName: string;
  actionKind: string;
  sheetId: string;
  targetColumnId: string;
  /** Row scope the run targets (spec.row_ids), remembered client-side at
   *  launch. `null`/absent means the whole sheet — the live-cell-fill pulse
   *  then marks every empty output cell; a set restricts the pulse to those
   *  rows so a selected-row rerun never pulses untargeted rows. */
  targetRowIds?: string[] | null;
  status: 'queued' | 'running' | 'stalled' | 'orphaned' | 'complete' | 'failed' | 'cancelled';
  completedRows: number;
  totalRows: number;
  failedRows: number;
  costSoFar: number; // USD
  /** Reconciler's reason a run is stalled/orphaned (e.g. 'no_live_worker'). */
  staleReason?: string | null;
  /** A typed recipe/session halt behind a resumable 'cancelled' (e.g.
   *  'local_artifact_unavailable' when a local model was not yet cached).
   *  `haltedReason` is the human sentence; a retry is the fix. */
  haltedCode?: string | null;
  haltedReason?: string | null;
  /** True when the run is queued but no worker heartbeated recently. */
  noLiveWorker?: boolean;
  /** Run-level failure message, when the backend carries one (mapped from the
   *  wire status `error`). Lets a failed DIRECT run surface in the Errors dock. */
  error?: string | null;
  /** Distinct per-row failure messages grouped with counts + example row ids
   *  — undefined when nothing failed. Feeds the Jobs tab's and Errors tab's
   *  job-detail pane (WorkbenchBottomDock.tsx's shared renderJobDetail). */
  rowErrors?: RunRowErrorSummary;
}

export interface ActionJob {
  schemaVersion: string;
  projectId: string;
  jobId: number;
  kind: string;
  runId: string | null;
  receiptId?: string | null;
  status: string;
  actionKind: string | null;
  actionName: string | null;
  attempts: number;
  maxAttempts: number;
  lease: {
    lockedBy: string | null;
    lockedAt: string | null;
    leaseExpiresAt: string | null;
    leaseExpired: boolean;
  };
  timing: {
    createdAt: string | null;
    startedAt: string | null;
    finishedAt: string | null;
  };
  error: string | null;
  resultSummary?: Record<string, unknown>;
  progress?: RunProgress | null;
}

export interface ActionJobsPage {
  schemaVersion: string;
  projectId: string;
  jobs: ActionJob[];
}

/** The settlement half of an attempt receipt, exactly as the server's
 *  `frisket.execution.price_book.settle` produces it (snake_case wire, raw
 *  transport — no generated contract). It has FOUR shapes and the UI must not
 *  blur them:
 *
 *  - a rated charge (`pricing_key` set, `charge_usd` a decimal string).
 *    `rated_charge_usd` preserves the full actual meter at the pinned rate;
 *    when the pinned terms carry a ceiling, `charge_usd` /
 *    `charged_quantity` stop there and `absorbed_overage_usd` names the
 *    excluded rated value;
 *  - operator-borne zero (`pricing_key` null, `charge_usd` "0") — the free /
 *    local run, which is an absence of a charge, not a $0.00 line item;
 *  - unpriceable (`pricing_key` null, `charge_usd` null) — "cannot say";
 *  - `unsettleable` — the metering was reclaimed by compaction, the run
 *    settled under terms this build can no longer decode, or a priced
 *    attempt is missing valid metering for all or part of its work
 *    (`unmetered`). `charge_usd` is null; the quoted side
 *    (`pricing_key`/`unit_rate`) and any valid lower-bound meter evidence may
 *    still accompany it.
 *
 *  Nothing here is invented client-side: the panel names the pricing key and
 *  rate the server actually pinned. */
export interface AttemptSettlement {
  /** Retained wire spelling; opaque offering terms version, or null for
   *  provider-direct and unpriced attempts. */
  price_card_version: string | null;
  terminal_status: string | null;
  charge_usd: string | null;
  unsettleable?: string;
  pricing_key?: string | null;
  unit_rate?: string;
  quantity_unit?: string;
  charge_authority?: string;
  ceiling_mode?: string;
  row_settlement_mode?: string;
  metered_quantity?: string;
  metered_unit?: string;
  billable_quantity?: string;
  /** Full valid meter at the pinned rate, before any pinned charge ceiling. */
  rated_charge_usd?: string | null;
  /** Quantity actually applied to `charge_usd`; may be below billable quantity. */
  charged_quantity?: string | null;
  /** Rated value excluded from the charge by the pinned ceiling. */
  absorbed_overage_usd?: string | null;
  rated_calls?: number;
  unmetered_calls?: number;
  consented_quantity?: string | null;
  exceeds_consented?: boolean | null;
}

export interface AttemptReceiptConsent {
  id: string;
  grant_basis: string | null;
  actor: string | null;
  granted_at: string | null;
  promise_set_hash: string | null;
}

export interface AttemptReceiptTarget {
  target_id?: string | null;
  transport?: string | null;
  engine?: string | null;
  operator?: string | null;
  egress_class?: string | null;
  region?: string | null;
  credential_source?: string | null;
}

/** One execution attempt's receipt. `run_id` is null for a COMPACTION-ORPHANED
 *  attempt: ruling 7 keeps the consent + charge record after `compact()` has
 *  reclaimed the run's data, so a null run id is the honest "the run is gone",
 *  never a missing field. */
export interface AttemptReceipt {
  attempt_id: string;
  run_id: number | null;
  seq: number;
  state: string;
  action_identity_hash: string;
  scope: number[];
  target: AttemptReceiptTarget | null;
  consent: AttemptReceiptConsent | null;
  cost_basis: JsonValue | null;
  price_card_version: string | null;
  settlement: AttemptSettlement | null;
  evaluation: JsonValue | null;
  /** Whose credential actually bore the work, derived server-side from the
   *  calls this attempt authorized. `cost_basis: operator_borne_zero` means
   *  the PLATFORM charged nothing — it does not mean the run was free, and a
   *  run billed to the user's own provider key is both. `null` is "no
   *  metering to read", which is not the same as "nothing external ran". */
  borne_by: AttemptReceiptBorneBy | null;
  created_at: string;
}

/** Providers whose cost landed on a credential the operator holds (a project
 *  key, an org BYOK key, or the deployment's env key). Empty means every call
 *  ran locally or came from cache — genuinely free. */
export interface AttemptReceiptBorneBy {
  credentialed_providers: string[];
}

export interface AttemptReceiptsPage {
  schema_version: string;
  order: string;
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
  next_offset: number | null;
  run_id: number | null;
  attempts: AttemptReceipt[];
}

export interface BackfillResult {
  runId: string;
  filled: number;
}

export interface ClusterValue {
  value: string;
  count: number;
}

export interface ClusterGroup {
  key: string;
  canonical: string;
  size: number;
  values: ClusterValue[];
  rowIds: string[];
}

/** Read-only cluster preview (POST /clusters/v1/preview): the group cards the
 *  review UI renders + edits before committing. `valueHash` is threaded back
 *  as the commit's `expected_value_hash` staleness guard. */
export interface ClusterPreviewResult {
  clusters: ClusterGroup[];
  count: number;
  valueHash: string;
  method: string;
  semantic: boolean;
}

/** One distinct value in a column-values preview page, with its row count. */
export interface ColumnValueCount {
  value: string;
  count: number;
}

export interface NumberColumnDistributionBin {
  start: number;
  end: number;
  count: number;
}

export interface IntegerColumnDistributionBin {
  start: string;
  end: string;
  count: number;
}

export type ColumnDistribution =
  | {
      kind: 'number';
      min: number;
      max: number;
      bins: NumberColumnDistributionBin[];
    }
  | {
      kind: 'integer';
      min: string;
      max: string;
      bins: IntegerColumnDistributionBin[];
    }
  | {
      kind: 'date';
      min: string;
      max: string;
      bins: Array<{ start: string; end: string; count: number }>;
    };

/** Read-only column-values preview (POST /column-values/v1/preview): the
 *  distinct-value inventory the RESOLVE authoring surfaces (Substitute /
 *  Combine) page through. `totalRows`/`distinct`/`missing` are UNFILTERED
 *  column facts; `values` is the current (search-filtered, paged) window,
 *  sorted count desc then value asc; `truncated` means more filtered values
 *  exist beyond this page. `valueHash` identifies the complete value
 *  inventory so independently fetched search pages are never mixed. */
export interface ColumnValuesPreview {
  sheetId: string;
  columnId: string;
  inputColumn: string;
  totalRows: number;
  distinct: number;
  missing: number;
  values: ColumnValueCount[];
  offset: number;
  limit: number;
  truncated: boolean;
  valueHash: string;
  search: string | null;
  distribution: ColumnDistribution | null;
  /** Additive member inventory for a list-valued JSON column. `values` keeps
   * its whole-cell Resolve-authoring meaning. */
  listFacet?: ColumnListFacet | null;
}

/** One selectable member in a list-valued-column facet. `count` is distinct
 * sheet rows, rather than member occurrences, so it equals the filter result
 * for that one selector. */
export interface ColumnListFacetChoice {
  key: string;
  label: string;
  count: number;
  selector: GridFilterListSelector;
}

/** Independently paged member inventory carried alongside a whole-cell
 * column-values preview. */
export interface ColumnListFacet {
  distinct: number;
  offset: number;
  limit: number;
  truncated: boolean;
  search: string | null;
  choices: ColumnListFacetChoice[];
}

/** The `entity_eq` selector a mention group emits, verbatim from the preview
 *  payload. `fingerprint` is an internal comparison token — it travels in the
 *  filter but is NEVER shown to a user as a label. */
export type EntityMentionSelector =
  | { kind: 'fingerprint'; fingerprint: string }
  | { kind: 'text'; text: string };

/** One raw spelling inside a mention group, with its own distinct-row and
 *  occurrence counts. */
export interface EntityMentionSurface {
  text: string;
  rowCount: number;
  mentionCount: number;
}

/** One mention group: every raw spelling sharing a stored fingerprint (or, for
 *  the unfingerprinted numeric/temporal types, one exact text). `rowCount` is
 *  distinct sheet rows — the primary number, because clicking filters rows —
 *  and equals the row count the group's own `selector` returns. `label` is the
 *  real surface occurring in the most distinct rows, never the fingerprint. */
export interface EntityMentionGroup {
  type: string;
  selector: EntityMentionSelector;
  label: string;
  rowCount: number;
  mentionCount: number;
  surfaceCount: number;
  surfaces: EntityMentionSurface[];
}

/** Extraction coverage for the entity column's CURRENT run, read verbatim from
 *  the run counters. `completedRows` counts every row the run PROCESSED,
 *  INCLUDING failures; `failedRows` is the failing subset — so rows that
 *  actually produced mentions is `completedRows - failedRows`. Those three are
 *  null when the column has no current run (an un-run column has no coverage;
 *  zeroes would read as a dishonest "0 of 0 rows extracted").
 *
 *  `sheetRows` is a fact about the SHEET, not a run counter: its live visible
 *  row count, which the server sends even when the three counters are null.
 *  It is what makes `targetRows < sheetRows` — rows the extraction never
 *  covered — visible. Rows added after a run leave the run counters at their
 *  historical values and `map.ner` refuses `run.backfill`, so that gap is
 *  permanent until a whole-sheet re-run, and a panel reading only the run
 *  counters would print "2 of 2 rows extracted" over a half-covered sheet.
 *  Typed nullable because it is parsed from an untrusted payload: a response
 *  without the key must read as "cannot say" rather than as NaN or as a
 *  fabricated comparison. */
export interface EntityMentionsCoverage {
  targetRows: number | null;
  completedRows: number | null;
  failedRows: number | null;
  sheetRows: number | null;
  scopeKind: 'all_rows' | 'exact_membership' | null;
}

/** One type section's exact constrained group count, from the response's
 *  `type_totals`. Section-ordered (type weight desc, ties on type name),
 *  computed AFTER the search/type constraints, and independent of the page
 *  requested — so a section heading can state its full count while holding
 *  only a page of groups. */
export interface EntityMentionTypeTotal {
  type: string;
  totalGroups: number;
}

/** Read-only entity-mentions preview (POST /entity-mentions/v1/preview,
 *  entity-mentions-preview.v2): the fingerprint-grouped mention inventory the
 *  Mentions panel browses. `search` and `type` are applied SERVER-side over
 *  every raw surface before paging, so every count here is an exact
 *  constrained fact — the panel never claims the loaded page is the full set.
 *
 *  Paging is PER TYPE SECTION. An untyped request slices `limit`/`offset`
 *  WITHIN each type: `items` is the concatenation, in section order, of every
 *  type's `groups[offset : offset + limit]`, so no section is starved by a
 *  heavier one. A typed request pages within that one type only. `totalGroups`
 *  stays the overall constrained count (the one type's count when typed), and
 *  `typeTotals` carries each section's exact count — always present, empty
 *  when there are no groups, a single entry for typed requests. Items arrive
 *  already sorted (type section weight, then row count, then label); render
 *  them in the order received. */
export interface EntityMentionsPreview {
  sheetId: string;
  column: { id: string; name: string; semanticType: string | null };
  coverage: EntityMentionsCoverage;
  search: string | null;
  type: string | null;
  totalGroups: number;
  typeTotals: EntityMentionTypeTotal[];
  limit: number;
  offset: number;
  items: EntityMentionGroup[];
}

/** One ordered Replace rule, in the exact resolve.replace preview-wire shape
 *  (frisket.actions.types.ReplaceRule): whole-cell set-to on
 *  first match; `target` null writes a null cell; `case_sensitive` defaults
 *  false (casefold / re.IGNORECASE). */
export interface ResolveReplaceRuleDraft {
  match: 'contains' | 'exact' | 'regex';
  pattern: string;
  target: string | null;
  case_sensitive?: boolean;
}

/** Per-rule match counts from the replace-rules preview (`index` is the
 *  rule's position in the submitted list). */
export interface ReplaceRuleMatchCount {
  index: number;
  matchedRows: number;
  matchedValues: number;
}

/** Read-only replace-rules preview (POST /replace-rules/v1/preview):
 *  first-match-wins counts per rule plus an optional live single-value test
 *  (`testResult` is null when no test_value was sent; `matchedRuleIndex` null
 *  means no rule matched, `output` null means the resulting cell is null).
 *  `valueHash` identifies the value inventory used for the counts. */
export interface ReplaceRulesPreview {
  sheetId: string;
  columnId: string;
  totalRows: number;
  ruleCounts: ReplaceRuleMatchCount[];
  unmatchedRows: number;
  testResult: { matchedRuleIndex: number | null; output: string | null } | null;
  valueHash: string;
}


export interface RunInspectorCell {
  columnId: string;
  columnName: string;
  value: unknown;
  error: string | null;
  tokensIn: number | null;
  tokensOut: number | null;
  cost: number | null;
  confidence: number | null;
  justification: string | null;
  reviewState: string;
}

export interface RunInspectorRow {
  rowId: string;
  rowIndex: number;
  status: 'complete' | 'error' | 'pending' | 'cancelled' | 'not_run' | string;
  error: string | null;
  tokensIn: number | null;
  tokensOut: number | null;
  cost: number | null;
  retryCount: number;
  retries: Array<Record<string, unknown>>;
  cells: RunInspectorCell[];
}

export interface PageMeta {
  offset: number;
  limit: number;
  total: number;
  hasMore: boolean;
  nextOffset: number | null;
}

export interface RunRowsPage extends PageMeta {
  run: {
    id: string;
    actionKind: string;
    actionName: string;
    status: string;
    totalRows: number;
    completedRows: number;
    failedRows: number;
  };
  rows: RunInspectorRow[];
}

/** One run that wrote into an AI column (column provenance). */
export interface ReviewPassRate {
  passed: number;
  graded: number;
}

/** One completed judge run pinned to this exact output run and column. */
export interface JudgeReviewPassRate extends ReviewPassRate {
  runId: string;
  model: string | null;
  startedAt: string;
  verdictColumnId: string;
  /** Cells that have both a human decision and this judge verdict. */
  compared: number;
  /** Null when no faithful row-level comparison exists. */
  disagreementCount: number | null;
}

export interface ColumnRun {
  runId: string;
  actionKind: string;
  actionName: string;
  model: string;
  status: string;
  /** The raw spec submitted: prompt/context/instruction, fields, etc. */
  spec: Record<string, unknown>;
  totalRows: number;
  completedRows: number;
  failedRows: number;
  /** Provider cost. Null means at least one call's cost is unknown — render
   *  the unknown idiom (formatUsdOrNone's em dash), never $0.00. */
  cost: number | null;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  tokensIn: number | null;
  tokensOut: number | null;
  /** True for the run the column's values currently come from. */
  current: boolean;
  /** Exact human grades of this run's original output; legacy state is excluded. */
  humanScore: ReviewPassRate;
  /** Each judge is separate: never blend model verdicts from different runs. */
  judgeScores: JudgeReviewPassRate[];
}

export interface ColumnRunsInfo {
  columnName: string;
  offset: number;
  limit: number;
  totalRuns: number;
  hasMore: boolean;
  nextOffset: number | null;
  currentRun: ColumnRun | null;
  currentRunLoaded: boolean;
  /** Latest output family for display/action replay, not managed value authority. */
  latestRun: ColumnRun | null;
  latestRunLoaded: boolean;
  mixedOrigins: boolean;
  runs: ColumnRun[];
}

// ---------------------------------------------------------------------------
// Run traces (action trace endpoint over best-effort sidecar)

/** One row of a model-call trace: the exact prompt the model saw and the raw
 *  text it returned, plus tokens/latency/cache/retry detail. Deterministic
 *  (non-LLM) ops record rows with no prompt/raw response. */
export interface RunTraceRow {
  rowId: string | null;
  /** Rendered messages as recorded (redacted): usually [{role, content}…]. */
  prompt: unknown;
  /** The rawest text the provider returned, before any parsing. */
  rawResponse: string | null;
  data: unknown;
  error: string | null;
  tokensIn: number | null;
  tokensOut: number | null;
  cost: number | null;
  cached: boolean;
  latencyMs: number | null;
  /** Count of retry/failure events recorded for this row. */
  retries: number;
}

export type RunTraceRowEvidenceStatus =
  | 'recorded'
  | 'not_recorded'
  | 'row_not_in_run'
  | 'missing';

export interface RunTraceRowEvidenceTrace {
  runId: string;
  traceId: string | null;
  actionKind: string | null;
  actionName: string | null;
  model: string | null;
  createdAt: string | null;
  row: RunTraceRow | null;
  recordCount: number;
}

/** Run-level facts available even when no trace was recorded — lets
 *  "Explain this cell" show useful provenance for a deterministic (non-LLM)
 *  op (action, engine, timing, cost, status) instead of a dead-end message. */
export interface RunTraceRowEvidenceRun {
  actionKind: string | null;
  actionName: string | null;
  status: string | null;
  model: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  costActual: number | null;
}

export interface RunTraceRowEvidence {
  runId: string;
  rowId: string;
  columnId: string | null;
  recorded: boolean;
  status: RunTraceRowEvidenceStatus;
  run?: RunTraceRowEvidenceRun | null;
  trace: RunTraceRowEvidenceTrace | null;
}

// ---------------------------------------------------------------------------
// Project provenance manifest (GET /provenance)

export interface ProvenanceModelSummary {
  model: string;
  provider: string | null;
  runs: number;
  rows: number;
  cost: number;
}

export interface ProvenanceActionKindSummary {
  actionKind: string;
  actionName: string;
  runs: number;
  rows: number;
  failedRows: number;
  cost: number;
}

export interface ProvenanceRunSummary {
  runId: string;
  sheetId: string;
  actionKind: string;
  actionName: string;
  model: string | null;
  provider: string | null;
  status: string;
  totalRows: number;
  completedRows: number;
  failedRows: number;
  /** Null when the provider cost is unknown — render with formatUsdOrNone, never $0. */
  cost: number | null;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface ProvenanceReceiptSummary {
  receiptId: string;
  actionKind: string;
  status: string;
  runId: string | null;
  createdAt: string | null;
}

export interface ProvenancePage extends PageMeta {
  schemaVersion: 'frisket.provenance_runs_page.v1' | 'frisket.provenance_receipts_page.v1';
  order: 'desc';
}

export interface ProvenanceManifest {
  projectId: string;
  models: ProvenanceModelSummary[];
  touched: string[];
  providers: string[];
  /** Sum of the known run costs only; runs with unknown cost are excluded
   *  and reported via hasUnknownCosts/unknownCostRuns. */
  totalCost: number;
  hasUnknownCosts: boolean;
  unknownCostRuns: number;
  actionKinds: ProvenanceActionKindSummary[];
  runs: ProvenanceRunSummary[];
  runsPage: ProvenancePage;
  receipts: ProvenanceReceiptSummary[];
  receiptsPage: ProvenancePage;
}

export interface ReceiptRef {
  name: string;
  ref: Record<string, unknown>;
}

export interface ReceiptEvidence {
  ref: Record<string, unknown>;
  retention: string;
}

export interface ReceiptError {
  code: string;
  message: string;
  actionKind?: string | null;
  field?: string | null;
  details?: Record<string, unknown>;
}

export interface V1Receipt {
  schemaVersion: 'frisket.receipt.v1' | string;
  receiptId: string;
  projectId: string;
  actionId: string;
  actionKind: string;
  runId: string | null;
  opIds: number[];
  idempotencyKey: string | null;
  paramsHash: string | null;
  status: string;
  inputs: ReceiptRef[];
  outputs: ReceiptRef[];
  value?: HttpReceipt['value'];
  providerUse: Array<Record<string, unknown>>;
  evidence: ReceiptEvidence[];
  errors: ReceiptError[];
}

// ---------------------------------------------------------------------------
// History (the OpenRefine-style op log)

/** An output column a run actually wrote cells to. */
export interface HistoryOpRunColumn {
  id: string;
  name: string;
}

/**
 * The FULL run record behind a history op: the history detail panel used to
 * show only kind/label/state/rows/cost, and rows/cost were always "--"
 * (backend never sent them; see api/real.ts's mapOp). Present only for ops
 * with an associated run (map/derive-style action runs) — import/edit/sort
 * ops have none.
 */
export interface HistoryOpRun {
  runId: string;
  status: string;
  actionKind: string;
  model: string | null;
  params: Record<string, unknown>;
  totalRows: number;
  completedRows: number;
  failedRows: number;
  costEstimate?: number;
  /** Provider cost; null = unknown (a live call could not be priced). */
  costActual: number | null;
  startedAt: string; // ISO
  finishedAt: string | null; // ISO
  /** Claimant stamp — which code touched this run. */
  workerVersion: string | null;
  outputColumns: HistoryOpRunColumn[];
  /** Undefined when nothing failed. Feeds the History panel's run-detail
   *  deep link (HistoryPanel.tsx's renderDetail). */
  rowErrors?: RunRowErrorSummary;
}

export interface HistoryOp {
  id: string;
  index: number;
  label: string; // e.g. "Classify topic on Articles (10,000 rows)"
  kind: 'import' | 'map' | 'derive' | 'edit' | 'review-batch' | 'sort';
  at: string; // ISO
  rowsAffected: number;
  /** Absent for run-less ops; null = the run's provider cost is unknown. */
  cost?: number | null;
  /** Irreversible barriers (blob deletion, compaction) render as marked steps. */
  barrier?: boolean;
  run?: HistoryOpRun;
}

export interface HistoryStepTarget {
  id: string;
  index: number;
  barrier: boolean;
}

export interface HistoryPage {
  ops: HistoryOp[];
  offset: number;
  limit: number;
  total: number;
  hasMoreBefore: boolean;
  hasMoreAfter: boolean;
  prevOffset: number | null;
  nextOffset: number | null;
  /** Global op-log index of the current cursor; -1 = before all ops. */
  cursorIndex: number;
  cursorOp: HistoryOp | null;
  cursorOpLoaded: boolean;
  undoTarget: HistoryStepTarget | null;
  redoTarget: HistoryStepTarget | null;
  revision: {
    total: number;
    maxOpId: string | null;
    opCursor: string | null;
  };
}

export type HistoryState = HistoryPage;

export type ReviewState = 'unreviewed' | 'verified' | 'rejected' | (string & {});

export interface ReviewBundleField {
  id: string;
  runId: string;
  sheetId: string;
  rowId: string;
  columnId: string;
  columnName: string;
  columnType: ColumnType;
  value: CellValue;
  confidence: number;
  justification: string;
  /** Explicit review outcome; absent for legacy review-state-only rows. */
  reviewDecision?: ReviewAction | null;
  /** Reviewer note stored for this exact run/result cell, if one was recorded. */
  note?: string | null;
  reviewState: ReviewState;
  role: 'field' | 'evidence';
  chore: boolean;
  changed?: boolean;
}

export interface ReviewBundle {
  id: string;
  runId: string;
  sheetId: string;
  sheetName: string;
  rowId: string;
  rowIndex: number;
  actionKind: string;
  actionName: string;
  model: string;
  confidence: number;
  /** Source row values shown as the shared context for sibling outputs. */
  source: Record<string, CellValue>;
  context: string;
  fields: ReviewBundleField[];
  evidence: ReviewBundleField[];
}

export interface ReviewBundlePage extends PageMeta {
  schemaVersion: 'frisket.review_bundles_page.v1';
  bundles: ReviewBundle[];
}

export type ReviewAction = 'accept' | 'reject' | 'reject_clear' | 'edit';

// ---------------------------------------------------------------------------
// Live sources: a recurring fetch that lands new rows on a sheet. Source
// polling is kind-generic: RSS bridges through the generic source.poll host,
// and investigative sources register pollers behind it.

/** Polling cadence presets — the only schedule strings the backend interval
 *  parser (jobs/sources.py::_schedule_interval) accepts. '' = manual/on-demand. */
export type SourceInterval = '' | '@hourly' | '0 */6 * * *' | '@daily';

export type SourceKind =
  | 'rss'
  | 'youtube_playlist'
  | 'youtube_channel'
  | 'api_list_dicts'
  | 'courtlistener_docket'
  | (string & {});

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type SourceConfig = Exclude<JsonValue, null>;

/** A configured source (mirrors the server `sources` row / _source_dict). */
export interface SourceInfo {
  id: number;
  name: string;
  kind: string; // 'rss' | 'url' | 'search' | 'api'
  url: string | null;
  config: SourceConfig;
  sheetId: number | null;
  schedule: string | null; // null = manual
  enabled: boolean;
  lastCheckedAt: string | null;
  lastStatus: string | null; // 'ok' | 'error' | 'never' | null
  newRowsTotal: number;
}

/** One poll of a source (source_runs row). */
export interface SourceRun {
  id: number;
  status: string; // 'ok' | 'error'
  newRows: number;
  skippedRows: number;
  changedRows: number;
  revisions: number;
  warningCount: number;
  durationMs: number | null;
  costMicro: number;
  receiptId: string | null;
  error: string | null;
  startedAt: string;
  finishedAt: string | null;
}

export interface SourceRunPage extends PageMeta {
  schemaVersion: 'frisket.source_runs_page.v1';
  order: 'desc';
  latestRun: SourceRun | null;
  latestRunLoaded: boolean;
}

/** A source plus its recent poll history (GET /sources/{id}). */
export interface SourceDetail extends SourceInfo {
  runs: SourceRun[];
  runsPage: SourceRunPage;
}

/** Fields the add/edit form submits. */
export interface SourceInput {
  name: string;
  kind: string;
  url?: string | null;
  schedule?: string | null;
  enabled?: boolean;
  config?: Record<string, unknown>;
}

/** Outcome of a manual "Fetch now". */
export interface SourceFetchResult {
  runId: number;
  newRows: number;
  revisions: number;
  skippedRows: number;
  changedRows: number;
  warningCount: number;
  sheetId: number | null;
}

export type SourceHealthStatus =
  | 'healthy'
  | 'failing'
  | 'stale'
  | 'disabled'
  | 'never_run'
  | (string & {});

export interface SourceHealthSummary {
  status: SourceHealthStatus;
  lastSuccessAt: string | null;
  lastFailureAt: string | null;
  consecutiveFailures: number;
  newRowsTotal: number;
  newRowsRecent: number;
  changedRowsRecent: number;
  skippedRowsRecent: number;
  revisionsRecent: number;
  recentRunCount: number;
  lastCursorSummary: string;
}

export interface SourceHealthRun extends SourceRun {
  sourceId: number;
  opId: number | null;
  errorSummary: string | null;
  cursorBeforePresent: boolean;
  cursorAfterPresent: boolean;
  summaryPresent: boolean;
}

export interface SourceHealthJob {
  jobId: number | string;
  kind: string;
  status: string;
  attempts: number;
  maxAttempts: number;
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  refs: Record<string, unknown>;
  resultSummary: Record<string, unknown>;
  errorSummary: string | null;
  stalled: boolean;
}

export interface SourceHealthNotice {
  code?: string;
  message: string;
  [key: string]: unknown;
}

export interface SourceHealth {
  schemaVersion: 'frisket.source_health.v1';
  source: {
    id: number;
    name: string;
    kind: string;
    url: string | null;
    enabled: boolean;
    schedule: string | null;
    sheetId: number | null;
    createdAt: string | null;
    redactions: string[];
  };
  summary: SourceHealthSummary;
  runsPage: Omit<SourceRunPage, 'latestRun' | 'latestRunLoaded'>;
  runs: SourceHealthRun[];
  downstreamJobs: SourceHealthJob[];
  costs: {
    recentActualMicro: number;
    recentEstimatedMicro: number;
    basis: string;
  };
  alerts: SourceHealthNotice[];
  warnings: SourceHealthNotice[];
}

// ---------------------------------------------------------------------------
// Native embeddings.

export interface EmbeddingProvider {
  providerId: string;
  providerKind: string;
  modelId: string;
  label: string;
  modalities: string[];
  /** null for discovery-required remote ids whose dimension is found at create. */
  dimensions: number[] | null;
  local: boolean;
  available: boolean;
  /** Why the provider is unavailable (missing key / sidecar not built / …). */
  disabledReason: string | null;
  /** 'remote' for providers that send data off the machine. */
  egress: string | null;
  /** True for the ONE default model the picker selects + badges on open. */
  recommended: boolean;
  /** On-disk model size in GB (factual catalog field), null when unknown. */
  sizeGb: number | null;
  /** Max input tokens the model accepts (factual), null when unknown. */
  maxInputTokens: number | null;
  /** Provider list-price metadata. Unknown custom-model rates stay null. */
  pricing: {
    policy: string;
    inputUsdPerMillionTokens: number | null;
    sourceUrl: string | null;
    updated: string | null;
  } | null;
  /** False when the model's modality can't embed the chosen source column
   *  type — rendered disabled with `disabledReason`. */
  modalityCompatible: boolean;
  /** True for custom-remote ids (OpenRouter) whose dimension is discovered with a
   *  probe at create — the picker shows no dimension and notes "discovered at
   *  create" rather than inventing one. */
  dimensionDiscoveryRequired: boolean;
}

export interface EmbeddingIndexSummary {
  indexId: string;
  name: string;
  sheetId: number | null;
  modality: string | null;
  providerId: string | null;
  modelId: string | null;
  sourceColumns: string[];
  status: string;
  /** Rows the CURRENT source scope should embed (not the stale materialized total). */
  totalItems: number;
  readyItems: number;
  staleItems: number;
  /** Embeddable rows in scope with no vector yet (e.g. appended). */
  missingItems: number;
  errorItems: number;
  /** True when something needs (re)embedding: stale/errored items, or never
   *  refreshed. The UI surfaces this as "refresh needed", not a failure. */
  refreshNeeded: boolean;
  lastRefreshedAt: string | null;
  remote: boolean;
  providerKind: string | null;
  allowRemote: boolean;
  allowRemoteAutomaticRefresh: boolean;
  maxCostUsdPerRefresh: number | null;
  /** Maintenance policy (governance surface). */
  maintenanceMode: 'manual' | 'on_source_append' | 'scheduled';
  schedule: string | null;
  /** A legacy/corrupt known policy leaf was normalized safely for this UI. */
  policyNeedsRepair: boolean;
  /** First-class freshness state for the detail surface. */
  freshness: EmbeddingIndexFreshness;
  /** Provenance/details only — not the primary UI surface. */
  spaceId: string;
  dimension: number | null;
  distanceMetric: string | null;
}

export interface EmbeddingIndexFreshness {
  reason: string;
  current: number;
  missing: number;
  stale: number;
  error: number;
  total: number;
  lastRefreshJobId: string | null;
  lastRefreshReceiptId: string | null;
  pendingRefreshJobId: number | null;
}

export interface UpdateEmbeddingIndexPolicyInput {
  indexId: string;
  maintenancePolicy?: {
    mode: 'manual' | 'on_source_append' | 'scheduled';
    schedule?: string | null;
  };
  providerPolicy?: {
    allowRemote?: boolean;
    allowRemoteAutomaticRefresh?: boolean;
    maxCostUsdPerRefresh?: number | null;
  };
}

export interface EmbeddingSimilarityHit {
  rowId: number;
  distance: number | null;
  score: number | null;
  values: Record<string, unknown>;
}

export interface EmbeddingSimilarityResult {
  indexId: string;
  distanceMetric: string;
  hits: EmbeddingSimilarityHit[];
}

/** One weighted term of a composed semantic query. A negative weight steers
 *  AWAY (lean), not a hard exclude. The manual_text_query anchor carries a
 *  list of these; a single phrase is one term, weight 1.0. */
export interface EmbeddingQueryTerm {
  text: string;
  weight: number;
}

/** One hard-exclude term: rows whose cosine score to this concept is >=
 *  `threshold` are REMOVED from results (a gate, not a lean). */
export interface EmbeddingExcludeTerm {
  text: string;
  threshold?: number;
}

/** A composed semantic query: weighted steer terms + optional hard-exclude terms. */
export interface EmbeddingComposedQuery {
  terms: EmbeddingQueryTerm[];
  exclude?: EmbeddingExcludeTerm[];
}

export interface EmbeddingExportArtifact {
  format: string;
  path: string;
  byteCount: number;
  sha256: string;
  rowCount: number;
}

export interface EmbeddingExportResult {
  receiptId: string | null;
  artifacts: EmbeddingExportArtifact[];
}

export interface CreateEmbeddingIndexInput {
  sheetId: number;
  sourceColumns: string[];
  modality: string;
  provider: string;
  model?: string;
  allowRemote: boolean;
  allowRemoteAutomaticRefresh: boolean;
  maxCostUsdPerRefresh?: number | null;
}

/** Typed analysis params come from the action; output names belong to its request. */
export type EmbeddingIndexAnalysisInput = {
  [Id in 'embedding.index_project' | 'embedding.index_cluster']: {
    action_id: Id;
    params: GeneratedActionParams[Id];
    sheet_name: string;
    output_names?: RegisteredActionRequest['output_names'];
  }
}['embedding.index_project' | 'embedding.index_cluster'];

export interface EmbeddingIndexAnalysisResult {
  /** The child sheet minted by the analysis action — open it in the grid. */
  sheetId: number;
}

/** A saved lens: a named, normalized row-anchor embedding_similarity
 *  QuerySpec persisted through the contract spine
 *  (POST /api/projects/{pid}/lenses). The spec's query resolves to an
 *  ordered row-set WITH numeric distance/score — the fields the watch path
 *  drops. */
export interface Lens {
  id: number;
  name: string;
  sheetId: number | null;
  /** {schema_version, query, presentation}. The query for a row lens is a
   *  normalized embedding_similarity QuerySpec (kind/embedding_index_id/anchor). */
  spec: Record<string, unknown>;
  opId: number | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface LensSaveInput {
  name: string;
  /** A normalized embedding_similarity QuerySpec (row anchor). */
  query: Record<string, unknown>;
  presentation?: Record<string, unknown>;
}

/** One resolved row of a lens, carrying the numeric distance/score. */
export interface LensResolvedRow {
  rowId: number;
  distance: number | null;
  score: number | null;
}

/** Resolve of a saved lens (GET /lenses/{id}/resolve): the current ordered
 *  row-set with per-row distance/score (frisket.query_preview.v1). */
export interface LensResolved {
  lensId: number;
  schemaVersion: string;
  sheetId: number | null;
  queryHash: string | null;
  rowIds: number[];
  rows: LensResolvedRow[];
  scores: Record<string, { distance: number | null; score: number | null }>;
  rowCount: number;
  total: number;
  offset: number;
  limit: number;
}

export interface WorkbenchPluginContributionSummary {
  kind: string;
  count: number;
  ids: string[];
}

export interface WorkbenchPluginFrontendComponentBinding {
  schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1';
  contributionId: string;
  moduleKey: string;
  componentKey: string;
  modulePath?: string;
  moduleUrl?: string;
}

export interface WorkbenchPluginDescriptorPackage {
  schemaVersion: 'frisket.workbench_descriptor_package.v1';
  sourcePath: string;
  descriptorCount: number;
  runtimeOnlyFieldsStripped: string[];
}

export interface WorkbenchPluginDescriptorManifest {
  schemaVersion?: string;
  id?: string;
  ownerPluginId?: string;
  placements?: Array<{
    host?: string;
    mode?: string;
    slot?: string;
  }>;
  [key: string]: unknown;
}

export interface WorkbenchPluginRuntimePlugin {
  schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1';
  pluginId: string;
  version: string;
  installState: 'installed' | 'enabled' | 'disabled' | 'failed' | 'uninstalled' | string;
  activation: 'manifestLoaded' | 'registryManifestRegistered' | 'blocked' | 'failed' | string;
  runtimeSource: 'plugin.load_receipt' | string;
  receiptId: string | null;
  manifestSha256: string;
  packageSha256?: string;
  byteCount: number;
  source: { kind: string; path?: string; value?: string; [key: string]: unknown };
  contributionSummary: WorkbenchPluginContributionSummary[];
  frontendComponentBindings?: WorkbenchPluginFrontendComponentBinding[];
  workbenchDescriptorPackage?: WorkbenchPluginDescriptorPackage;
  workbenchDescriptorManifests?: WorkbenchPluginDescriptorManifest[];
  settings?: WorkbenchPluginSettingDescriptor[];
  requires: {
    capabilities: string[];
    secrets: string[];
  };
  arbitraryPackageLoadAllowed: boolean;
  registryActivated?: boolean;
  installStateSchemaVersion?: 'frisket.workbench_plugin_install_state.v1' | string | null;
  disabledReason?: string | null;
  installFailure?: WorkbenchPluginInstallFailure | null;
}

// The honest firstParty section the runtime index serves — exactly the
// checked-in artifact
// (src/frisket/data/first_party_workbench_descriptors.json), NO
// installState/receipts/moduleUrl lifecycle rows. The app renders
// first-party from the statically-imported artifact (web/src/workbench/
// descriptors.ts), not from this response; the section exists so the index
// is the one registry-of-record endpoint for tooling/plugin-manager reads.
export interface WorkbenchPluginRuntimeIndexFirstPartySection {
  schemaVersion: 'frisket.workbench_descriptor_package.v1';
  descriptors: WorkbenchPluginDescriptorManifest[];
}

export interface WorkbenchPluginRuntimeIndex {
  schemaVersion: 'frisket.workbench_plugin_runtime_index.v1';
  projectId: string;
  arbitraryPackageLoadAllowed: boolean;
  receiptScanLimit: number;
  skippedInvalidReceipts: number;
  skippedInvalidManifestRefs: number;
  loadedPluginCount: number;
  plugins: WorkbenchPluginRuntimePlugin[];
  firstParty: WorkbenchPluginRuntimeIndexFirstPartySection;
}

export interface WorkbenchPluginSettingDescriptor {
  id: string;
  title: string;
  type: 'boolean' | 'string' | 'number' | 'enum' | string;
  default?: unknown;
  enum?: unknown[] | null;
  min?: number | null;
  max?: number | null;
  description?: string | null;
}

export interface WorkbenchPluginSettingInfo {
  id: string;
  title: string;
  type: 'boolean' | 'string' | 'number' | 'enum' | string;
  description: string | null;
  defaultValue: unknown;
  effectiveValue: unknown;
  source: 'project' | 'default' | string;
  enum: unknown[] | null;
  min: number | null;
  max: number | null;
  readOnly: boolean;
}

export interface WorkbenchPluginSettings {
  schemaVersion: 'frisket.workbench_plugin_settings.v1';
  projectId: string;
  pluginId: string;
  canMutate: boolean;
  settings: WorkbenchPluginSettingInfo[];
}

export interface WorkbenchPluginActivationRequest {
  pluginId: string;
  receiptId: string;
  trustAcknowledged: boolean;
  permissionsAccepted: string[];
  arbitraryPackageLoadAllowed: boolean;
}

export interface WorkbenchPluginLocalInstallRequest {
  pluginId: string;
  source: {
    kind: 'localPath' | 'local_file' | string;
    value?: string;
    path?: string;
    [key: string]: unknown;
  };
  arbitraryPackageLoadAllowed: boolean;
}

export interface WorkbenchPluginLocalInstallExecution {
  schemaVersion: 'frisket.plugin_install_plan_execution.v1';
  projectId: string;
  pluginId: string;
  // The server preserves the submitted source as an open JSON object. In
  // particular, failed lifecycle responses need not contain a `kind` field.
  source: Record<string, JsonValue>;
  installState: 'installed' | 'enabled' | 'failed' | string;
  activation: 'manifestLoaded' | 'registryManifestRegistered' | 'failed' | string;
  runtimeSource: 'plugin.load_receipt' | string;
  receiptId: string | null;
  manifestSha256: string;
  packageSha256: string;
  arbitraryPackageLoadAllowed: boolean;
  // Lifecycle failures are server-owned diagnostic envelopes, not the
  // normalized runtime-index failure model below.
  installFailure: Record<string, JsonValue> | null;
  layoutMutated?: boolean;
  workbenchDescriptorPackage?: WorkbenchPluginDescriptorPackage | null;
  workbenchDescriptorManifests?: WorkbenchPluginDescriptorManifest[] | null;
}

export interface WorkbenchPluginActivation {
  schemaVersion: 'frisket.workbench_plugin_activation.v1';
  projectId: string;
  pluginId: string;
  receiptId: string;
  manifestSha256: string;
  packageSha256: string;
  runtimeSource: 'plugin.load_receipt' | string;
  activation: 'registryManifestRegistered' | string;
  installState: 'enabled' | string;
  registryActivated: boolean;
  arbitraryPackageLoadAllowed: boolean;
  permissionsAccepted: string[];
  registeredPluginManifests: string[];
}

export interface WorkbenchPluginBackendActivationRequest {
  pluginId: string;
  trustAcknowledged: boolean;
  arbitraryPackageLoadAllowed: boolean;
  executableHandlersAllowed: boolean;
}

export interface WorkbenchPluginBackendActivation {
  schemaVersion: 'frisket.workbench_plugin_backend_activation.v1';
  projectId: string;
  pluginId: string;
  receiptId: string;
  manifestSha256: string;
  packageSha256: string;
  runtimeSource: 'plugin.load_receipt' | string;
  arbitraryPackageLoadAllowed: boolean;
  executableHandlersRegistered: boolean;
  trustedRuntimeBindingsRegistered: boolean;
  // Registry summaries are intentionally open: each plugin runtime may
  // register arbitrary JSON-shaped contribution metadata.
  registeredBackendContributions: Record<string, JsonValue>;
  registeredExecutableHandlers: Record<string, JsonValue>;
  registeredRuntimeBindings: Record<string, JsonValue>;
}

export interface WorkbenchPluginInstallFailure {
  code: string;
  message: string;
  retryable?: boolean;
  rollbackAction?: string;
  ref?: string;
  [key: string]: unknown;
}

export interface WorkbenchPluginInstallStateChange {
  schemaVersion: 'frisket.workbench_plugin_install_state.v1';
  projectId: string;
  pluginId: string;
  receiptId: string | null;
  manifestSha256: string;
  packageSha256: string;
  installState: 'disabled' | 'uninstalled' | 'enabled' | 'failed' | string;
  activation: 'blocked' | 'removed' | 'registryManifestRegistered' | 'failed' | string;
  runtimeSource: 'plugin.load_receipt' | string;
  permissionsAccepted: string[];
  registryActivated: boolean;
  arbitraryPackageLoadAllowed: boolean;
  disabledReason: string | null;
  source: Record<string, JsonValue>;
  installFailure: Record<string, JsonValue> | null;
}

// ---------------------------------------------------------------------------
// The API surface. The live implementation is composed in src/api/real.ts;
// contract-backed transport is centralized in src/api/httpContract.ts.

// Copilot chat domain types. `needsImport` is the model's import-prerequisite
// decision; when true the reply carries no proposals and the UI surfaces the
// import CTA instead of an invented action card.
export interface CopilotChatMessageInput {
  role: 'user' | 'assistant';
  content: string;
}
export type CopilotProposalKind = 'map' | 'derive' | 'reduce' | 'resolve' | 'media' | 'enrich' | 'web' | 'research';
export type CopilotParamValue =
  | null
  | string
  | number
  | boolean
  | CopilotParamValue[]
  | { [key: string]: CopilotParamValue };
export type CopilotRegisteredActionDraft = RegisteredActionDraft<
  CopilotParamValue,
  RegisteredActionScope
>;
export type CopilotActionSpec = CopilotRegisteredActionDraft;

export function isCopilotRegisteredActionDraft(
  spec: CopilotActionSpec | Record<string, unknown>,
): spec is CopilotRegisteredActionDraft {
  return hasRegisteredActionDraftShape(spec);
}
export interface CopilotProposal {
  kind: CopilotProposalKind;
  title: string;
  spec: CopilotActionSpec;
}
export interface CopilotReply {
  reply: string;
  proposals: CopilotProposal[];
  needsImport: boolean;
  costUsd: number | null;
}

export interface FrisketApi {
  /** Compatible embedding providers/models for the picker, by source column type. */
  embeddingProviderCatalog(opts: {
    modality?: string;
    sourceColumnType?: string;
  }): Promise<EmbeddingProvider[]>;
  /** Existing embedding indexes for a sheet (provider/model + counts + state). */
  embeddingIndexes(sheetId: number): Promise<EmbeddingIndexSummary[]>;
  /** Create an embedding index (metadata only; no provider call). Returns its id. */
  createEmbeddingIndex(input: CreateEmbeddingIndexInput): Promise<string>;
  /** Manually (re)embed an index. mode 'full' rebuilds; 'incremental' fills gaps.
   *  embedding.index_refresh is a QUEUED_ACTION_JOB (executor/action_specs.py) —
   *  the resolved value carries the launched job so callers can track it to
   *  completion instead of assuming it already finished. `status` is the v1
   *  action result status at launch time ('completed' when replayed/instant,
   *  else 'queued'/'running'); `jobId` is null exactly when `status` is
   *  already terminal. */
  refreshEmbeddingIndex(
    indexId: string,
    mode: 'incremental' | 'full',
  ): Promise<{ jobId: number | null; status: string }>;
  /** Edit an index's maintenance/provider policy (wholesale replace per policy).
   *  Preauthorization only — the remote/cost gates still fire at refresh time. */
  updateEmbeddingIndexPolicy(input: UpdateEmbeddingIndexPolicyInput): Promise<void>;
  /** Export an index's originals + vectors to server artifacts (JSONL/Parquet/Arrow). */
  exportEmbeddingIndex(
    indexId: string,
    opts?: { formats?: string[]; includeVectors?: boolean },
  ): Promise<EmbeddingExportResult>;
  /** Run a PCA projection / k-means analysis over an index (v1 action contract).
   *  Returns the minted child sheet id so the UI can open it. */
  runEmbeddingIndexAnalysis(
    input: EmbeddingIndexAnalysisInput,
  ): Promise<EmbeddingIndexAnalysisResult>;
  /** Browser download URL for a previously-exported artifact of the given format. */
  embeddingExportArtifactUrl(indexId: string, format: string): string;
  /** Show-similar preview for a manual text query over one index. */
  embeddingSimilarityPreview(
    indexId: string,
    // a plain phrase (legacy single `text`) OR a composed query (terms + exclude)
    query: string | EmbeddingComposedQuery,
    opts?: { limit?: number },
  ): Promise<EmbeddingSimilarityResult>;
  /** Hybrid keyword+vector preview: sheet FTS + vector search fused by RRF.
   *  The per-row `score` is the fused RRF score. */
  embeddingHybridPreview(
    indexId: string,
    sheetId: number | null,
    text: string,
    opts?: { limit?: number },
  ): Promise<EmbeddingSimilarityResult>;
  /** The column-type registry (core + plugin types, with presentation hints). */
  listColumnTypes(): Promise<ColumnTypeInfo[]>;
  /** Canonical action catalog. A scoped API defaults to its own project. */
  listActionCatalog(projectId?: string | null): Promise<ActionCatalogPayload>;
  /** Project-scoped workbench view of completed plugin.load manifest receipts. */
  getWorkbenchPluginRuntimeIndex(): Promise<WorkbenchPluginRuntimeIndex>;
  getWorkbenchPluginSettings(pluginId: string): Promise<WorkbenchPluginSettings>;
  patchWorkbenchPluginSettings(
    pluginId: string,
    values: Record<string, unknown>,
  ): Promise<WorkbenchPluginSettings>;
  /** Execute a trusted-local install plan through the backend plugin.load lifecycle. */
  installLocalWorkbenchPlugin(
    input: WorkbenchPluginLocalInstallRequest,
  ): Promise<WorkbenchPluginLocalInstallExecution>;
  /** Trust-gated workbench activation of manifest metadata from a plugin.load receipt. */
  activateWorkbenchPlugin(
    input: WorkbenchPluginActivationRequest,
  ): Promise<WorkbenchPluginActivation>;
  activateWorkbenchPluginBackend(
    input: WorkbenchPluginBackendActivationRequest,
  ): Promise<WorkbenchPluginBackendActivation>;
  disableWorkbenchPlugin(pluginId: string): Promise<WorkbenchPluginInstallStateChange>;
  uninstallWorkbenchPlugin(pluginId: string): Promise<WorkbenchPluginInstallStateChange>;
  listOAuthConnections(provider?: string): Promise<OAuthConnectionInfo[]>;
  googleOAuthStartUrl(): string;
  exportGoogleSheets(input: GoogleSheetsExportInput): Promise<GoogleSheetsExportResult>;
  listViews(sheetId?: string): Promise<SavedView[]>;
  saveView(input: SavedViewCreateInput): Promise<SavedView>;
  renameView(viewId: number, input: SavedViewRenameInput): Promise<SavedView>;
  replaceViewDefinition(
    viewId: number,
    input: SavedViewDefinitionReplaceInput,
  ): Promise<SavedView>;
  deleteView(viewId: number): Promise<void>;
  /** Saved lenses for a sheet. A lens is a named row-anchor
   *  embedding_similarity QuerySpec persisted through the contract spine. */
  listLenses(sheetId?: number | string): Promise<Lens[]>;
  /** Persist a lens (POST /lenses). The query is a normalized
   *  embedding_similarity QuerySpec (row anchor). */
  saveLens(input: LensSaveInput): Promise<Lens>;
  /** Resolve a lens to its current ordered row-set WITH per-row distance/score.
   *  A stale anchor surfaces a typed ApiError (embedding_anchor_stale). */
  resolveLens(lensId: number, opts?: { limit?: number; offset?: number }): Promise<LensResolved>;
  listWatches(): Promise<WatchInfo[]>;
  createWatch(input: WatchInput): Promise<WatchInfo>;
  updateWatch(watchId: number, input: WatchPatchInput): Promise<WatchInfo>;
  deleteWatch(watchId: number): Promise<void>;
  runWatch(watchId: number): Promise<WatchRunResult>;
  getWatchRuns(watchId: number, offset?: number, limit?: number): Promise<WatchRunsPage>;
  getWatchRunEvents(
    watchId: number,
    runId: number,
    offset?: number,
    limit?: number,
    eventKind?: string,
  ): Promise<WatchRunEventsPage>;
  listNotifications(params?: NotificationListParams): Promise<NotificationPage>;
  getNotificationsSummary(): Promise<NotificationSummary>;
  markNotificationsSeen(filter: NotificationStateFilter): Promise<{ seenCount: number }>;
  markNotificationRead(notificationId: number): Promise<NotificationActorState>;
  ackNotification(notificationId: number): Promise<NotificationActorState>;
  unackNotification(notificationId: number): Promise<NotificationActorState>;
  listNotificationChannels(): Promise<NotificationChannelsPage>;
  createNotificationChannel(input: NotificationChannelInput): Promise<NotificationChannel>;
  updateNotificationChannel(channelId: number, input: Partial<NotificationChannelInput>): Promise<NotificationChannel>;
  listNotificationRoutes(): Promise<NotificationRoutesPage>;
  createNotificationRoute(input: NotificationRouteInput): Promise<NotificationRoute>;
  updateNotificationRoute(routeId: number, input: Partial<NotificationRouteInput>): Promise<NotificationRoute>;
  testNotificationRoute(routeId: number, ownerKind?: string): Promise<NotificationDeliveryRequest>;
  listNotificationDeliveryRequests(params?: {
    status?: NotificationDeliveryStatus;
    routeId?: number;
    channelId?: number;
    notificationId?: number;
    offset?: number;
    limit?: number;
  }): Promise<NotificationDeliveryRequestsPage>;
  getMapPoints(sheetId: string, columnId: string, options?: MapPointsOptions | null): Promise<MapPointsResult>;
  /** Raw Arrow IPC bytes from the SAME map-points route `getMapPoints` decodes
   *  (`/api/projects/:pid/sheets/:sheetId/map/points`), undecoded — the
   *  transport behind `ctx.projection.fetchData` (capability
   *  `projection.data.read`). Plugin code never sees the route; this is the
   *  one api-client seam it goes through. */
  getMapPointsArrowBuffer(
    sheetId: string,
    columnId: string,
    options?: MapPointsOptions | null,
  ): Promise<ArrayBuffer>;
  getRuntimeProjectionStatus(
    input: RuntimeProjectionStatusRequest,
  ): Promise<RuntimeProjectionStatus>;
  buildRuntimeProjection(
    input: RuntimeProjectionBuildRequest,
  ): Promise<RuntimeProjectionBuildPlan>;
  readRuntimeProjectionArtifact(
    input: RuntimeProjectionArtifactRequest,
  ): Promise<TimelineProjectionArtifact>;
  getSheetGraph(options: SheetGraphOptions): Promise<SheetGraphResult>;
  compareOcrScratch(
    file: File,
    input: OcrCompareScratchInput,
  ): Promise<PreviewStartResult>;
  estimateOcrScratch(file: File, input: OcrCompareScratchInput): Promise<RunEstimate>;
  compareTranscribeScratch(
    file: File,
    input: TranscribeCompareScratchInput,
  ): Promise<PreviewStartResult>;
  estimateTranscribeScratch(file: File, input: TranscribeCompareScratchInput): Promise<RunEstimate>;
  compareTopicSegmentationScratch(
    file: File,
    input: TopicSegmentationCompareScratchInput,
  ): Promise<TopicSegmentationCompareScratchResult>;
  compareTranslateScratch(
    input: TranslateCompareScratchInput,
  ): Promise<TranslateCompareScratchResult>;
  listSheets(): Promise<SheetMeta[]>;
  /** Set (or, with `null`, clear) the sheet-level row-title override.
   *  Resolves once persisted; callers refetch (listSheets/refreshSheets) to
   *  see it reflected. */
  setSheetTitleColumn(sheetId: string, titleColumnId: string | null): Promise<void>;
  deleteSheet(sheetId: string): Promise<DeleteSheetResult>;
  getLineage(): Promise<LineageDag>;
  refreshSheet(
    sheetId: string,
    confirmation?: string,
  ): Promise<SheetRefreshResult>;
  getSheetData(sheetId: string, offset: number, limit: number, options?: SheetDataOptions | null): Promise<SheetDataPage>;
  getColumnStats(sheetId: string, columnId: string, opts?: { force?: boolean }): Promise<ColumnStats>;
  locateSheetRow(sheetId: string, rowId: string, options?: SheetDataOptions | null, pageSize?: number): Promise<SheetRowLocation>;
  addRow(sheetId: string, cells?: Record<string, CellValue>): Promise<AddRowResult>;
  addColumn(
    sheetId: string,
    name: string,
    options?: { type?: string; position?: number | null },
  ): Promise<AddColumnResult>;
  deleteRows(sheetId: string, rowIds: string[]): Promise<DeleteRowsResult>;
  editCells(edits: CellEdit[]): Promise<void>;
  /** Replay preserve+surface: accept the fresh generated value for one
   *  pending cell as a new attributed edit op. */
  acceptReplayValue(
    sheetId: string,
    columnId: string,
    rowId: string,
    generatedValueHash: string,
    runId: string,
  ): Promise<void>;
  /** Accept the fresh value for every pending cell in a column in one op. */
  acceptReplayValuesInColumn(sheetId: string, columnId: string): Promise<void>;
  /** Durably keep the human edit against a specific fresh generated value. */
  dismissReplayPending(
    sheetId: string,
    columnId: string,
    rowId: string,
    generatedValueHash: string,
    runId: string,
  ): Promise<void>;
  updateColumn(columnId: string, patch: ColumnPatch): Promise<ColumnDef>;
  estimateAction(req: ActionExecutionRequest): Promise<RunEstimate>;
  resolveActionParams(
    req: Pick<RegisteredActionRequest, 'action_id' | 'scope' | 'params'>,
  ): Promise<ActionParamResolution>;
  runAction(
    req: ActionExecutionRequest,
    options?: RunActionInvocationOptions,
  ): Promise<RunActionLaunchResult>;
  runProposal(
    proposal: CopilotProposal,
    confirmed?: boolean,
    consentedPromiseSetHash?: string,
    options?: RunActionInvocationOptions,
  ): Promise<{ run_id: number | null; output_sheet_id?: string | null }>;
  listActionJobs(
    status?: string | null,
    limit?: number,
    options?: ProjectInvocationOptions,
  ): Promise<ActionJobsPage>;
  getActionJob?(jobId: number | string, options?: ProjectInvocationOptions): Promise<ActionJob>;
  /** Attempt receipts for this project, newest first. `runId` narrows to one
   *  run; omitting it is the ONLY way to reach a compaction-orphaned attempt,
   *  whose `run_id` is null by design. */
  listAttemptReceipts(
    runId?: number | string | null,
    limit?: number,
  ): Promise<AttemptReceiptsPage>;
  getRunProgress(runId: string, options?: ProjectInvocationOptions): Promise<RunProgress>;
  cancelRun(runId: string, options?: ProjectInvocationOptions): Promise<RunProgress>;
  backfillColumn(
    sheetId: string,
    columnName: string,
    confirmed?: boolean,
    /** Deliberate exact-row retry (run.backfill row_ids): re-run EXACTLY these
     *  rows regardless of current outcome — including terminal empty_output
     *  rows the default sweep never touches. Omitted: the default sweep. */
    rowIds?: number[],
    /** Echo the promise set shown by the 402 being confirmed. */
    consentedPromiseSetHash?: string,
  ): Promise<BackfillResult>;
  clusterPreview(input: {
    sheetId: string;
    inputColumn: string;
    method: string;
    minSize?: number;
    threshold?: number;
    ngramSize?: number;
    /** Cluster-by-key: an optional derived-key template ({{value}} + blessed
     *  before/after/lower transforms). The method keys/embeds on the derived
     *  value; display + merge stay on the original forms. Omitted -> cluster
     *  on the surface directly. */
    keyTemplate?: string;
  }): Promise<ClusterPreviewResult>;
  /** Read-only distinct-value inventory for the RESOLVE authoring surfaces
   *  (POST /column-values/v1/preview). `search` is a case-insensitive
   *  substring filter over values; limit defaults server-side to 500 (max
   *  2000), offset to 0. */
  columnValuesPreview(input: {
    sheetId: string;
    inputColumn: string;
    search?: string;
    limit?: number;
    offset?: number;
  }): Promise<ColumnValuesPreview>;
  /** Read-only fingerprint-grouped mention inventory for the Mentions panel
   *  (POST /entity-mentions/v1/preview). The column must be a `json` column
   *  marked `semantic_type='entity_mentions'` — an ineligible column is a
   *  typed `column_not_entity_mentions` ApiError, never adapted at read time.
   *  `search` is a case-insensitive substring match over every raw surface and
   *  `type` a canonical entity type; both are applied server-side BEFORE
   *  paging. limit/offset page WITHIN each type section (within the one type
   *  when `type` is sent); limit defaults server-side to 100 (max 500),
   *  offset to 0. */
  entityMentionsPreview(input: {
    sheetId: string;
    columnId: string;
    search?: string;
    type?: string;
    limit?: number;
    offset?: number;
  }): Promise<EntityMentionsPreview>;
  /** Read-only rules dry-run for the Replace drawer (POST
   *  /replace-rules/v1/preview): per-rule first-match-wins counts plus an
   *  optional single-value live test. */
  replaceRulesPreview(input: {
    sheetId: string;
    inputColumn: string;
    rules: ResolveReplaceRuleDraft[];
    unmatched?: 'keep' | 'null';
    testValue?: string;
  }): Promise<ReplaceRulesPreview>;
  /** The annotation layers whose coordinate surface is this cell — the inverse
   *  of `getCellEvidence`'s "what supports
   *  this output". Fetched lazily for the ACTIVE cell only; a cell with no
   *  layers resolves with `layers: []` rather than throwing. */
  getTextAnnotations(rowId: string, columnId: string): Promise<TextAnnotations>;
  /** The documents ONE normalized mention appears in, paged (POST
   *  /entity-mentions/v1/documents). The SCOPED counterpart to
   *  `entityMentionsPreview`: identity is exactly one of `fingerprint` or
   *  `text`, matching the two selector kinds a mention group emits. */
  entityMentionDocuments(input: {
    sheetId: string;
    columnId: string;
    type: string;
    fingerprint?: string;
    text?: string;
    limit?: number;
    offset?: number;
  }): Promise<EntityMentionDocumentsPage>;
  /** Where inside ONE document a mention occurs, paged, with a text snippet
   *  around each (POST /entity-mentions/v1/occurrences). Fetched LAZILY, only
   *  for a document the reader expands: resolving every listed document's
   *  occurrences up front is the cost R-snippet warns about. The identity is
   *  the same exactly-one-of pair the documents route takes. */
  entityMentionOccurrences(input: {
    sheetId: string;
    rowId: string;
    columnId: string;
    type: string;
    fingerprint?: string;
    text?: string;
    limit?: number;
    offset?: number;
    snippetRadius?: number;
  }): Promise<EntityMentionOccurrencesPage>;
  getRunRows(runId: string, offset?: number, limit?: number, status?: 'error'): Promise<RunRowsPage>;
  /** Provenance for an AI column: a bounded newest-first run history page. */
  getColumnRuns(columnId: string, offset?: number, limit?: number): Promise<ColumnRunsInfo>;
  /** Reopen a persisted run on exactly the rows graded by a human. */
  getReviewedRunRevision(columnId: string, runId: string): Promise<GeneratedActionDraft>;
  /** Row-scoped trace evidence for "Explain this cell". Keeps large/debug
   *  runs bounded and returns typed absence states instead of a full row list. */
  getRunTraceRow(
    runId: string,
    rowId: string,
    columnId?: string | null,
  ): Promise<RunTraceRowEvidence>;
  getCellEvidence(
    rowId: string,
    columnId: string,
    opts?: { includeStale?: boolean },
  ): Promise<CellEvidencePayload>;
  /** The Grounded Answers view's middle-pane batch — one call per (sheet,
   *  column) instead of one getCellEvidence call per visible row. `rowIds`
   *  scopes the batch to the list's virtualization window; omitted = every
   *  row on the sheet. */
  getColumnEvidence(
    sheetId: string,
    columnId: string,
    opts?: { rowIds?: readonly string[] },
  ): Promise<ColumnEvidenceBatchPayload>;
  getEvidenceViewer(evidenceLinkId: string | number): Promise<EvidenceViewerPayload>;
  /** Project-level data-flow manifest: which providers/models/actions/runs touched data. */
  getProvenanceManifest(runsOffset?: number, runsLimit?: number, receiptsOffset?: number, receiptsLimit?: number): Promise<ProvenanceManifest>;
  /** Canonical v1 receipt lookup shared by provenance, runs, results, and exports. */
  getReceipt(receiptId: string, options?: ProjectInvocationOptions): Promise<V1Receipt>;
  undo(): Promise<HistoryState>;
  redo(): Promise<HistoryState>;
  /** Start an in-memory action preview: computes a small sample server-side and
   *  returns a job id to poll. Nothing is persisted (no op/run/column/cell). */
  startPreview(req: ActionExecutionRequest): Promise<PreviewStartResult>;
  /** Poll a preview job's status; once `status === 'done'` the sample rows/columns
   *  are on the result for the grid overlay. */
  getPreview(previewId: string): Promise<PreviewSampleResult>;
  /** Cancel a preview job (idempotent): stops server compute and drops the job. */
  cancelPreview(previewId: string): Promise<void>;
  getHistory(offset?: number | null, limit?: number): Promise<HistoryState>;
  /** Jump the op-log pointer to a global operation index (step back/forward in the panel). */
  stepTo(opIndex: number): Promise<HistoryState>;
  /** Pending review count, optionally scoped to one action run. */
  getReviewCount(runId?: string): Promise<number>;
  getReviewBundles(
    offset?: number,
    limit?: number,
    runId?: string,
    includeReviewed?: boolean,
  ): Promise<ReviewBundlePage>;
  reviewItem(
    itemId: string,
    action: ReviewAction,
    editedValue?: CellValue,
    note?: string | null,
  ): Promise<void>;
  projectExportUrl(includeMediaOrOptions?: boolean | ProjectExportOptions): string;
  sheetDatasetExportUrl(options: SheetDatasetExportOptions): string;
  workLogExportUrl(format?: 'md' | 'html' | 'pdf'): string;
  listProjects(): Promise<ProjectInfo[]>;
  createProject(name: string): Promise<ProjectInfo>;
  copilotChat(
    messages: CopilotChatMessageInput[],
    model?: string | null,
  ): Promise<CopilotReply>;
  updateProject(
    projectId: string,
    patch: { name?: string; description?: string; starred?: boolean; archived?: boolean },
  ): Promise<ProjectInfo>;
  getProject(): Promise<ProjectInfo>;
  updateCurrentProject(input: {
    name?: string;
    description?: string | null;
  }): Promise<ProjectInfo>;
  getProjectRetention(): Promise<ProjectRetentionPolicy>;
  updateProjectRetention(
    input: Partial<ProjectRetentionPolicy>,
  ): Promise<ProjectRetentionPolicy>;
  getProjectNetworkPolicy(): Promise<ProjectNetworkPolicy>;
  updateProjectNetworkPolicy(input: {
    mode: string;
  }): Promise<ProjectNetworkPolicy>;
  /** Egress-proxy status for the youtube_provider_blocked remediation card.
   *  Local tier (no org identity) 404s — callers branch on ApiError.status. */
  getMediaProxyStatus(): Promise<MediaProxyStatus>;
  compactProject(): Promise<Record<string, unknown>>;
  getProjectSettings(): Promise<ProjectSettings>;
  updateProjectSettings(
    input: Partial<ProjectSettings>,
  ): Promise<ProjectSettings>;
  getProjectProviderKeys(): Promise<ProjectProviderKeys>;
  setProjectProviderKey(
    provider: string,
    key: string,
    spendCapUsd?: number | null,
    validationToken?: string | null,
  ): Promise<ProjectProviderKeys>;
  validateProjectProviderKey(
    provider: string,
    key?: string,
  ): Promise<ProviderValidateResult>;
  deleteProjectProviderKey(provider: string): Promise<{
    ok: boolean;
    deleted: boolean;
    provider: string;
  }>;
  getProjectSecrets(): Promise<ProjectSecrets>;
  setProjectSecret(name: string, value: string): Promise<ProjectSecrets>;
  deleteProjectSecret(name: string): Promise<{
    ok: boolean;
    deleted: boolean;
    name: string;
  }>;
  listMcpServers(): Promise<import('./mcpServers').McpServer[]>;
  createMcpServer(input: import('./mcpServers').McpServerDraft): Promise<import('./mcpServers').McpServer>;
  updateMcpServer(id: string, patch: Partial<import('./mcpServers').McpServerDraft>): Promise<import('./mcpServers').McpServer>;
  deleteMcpServer(id: string): Promise<{ ok: boolean; deleted: boolean }>;
  testMcpServer(id: string): Promise<import('./mcpServers').McpServer>;
  deleteProject(projectId: string | undefined, confirmName: string): Promise<void>;
  // ---- live sources -------------------------------------------------------
  /** All configured sources for the project (empty when none). */
  listSources(): Promise<SourceInfo[]>;
  /** One source with its recent poll history. */
  getSource(sourceId: number, runsOffset?: number | null, runsLimit?: number): Promise<SourceDetail>;
  /** Redacted operational health for one source. */
  getSourceHealth(sourceId: number, runsOffset?: number | null, runsLimit?: number): Promise<SourceHealth>;
  createSource(input: SourceInput): Promise<SourceInfo>;
  updateSource(sourceId: number, patch: Partial<SourceInput>): Promise<SourceInfo>;
  deleteSource(sourceId: number): Promise<void>;
  /** Manual "fetch now" through generic source.poll. Rejects on ingest failure;
   *  the failed poll is still recorded in the source's run history. */
  fetchSource(sourceId: number): Promise<SourceFetchResult>;
}
