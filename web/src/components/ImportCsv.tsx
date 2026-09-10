import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type DragEvent } from 'react';
import {
  AtSign,
  CheckCircle2,
  Globe2,
  Link2,
  ListVideo,
  RefreshCw,
  Rss,
  Scale,
  Upload,
  X,
} from 'lucide-react';
import {
  confirmImportRowsDraft,
  detectPastedRowsDraft,
  importCsv,
  importFiles,
  importFollowTheMoney,
  importUrls,
  importXlsx,
  executeBulkImport,
  planBulkImport,
  previewCsv,
  previewXlsx,
  previewImportRowUpdates,
  previewImportedFileUpdates,
  updateRowsFromImportedFile,
  type ImportDraft,
  type ImportDraftMappingColumn,
  type ImportCsvPreview,
  type ImportUpdatePreview,
  type BulkImportFailedOutput,
  type BulkImportPlan,
  type SourceInfo,
  type ActionTemplate,
  type SourceInput,
  type SourceInterval,
  type SheetMeta,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { PanelSelect } from './PanelSelect';
import { ImportDraftMappingPanel } from './importWorkspace/ImportDraftMappingPanel';
import { ImportModeRail } from './importWorkspace/ImportModeRail';
import { ImportPasteDetectPanel } from './importWorkspace/ImportPasteDetectPanel';
import { ImportStageStepper } from './importWorkspace/ImportStageStepper';
import {
  IMPORT_MODES,
  IMPORT_WORKSPACE_STAGES,
  type FileImportMode,
  type ImportMode,
  type ImportWorkspaceStage,
} from './importWorkspace/model';
import {
  describeColumns,
  nerImportOffer,
  type NerImportOffer,
} from './nerImportPromptModel';
import {
  byteBucket,
  columnBucket,
  durationBucket,
  failureCategory,
  rowBucket,
  sendProductTelemetry,
  type ImportFormat,
  type SourceKind,
} from '../telemetry/productTelemetry';

interface ImportHandlers {
  onImported(sheetId: number): void;
  onError(message: string): void;
  /** The "Download media?" prompt's Download
   *  button routes here — the same runActionFromSurface(kind, column) shape
   *  the caret menu uses, so the action drawer opens pre-bound to the
   *  detected URL column. */
  onLaunchDownload(launcherKind: string, columnName: string): void;
  /** The "Find the names in it?" prompt's primary button. Opens the map.ner
   *  drawer on the just-imported sheet; it never starts extraction. */
  onExtractEntities?(): void;
}

type FeedSourceKind =
  | 'rss'
  | 'youtube_playlist'
  | 'youtube_channel'
  | 'api_list_dicts'
  | 'courtlistener_docket';

function importFormat(files: File[], mode?: FileImportMode): ImportFormat {
  if (mode === 'csv') return 'csv';
  if (mode === 'xlsx') return 'xlsx';
  const formats = new Set(files.map((file) => {
    const suffix = file.name.split('.').pop()?.toLowerCase();
    if (suffix === 'jpeg' || suffix === 'jpg' || suffix === 'png' || suffix === 'gif' || suffix === 'webp') return 'image';
    if (suffix === 'mp3' || suffix === 'wav' || suffix === 'm4a' || suffix === 'flac') return 'audio';
    if (suffix === 'mp4' || suffix === 'mov' || suffix === 'webm') return 'video';
    if (suffix === 'txt' || suffix === 'md') return 'text';
    if (suffix && ['csv', 'xlsx', 'json', 'jsonl', 'parquet', 'pdf', 'html', 'htm'].includes(suffix)) return suffix === 'htm' ? 'html' : suffix;
    return 'other';
  }));
  return formats.size === 1 ? Array.from(formats)[0] as ImportFormat : 'mixed';
}

function telemetrySourceKind(kind: FeedSourceKind): SourceKind {
  if (kind === 'api_list_dicts') return 'api';
  if (kind === 'courtlistener_docket') return 'courtlistener';
  return kind;
}

const FEED_SOURCE_KINDS: Array<{
  value: FeedSourceKind;
  label: string;
  placeholder: string;
}> = [
  {
    value: 'rss',
    label: 'RSS',
    placeholder: 'https://example.com/feed.xml',
  },
  {
    value: 'youtube_playlist',
    label: 'YouTube playlist',
    placeholder: 'https://www.youtube.com/playlist?list=PL...',
  },
  {
    value: 'youtube_channel',
    label: 'YouTube channel',
    placeholder: 'https://www.youtube.com/@example',
  },
  {
    value: 'api_list_dicts',
    label: 'API list',
    placeholder: 'https://example.gov/api/items.json',
  },
  {
    value: 'courtlistener_docket',
    label: 'CourtListener docket',
    placeholder: 'https://www.courtlistener.com/docket/123456/example/',
  },
];

const INTERVALS: { value: SourceInterval; label: string }[] = [
  { value: '', label: 'Manual' },
  { value: '@hourly', label: 'Hourly' },
  { value: '0 */6 * * *', label: 'Every 6 hours' },
  { value: '@daily', label: 'Daily' },
];

const ACCEPTS: Record<FileImportMode, string> = {
  csv: '.csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  xlsx: '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  files: 'image/*,audio/*,video/*,application/pdf,text/plain,application/json,.csv,.xlsx,.json,.txt,.eml,.mbox,.zip,message/rfc822,application/mbox,application/x-mbox,application/zip,application/x-zip-compressed,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

const FTM_ACCEPT = '.json,.jsonl,application/json,application/x-ndjson';
type SingleFileFormat = 'csv' | 'ftm';

const CSV_ENCODINGS = [
  { value: '', label: 'Auto-detect' },
  { value: 'utf-8', label: 'UTF-8' },
  { value: 'cp1252', label: 'Windows-1252' },
  { value: 'iso-8859-1', label: 'ISO-8859-1 (Latin-1)' },
  { value: 'iso-8859-2', label: 'ISO-8859-2 (Central European)' },
  { value: 'shift_jis', label: 'Shift JIS' },
  { value: 'mac_roman', label: 'Mac Roman' },
  { value: 'utf-16', label: 'UTF-16' },
  { value: 'utf-32', label: 'UTF-32' },
] as const;

function classifySingleImportFile(
  file: File,
): Extract<FileImportMode, 'csv' | 'xlsx'> | null {
  const name = file.name.toLowerCase();
  if (name.endsWith('.xlsx')) return 'xlsx';
  if (name.endsWith('.csv')) return 'csv';
  if (file.type === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') {
    return 'xlsx';
  }
  if (file.type === 'text/csv') return 'csv';
  return null;
}

function logicalPathForImport(file: File): string {
  const relativePath = (file as File & { webkitRelativePath?: unknown }).webkitRelativePath;
  return typeof relativePath === 'string' && relativePath ? relativePath : file.name;
}

function directSelectionNeedsBulkPlan(files: File[]): boolean {
  return files.length >= 2 || (
    files.length === 1 && (
      /\.(?:eml|mbox|zip)$/i.test(files[0].name) ||
      !/\.[^./]+$/.test(files[0].name)
    )
  );
}

function directSelectionExpandsArchive(files: File[]): boolean {
  return files.length === 1 && files[0].name.toLowerCase().endsWith('.zip');
}

function bulkPlanValidationError(plan: BulkImportPlan): string | null {
  if (!plan || typeof plan.plan_id !== 'string' || !plan.plan_id) {
    return 'The import plan is malformed and cannot be executed.';
  }
  if (!Array.isArray(plan.questions) || !Array.isArray(plan.proposed_outputs)) {
    return 'The import plan is malformed and cannot be executed.';
  }
  const questionIds = new Set<string>();
  for (const question of plan.questions) {
    if (
      !question ||
      typeof question.id !== 'string' ||
      !question.id ||
      questionIds.has(question.id) ||
      question.kind !== 'csv_combine' ||
      (question.default !== 'combine' && question.default !== 'separate') ||
      !Array.isArray(question.logical_paths) ||
      !question.logical_paths.every((path) => typeof path === 'string')
    ) {
      return 'The import plan contains an unsupported required choice and cannot be executed.';
    }
    questionIds.add(question.id);
  }
  for (const output of plan.proposed_outputs) {
    if (
      !output ||
      typeof output.id !== 'string' ||
      typeof output.kind !== 'string' ||
      typeof output.sheet_name !== 'string' ||
      !Array.isArray(output.logical_paths) ||
      !output.logical_paths.every((path) => typeof path === 'string')
    ) {
      return 'The import plan is malformed and cannot be executed.';
    }
  }
  return null;
}

interface ImportWorkspaceState {
  mode: ImportMode;
  stage: ImportWorkspaceStage;
  urlText: string;
  pasteText: string;
  draft: ImportDraft | null;
  draftColumns: ImportDraftMappingColumn[];
  draftSheetName: string;
  draftBusy: boolean;
  draftError: string | null;
}

type ImportWorkspaceAction =
  | { type: 'selectMode'; mode: ImportMode }
  | { type: 'resetDraft' }
  | { type: 'setUrlText'; value: string }
  | { type: 'setPasteText'; value: string }
  | { type: 'setDraftSheetName'; value: string }
  | { type: 'setDraftStage'; stage: ImportWorkspaceStage }
  | { type: 'updateDraftColumn'; key: string; patch: Partial<ImportDraftMappingColumn> }
  | { type: 'detectDraftStart' }
  | { type: 'detectDraftSuccess'; draft: ImportDraft }
  | { type: 'draftFailure'; message: string }
  | { type: 'confirmDraftStart' }
  | { type: 'confirmDraftSettled' };

const initialImportWorkspaceState = (mode: ImportMode): ImportWorkspaceState => ({
  mode,
  stage: 'detect',
  urlText: '',
  pasteText: '',
  draft: null,
  draftColumns: [],
  draftSheetName: '',
  draftBusy: false,
  draftError: null,
});

function resetImportDraft(state: ImportWorkspaceState): ImportWorkspaceState {
  return {
    ...state,
    stage: 'detect',
    draft: null,
    draftColumns: [],
    draftSheetName: '',
    draftError: null,
  };
}

function importWorkspaceReducer(
  state: ImportWorkspaceState,
  action: ImportWorkspaceAction,
): ImportWorkspaceState {
  switch (action.type) {
    case 'selectMode':
      return resetImportDraft({ ...state, mode: action.mode });
    case 'resetDraft':
      return resetImportDraft(state);
    case 'setUrlText':
      return { ...state, urlText: action.value };
    case 'setPasteText':
      return { ...state, pasteText: action.value, draftError: null };
    case 'setDraftSheetName':
      return { ...state, draftSheetName: action.value };
    case 'setDraftStage':
      return { ...state, stage: action.stage };
    case 'updateDraftColumn':
      return {
        ...state,
        draftColumns: state.draftColumns.map((column) =>
          column.key === action.key ? { ...column, ...action.patch } : column,
        ),
      };
    case 'detectDraftStart':
      return { ...state, draftBusy: true, draftError: null };
    case 'detectDraftSuccess':
      return {
        ...state,
        draft: action.draft,
        draftColumns: action.draft.columns.map((column) => ({
          key: column.key,
          name: column.name,
          type: column.type,
          format: column.format,
          include: column.include,
        })),
        draftSheetName: action.draft.sheet_name,
        stage: 'map',
        draftBusy: false,
        draftError: null,
      };
    case 'draftFailure':
      return { ...state, draftBusy: false, draftError: action.message };
    case 'confirmDraftStart':
      return { ...state, draftBusy: true, draftError: null };
    case 'confirmDraftSettled':
      return { ...state, draftBusy: false };
    default:
      return state;
  }
}

interface FeedSourceFormState {
  name: string;
  url: string;
  kind: FeedSourceKind;
  cadence: SourceInterval;
  apiListPath: string;
  apiItemIdPath: string;
  apiUpdatedAtPath: string;
  apiMaxItems: string;
  youtubeMaxPages: string;
  youtubeMaxItems: string;
  courtKeywords: string;
  courtIncludeParties: boolean;
  courtIncludeDocuments: boolean;
  courtMaxEntries: string;
  busy: boolean;
  error: string | null;
}

type FeedSourceFormAction =
  | { type: 'setName'; value: string }
  | { type: 'setUrl'; value: string; detectedKind?: FeedSourceKind }
  | { type: 'setKind'; value: FeedSourceKind }
  | { type: 'setCadence'; value: SourceInterval }
  | { type: 'setApiListPath'; value: string }
  | { type: 'setApiItemIdPath'; value: string }
  | { type: 'setApiUpdatedAtPath'; value: string }
  | { type: 'setApiMaxItems'; value: string }
  | { type: 'setYoutubeMaxPages'; value: string }
  | { type: 'setYoutubeMaxItems'; value: string }
  | { type: 'setCourtKeywords'; value: string }
  | { type: 'setCourtIncludeParties'; value: boolean }
  | { type: 'setCourtIncludeDocuments'; value: boolean }
  | { type: 'setCourtMaxEntries'; value: string }
  | { type: 'createStart' }
  | { type: 'createFailure'; message: string }
  | { type: 'createSettled' };

const initialFeedSourceFormState = (): FeedSourceFormState => ({
  name: '',
  url: '',
  kind: 'rss',
  cadence: '',
  apiListPath: '/',
  apiItemIdPath: '',
  apiUpdatedAtPath: '',
  apiMaxItems: '',
  youtubeMaxPages: '',
  youtubeMaxItems: '',
  courtKeywords: '',
  courtIncludeParties: true,
  courtIncludeDocuments: true,
  courtMaxEntries: '',
  busy: false,
  error: null,
});

function feedSourceFormReducer(
  state: FeedSourceFormState,
  action: FeedSourceFormAction,
): FeedSourceFormState {
  switch (action.type) {
    case 'setName':
      return { ...state, name: action.value };
    case 'setUrl':
      return {
        ...state,
        url: action.value,
        kind: action.detectedKind ?? state.kind,
        error: null,
      };
    case 'setKind':
      return { ...state, kind: action.value, error: null };
    case 'setCadence':
      return { ...state, cadence: action.value };
    case 'setApiListPath':
      return { ...state, apiListPath: action.value, error: null };
    case 'setApiItemIdPath':
      return { ...state, apiItemIdPath: action.value };
    case 'setApiUpdatedAtPath':
      return { ...state, apiUpdatedAtPath: action.value };
    case 'setApiMaxItems':
      return { ...state, apiMaxItems: action.value };
    case 'setYoutubeMaxPages':
      return { ...state, youtubeMaxPages: action.value };
    case 'setYoutubeMaxItems':
      return { ...state, youtubeMaxItems: action.value };
    case 'setCourtKeywords':
      return { ...state, courtKeywords: action.value };
    case 'setCourtIncludeParties':
      return { ...state, courtIncludeParties: action.value };
    case 'setCourtIncludeDocuments':
      return { ...state, courtIncludeDocuments: action.value };
    case 'setCourtMaxEntries':
      return { ...state, courtMaxEntries: action.value };
    case 'createStart':
      return { ...state, busy: true, error: null };
    case 'createFailure':
      return { ...state, busy: false, error: action.message };
    case 'createSettled':
      return { ...state, busy: false };
    default:
      return state;
  }
}

const inferDroppedFileMode = (
  files: FileList | File[],
  selectedMode: FileImportMode,
): FileImportMode | null => {
  const list = Array.from(files);
  if (!list.length) return null;
  if (selectedMode === 'files' || list.length > 1) return 'files';

  const file = list[0];
  const classified = classifySingleImportFile(file);
  if (selectedMode === 'xlsx' && classified === 'xlsx') return classified;
  if (selectedMode === 'csv' && classified === 'csv') return classified;
  if (classified) return classified;
  return 'files';
};

function feedKindLabel(kind: FeedSourceKind): string {
  return FEED_SOURCE_KINDS.find((candidate) => candidate.value === kind)?.label ?? kind;
}

function feedKindIcon(kind: FeedSourceKind) {
  switch (kind) {
    case 'youtube_playlist':
    case 'youtube_channel':
      return <ListVideo size={14} aria-hidden />;
    case 'api_list_dicts':
      return <Globe2 size={14} aria-hidden />;
    case 'courtlistener_docket':
      return <Scale size={14} aria-hidden />;
    default:
      return <Rss size={14} aria-hidden />;
  }
}

function detectFeedSourceKind(raw: string): FeedSourceKind {
  const value = raw.trim();
  if (!value) return 'rss';
  if (/^[1-9][0-9]*$/.test(value)) return 'courtlistener_docket';
  if (/^@[\w.-]+$/.test(value)) return 'youtube_channel';
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
    const path = parsed.pathname.toLowerCase();
    if (host === 'youtube.com' || host === 'youtu.be' || host.endsWith('.youtube.com')) {
      if (parsed.searchParams.has('list') || path.includes('/playlist')) return 'youtube_playlist';
      if (
        host === 'youtu.be' ||
        path.startsWith('/@') ||
        path.startsWith('/channel/') ||
        path.startsWith('/c/') ||
        path.startsWith('/user/')
      ) {
        return 'youtube_channel';
      }
      return 'youtube_channel';
    }
    if (host === 'courtlistener.com' || host === 'www.courtlistener.com') {
      if (path.startsWith('/docket/')) return 'courtlistener_docket';
    }
    if (path.endsWith('.json') || path.includes('/api/') || path.endsWith('/api')) {
      return 'api_list_dicts';
    }
    if (
      path.endsWith('.xml') ||
      path.endsWith('.rss') ||
      path.endsWith('.atom') ||
      path.includes('/feed') ||
      path.includes('/rss') ||
      path.includes('/atom')
    ) {
      return 'rss';
    }
  } catch {
    // Free-form references such as YouTube handles and CourtListener ids are handled above.
  }
  return 'rss';
}

function optionalPositiveInt(value: string, field: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${field} must be a positive whole number`);
  }
  return parsed;
}

function splitKeywords(value: string): string[] {
  return value.split(/[\n,]/).flatMap((item) => {
    const trimmed = item.trim();
    return trimmed ? [trimmed] : [];
  });
}

function apiUrlIsHttps(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:';
  } catch {
    return false;
  }
}

function jsonPointerError(value: string, field: string): string | null {
  const text = value.trim() || '/';
  if (!text.startsWith('/')) return `${field} must start with /`;
  for (const segment of text.split('/').slice(1)) {
    for (let index = 0; index < segment.length; index += 1) {
      if (segment[index] !== '~') continue;
      const escaped = segment[index + 1];
      if (escaped !== '0' && escaped !== '1') return `${field} has an invalid ~ escape`;
      index += 1;
    }
  }
  return null;
}

function defaultFeedName(kind: FeedSourceKind, reference: string): string {
  const label = feedKindLabel(kind);
  const compact = reference
    .trim()
    .replace(/^https?:\/\//i, '')
    .replace(/^www\./i, '')
    .slice(0, 72);
  return compact ? `${label}: ${compact}` : label;
}

function buildFeedSourceInput({
  name,
  kind,
  url,
  cadence,
  apiListPath,
  apiItemIdPath,
  apiUpdatedAtPath,
  apiMaxItems,
  youtubeMaxPages,
  youtubeMaxItems,
  courtKeywords,
  courtIncludeParties,
  courtIncludeDocuments,
  courtMaxEntries,
}: {
  name: string;
  kind: FeedSourceKind;
  url: string;
  cadence: SourceInterval;
  apiListPath: string;
  apiItemIdPath: string;
  apiUpdatedAtPath: string;
  apiMaxItems: string;
  youtubeMaxPages: string;
  youtubeMaxItems: string;
  courtKeywords: string;
  courtIncludeParties: boolean;
  courtIncludeDocuments: boolean;
  courtMaxEntries: string;
}): SourceInput {
  const reference = url.trim();
  if (!reference) throw new Error('Feed URL or reference is required');
  const base: SourceInput = {
    name: name.trim() || defaultFeedName(kind, reference),
    kind,
    url: reference,
    schedule: cadence || null,
    enabled: true,
    config: {},
  };

  switch (kind) {
    case 'rss':
      // RSS uses the existing SourcesPanel-compatible unversioned config shape.
      return {
        ...base,
        config: { merge_strategy: 'append-new' },
      };
    case 'youtube_playlist': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.youtube.v1',
        playlist_url: reference,
      };
      const maxPages = optionalPositiveInt(youtubeMaxPages, 'Max pages');
      const maxItems = optionalPositiveInt(youtubeMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'youtube_channel': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.youtube.v1',
        channel_url: reference,
      };
      const maxPages = optionalPositiveInt(youtubeMaxPages, 'Max pages');
      const maxItems = optionalPositiveInt(youtubeMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'api_list_dicts': {
      if (!apiUrlIsHttps(reference)) throw new Error('API list sources require HTTPS');
      const listPathError = jsonPointerError(apiListPath, 'Response list path');
      if (listPathError) throw new Error(listPathError);
      const itemIdError = apiItemIdPath.trim()
        ? jsonPointerError(apiItemIdPath, 'Item ID path')
        : null;
      if (itemIdError) throw new Error(itemIdError);
      const updatedAtError = apiUpdatedAtPath.trim()
        ? jsonPointerError(apiUpdatedAtPath, 'Updated-at path')
        : null;
      if (updatedAtError) throw new Error(updatedAtError);
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.api_list_dicts.v1',
        method: 'GET',
        url: reference,
        list_path: apiListPath.trim() || '/',
        schema_policy: 'additive',
      };
      if (apiItemIdPath.trim()) config.item_id_path = apiItemIdPath.trim();
      if (apiUpdatedAtPath.trim()) config.updated_at_path = apiUpdatedAtPath.trim();
      const maxItems = optionalPositiveInt(apiMaxItems, 'Max items');
      if (maxItems !== undefined) config.max_items = maxItems;
      return { ...base, config };
    }
    case 'courtlistener_docket': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.courtlistener_docket.v1',
        keywords: splitKeywords(courtKeywords),
        include_parties: courtIncludeParties,
        include_documents: courtIncludeDocuments,
        recap_pdf_policy: 'link_only',
      };
      if (/^[1-9][0-9]*$/.test(reference)) {
        config.docket_id = Number(reference);
        return { ...base, url: null, config };
      } else {
        config.docket_url = reference;
      }
      const maxEntries = optionalPositiveInt(courtMaxEntries, 'Max entries');
      if (maxEntries !== undefined) config.max_entries_per_poll = maxEntries;
      return { ...base, config };
    }
  }
}

// uploadFiles/uploadUrls resolve to the
// sheet's id (or null on failure/no-op) instead of a bare success boolean —
// the caller decides whether to call onImported directly (the bare
// ImportDropzone drop path) or route through the shared finishImport
// classifier (ImportWorkspaceDialog's rich flow), which needs the id to
// await getSheetData before deciding whether to close or hold open.
function firstMaterializedSheetId(result: unknown): number | null {
  if (!result || typeof result !== 'object') return null;
  const outputs = (result as { outputs?: unknown }).outputs;
  if (!Array.isArray(outputs)) return null;
  const sheet = outputs.find((output) => (
    output && typeof output === 'object'
    && (output as { kind?: unknown }).kind === 'sheet'
    && typeof (output as { sheet_id?: unknown }).sheet_id === 'number'
  )) as { sheet_id?: number } | undefined;
  return sheet?.sheet_id ?? null;
}

function resultRows(result: unknown): number | undefined {
  if (!result || typeof result !== 'object') return undefined;
  const rows = (result as { rows?: unknown }).rows;
  return typeof rows === 'number' && Number.isFinite(rows) ? rows : undefined;
}

function useImportUpload({ onError }: ImportHandlers) {
  const { projectId } = useWorkspaceStores().chromePreferences;
  const [busy, setBusy] = useState(false);

  const uploadFiles = async (
    mode: FileImportMode | SingleFileFormat,
    files: FileList | File[] | null,
    encoding?: string,
    datasetName?: string,
    destinationSheetId?: number,
    columnMapping?: Record<string, string | null>,
    appendRequestKey?: string,
  ): Promise<number | null> => {
    if (busy) return null;
    const list = Array.from(files ?? []);
    if (!list.length) return null;
    const file = list[0];
    const resolvedMode = file && mode === 'csv' ? classifySingleImportFile(file) : mode;
    if (!file || resolvedMode == null) {
      onError('Import failed: choose a CSV or Excel file for single-file import');
      return null;
    }
    setBusy(true);
    const startedAt = performance.now();
    const kind = list.length > 1 ? 'files' : 'file';
    const format = resolvedMode === 'ftm' ? 'json' : importFormat(list, resolvedMode);
    const bytes = byteBucket(list.reduce((total, item) => total + item.size, 0));
    sendProductTelemetry({ type: 'Import.started', properties: { importKind: kind, format, bytes } }, projectId);
    try {
      const result =
        resolvedMode === 'csv'
          ? await importCsv(
            projectId, file, encoding, destinationSheetId,
            destinationSheetId ? appendRequestKey : undefined,
            columnMapping,
          )
          : resolvedMode === 'xlsx'
            ? await importXlsx(
              projectId, file, destinationSheetId,
              destinationSheetId ? appendRequestKey : undefined,
              columnMapping,
            )
            : resolvedMode === 'ftm'
              ? await importFollowTheMoney(projectId, file, datasetName)
            : await importFiles(projectId, list);
      sendProductTelemetry({
        type: 'Import.finished',
        properties: {
          importKind: kind,
          format,
          bytes,
          rows: rowBucket(resultRows(result)),
          columns: columnBucket(
            'columns' in result && Array.isArray(result.columns)
              ? result.columns.length
              : undefined,
          ),
          requestDuration: durationBucket(performance.now() - startedAt),
          result: 'success',
          failureCategory: 'none',
        },
      }, projectId);
      return resolvedMode === 'ftm'
        ? firstMaterializedSheetId(result)
        : ('sheet_id' in result && typeof result.sheet_id === 'number' ? result.sheet_id : null);
    } catch (e) {
      sendProductTelemetry({
        type: 'Import.finished',
        properties: {
          importKind: kind,
          format,
          bytes,
          rows: 'unknown',
          columns: 'unknown',
          requestDuration: durationBucket(performance.now() - startedAt),
          result: 'failed',
          failureCategory: failureCategory(e),
        },
      }, projectId);
      onError(`Import failed: ${e instanceof Error ? e.message : String(e)}`);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const uploadUrls = async (raw: string): Promise<number | null> => {
    if (busy) return null;
    const urls: string[] = [];
    for (const line of raw.split(/\r?\n/)) {
      const url = line.trim();
      if (url) urls.push(url);
    }
    if (!urls.length) {
      onError('Import failed: enter at least one URL');
      return null;
    }
    setBusy(true);
    const startedAt = performance.now();
    sendProductTelemetry({ type: 'Import.started', properties: { importKind: 'url', format: 'unknown', bytes: 'unknown' } }, projectId);
    try {
      const result = await importUrls(projectId, urls);
      sendProductTelemetry({
        type: 'Import.finished',
        properties: {
          importKind: 'url', format: 'unknown', bytes: 'unknown',
          rows: rowBucket(result.rows), columns: 'unknown',
          requestDuration: durationBucket(performance.now() - startedAt),
          result: 'success', failureCategory: 'none',
        },
      }, projectId);
      return result.sheet_id;
    } catch (e) {
      sendProductTelemetry({
        type: 'Import.finished',
        properties: {
          importKind: 'url', format: 'unknown', bytes: 'unknown', rows: 'unknown', columns: 'unknown',
          requestDuration: durationBucket(performance.now() - startedAt),
          result: 'failed', failureCategory: failureCategory(e),
        },
      }, projectId);
      onError(`Import failed: ${e instanceof Error ? e.message : String(e)}`);
      return null;
    } finally {
      setBusy(false);
    }
  };

  return { busy, uploadFiles, uploadUrls };
}

type ImportWorkspaceDialogProps = ImportHandlers & {
  open: boolean;
  initialCsv?: { file: File; preview: ImportCsvPreview } | null;
  initialFiles?: File[] | null;
  /** The resolved `ner` catalog entry, for its engine availability. Absent
   *  (no catalog yet) means the prompt simply does not offer. */
  nerTemplate?: ActionTemplate;
  /** True only when the ready action catalog exposes the FtM importer. */
  ftmImportEnabled?: boolean;
  /** Forces the dialog onto this mode for this open (Sources' "Add source"
   *  passes 'feed', since sources are for feeds).
   *  `null`/omitted -> the dialog reuses the last user-selected rail. */
  entryMode?: ImportMode | null;
  onClose(): void;
  onModeChange?(mode: ImportMode | null): void;
};

export function ImportWorkspaceDialog({ open, ...props }: ImportWorkspaceDialogProps) {
  // Keep only the selected rail outside an open session so a normal close can
  // reopen at the same entry point. Plans, choices, warnings, and requests
  // remain below this boundary and are discarded when `open` becomes false.
  const [lastMode, setLastMode] = useState<ImportMode>('csv');
  const initialMode = props.entryMode ?? lastMode;

  return open ? (
    <OpenImportWorkspaceSession
      {...props}
      initialMode={initialMode}
      onSessionModeChange={setLastMode}
    />
  ) : null;
}

function OpenImportWorkspaceSession({
  initialMode,
  onSessionModeChange,
  initialCsv = null,
  initialFiles = null,
  onClose,
  onImported,
  onError,
  onModeChange,
  onLaunchDownload,
  onExtractEntities,
  nerTemplate,
  ftmImportEnabled = false,
}: Omit<ImportWorkspaceDialogProps, 'open' | 'entryMode'> & {
  initialMode: ImportMode;
  onSessionModeChange(mode: ImportMode): void;
}) {
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
  const { busy: uploadBusy, uploadFiles, uploadUrls } = useImportUpload({ onImported, onError, onLaunchDownload });
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const onCloseRef = useRef(onClose);
  const onModeChangeRef = useRef(onModeChange);
  const [state, dispatch] = useReducer(
    importWorkspaceReducer,
    initialMode,
    initialImportWorkspaceState,
  );
  const {
    mode,
    stage,
    urlText,
    pasteText,
    draft,
    draftColumns,
    draftSheetName,
    draftBusy,
    draftError,
  } = state;
  const fileMode: FileImportMode =
    mode === 'xlsx' || mode === 'files' ? mode : 'csv';
  const railMode: ImportMode =
    mode === 'paste' || mode === 'xlsx' ? 'csv' : mode === 'urls' ? 'files' : mode;
  const active = IMPORT_MODES.find((m) => m.id === railMode) ?? IMPORT_MODES[0];

  // Sibling to
  // FeedSourceForm's own `populate` state — local, not a store slot,
  // for the same reason (no unmounted-target race: this prompt and its
  // onImported/onLaunchDownload targets live in the same always-mounted
  // component tree for the dialog's lifetime).
  const [downloadPrompt, setDownloadPrompt] = useState<{
    columnName: string;
    launcherKind: string;
  } | null>(null);
  const [nerPrompt, setNerPrompt] = useState<NerImportOffer | null>(null);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [singleFileFormat, setSingleFileFormat] = useState<SingleFileFormat>('csv');
  const [ftmDatasetName, setFtmDatasetName] = useState('');
  const [csvEncoding, setCsvEncoding] = useState('');
  const [csvPreview, setCsvPreview] = useState<ImportCsvPreview | null>(null);
  const [csvPreviewBusy, setCsvPreviewBusy] = useState(false);
  const [csvPreviewError, setCsvPreviewError] = useState<string | null>(null);
  const csvPreviewControllerRef = useRef<AbortController | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const zipInputRef = useRef<HTMLInputElement | null>(null);
  const bulkPlanControllerRef = useRef<AbortController | null>(null);
  const bulkExecuteControllerRef = useRef<AbortController | null>(null);
  const bulkGenerationRef = useRef(0);
  const initialFilesConsumedRef = useRef(false);
  const [bulkPlan, setBulkPlan] = useState<BulkImportPlan | null>(null);
  const [bulkDecisions, setBulkDecisions] = useState<Record<string, string>>({});
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkSuccessMessage, setBulkSuccessMessage] = useState<string | null>(null);
  const [bulkWarnings, setBulkWarnings] = useState<string[]>([]);
  const [bulkFailures, setBulkFailures] = useState<BulkImportFailedOutput[]>([]);
  const [bulkFailuresOmitted, setBulkFailuresOmitted] = useState(0);
  const [destinationSheets, setDestinationSheets] = useState<SheetMeta[]>([]);
  const [destinationSheetId, setDestinationSheetId] = useState('');
  const [draftDestinationMode, setDraftDestinationMode] = useState<'append' | 'update'>('append');
  const [keepExistingOnBlank, setKeepExistingOnBlank] = useState(false);
  const [updatePreview, setUpdatePreview] = useState<ImportUpdatePreview | null>(null);
  const updatePreviewControllerRef = useRef<AbortController | null>(null);
  const [csvColumnMapping, setCsvColumnMapping] = useState<Record<string, string | null>>({});
  const [fileDestinationMode, setFileDestinationMode] = useState<'append' | 'update'>('append');
  const [fileColumnPolicies, setFileColumnPolicies] = useState<Record<string, 'match' | 'update'>>({});
  const [fileUpdatePreview, setFileUpdatePreview] = useState<ImportUpdatePreview | null>(null);
  const fileUpdateControllerRef = useRef<AbortController | null>(null);
  const [fileUpdateBusy, setFileUpdateBusy] = useState(false);
  const [fileUpdateApplying, setFileUpdateApplying] = useState(false);
  const busy = uploadBusy || fileUpdateApplying;
  const [appendRequestKey, setAppendRequestKey] = useState(() => crypto.randomUUID());
  const activeCsvFile = csvFile ?? initialCsv?.file ?? null;
  const activeCsvPreview = csvFile ? csvPreview : initialCsv?.preview ?? null;
  // SheetMeta.parent is populated only from the list wire's parent_sheet_id;
  // imported roots may have a creation parent_op_id and remain appendable.
  const appendDestinationSheets = useMemo(
    () => destinationSheets.filter((sheet) => sheet.parent === undefined),
    [destinationSheets],
  );
  // A destination can disappear from a refreshed sheet list. Treat that
  // stale value as a new-sheet choice everywhere that can write an import.
  const appendDestinationSheetId = appendDestinationSheets.some(
    (sheet) => sheet.id === destinationSheetId,
  ) ? destinationSheetId : '';
  const appendColumnMapping = useCallback((preview: ImportCsvPreview, sheetId: string) => {
    const destination = appendDestinationSheets.find((sheet) => sheet.id === sheetId);
    if (!destination) return {};
    return Object.fromEntries(preview.columns.map((column) => [
      column.name,
      destination.columns.find(
        (target) => target.name.toLocaleLowerCase() === column.name.toLocaleLowerCase(),
      )?.name ?? null,
    ]));
  }, [appendDestinationSheets]);

  const loadCsvPreview = useCallback((file: File, encoding: string) => {
    fileUpdateControllerRef.current?.abort();
    fileUpdateControllerRef.current = null;
    setFileUpdateBusy(false);
    csvPreviewControllerRef.current?.abort();
    const controller = new AbortController();
    csvPreviewControllerRef.current = controller;
    setCsvPreviewBusy(true);
    setCsvPreview(null);
    setCsvPreviewError(null);
    setFileUpdatePreview(null);
    const previewRequest = classifySingleImportFile(file) === 'xlsx'
      ? previewXlsx(projectId, file)
      : previewCsv(projectId, file, encoding || undefined, { signal: controller.signal });
    void previewRequest.then((nextPreview) => {
      if (controller.signal.aborted) return;
      setCsvPreview(nextPreview);
      setCsvColumnMapping(appendColumnMapping(nextPreview, appendDestinationSheetId));
      setCsvPreviewBusy(false);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setCsvPreviewError(error instanceof Error ? error.message : String(error));
      setCsvPreviewBusy(false);
    });
  }, [appendColumnMapping, appendDestinationSheetId, projectId]);

  const resetCsvPreview = useCallback(() => {
    fileUpdateControllerRef.current?.abort();
    fileUpdateControllerRef.current = null;
    csvPreviewControllerRef.current?.abort();
    csvPreviewControllerRef.current = null;
    setCsvFile(null);
    setCsvEncoding('');
    setCsvPreview(null);
    setCsvPreviewBusy(false);
    setCsvPreviewError(null);
    setFileDestinationMode('append');
    setFileColumnPolicies({});
    setFileUpdatePreview(null);
    setFileUpdateBusy(false);
    setAppendRequestKey(crypto.randomUUID());
  }, []);

  useEffect(() => () => fileUpdateControllerRef.current?.abort(), []);

  const invalidateBulkRequests = useCallback(() => {
    bulkGenerationRef.current += 1;
    bulkPlanControllerRef.current?.abort();
    bulkPlanControllerRef.current = null;
    bulkExecuteControllerRef.current?.abort();
    bulkExecuteControllerRef.current = null;
  }, []);

  const resetBulkPlan = useCallback(() => {
    invalidateBulkRequests();
    setBulkPlan(null);
    setBulkDecisions({});
    setBulkBusy(false);
    setBulkError(null);
    setBulkSuccessMessage(null);
    setBulkWarnings([]);
    setBulkFailures([]);
    setBulkFailuresOmitted(0);
  }, [invalidateBulkRequests]);

  // A controlled close unmounts this entire open session. Abort requests here
  // rather than mirroring `open` into state: no response from this session may
  // publish into the next one, and unmount discards all local UI state.
  useEffect(() => () => {
    csvPreviewControllerRef.current?.abort();
    csvPreviewControllerRef.current = null;
    invalidateBulkRequests();
    onModeChangeRef.current?.(null);
  }, [invalidateBulkRequests]);

  const resetDraft = useCallback(() => {
    updatePreviewControllerRef.current?.abort();
    dispatch({ type: 'resetDraft' });
    setDestinationSheetId('');
    setDraftDestinationMode('append');
    setKeepExistingOnBlank(false);
    setUpdatePreview(null);
  }, []);

  useEffect(() => {
    let active = true;
    void projectApi.listSheets().then((sheets) => {
      if (active) setDestinationSheets(sheets);
    }).catch(() => {
      if (active) setDestinationSheets([]);
    });
    return () => { active = false; };
  }, [projectApi]);

  const selectDraftDestination = (sheetId: string) => {
    updatePreviewControllerRef.current?.abort();
    setUpdatePreview(null);
    setDestinationSheetId(sheetId);
    if (!sheetId) {
      setDraftDestinationMode('append');
      return;
    }
    const destination = destinationSheets.find((sheet) => sheet.id === sheetId);
    if (!destination || destination.parent !== undefined) {
      setDestinationSheetId('');
      return;
    }
    for (const source of draftColumns) {
      const matched = destination.columns.find(
        (column) => column.name.toLocaleLowerCase() === source.name.trim().toLocaleLowerCase(),
      );
      if (matched) {
        dispatch({ type: 'updateDraftColumn', key: source.key, patch: { name: matched.name, type: matched.type } });
      }
    }
  };

  const closeWorkspace = useCallback(() => {
    resetDraft();
    resetCsvPreview();
    resetBulkPlan();
    setDownloadPrompt(null);
    setNerPrompt(null);
    onClose();
  }, [onClose, resetBulkPlan, resetCsvPreview, resetDraft]);
  const closeWorkspaceFromUser = useCallback(() => {
    if (busy || bulkBusy || draftBusy) return;
    closeWorkspace();
  }, [bulkBusy, busy, closeWorkspace, draftBusy]);

  // The ONE shared success
  // handler replacing the three independent close-on-success calls
  // (confirmDraft; the file-input onChange and URL-submit upload callbacks
  // below — FeedSourceForm's own create()/populate flow is untouched, it
  // already holds the dialog open for its own "Populate feed?" prompt).
  // 1) reports the import up FIRST, unconditionally — every existing
  //    onImported consumer (the global sheets refresh) keeps working with
  //    no regression. 2) awaits getSheetData for the resulting
  //    sheet's columns — a call this adds, not a reuse of onImported's
  //    refresh, which cannot supply columns. 3) classifies via the
  //    server-computed mediaDownloadCandidate (compute_media_download_
  //    candidates, gated on the sheet having no audio/video/file column yet):
  //    found -> hold the dialog open on the download prompt; otherwise close
  //    immediately (unchanged for ordinary CSV/paste
  //    imports).
  const finishImport = useCallback(
    async (sheetId: number) => {
      onImported(sheetId);
      try {
        const page = await projectApi.getSheetData(String(sheetId), 0, 25);
        const candidate = page.columns.find((column) => column.mediaDownloadCandidate);
        if (candidate?.mediaDownloadCandidate) {
          setDownloadPrompt({
            columnName: candidate.name,
            launcherKind: candidate.mediaDownloadCandidate,
          });
          return;
        }
        // Media first when a sheet qualifies for both: undownloaded links are
        // the blocking next step, and there is no text to read until they
        // land. One prompt per import either way.
        const offer = nerImportOffer({
          columns: page.columns,
          rowCount: page.total,
          nerTemplate,
        });
        if (offer) {
          setNerPrompt(offer);
          return;
        }
      } catch {
        // Classification is a nicety, not a requirement — a failed lookup
        // here must never block the ordinary close-on-success path.
      }
      closeWorkspace();
    },
    [closeWorkspace, nerTemplate, onImported],
  );

  useEffect(() => {
    onCloseRef.current = closeWorkspace;
    onModeChangeRef.current = onModeChange;
  }, [closeWorkspace, onModeChange]);

  useEffect(() => {
    onModeChangeRef.current?.(mode);
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    const focusFrame = window.requestAnimationFrame(() => {
      dialogRef.current?.focus();
    });
    return () => {
      window.cancelAnimationFrame(focusFrame);
    };
  }, [mode]);

  const selectMode = (nextMode: ImportMode) => {
    if (busy) return;
    resetCsvPreview();
    resetBulkPlan();
    if (nextMode !== 'csv') {
      setSingleFileFormat('csv');
      setFtmDatasetName('');
    }
    onSessionModeChange(nextMode);
    dispatch({ type: 'selectMode', mode: nextMode });
  };

  const runDownloadPrompt = () => {
    if (!downloadPrompt) return;
    const { launcherKind, columnName } = downloadPrompt;
    closeWorkspace();
    onLaunchDownload(launcherKind, columnName);
  };

  const dismissDownloadPrompt = () => {
    closeWorkspace();
  };

  const runNerPrompt = () => {
    closeWorkspace();
    onExtractEntities?.();
  };

  const dismissNerPrompt = () => {
    closeWorkspace();
  };

  const detectPasteDraft = async () => {
    if (draftBusy || !pasteText.trim()) return;
    dispatch({ type: 'detectDraftStart' });
    try {
      const nextDraft = await detectPastedRowsDraft(projectId, pasteText);
      dispatch({ type: 'detectDraftSuccess', draft: nextDraft });
    } catch (e) {
      const message = `Import detection failed: ${e instanceof Error ? e.message : String(e)}`;
      dispatch({ type: 'draftFailure', message });
      onError(message);
    }
  };

  const updateDraftColumn = (
    key: string,
    patch: Partial<ImportDraftMappingColumn>,
  ) => {
    updatePreviewControllerRef.current?.abort();
    setUpdatePreview(null);
    dispatch({ type: 'updateDraftColumn', key, patch });
  };

  const previewDraftUpdates = async () => {
    if (!draft || draftBusy || !appendDestinationSheetId) return;
    const keyColumns = draftColumns
      .filter((column) => column.include && column.updatePolicy === 'match')
      .map((column) => column.name.trim());
    const controller = new AbortController();
    updatePreviewControllerRef.current?.abort();
    updatePreviewControllerRef.current = controller;
    dispatch({ type: 'confirmDraftStart' });
    try {
      const preview = await previewImportRowUpdates(projectId, {
        destinationSheetId: Number(appendDestinationSheetId),
        columns: draftColumns,
        keyColumns,
        raw: draft.raw,
        draftId: draft.draft_id,
        keepExistingOnBlank,
      }, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setUpdatePreview(preview);
      dispatch({ type: 'setDraftStage', stage: 'confirm' });
    } catch (e) {
      if (controller.signal.aborted) return;
      const message = `Preview failed: ${e instanceof Error ? e.message : String(e)}`;
      dispatch({ type: 'draftFailure', message });
      onError(message);
    } finally {
      if (updatePreviewControllerRef.current === controller) {
        updatePreviewControllerRef.current = null;
        dispatch({ type: 'confirmDraftSettled' });
      }
    }
  };

  const confirmDraft = async () => {
    if (!draft || draftBusy) return;
    if (draftDestinationMode === 'update' && !updatePreview) {
      const message = 'Preview updates before confirming.';
      dispatch({ type: 'draftFailure', message });
      onError(message);
      return;
    }
    dispatch({ type: 'confirmDraftStart' });
    const startedAt = performance.now();
    const bytes = byteBucket(new TextEncoder().encode(pasteText).byteLength);
    sendProductTelemetry({ type: 'Import.started', properties: { importKind: 'paste', format: 'text', bytes } }, projectId);
    try {
      const result = await confirmImportRowsDraft(projectId, draft, {
        sheetName: draftSheetName,
        destination: appendDestinationSheetId
          ? { kind: 'existing_sheet', sheetId: Number(appendDestinationSheetId) }
          : { kind: 'new_sheet' },
        columns: draftColumns,
        ...(draftDestinationMode === 'update' && updatePreview ? {
          update: {
            keyColumns: draftColumns
              .filter((column) => column.include && column.updatePolicy === 'match')
              .map((column) => column.name.trim()),
            keepExistingOnBlank,
            confirmation: updatePreview.confirmation,
          },
        } : {}),
      });
      sendProductTelemetry({
        type: 'Import.finished',
        properties: {
          importKind: 'paste', format: 'text', bytes,
          rows: rowBucket(result.rows), columns: columnBucket(result.columns?.length),
          requestDuration: durationBucket(performance.now() - startedAt), result: 'success', failureCategory: 'none',
        },
      }, projectId);
      await finishImport(result.sheet_id);
    } catch (e) {
      sendProductTelemetry({
        type: 'Import.finished',
        properties: {
          importKind: 'paste', format: 'text', bytes, rows: 'unknown', columns: 'unknown',
          requestDuration: durationBucket(performance.now() - startedAt), result: 'failed', failureCategory: failureCategory(e),
        },
      }, projectId);
      const message = `Import failed: ${e instanceof Error ? e.message : String(e)}`;
      dispatch({ type: 'draftFailure', message });
      onError(message);
    } finally {
      dispatch({ type: 'confirmDraftSettled' });
    }
  };

  const executeBulkPlan = useCallback((
    plan: BulkImportPlan,
    decisions: Record<string, string>,
  ) => {
    invalidateBulkRequests();
    const generation = bulkGenerationRef.current;
    const controller = new AbortController();
    bulkExecuteControllerRef.current = controller;
    setBulkBusy(true);
    setBulkError(null);
    void executeBulkImport(
      projectId,
      plan.plan_id,
      decisions,
      { signal: controller.signal },
    ).then(async (result) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      const hasFailures = result.failed.length > 0 || result.failed_omitted > 0;
      if (hasFailures) {
        // A 200 can still represent a partial (or total) import failure. The
        // server has consumed this one-shot plan, so remove its ready state
        // before navigating to any successful sheet.
        setBulkPlan(null);
        setBulkDecisions({});
        setBulkError(result.message);
        setBulkWarnings(result.warnings);
        setBulkFailures(result.failed);
        setBulkFailuresOmitted(result.failed_omitted);
        if (typeof result.first_sheet_id === 'number') {
          await onImported(result.first_sheet_id);
        }
        return;
      }
      if (typeof result.first_sheet_id === 'number') {
        await onImported(result.first_sheet_id);
        if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
        if (result.warnings.length) {
          // The plan is one-shot once execution starts. Keep only the
          // completed result open so warnings can be acknowledged without
          // offering a stale Execute button.
          setBulkPlan(null);
          setBulkDecisions({});
          setBulkSuccessMessage(result.message);
          setBulkWarnings(result.warnings);
          return;
        }
        closeWorkspace();
        return;
      }
      throw new Error('The import completed without a landing sheet.');
    }).catch((error: unknown) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      setBulkPlan(null);
      setBulkDecisions({});
      setBulkError(`Import failed: ${error instanceof Error ? error.message : String(error)}`);
    }).finally(() => {
      if (bulkExecuteControllerRef.current === controller && bulkGenerationRef.current === generation) {
        bulkExecuteControllerRef.current = null;
        setBulkBusy(false);
      }
    });
  }, [closeWorkspace, invalidateBulkRequests, onImported, projectId]);

  const startBulkPlan = useCallback((files: File[], expandArchive: boolean) => {
    if (busy || !files.length) return;
    invalidateBulkRequests();
    const generation = bulkGenerationRef.current;
    const controller = new AbortController();
    bulkPlanControllerRef.current = controller;
    setBulkPlan(null);
    setBulkDecisions({});
    setBulkError(null);
    setBulkSuccessMessage(null);
    setBulkWarnings([]);
    setBulkFailures([]);
    setBulkFailuresOmitted(0);
    setBulkBusy(true);
    void planBulkImport(
      projectId,
      files,
      files.map(logicalPathForImport),
      expandArchive,
      { signal: controller.signal },
    ).then((nextPlan) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      const validationError = bulkPlanValidationError(nextPlan);
      if (validationError) {
        setBulkPlan(nextPlan);
        setBulkError(validationError);
        return;
      }
      const decisions = Object.fromEntries(
        nextPlan.questions.map((question) => [question.id, question.default]),
      );
      if (nextPlan.proposed_outputs.length > 0 && nextPlan.questions.length === 0) {
        executeBulkPlan(nextPlan, decisions);
        return;
      }
      setBulkPlan(nextPlan);
      setBulkDecisions(decisions);
    }).catch((error: unknown) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      setBulkError(`Import planning failed: ${error instanceof Error ? error.message : String(error)}`);
    }).finally(() => {
      if (bulkPlanControllerRef.current === controller && bulkGenerationRef.current === generation) {
        bulkPlanControllerRef.current = null;
        setBulkBusy(false);
      }
    });
  }, [busy, executeBulkPlan, invalidateBulkRequests, projectId]);

  useEffect(() => {
    if (initialFilesConsumedRef.current || !initialFiles?.length) return;
    initialFilesConsumedRef.current = true;
    startBulkPlan(initialFiles, directSelectionExpandsArchive(initialFiles));
  }, [initialFiles, startBulkPlan]);

  const executePlannedImport = () => {
    if (!bulkPlan || bulkBusy || bulkPlanValidationError(bulkPlan)) return;
    if (bulkPlan.questions.some((question) => (
      bulkDecisions[question.id] !== 'combine' && bulkDecisions[question.id] !== 'separate'
    ))) {
      setBulkError('Choose an answer for every import decision before executing.');
      return;
    }
    executeBulkPlan(bulkPlan, bulkDecisions);
  };

  return (
    <dialog
      ref={dialogRef}
      className="import-workspace-layer"
      data-testid="import-workspace-dialog"
      aria-label="Import"
      onCancel={(event) => {
        event.preventDefault();
        closeWorkspaceFromUser();
      }}
      onClose={() => {
        closeWorkspaceFromUser();
      }}
    >
      <button
        type="button"
        className="import-workspace-backdrop"
        aria-label="Close import"
        onMouseDown={closeWorkspaceFromUser}
      />
      <section
        className="import-workspace"
        data-testid="import-workspace"
        aria-label="Import"
        tabIndex={-1}
      >
        <input
          aria-label="Choose import file"
          ref={inputRef}
          type="file"
          accept={singleFileFormat === 'ftm' ? FTM_ACCEPT : ACCEPTS[fileMode]}
          multiple={railMode === 'files'}
          style={{ display: 'none' }}
          data-testid={mode === 'csv' ? 'import-csv-input' : 'import-file-input'}
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            const selectedFile = files[0];
            if (directSelectionNeedsBulkPlan(files)) {
              resetCsvPreview();
              startBulkPlan(files, directSelectionExpandsArchive(files));
            } else if (fileMode === 'csv' && singleFileFormat === 'csv' && selectedFile && classifySingleImportFile(selectedFile)) {
              resetBulkPlan();
              setAppendRequestKey(crypto.randomUUID());
              setCsvFile(selectedFile);
              setCsvEncoding('');
              setCsvPreview(null);
              setCsvPreviewError(null);
              loadCsvPreview(selectedFile, '');
            } else {
              resetCsvPreview();
              resetBulkPlan();
              void uploadFiles(singleFileFormat === 'ftm' ? 'ftm' : fileMode, files, undefined, ftmDatasetName).then((sheetId) => {
                if (sheetId != null) void finishImport(sheetId);
              });
            }
            e.target.value = '';
          }}
        />
        <input
          aria-label="Choose folder"
          ref={(input) => {
            folderInputRef.current = input;
            input?.setAttribute('webkitdirectory', '');
          }}
          type="file"
          multiple
          style={{ display: 'none' }}
          data-testid="import-folder-input"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            resetCsvPreview();
            startBulkPlan(files, false);
            e.target.value = '';
          }}
        />
        <input
          aria-label="Choose ZIP"
          ref={zipInputRef}
          type="file"
          accept=".zip,application/zip,application/x-zip-compressed"
          style={{ display: 'none' }}
          data-testid="import-zip-input"
          onChange={(e) => {
            const file = e.target.files?.[0];
            resetCsvPreview();
            if (!file || !file.name.toLowerCase().endsWith('.zip')) {
              resetBulkPlan();
              setBulkError('Choose a ZIP archive to import its contents.');
            } else {
              startBulkPlan([file], true);
            }
            e.target.value = '';
          }}
        />
        <header className="import-workspace-header">
          <div>
            <div className="import-workspace-title">Import</div>
            <div className="import-workspace-kicker">Project data intake</div>
          </div>
          <button
            type="button"
            className="icon-btn"
            aria-label="Close import"
            data-testid="import-workspace-close"
            disabled={busy || bulkBusy || draftBusy}
            onClick={closeWorkspaceFromUser}
          >
            <X size={16} />
          </button>
        </header>
        <div className="import-workspace-main">
          <aside className="import-workspace-sidebar">
            <ImportModeRail modes={IMPORT_MODES} mode={railMode} onSelectMode={selectMode} />
          </aside>
          <main className="import-workspace-content">
            {/* Only the paste path has the numbered `detect → map → confirm`
                stages. CSV shows its preview and explicit confirmation inline;
                XLSX and generic file modes commit in one step.
                Drawn unconditionally, the numbered header promised a Check and
                a Confirm step that a Files import silently skipped — the sheet
                was already written behind the still-open dialog. The steps are
                drawn where they are real. */}
            {mode === 'paste' ? (
              <ImportStageStepper stages={IMPORT_WORKSPACE_STAGES} stage={stage} />
            ) : null}
            <div className="import-workspace-panel">
              {downloadPrompt ? (
                <MediaDownloadPrompt
                  columnName={downloadPrompt.columnName}
                  onRun={runDownloadPrompt}
                  onDismiss={dismissDownloadPrompt}
                />
              ) : nerPrompt ? (
                <NerExtractPrompt
                  offer={nerPrompt}
                  onRun={runNerPrompt}
                  onDismiss={dismissNerPrompt}
                />
              ) : mode === 'paste' ? (
                stage === 'map' && draft ? (
                  <ImportDraftMappingPanel
                    draft={draft}
                    columns={draftColumns}
                    sheetName={draftSheetName}
                    sheets={appendDestinationSheets}
                    destinationSheetId={appendDestinationSheetId}
                    destinationMode={draftDestinationMode}
                    keepExistingOnBlank={keepExistingOnBlank}
                    busy={draftBusy}
                    error={draftError}
                    onSheetNameChange={(value) => dispatch({ type: 'setDraftSheetName', value })}
                    onDestinationSheetChange={selectDraftDestination}
                    onDestinationModeChange={(value) => {
                      updatePreviewControllerRef.current?.abort();
                      setUpdatePreview(null);
                      setDraftDestinationMode(value);
                      if (value === 'update') {
                        for (const column of draftColumns) {
                          dispatch({
                            type: 'updateDraftColumn',
                            key: column.key,
                            patch: {
                              updatePolicy: 'update',
                            },
                          });
                        }
                      }
                    }}
                    onKeepExistingOnBlankChange={(value) => {
                      updatePreviewControllerRef.current?.abort();
                      setUpdatePreview(null);
                      setKeepExistingOnBlank(value);
                    }}
                    onColumnChange={updateDraftColumn}
                    onBack={() => dispatch({ type: 'setDraftStage', stage: 'detect' })}
                    onContinue={() => {
                      if (draftDestinationMode === 'update') void previewDraftUpdates();
                      else dispatch({ type: 'setDraftStage', stage: 'confirm' });
                    }}
                  />
                ) : stage === 'confirm' && draft ? (
                  <ImportDraftConfirmStep
                    draft={draft}
                    columns={draftColumns}
                    sheetName={draftSheetName}
                    destinationSheet={appendDestinationSheets.find((sheet) => sheet.id === appendDestinationSheetId) ?? null}
                    updatePreview={draftDestinationMode === 'update' ? updatePreview : null}
                    busy={draftBusy}
                    error={draftError}
                    onBack={() => dispatch({ type: 'setDraftStage', stage: 'map' })}
                    onConfirm={() => void confirmDraft()}
                  />
                ) : (
                  <ImportPasteDetectPanel
                    pasteText={pasteText}
                    busy={draftBusy}
                    error={draftError}
                    onPasteTextChange={(value) => {
                      dispatch({ type: 'setPasteText', value });
                    }}
                    onDetect={() => {
                      void detectPasteDraft();
                    }}
                  />
                )
              ) : mode === 'urls' ? (
                <>
                  <div className="import-mode-summary">
                    <Link2 size={16} aria-hidden />
                    <strong>URLs</strong>
                  </div>
                  <label className="import-field-label" htmlFor="import-url-list">
                    URLs
                  </label>
                  <textarea
                    id="import-url-list"
                    className="form-input import-url-list"
                    data-testid="import-url-list"
                    rows={8}
                    value={urlText}
                    placeholder="https://example.com/file.pdf"
                    onChange={(e) => dispatch({ type: 'setUrlText', value: e.target.value })}
                  />
                  <button
                    type="button"
                    className="btn btn-primary import-primary"
                    data-testid="import-url-submit"
                    disabled={busy}
                    onClick={() => {
                      void uploadUrls(urlText).then((sheetId) => {
                        if (sheetId != null) void finishImport(sheetId);
                      });
                    }}
                  >
                    <Link2 size={13} /> Import URLs
                  </button>
                </>
              ) : mode === 'feed' ? (
                <FeedSourceForm onCreated={onClose} onImported={onImported} onError={onError} />
              ) : (
                <>
                  <div className="import-mode-summary">
                    <Upload size={16} aria-hidden />
                    <strong>{active.label}</strong>
                    <span>{active.hint}</span>
                  </div>
                  {railMode === 'csv' ? (
                    <>
                      <label className="import-field-label" htmlFor="import-file-format">
                        Format
                      </label>
                      <select
                        id="import-file-format"
                        className="form-input"
                        data-testid="import-file-format"
                        value={singleFileFormat}
                        onChange={(event) => {
                          const next = event.target.value as SingleFileFormat;
                          setSingleFileFormat(next);
                          resetCsvPreview();
                        }}
                      >
                        <option value="csv">CSV / Excel (autodetect)</option>
                        {ftmImportEnabled ? <option value="ftm">FollowTheMoney (JSON/JSONL)</option> : null}
                      </select>
                      {singleFileFormat === 'ftm' ? (
                        <>
                          <label className="import-field-label" htmlFor="import-ftm-dataset-name">
                            Dataset name (optional)
                          </label>
                          <input
                            id="import-ftm-dataset-name"
                            className="form-input"
                            data-testid="import-ftm-dataset-name"
                            value={ftmDatasetName}
                            onChange={(event) => setFtmDatasetName(event.target.value)}
                          />
                        </>
                      ) : null}
                    </>
                  ) : null}
                  <button
                    type="button"
                    className="btn btn-primary import-primary import-file-choose"
                    data-testid="import-file-picker"
                    disabled={busy || bulkBusy}
                    onClick={() => inputRef.current?.click()}
                  >
                    <Upload size={13} /> {railMode === 'files' ? 'Choose files' : 'Choose one file'}
                  </button>
                  {(railMode === 'csv' || railMode === 'files') && (
                    <div className="import-step-actions">
                      <button
                        type="button"
                        className="btn"
                        disabled={busy || bulkBusy}
                        onClick={() => folderInputRef.current?.click()}
                      >
                        Choose folder
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={busy || bulkBusy}
                        onClick={() => zipInputRef.current?.click()}
                      >
                        Choose ZIP
                      </button>
                    </div>
                  )}
                  {railMode === 'csv' && (
                    <>
                      <p className="form-hint import-entry-note" data-testid="import-single-supported">
                        Frisket detects CSV and Excel files after you choose one.
                      </p>
                      {activeCsvFile ? (
                        <CsvEncodingPreview
                          file={activeCsvFile}
                          selectedEncoding={csvEncoding}
                          preview={activeCsvPreview}
                          busy={csvPreviewBusy}
                          importBusy={busy || fileUpdateBusy}
                          error={csvPreviewError}
                          sheets={appendDestinationSheets}
                          destinationSheetId={appendDestinationSheetId}
                          destinationMode={fileDestinationMode}
                          columnPolicies={fileColumnPolicies}
                          keepExistingOnBlank={keepExistingOnBlank}
                          updatePreview={fileUpdatePreview}
                          onDestinationSheetChange={(value) => {
                            setDestinationSheetId(value);
                            setCsvColumnMapping(
                              value && activeCsvPreview ? appendColumnMapping(activeCsvPreview, value) : {},
                            );
                            setAppendRequestKey(crypto.randomUUID());
                            if (!value) setFileDestinationMode('append');
                            setFileUpdatePreview(null);
                          }}
                          onDestinationModeChange={(value) => {
                            setFileDestinationMode(value);
                            setAppendRequestKey(crypto.randomUUID());
                            setFileUpdatePreview(null);
                            setFileColumnPolicies(Object.fromEntries(
                              (activeCsvPreview?.columns ?? []).map((column) => [column.name, 'update']),
                            ));
                          }}
                          onColumnPolicyChange={(source, policy) => {
                            setFileColumnPolicies((current) => ({ ...current, [source]: policy }));
                            setAppendRequestKey(crypto.randomUUID());
                            setFileUpdatePreview(null);
                          }}
                          onKeepExistingOnBlankChange={(value) => {
                            setKeepExistingOnBlank(value);
                            setAppendRequestKey(crypto.randomUUID());
                            setFileUpdatePreview(null);
                          }}
                          columnMapping={csvColumnMapping}
                          onColumnMappingChange={(source, target) => {
                            setCsvColumnMapping((current) => ({ ...current, [source]: target }));
                            setAppendRequestKey(crypto.randomUUID());
                            setFileUpdatePreview(null);
                          }}
                          onEncodingChange={(encoding) => {
                            setCsvFile(activeCsvFile);
                            setCsvEncoding(encoding);
                            loadCsvPreview(activeCsvFile, encoding);
                          }}
                          onImport={() => {
                            if (fileDestinationMode === 'update' && appendDestinationSheetId && activeCsvPreview) {
                              const format = classifySingleImportFile(activeCsvFile) === 'xlsx' ? 'xlsx' : 'csv';
                              const keyColumns = activeCsvPreview.columns
                                .filter((column) => csvColumnMapping[column.name] && fileColumnPolicies[column.name] === 'match')
                                .map((column) => csvColumnMapping[column.name] as string);
                              const input = {
                                destinationSheetId: Number(appendDestinationSheetId),
                                columnMapping: csvColumnMapping,
                                keyColumns,
                                keepExistingOnBlank,
                                ...(format === 'csv' && csvEncoding ? { encoding: csvEncoding } : {}),
                              };
                              if (fileUpdatePreview) {
                                setFileUpdateApplying(true);
                                void updateRowsFromImportedFile(projectId, activeCsvFile, format, {
                                  ...input, confirmation: fileUpdatePreview.confirmation,
                                  requestKey: appendRequestKey,
                                }).then((result) => finishImport(result.sheet_id))
                                  .catch((error) => onError(`Import failed: ${error instanceof Error ? error.message : String(error)}`))
                                  .finally(() => setFileUpdateApplying(false));
                                return;
                              }
                              fileUpdateControllerRef.current?.abort();
                              const controller = new AbortController();
                              fileUpdateControllerRef.current = controller;
                              setFileUpdateBusy(true);
                              void previewImportedFileUpdates(projectId, activeCsvFile, format, input, { signal: controller.signal }).then((preview) => {
                                if (controller.signal.aborted) return null;
                                setFileUpdatePreview(preview);
                                return null;
                              }).catch((error) => {
                                if (!controller.signal.aborted) {
                                  onError(`Import failed: ${error instanceof Error ? error.message : String(error)}`);
                                }
                              }).finally(() => {
                                if (fileUpdateControllerRef.current === controller) {
                                  fileUpdateControllerRef.current = null;
                                  setFileUpdateBusy(false);
                                }
                              });
                              return;
                            }
                            void uploadFiles(
                              classifySingleImportFile(activeCsvFile) ?? 'csv',
                              [activeCsvFile], csvEncoding || undefined, undefined,
                              appendDestinationSheetId ? Number(appendDestinationSheetId) : undefined,
                              appendDestinationSheetId ? csvColumnMapping : undefined,
                              appendRequestKey,
                            )
                              .then((sheetId) => {
                                if (sheetId != null) void finishImport(sheetId);
                              });
                          }}
                        />
                      ) : null}
                      <button
                        type="button"
                        className="mini-btn import-secondary"
                        data-testid="import-mode-paste"
                        onClick={() => selectMode('paste')}
                      >
                        Paste rows instead
                      </button>
                    </>
                  )}
                  {railMode === 'files' && (
                    <>
                      <p className="form-hint import-entry-note" data-testid="import-multiple-supported">
                        PDFs are added as documents. Import does not extract PDF tables.
                      </p>
                      <button
                        type="button"
                        className="mini-btn import-secondary"
                        data-testid="import-mode-urls"
                        onClick={() => selectMode('urls')}
                      >
                        Import a list of URLs instead
                      </button>
                    </>
                  )}
                  {(bulkPlan || bulkBusy || bulkError || bulkSuccessMessage || bulkWarnings.length || bulkFailures.length || bulkFailuresOmitted > 0) && (
                    <BulkImportPlanPanel
                      plan={bulkPlan}
                      decisions={bulkDecisions}
                      busy={bulkBusy}
                      error={bulkError}
                      successMessage={bulkSuccessMessage}
                      warnings={bulkWarnings}
                      failures={bulkFailures}
                      failuresOmitted={bulkFailuresOmitted}
                      onDecisionChange={(questionId, value) => {
                        setBulkDecisions((current) => ({ ...current, [questionId]: value }));
                      }}
                      onExecute={executePlannedImport}
                    />
                  )}
                </>
              )}
            </div>
          </main>
        </div>
      </section>
    </dialog>
  );
}

function BulkImportPlanPanel({
  plan,
  decisions,
  busy,
  error,
  successMessage,
  warnings,
  failures,
  failuresOmitted,
  onDecisionChange,
  onExecute,
}: {
  plan: BulkImportPlan | null;
  decisions: Record<string, string>;
  busy: boolean;
  error: string | null;
  successMessage: string | null;
  warnings: string[];
  failures: BulkImportFailedOutput[];
  failuresOmitted: number;
  onDecisionChange(questionId: string, value: string): void;
  onExecute(): void;
}) {
  const validationError = plan ? bulkPlanValidationError(plan) : null;
  if (!plan) {
    return (
      <>
        {busy ? <div role="status">Preparing import plan…</div> : null}
        {error ? <div className="import-field-error" role="alert">{error}</div> : null}
        {successMessage ? <div role="status">{successMessage}</div> : null}
        {(failures.length || failuresOmitted > 0) ? (
          <section aria-label="Import failures" data-testid="bulk-import-failures">
            {failures.length ? (
              <ul>
                {failures.map((failure, index) => (
                  <li key={`${failure.logical_paths.join('\u0000')}-${index}`}>
                    <span>{failure.logical_paths.join(', ') || 'Unknown file'}</span>: <span>{failure.error}</span>
                    {failure.logical_paths_omitted > 0 ? (
                      <div>
                        {failure.logical_paths_omitted} additional path{failure.logical_paths_omitted === 1 ? '' : 's'} were omitted.
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
            {failuresOmitted > 0 ? (
              <div>{failuresOmitted} additional failure{failuresOmitted === 1 ? '' : 's'} were omitted.</div>
            ) : null}
          </section>
        ) : null}
        {warnings.length ? (
          <section aria-label="Import warnings" data-testid="bulk-import-warnings">
            <h3>Import warnings</h3>
            <ul>
              {warnings.map((warning, index) => <li key={index}>{warning}</li>)}
            </ul>
          </section>
        ) : null}
      </>
    );
  }
  if (validationError) {
    return <div className="import-field-error" role="alert">{validationError}</div>;
  }
  if (!plan.proposed_outputs.length) {
    return (
      <div role="status">
        {plan.message || 'No importable files found.'}
      </div>
    );
  }
  return (
    <section aria-label="Bulk import plan">
      {busy ? <div role="status">Executing import…</div> : null}
      {error ? <div className="import-field-error" role="alert">{error}</div> : null}
      {plan.message ? <div role="status">{plan.message}</div> : null}
      <h3>Proposed outputs</h3>
      <ul aria-label="Proposed outputs">
        {plan.proposed_outputs.map((output) => (
          <li key={output.id}>
            <strong>{output.sheet_name}</strong> ({output.kind})
            <ul>
              {output.logical_paths.map((path) => <li key={path}>{path}</li>)}
            </ul>
          </li>
        ))}
      </ul>
      {plan.questions.map((question, index) => {
        const legendId = `bulk-import-question-${index}-legend`;
        return (
          <fieldset key={question.id} disabled={busy} aria-labelledby={legendId}>
            <legend id={legendId}>Matching CSV files {index + 1}</legend>
            <ul aria-label={`Files for matching CSV files ${index + 1}`}>
              {question.logical_paths.map((path) => <li key={path}>{path}</li>)}
            </ul>
            <label>
              <input
                type="radio"
                name={`bulk-import-${question.id}`}
                value="combine"
                checked={decisions[question.id] === 'combine'}
                onChange={() => onDecisionChange(question.id, 'combine')}
              />{' '}
              Combine into one sheet
            </label>
            <label>
              <input
                type="radio"
                name={`bulk-import-${question.id}`}
                value="separate"
                checked={decisions[question.id] === 'separate'}
                onChange={() => onDecisionChange(question.id, 'separate')}
              />{' '}
              Import as separate sheets
            </label>
          </fieldset>
        );
      })}
      <button
        type="button"
        className="btn btn-primary import-primary"
        disabled={busy}
        onClick={onExecute}
      >
        {busy ? 'Importing…' : 'Execute import'}
      </button>
    </section>
  );
}

function previewCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function fileUpdateButtonLabel(preview: ImportUpdatePreview | null, isXlsx: boolean): string {
  if (!preview) return `Preview ${isXlsx ? 'Excel' : 'CSV'} updates`;
  return `Update ${preview.matched} rows`;
}

function ImportUpdatePreviewSummary({ preview, testId }: {
  preview: ImportUpdatePreview;
  testId: string;
}) {
  const readableValues = (values: Record<string, unknown>) => Object.entries(values)
    .map(([name, value]) => `${name}: ${value == null ? '(blank)' : String(value)}`)
    .join(' · ');
  return (
    <div data-testid={testId}>
      <div className={preview.ambiguous ? 'import-field-error' : 'import-draft-warnings'}>
        {preview.matched} matched · {preview.unmatched} unmatched · {preview.blank_keys} blank keys · {preview.ambiguous} ambiguous
      </div>
      <div className="import-confirm-summary">
        <span>{preview.changed_cells} cells changed · {preview.cleared_cells} cells cleared</span>
      </div>
      {preview.samples.length > 0 && (
        <div className="import-draft-preview">
          <table>
            <thead><tr><th>Source row</th><th>Status</th><th>Before</th><th>After</th></tr></thead>
            <tbody>
              {preview.samples.slice(0, 8).map((sample, index) => (
                <tr key={`${sample.source_row}-${index}`}>
                  <td>{sample.source_row}</td>
                  <td>{sample.status}</td>
                  <td>{readableValues(sample.before)}</td>
                  <td>{readableValues(sample.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CsvEncodingPreview({
  file,
  selectedEncoding,
  preview,
  busy,
  importBusy,
  error,
  sheets,
  destinationSheetId,
  destinationMode,
  columnPolicies,
  keepExistingOnBlank,
  updatePreview,
  onDestinationSheetChange,
  onDestinationModeChange,
  onColumnPolicyChange,
  onKeepExistingOnBlankChange,
  columnMapping,
  onColumnMappingChange,
  onEncodingChange,
  onImport,
}: {
  file: File;
  selectedEncoding: string;
  preview: ImportCsvPreview | null;
  busy: boolean;
  importBusy: boolean;
  error: string | null;
  sheets: SheetMeta[];
  destinationSheetId: string;
  destinationMode: 'append' | 'update';
  columnPolicies: Record<string, 'match' | 'update'>;
  keepExistingOnBlank: boolean;
  updatePreview: ImportUpdatePreview | null;
  onDestinationSheetChange(value: string): void;
  onDestinationModeChange(value: 'append' | 'update'): void;
  onColumnPolicyChange(source: string, policy: 'match' | 'update'): void;
  onKeepExistingOnBlankChange(value: boolean): void;
  columnMapping: Record<string, string | null>;
  onColumnMappingChange(source: string, target: string | null): void;
  onEncodingChange(value: string): void;
  onImport(): void;
}) {
  const isXlsx = classifySingleImportFile(file) === 'xlsx';
  const destination = sheets.find((sheet) => sheet.id === destinationSheetId);
  const incompatible = destination && preview
    ? preview.columns.filter((column) => columnMapping[column.name] != null && !destination.columns.some(
      (target) => target.name === columnMapping[column.name],
    ))
    : [];
  const mappedTargets = preview?.columns.map((column) => columnMapping[column.name]) ?? [];
  const hasMappedTarget = mappedTargets.some(Boolean);
  const hasDuplicateTargets = new Set(mappedTargets.filter(Boolean)).size
    !== mappedTargets.filter(Boolean).length;
  const updateMode = Boolean(destination && destinationMode === 'update');
  const matchedKeys = preview?.columns.filter(
    (column) => columnMapping[column.name] && columnPolicies[column.name] === 'match',
  ) ?? [];
  const updateColumns = preview?.columns.filter(
    (column) => columnMapping[column.name] && columnPolicies[column.name] !== 'match',
  ) ?? [];
  return (
    <div className="import-csv-preview" data-testid="import-csv-preview">
      <div className="import-csv-preview-controls">
        <div>
          <strong>{file.name}</strong>
          {preview ? (
            <span>
              {preview.row_count} {preview.row_count === 1 ? 'row' : 'rows'} ·{' '}
              {preview.columns.length} {preview.columns.length === 1 ? 'column' : 'columns'}
            </span>
          ) : null}
        </div>
        {!isXlsx ? (
          <label className="import-field-label" htmlFor="import-csv-encoding">
            Character encoding
            <PanelSelect
              id="import-csv-encoding"
              className="form-input import-csv-encoding"
              testId="import-csv-encoding"
              ariaLabel="Character encoding"
              value={selectedEncoding}
              disabled={importBusy}
              options={[...CSV_ENCODINGS]}
              onValueChange={onEncodingChange}
            />
          </label>
        ) : null}
      </div>
      <label className="import-field-label" htmlFor="import-csv-destination">
        Destination
        <PanelSelect
          id="import-csv-destination"
          className="form-input"
          value={destinationSheetId}
          disabled={importBusy || busy}
          onChange={(event) => onDestinationSheetChange(event.target.value)}
        >
          <option value="">New sheet</option>
          {sheets.map((sheet) => <option key={sheet.id} value={sheet.id}>{sheet.name}</option>)}
        </PanelSelect>
      </label>
      {destination ? (
        <label className="import-field-label" htmlFor="import-file-destination-mode">
          Import method
          <PanelSelect
            id="import-file-destination-mode"
            className="form-input"
            value={destinationMode}
            disabled={importBusy}
            onChange={(event) => onDestinationModeChange(event.target.value as 'append' | 'update')}
          >
            <option value="append">Append rows</option>
            <option value="update">Update matching rows</option>
          </PanelSelect>
        </label>
      ) : null}
      {updateMode ? (
        <label className="import-check-inline">
          <input type="checkbox" checked={keepExistingOnBlank} disabled={importBusy}
            onChange={(event) => onKeepExistingOnBlankChange(event.target.checked)} />
          Keep existing values where imported cells are blank
        </label>
      ) : null}
      {busy ? <div className="form-hint">Reading preview…</div> : null}
      {error ? <div className="import-field-error" role="alert">{error}</div> : null}
      {preview ? (
        <>
          {!isXlsx ? (
            <p className="form-hint import-csv-detected" data-testid="import-csv-detected">
              {selectedEncoding ? 'Using' : 'Detected'} {preview.encoding}; delimiter:{' '}
              {preview.delimiter === '\t' ? 'tab' : preview.delimiter}
            </p>
          ) : null}
          {destination ? (
            <div className={incompatible.length || hasDuplicateTargets ? 'import-field-error' : 'import-draft-warnings'}>
              {!hasMappedTarget
                ? 'Choose at least one destination column.'
                : hasDuplicateTargets
                ? 'Each source column must map to a different destination column.'
                : incompatible.length
                ? `${incompatible.length} source column${incompatible.length === 1 ? '' : 's'} still need a destination.`
                : updateMode
                  ? 'Preview exact matches before updating existing rows; unmatched rows will be left unchanged.'
                  : `${preview.row_count} rows will be appended; no existing rows will be changed.`}
            </div>
          ) : null}
          {destination ? preview.columns.map((column) => (
            <div className="import-field-label" key={column.name}>
              <span>{column.name}</span>
              <PanelSelect
                className="form-input"
                value={columnMapping[column.name] ?? ''}
                disabled={importBusy}
                onChange={(event) => onColumnMappingChange(
                  column.name,
                  event.target.value || null,
                )}
              >
                <option value="">Do not import</option>
                {destination.columns.map((target) => (
                  <option key={target.id} value={target.name}>{target.name} ({target.type})</option>
                ))}
              </PanelSelect>
              {updateMode && columnMapping[column.name] ? (
                <PanelSelect
                  className="form-input"
                  aria-label={`${column.name} update action`}
                  value={columnPolicies[column.name] ?? 'update'}
                  disabled={importBusy}
                  onChange={(event) => onColumnPolicyChange(
                    column.name, event.target.value as 'match' | 'update',
                  )}
                >
                  <option value="match">Match</option>
                  <option value="update">Update</option>
                </PanelSelect>
              ) : null}
            </div>
          )) : null}
          {updateMode && updatePreview ? (
            <ImportUpdatePreviewSummary preview={updatePreview} testId="import-file-update-preview" />
          ) : null}
          <div className="import-draft-preview" data-testid="import-csv-preview-table">
            <table>
              <thead>
                <tr>
                  {preview.columns.map((column) => (
                    <th key={column.name}>
                      {column.name}<small>{column.type}</small>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.preview_rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {preview.columns.map((column) => (
                      <td key={column.name}>{previewCell(row[column.name])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button
            type="button"
            className="btn btn-primary import-primary"
            data-testid="import-csv-confirm"
            disabled={importBusy || busy || Boolean(destination && !hasMappedTarget) || incompatible.length > 0 || hasDuplicateTargets || (updateMode && (matchedKeys.length === 0 || updateColumns.length === 0 || Boolean(updatePreview?.ambiguous)))}
            onClick={onImport}
          >
            {importBusy ? 'Importing…' : updateMode
              ? fileUpdateButtonLabel(updatePreview, isXlsx)
              : `Import ${isXlsx ? 'Excel' : 'CSV'}`}
          </button>
        </>
      ) : null}
    </div>
  );
}

function ImportDraftConfirmStep({
  draft,
  columns,
  sheetName,
  destinationSheet,
  updatePreview,
  busy,
  error,
  onBack,
  onConfirm,
}: {
  draft: ImportDraft;
  columns: ImportDraftMappingColumn[];
  sheetName: string;
  destinationSheet: SheetMeta | null;
  updatePreview: ImportUpdatePreview | null;
  busy: boolean;
  error: string | null;
  onBack(): void;
  onConfirm(): void;
}) {
  const activeColumns = columns.filter((column) => column.include);
  const updateBlocked = Boolean(updatePreview && updatePreview.ambiguous > 0);
  return (
    <div className="import-draft-step">
      <div className="import-mode-summary">
        <CheckCircle2 size={16} aria-hidden />
        <strong>Confirm import</strong>
        <span>Review the write before it happens</span>
      </div>
      <div className="import-confirm-summary" data-testid="import-confirm-summary">
        <strong>{destinationSheet?.name ?? (sheetName || draft.sheet_name)}</strong>
        <span>{updatePreview ? `${updatePreview.matched} matched rows` : `${draft.row_count} rows`}</span>
        <span>{activeColumns.length} columns</span>
        <span>{updatePreview ? `Update ${updatePreview.matched} rows` : destinationSheet ? 'Appends to the selected sheet' : 'Creates a new sheet'}</span>
      </div>
      {updatePreview && (
        <ImportUpdatePreviewSummary preview={updatePreview} testId="import-update-preview" />
      )}
      <div className="import-confirm-columns">
        {activeColumns.map((column) => (
          <span key={column.key}>{column.name}: {column.type}</span>
        ))}
      </div>
      {error && (
        <div className="import-field-error" role="alert">
          {error}
        </div>
      )}
      <div className="import-step-actions">
        <button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button>
        <button
          type="button"
          className="btn btn-primary import-primary"
          data-testid="import-confirm-submit"
          disabled={busy || updateBlocked}
          onClick={onConfirm}
        >
          {busy ? 'Importing...' : updatePreview ? `Update ${updatePreview.matched} rows` : 'Import rows'}
        </button>
      </div>
    </div>
  );
}

// The "playlist moment": an import that lands URL rows nothing has downloaded yet
// prompts "Download media?", never "Transcribe these?" (transcribe only
// makes sense once a media column exists, which this import path hasn't
// produced). Modeled directly on FeedSourceForm's own populate prompt below
// (same shape: durable inline swap, "Not now" quietly closes, the primary
// action closes + hands off to the real action surface).
function MediaDownloadPrompt({
  columnName,
  onRun,
  onDismiss,
}: {
  columnName: string;
  onRun(): void;
  onDismiss(): void;
}) {
  return (
    <div className="sources-form import-feed-form" data-testid="media-download-prompt">
      <div className="import-mode-summary">
        <ListVideo size={16} aria-hidden />
        <strong>Download media?</strong>
      </div>
      <p className="modal-body-text">
        <strong>{columnName}</strong> looks like it has media links that
        haven't been downloaded yet. Download now, or skip it — you can
        always run it later from the column's header menu.
      </p>
      <div className="import-step-actions">
        <button
          type="button"
          className="btn"
          data-testid="media-download-dismiss"
          onClick={onDismiss}
        >
          Not now
        </button>
        <button
          type="button"
          className="btn btn-primary import-primary"
          data-testid="media-download-run"
          onClick={onRun}
        >
          <ListVideo size={13} />
          Download
        </button>
      </div>
    </div>
  );
}

function NerExtractPrompt({
  offer,
  onRun,
  onDismiss,
}: {
  offer: NerImportOffer;
  onRun(): void;
  onDismiss(): void;
}) {
  return (
    <div className="sources-form import-feed-form" data-testid="ner-import-prompt">
      <div className="import-mode-summary">
        <AtSign size={16} aria-hidden />
        <strong>Find the names in it?</strong>
      </div>
      <p className="modal-body-text">
        Imported {offer.rowCount.toLocaleString()}{' '}
        {offer.rowCount === 1 ? 'row' : 'rows'} of text in{' '}
        <strong>{describeColumns(offer.columnNames)}</strong>. Pull out the
        people, organizations, places, dates and amounts mentioned in{' '}
        {offer.rowCount === 1 ? 'it' : 'them'}, so you can browse and filter by
        who and what appears
        {offer.free ? ', using a model that runs on this machine' : ''}?
      </p>
      <div className="import-step-actions">
        <button
          type="button"
          className="btn"
          data-testid="ner-import-dismiss"
          onClick={onDismiss}
        >
          Not now
        </button>
        <button
          type="button"
          className="btn btn-primary import-primary"
          data-testid="ner-import-run"
          onClick={onRun}
        >
          <AtSign size={13} />
          Extract entities
        </button>
      </div>
    </div>
  );
}

interface FeedPopulateState {
  source: SourceInfo;
  busy: boolean;
  error: string | null;
}

function FeedSourceForm({
  onCreated,
  onImported,
  onError,
}: {
  onCreated?(): void;
  onImported(sheetId: number): void;
  onError(message: string): void;
}) {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const [state, dispatch] = useReducer(
    feedSourceFormReducer,
    undefined,
    initialFeedSourceFormState,
  );
  const kindTouchedRef = useRef(false);
  // A successful
  // create does NOT close the workspace — it swaps to a "Populate feed?"
  // prompt offering the same run this source's Sources-panel row would (Poll
  // now -> api.fetchSource). No watchRunLinkStore intent slot needed here: unlike
  // the notification -> WatchesPanel handoff (pendingWatchRunLink), this
  // prompt and its onImported target live in the same always-mounted
  // component tree for the lifetime of the dialog — a direct prop (already
  // threaded through ImportWorkspaceDialog) reaches the right place
  // synchronously, so there is no unmounted-target race to buffer against.
  const [populate, setPopulate] = useState<FeedPopulateState | null>(null);
  const {
    name,
    url,
    kind,
    cadence,
    apiListPath,
    apiItemIdPath,
    apiUpdatedAtPath,
    apiMaxItems,
    youtubeMaxPages,
    youtubeMaxItems,
    courtKeywords,
    courtIncludeParties,
    courtIncludeDocuments,
    courtMaxEntries,
    busy,
    error,
  } = state;
  const selectedKind = FEED_SOURCE_KINDS.find((candidate) => candidate.value === kind)
    ?? FEED_SOURCE_KINDS[0];
  const apiListPathError = kind === 'api_list_dicts'
    ? jsonPointerError(apiListPath, 'Response list path')
    : null;
  const apiUrlError = kind === 'api_list_dicts' && url.trim() && !apiUrlIsHttps(url)
    ? 'API list sources require HTTPS'
    : null;
  const validationError = apiUrlError ?? apiListPathError;

  const create = async () => {
    if (!url.trim() || busy || validationError) return;
    dispatch({ type: 'createStart' });
    const startedAt = performance.now();
    try {
      const created = await api.createSource(buildFeedSourceInput({
        name,
        kind,
        url,
        cadence,
        apiListPath,
        apiItemIdPath,
        apiUpdatedAtPath,
        apiMaxItems,
        youtubeMaxPages,
        youtubeMaxItems,
        courtKeywords,
        courtIncludeParties,
        courtIncludeDocuments,
        courtMaxEntries,
      }));
      window.dispatchEvent(new CustomEvent('frisket:sources-changed'));
      // Don't close yet -- surface the populate prompt instead (see the
      // FeedPopulateState comment above).
      setPopulate({ source: created, busy: false, error: null });
      sendProductTelemetry({
        type: 'Source.created',
        properties: {
          sourceKind: telemetrySourceKind(kind), requestDuration: durationBucket(performance.now() - startedAt),
          result: 'success', failureCategory: 'none',
        },
      }, projectId);
    } catch (e) {
      sendProductTelemetry({
        type: 'Source.created',
        properties: {
          sourceKind: telemetrySourceKind(kind), requestDuration: durationBucket(performance.now() - startedAt),
          result: 'failed', failureCategory: failureCategory(e),
        },
      }, projectId);
      const message = `Feed source failed: ${e instanceof Error ? e.message : String(e)}`;
      dispatch({ type: 'createFailure', message });
      onError(message);
    } finally {
      dispatch({ type: 'createSettled' });
    }
  };

  const runPopulate = async () => {
    if (!populate || populate.busy) return;
    setPopulate({ ...populate, busy: true, error: null });
    try {
      // The exact fetch a Sources-panel "Poll now" click makes
      // (SourcesPanel.tsx's fetchNow -> sourceApi.fetchSource).
      const result = await api.fetchSource(populate.source.id);
      if (result.sheetId != null) onImported(result.sheetId);
      onCreated?.();
    } catch (e) {
      const message = `Populate failed: ${e instanceof Error ? e.message : String(e)}`;
      setPopulate({ ...populate, busy: false, error: message });
    }
  };

  const dismissPopulate = () => {
    onCreated?.();
  };

  if (populate) {
    return (
      <div className="sources-form import-feed-form" data-testid="feed-populate-prompt">
        <div className="import-mode-summary">
          {feedKindIcon(kind)}
          <strong>Populate feed?</strong>
        </div>
        <p className="modal-body-text">
          Run <strong>{populate.source.name}</strong> now to pull in its rows
          — the same fetch as Poll now in Sources. You can run it again
          later either way.
        </p>
        {populate.error && (
          <div className="import-field-error" role="alert">{populate.error}</div>
        )}
        <div className="import-step-actions">
          <button
            type="button"
            className="btn"
            data-testid="feed-populate-dismiss"
            disabled={populate.busy}
            onClick={dismissPopulate}
          >
            Not now
          </button>
          <button
            type="button"
            className="btn btn-primary import-primary"
            data-testid="feed-populate-run"
            disabled={populate.busy}
            onClick={() => void runPopulate()}
          >
            <RefreshCw size={13} className={populate.busy ? 'spin' : undefined} />
            {populate.busy ? 'Running...' : 'Run'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="sources-form import-feed-form" data-testid="source-form">
      <div className="import-mode-summary">
        {feedKindIcon(kind)}
        <strong>Feed</strong>
        <span>{feedKindLabel(kind)}</span>
      </div>
      <label className="import-field-label" htmlFor="source-url-input">
        URL or reference
      </label>
      <input
        id="source-url-input"
        className="form-input"
        placeholder={selectedKind.placeholder}
        aria-label="Feed URL or reference"
        data-testid="source-url"
        value={url}
        onChange={(e) => {
          const next = e.target.value;
          dispatch({
            type: 'setUrl',
            value: next,
            detectedKind: kindTouchedRef.current ? undefined : detectFeedSourceKind(next),
          });
        }}
      />
      <label className="import-field-label" htmlFor="source-kind-input">
        Type
      </label>
      <PanelSelect
        id="source-kind-input"
        className="form-input"
        aria-label="Source type"
        data-testid="source-kind"
        value={kind}
        onChange={(e) => {
          kindTouchedRef.current = true;
          dispatch({ type: 'setKind', value: e.target.value as FeedSourceKind });
        }}
      >
        {FEED_SOURCE_KINDS.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </PanelSelect>
      <label className="import-field-label" htmlFor="source-name-input">
        Name
      </label>
      <input
        id="source-name-input"
        className="form-input"
        placeholder={defaultFeedName(kind, url)}
        aria-label="Source name"
        data-testid="source-name"
        value={name}
        onChange={(e) => dispatch({ type: 'setName', value: e.target.value })}
      />

      {(kind === 'youtube_playlist' || kind === 'youtube_channel') && (
        <div className="sources-form-grid">
          <label>
            <span>Max pages</span>
            <input
              className="form-input"
              aria-label="YouTube max pages"
              data-testid="source-youtube-max-pages"
              value={youtubeMaxPages}
              onChange={(e) => dispatch({ type: 'setYoutubeMaxPages', value: e.target.value })}
            />
          </label>
          <label>
            <span>Max items</span>
            <input
              className="form-input"
              aria-label="YouTube max items"
              data-testid="source-youtube-max-items"
              value={youtubeMaxItems}
              onChange={(e) => dispatch({ type: 'setYoutubeMaxItems', value: e.target.value })}
            />
          </label>
        </div>
      )}

      {kind === 'api_list_dicts' && (
        <>
          <div className="sources-form-grid">
            <label>
              <span>Response list path</span>
              <input
                className="form-input"
                aria-label="API response list path"
                data-testid="source-api-list-path"
                value={apiListPath}
                onChange={(e) => dispatch({ type: 'setApiListPath', value: e.target.value })}
              />
            </label>
            <label>
              <span>Item ID path</span>
              <input
                className="form-input"
                aria-label="API item ID path"
                data-testid="source-api-item-id-path"
                value={apiItemIdPath}
                onChange={(e) => dispatch({ type: 'setApiItemIdPath', value: e.target.value })}
              />
            </label>
          </div>
          <div className="sources-form-grid">
            <label>
              <span>Updated path</span>
              <input
                className="form-input"
                aria-label="API updated-at path"
                data-testid="source-api-updated-at-path"
                value={apiUpdatedAtPath}
                onChange={(e) => dispatch({ type: 'setApiUpdatedAtPath', value: e.target.value })}
              />
            </label>
            <label>
              <span>Max items</span>
              <input
                className="form-input"
                aria-label="API max items"
                data-testid="source-api-max-items"
                value={apiMaxItems}
                onChange={(e) => dispatch({ type: 'setApiMaxItems', value: e.target.value })}
              />
            </label>
          </div>
        </>
      )}

      {kind === 'courtlistener_docket' && (
        <>
          <textarea
            className="form-input source-textarea"
            aria-label="CourtListener keywords"
            data-testid="source-court-keywords"
            value={courtKeywords}
            onChange={(e) => dispatch({ type: 'setCourtKeywords', value: e.target.value })}
            placeholder="injunction, settlement"
          />
          <div className="source-check-row">
            <label>
              <input
                type="checkbox"
                data-testid="source-court-include-parties"
                checked={courtIncludeParties}
                onChange={(e) => dispatch({ type: 'setCourtIncludeParties', value: e.target.checked })}
              />
              Parties
            </label>
            <label>
              <input
                type="checkbox"
                data-testid="source-court-include-documents"
                checked={courtIncludeDocuments}
                onChange={(e) => dispatch({ type: 'setCourtIncludeDocuments', value: e.target.checked })}
              />
              Documents
            </label>
          </div>
          <label className="import-field-label">
            Max entries
            <input
              className="form-input"
              aria-label="CourtListener max entries"
              data-testid="source-court-max-entries"
              value={courtMaxEntries}
              onChange={(e) => dispatch({ type: 'setCourtMaxEntries', value: e.target.value })}
            />
          </label>
        </>
      )}

      <label className="import-field-label" htmlFor="source-interval-input">
        Update frequency
      </label>
      <PanelSelect
        id="source-interval-input"
        className="form-input"
        aria-label="Update frequency"
        data-testid="source-interval"
        value={cadence}
        onChange={(e) => dispatch({ type: 'setCadence', value: e.target.value as SourceInterval })}
      >
        {INTERVALS.map((i) => (
          <option key={i.value} value={i.value}>{i.label}</option>
        ))}
      </PanelSelect>
      {(validationError || error) && (
        <div className="import-field-error" role="alert">
          {validationError ?? error}
        </div>
      )}
      <button
        type="button"
        className="btn btn-primary import-primary"
        data-testid="source-create"
        disabled={busy || !url.trim() || Boolean(validationError)}
        onClick={() => void create()}
      >
        {busy ? 'Adding...' : 'Add feed'}
      </button>
    </div>
  );
}

/** Empty-project state: direct XLSX/generic drops still import files, while a
 *  CSV drop opens preview in the SINGLE global Import workspace. The dropzone no
 *  longer mounts its own ImportWorkspaceDialog — exactly one import workspace
 *  exists in the tree so the Copilot handoff cannot land on a duplicate. */
export function ImportDropzone(props: ImportHandlers & {
  onOpenWorkspace(): void;
  onOpenCsv(file: File, preview: ImportCsvPreview): void;
  onOpenBulk(files: File[]): void;
  ftmImportEnabled?: boolean;
}) {
  const { onOpenWorkspace, onOpenCsv, onOpenBulk, ftmImportEnabled = false, ...handlers } = props;
  const { projectId } = useWorkspaceStores().chromePreferences;
  const rich = useImportUpload(handlers);
  const [over, setOver] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (rich.busy || previewing) return;
    const files = Array.from(e.dataTransfer.files);
    const dropMode = inferDroppedFileMode(files, 'csv');
    if (
      ftmImportEnabled
      && files.length === 1
      && /\.(?:json|jsonl)$/i.test(files[0].name)
    ) {
      void rich.uploadFiles('ftm', files).then((sheetId) => {
        if (sheetId != null) handlers.onImported(sheetId);
      });
      return;
    }
    if (directSelectionNeedsBulkPlan(files)) {
      onOpenBulk(files);
      return;
    }
    if (files.length === 1 && dropMode === 'csv') {
      const file = files[0];
      setPreviewing(true);
      void previewCsv(projectId, file).then((preview) => {
        onOpenCsv(file, preview);
      }).catch((error: unknown) => {
        handlers.onError(`Import failed: ${error instanceof Error ? error.message : String(error)}`);
      }).finally(() => {
        setPreviewing(false);
      });
      return;
    }
    // XLSX and generic file drops keep their one-step direct-import path.
    // CSV always enters the global dialog above for preview and confirmation.
    if (dropMode) {
      void rich.uploadFiles(dropMode, files).then((sheetId) => {
        if (sheetId != null) handlers.onImported(sheetId);
      });
    }
  };

  return (
    <div
      className={`dropzone${over ? ' dropzone-over' : ''}`}
      data-testid="import-dropzone"
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
    >
      <Upload size={28} strokeWidth={1.6} />
      <div className="dropzone-title">
        {rich.busy ? 'Importing...' : previewing ? 'Reading preview...' : 'Start with data'}
      </div>
      <div className="dropzone-sub">Files, URLs, pasted rows, and feeds</div>
      <button
        type="button"
        className="btn btn-primary import-primary dropzone-import-open"
        data-testid="import-workspace-open"
        disabled={rich.busy || previewing}
        onClick={onOpenWorkspace}
      >
        <Upload size={13} /> Open import
      </button>
    </div>
  );
}
