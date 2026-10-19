import { Button } from './Button';
import { FormField, TextInput, TextArea } from './FormControls';
import { Fragment, useCallback, useEffect, useMemo, useReducer, useRef, useState, type DragEvent } from 'react';
import { ImportStep, ImportStepBody } from './importWorkspace/ImportStep';
import { ImportStepFooter } from './importWorkspace/ImportStepFooter';
import { ImportDropArea } from './importWorkspace/ImportDropArea';
import {
  AtSign,
  FileText,
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
  createImportSession,
  getImportSession,
  importFiles,
  importFollowTheMoney,
  importCsv,
  importUrls,
  importXlsx,
  snapshotImportFiles,
  uploadFilesToImportSession,
  executeBulkImport,
  prepareImportFiles,
  type ImportSessionStatus,
  type BulkImportFailedOutput,
  type BulkImportPlan,
  type PreparedImportFiles,
  type SourceInfo,
  type ActionTemplate,
  type SourceInput,
  type SourceInterval,
  type SheetMeta,
} from '../api/open';
import { pdfImportsApi } from '../api/pdfImports';
import {
  notifyImportSessionChanged,
  rememberImportSessionFiles,
  setImportSessionBrowserUploading,
} from './importProgressSession';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { usePoll } from '../hooks/usePoll';
import { PanelSelect } from './PanelSelect';
import { SegmentedToggle } from './PanelPrimitives';
import { ImportSessionProgress } from './importWorkspace/ImportSessionProgress';
import { ImportModeRail } from './importWorkspace/ImportModeRail';
import { ImportStageStepper } from './importWorkspace/ImportStageStepper';
import {
  PdfPacketImportFlow,
  type PdfPacketShellContext,
} from './importWorkspace/pdfPacket/PdfPacketImportFlow';
import { PDF_PACKET_STAGES } from './importWorkspace/pdfPacket/model';
import {
  DiscardImportConfirm,
  PdfBatchImportFlow,
  PdfBatchDrafts,
  canHandlePdfBatch,
  type PdfBatchShellContext,
} from './importWorkspace/pdfBatch';
import {
  TableImportFlow,
  type TableImportShellContext,
} from './importWorkspace/tableImport';
import { SearchPreparationReceipt } from './SearchPreparationReceipt';
import { IMPORT_MODES, type FileImportMode, type ImportMode } from './importWorkspace/model';
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

function fileImportFormat(file: File): ImportFormat {
  const suffix = file.name.split('.').pop()?.toLowerCase();
  if (suffix === 'jpeg' || suffix === 'jpg' || suffix === 'png' || suffix === 'gif' || suffix === 'webp') return 'image';
  if (suffix === 'mp3' || suffix === 'wav' || suffix === 'm4a' || suffix === 'flac') return 'audio';
  if (suffix === 'mp4' || suffix === 'mov' || suffix === 'webm') return 'video';
  if (suffix === 'txt' || suffix === 'md') return 'text';
  if (suffix && ['csv', 'xlsx', 'json', 'jsonl', 'parquet', 'pdf', 'html', 'htm'].includes(suffix)) {
    return suffix === 'htm' ? 'html' : suffix as ImportFormat;
  }
  return 'other';
}

const SELECTION_PREPARE_YIELD_FILES = 2_048;

interface PreparedFileSelection {
  bytes: number;
  directNeedsBulkPlan: boolean;
  format: ImportFormat;
  usesAttachmentSession: boolean;
}

async function prepareFileSelection(files: File[]): Promise<PreparedFileSelection> {
  await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
  const formats = new Set<ImportFormat>();
  let bytes = 0;
  let usesAttachmentSession = files.length >= 2;
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    bytes += file.size;
    formats.add(fileImportFormat(file));
    if (!/\.[^./]+$/.test(file.name) || /\.(?:csv|xlsx|eml|mbox|zip)$/i.test(file.name)) {
      usesAttachmentSession = false;
    }
    if ((index + 1) % SELECTION_PREPARE_YIELD_FILES === 0) {
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    }
  }
  const first = files[0];
  return {
    bytes,
    directNeedsBulkPlan: (files.length >= 2 && !usesAttachmentSession) || (
      files.length === 1 && (/\.(?:eml|mbox|zip)$/i.test(first.name) || !/\.[^./]+$/.test(first.name))
    ),
    format: formats.size === 1 ? Array.from(formats)[0] : 'mixed',
    usesAttachmentSession,
  };
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

function selectionExpandsArchives(files: readonly File[]): boolean {
  return files.some((file) => file.name.toLowerCase().endsWith('.zip'));
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
  urlText: string;
}

type ImportWorkspaceAction =
  | { type: 'selectMode'; mode: ImportMode }
  | { type: 'setUrlText'; value: string };

const initialImportWorkspaceState = (mode: ImportMode): ImportWorkspaceState => ({
  mode,
  urlText: '',
});

function importWorkspaceReducer(
  state: ImportWorkspaceState,
  action: ImportWorkspaceAction,
): ImportWorkspaceState {
  switch (action.type) {
    case 'selectMode':
      return { ...state, mode: action.mode };
    case 'setUrlText':
      return { ...state, urlText: action.value };
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
  const list = Array.isArray(files) ? files : Array.from(files);
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

interface FileImportCompletion {
  sheetId: number | null;
  searchPreparationBatchId: string | null;
  importRef: string | null;
  sessionStatus: ImportSessionStatus | null;
}

const TERMINAL_IMPORT_STATES = new Set<ImportSessionStatus['state']>([
  'completed', 'kept', 'cancelled', 'removed',
]);

function importSessionIsComplete(status: ImportSessionStatus | null): boolean {
  return status !== null && TERMINAL_IMPORT_STATES.has(status.state);
}

function importSessionNeedsPreparationPlan(status: ImportSessionStatus | null): boolean {
  return status !== null
    && (status.state === 'completed' || status.state === 'kept')
    && status.search_preparation_status === 'pending';
}

function useImportUpload({
  onError,
  onSessionStatus,
}: Pick<ImportHandlers, 'onError'> & {
  onSessionStatus?(status: ImportSessionStatus): void;
}) {
  const { projectId } = useWorkspaceStores().chromePreferences;
  const [busy, setBusy] = useState(false);
  const [sessionBusy, setSessionBusy] = useState(false);

  const uploadFiles = async (
    mode: FileImportMode | SingleFileFormat,
    files: FileList | File[] | null,
    encoding?: string,
    datasetName?: string,
    destinationSheetId?: number,
    columnMapping?: Record<string, string | null>,
    appendRequestKey?: string,
    preparedSelection?: PreparedFileSelection,
  ): Promise<FileImportCompletion | null> => {
    if (busy) return null;
    const list = Array.isArray(files) ? files : Array.from(files ?? []);
    if (!list.length) return null;
    const file = list[0];
    const resolvedMode = file && mode === 'csv' ? classifySingleImportFile(file) : mode;
    if (!file || resolvedMode == null) {
      onError('Import failed: choose a CSV or Excel file for single-file import');
      return null;
    }
    setBusy(true);
    const prepared = preparedSelection ?? await prepareFileSelection(list);
    const startedAt = performance.now();
    const kind = list.length > 1 ? 'files' : 'file';
    const format = resolvedMode === 'ftm' ? 'json'
      : resolvedMode === 'csv' ? 'csv'
        : resolvedMode === 'xlsx' ? 'xlsx' : prepared.format;
    const bytes = byteBucket(prepared.bytes);
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
            : prepared.usesAttachmentSession
              ? await (async () => {
                const session = await createImportSession(projectId, { sheetName: 'files' });
                onSessionStatus?.(session);
                setSessionBusy(true);
                setImportSessionBrowserUploading(session.import_ref, true);
                try {
                  await rememberImportSessionFiles(session.import_ref, list);
                  const status = await uploadFilesToImportSession(
                    projectId,
                    session,
                    list,
                    { onStatus: onSessionStatus },
                  );
                  notifyImportSessionChanged();
                  return {
                    sheet_id: status.sheet_id,
                    rows: status.committed_rows,
                    import_pending: true,
                    import_state: status.state,
                    search_preparation_status: status.search_preparation_status,
                    import_ref: status.import_ref,
                    search_preparation_batch_id: status.search_preparation_batch_id,
                    import_session_status: status,
                  };
                } catch (error) {
                  // The durable admitting session remains visible after a lost
                  // request or closed picker, so the user can reselect or cancel.
                  notifyImportSessionChanged();
                  throw error;
                } finally {
                  setSessionBusy(false);
                  setImportSessionBrowserUploading(session.import_ref, false);
                }
              })()
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
      const sheetId = resolvedMode === 'ftm'
        ? firstMaterializedSheetId(result)
        : ('sheet_id' in result && typeof result.sheet_id === 'number' ? result.sheet_id : null);
      const importRef = 'import_ref' in result && typeof result.import_ref === 'string'
        ? result.import_ref
        : null;
      if (sheetId === null && importRef === null) return null;
      return {
        sheetId,
        searchPreparationBatchId: 'search_preparation_batch_id' in result
          && typeof result.search_preparation_batch_id === 'string'
          ? result.search_preparation_batch_id
          : null,
        importRef,
        sessionStatus: 'import_session_status' in result
          ? result.import_session_status as ImportSessionStatus
          : null,
      };
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

  return { busy, sessionBusy, uploadFiles, uploadUrls };
}

type ImportWorkspaceDialogProps = ImportHandlers & {
  open: boolean;
  initialTableFile?: File | null;
  initialTableRerunDraftId?: string | null;
  initialFiles?: File[] | null;
  initialPreparationBatchId?: string | null;
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
      key={props.initialPreparationBatchId ?? props.initialTableRerunDraftId ?? 'import'}
      {...props}
      initialMode={initialMode}
      onSessionModeChange={setLastMode}
    />
  ) : null;
}

function OpenImportWorkspaceSession({
  initialMode,
  onSessionModeChange,
  initialTableFile = null,
  initialTableRerunDraftId = null,
  initialFiles = null,
  initialPreparationBatchId = null,
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
  const { projectApi, route, chromePreferences: { projectId } } = useWorkspaceStores();
  const [importSessionStatus, setImportSessionStatus] = useState<ImportSessionStatus | null>(null);
  const {
    busy: uploadBusy,
    sessionBusy,
    uploadFiles,
    uploadUrls,
  } = useImportUpload({ onError, onSessionStatus: setImportSessionStatus });
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const onCloseRef = useRef(onClose);
  const onModeChangeRef = useRef(onModeChange);
  const pdfPacketCloseGuardRef = useRef<(() => boolean) | null>(null);
  const pdfBatchCloseGuardRef = useRef<(() => boolean) | null>(null);
  const tableImportCloseGuardRef = useRef<(() => boolean) | null>(null);
  const [tableImportContext, setTableImportContext] = useState<TableImportShellContext | null>(null);
  const registerTableImportCloseGuard = useCallback((guard: (() => boolean) | null) => {
    tableImportCloseGuardRef.current = guard;
  }, []);
  const [pdfBatchFiles, setPdfBatchFiles] = useState<File[] | null>(null);
  const [initialDestinationSheetId] = useState(() => {
    const id = Number(route.store.get().sheetId);
    return Number.isSafeInteger(id) && id > 0 ? id : null;
  });
  const [pdfBatchResumeId, setPdfBatchResumeId] = useState<string | null>(null);
  const pdfBatchActive = pdfBatchFiles !== null || pdfBatchResumeId !== null;
  const [pdfBatchContext, setPdfBatchContext] = useState<PdfBatchShellContext | null>(null);
  const registerPdfBatchCloseGuard = useCallback((guard: (() => boolean) | null) => {
    pdfBatchCloseGuardRef.current = guard;
  }, []);
  const [pdfPacketContext, setPdfPacketContext] = useState<PdfPacketShellContext>({
    active: false,
    stage: 'check',
    subtitle: null,
    busy: false,
  });
  const [pdfPacketInitialFile, setPdfPacketInitialFile] = useState<File | null>(null);
  const updatePdfPacketContext = useCallback((context: PdfPacketShellContext) => {
    setPdfPacketContext(context);
  }, []);
  const registerPdfPacketCloseGuard = useCallback((guard: (() => boolean) | null) => {
    pdfPacketCloseGuardRef.current = guard;
  }, []);
  const [state, dispatch] = useReducer(
    importWorkspaceReducer,
    initialMode,
    initialImportWorkspaceState,
  );
  const {
    mode,
    urlText,
  } = state;
  const fileMode: FileImportMode =
    mode === 'xlsx' || mode === 'files' ? mode : 'csv';
  const railMode: ImportMode = mode === 'xlsx' ? 'csv' : mode === 'urls' ? 'files' : mode;

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
  const [preparationReceipt, setPreparationReceipt] = useState<{
    sheetId: number | null;
    batchId: string | null;
    importRef: string | null;
    bulkOutputs?: { sheetId: number; sheetName: string; batchId: string | null }[];
  } | null>(() => initialPreparationBatchId === null ? null : {
    sheetId: null,
    batchId: initialPreparationBatchId,
    importRef: null,
  });
  const [preparationStep, setPreparationStep] = useState<'summary' | 'review'>(initialPreparationBatchId ? 'review' : 'summary');
  const [fileSummary, setFileSummary] = useState<{ names: string[]; count: number } | null>(null);
  const [preparationFollowupError, setPreparationFollowupError] = useState<string | null>(null);
  const [ftmFile, setFtmFile] = useState<File | null>(null);
  const [singleFileFormat, setSingleFileFormat] = useState<SingleFileFormat>('csv');
  const tableImportSelected = railMode === 'csv' && singleFileFormat !== 'ftm';
  const [ftmDatasetName, setFtmDatasetName] = useState('');
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const zipInputRef = useRef<HTMLInputElement | null>(null);
  const bulkPlanControllerRef = useRef<AbortController | null>(null);
  const bulkExecuteControllerRef = useRef<AbortController | null>(null);
  const bulkGenerationRef = useRef(0);
  const initialFilesConsumedRef = useRef(false);
  const [bulkPlan, setBulkPlan] = useState<BulkImportPlan | null>(null);
  const [bulkDecisions, setBulkDecisions] = useState<Record<string, string>>({});
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkPreparing, setBulkPreparing] = useState(false);
  const [bulkPrepareDiscardPrompt, setBulkPrepareDiscardPrompt] = useState(false);
  const [bulkPrepareDiscarding, setBulkPrepareDiscarding] = useState(false);
  const bulkPrepareDiscardPromptRef = useRef(false);
  const heldPreparedImport = useRef<PreparedImportFiles | null>(null);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkSuccessMessage, setBulkSuccessMessage] = useState<string | null>(null);
  const [bulkWarnings, setBulkWarnings] = useState<string[]>([]);
  const [bulkFailures, setBulkFailures] = useState<BulkImportFailedOutput[]>([]);
  const [bulkFailuresOmitted, setBulkFailuresOmitted] = useState(0);
  const [selectionPreparing, setSelectionPreparing] = useState(false);
  const selectionPreparationGeneration = useRef(0);
  const [destinationSheets, setDestinationSheets] = useState<SheetMeta[]>([]);
  const busy = uploadBusy;

  const prepareSelection = useCallback((files: File[], use: (prepared: PreparedFileSelection) => void) => {
    setImportSessionStatus(null);
    const generation = ++selectionPreparationGeneration.current;
    setFileSummary({ names: files.slice(0, 20).map(logicalPathForImport), count: files.length });
    setPreparationStep('summary');
    setSelectionPreparing(true);
    void prepareFileSelection(files).then((prepared) => {
      if (generation === selectionPreparationGeneration.current) use(prepared);
    }).finally(() => {
      if (generation === selectionPreparationGeneration.current) setSelectionPreparing(false);
    });
  }, []);
  // SheetMeta.parent is populated only from the list wire's parent_sheet_id;
  // imported roots may have a creation parent_op_id and remain appendable.
  const appendDestinationSheets = useMemo(
    () => destinationSheets.filter((sheet) => sheet.parent === undefined),
    [destinationSheets],
  );
  // A destination can disappear from a refreshed sheet list. Treat that
  // stale value as a new-sheet choice everywhere that can write an import.

  const invalidateBulkRequests = useCallback(() => {
    bulkGenerationRef.current += 1;
    bulkPlanControllerRef.current?.abort();
    bulkPlanControllerRef.current = null;
    bulkExecuteControllerRef.current?.abort();
    bulkExecuteControllerRef.current = null;
  }, []);

  const resetBulkPlan = useCallback(() => {
    invalidateBulkRequests();
    bulkPrepareDiscardPromptRef.current = false;
    heldPreparedImport.current = null;
    setBulkPlan(null);
    setBulkDecisions({});
    setBulkBusy(false);
    setBulkPreparing(false);
    setBulkPrepareDiscardPrompt(false);
    setBulkPrepareDiscarding(false);
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
    selectionPreparationGeneration.current += 1;
    invalidateBulkRequests();
    onModeChangeRef.current?.(null);
  }, [invalidateBulkRequests]);

  useEffect(() => {
    let active = true;
    void projectApi.listSheets().then((sheets) => {
      if (active) setDestinationSheets(sheets);
    }).catch(() => {
      if (active) setDestinationSheets([]);
    });
    return () => { active = false; };
  }, [projectApi]);

  const closeWorkspace = useCallback(() => {
    resetBulkPlan();
    setFtmFile(null);
    setDownloadPrompt(null);
    setNerPrompt(null);
    setPreparationReceipt(null);
    setPreparationFollowupError(null);
    onClose();
  }, [onClose, resetBulkPlan]);
  const closeWorkspaceFromUser = useCallback(() => {
    if (bulkPrepareDiscardPromptRef.current) return;
    if (pdfBatchActive) {
      if (pdfBatchCloseGuardRef.current && !pdfBatchCloseGuardRef.current()) return;
      closeWorkspace();
      return;
    }
    if (tableImportSelected && tableImportCloseGuardRef.current && !tableImportCloseGuardRef.current()) return;
    if (bulkPreparing) {
      if (!bulkPrepareDiscardPromptRef.current) {
        bulkPrepareDiscardPromptRef.current = true;
        setBulkPrepareDiscardPrompt(true);
      }
      return;
    }
    if (selectionPreparing || (busy && !sessionBusy) || bulkBusy || pdfPacketContext.busy) return;
    if (mode === 'pdf_split' && pdfPacketCloseGuardRef.current && !pdfPacketCloseGuardRef.current()) return;
    closeWorkspace();
  }, [bulkBusy, bulkPreparing, busy, closeWorkspace, mode, pdfBatchActive, pdfPacketContext.busy, selectionPreparing, sessionBusy, tableImportSelected]);

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
    [closeWorkspace, nerTemplate, onImported, projectApi],
  );

  const finishFileImport = useCallback((completion: FileImportCompletion | null) => {
    if (!completion) return;
    if (completion.searchPreparationBatchId || completion.importRef) {
      if (completion.sheetId !== null) onImported(completion.sheetId);
      setPreparationFollowupError(null);
      setImportSessionStatus(completion.sessionStatus);
      setPreparationStep('summary');
      setPreparationReceipt({
        sheetId: completion.sheetId,
        batchId: completion.searchPreparationBatchId,
        importRef: completion.importRef,
      });
      return;
    }
    if (completion.sheetId !== null) void finishImport(completion.sheetId);
  }, [finishImport, onImported]);

  usePoll(async () => {
    if (!preparationReceipt?.importRef) return;
    let status: ImportSessionStatus;
    try {
      status = await getImportSession(projectId, preparationReceipt.importRef);
      setPreparationFollowupError(null);
    } catch {
      setPreparationFollowupError('Could not refresh import progress. Retrying…');
      return;
    }
    setImportSessionStatus(status);
    if (preparationReceipt.sheetId === null && status.sheet_id !== null) onImported(status.sheet_id);
    setPreparationReceipt((current) => current ? {
      ...current,
      sheetId: current.sheetId ?? status.sheet_id,
      batchId: status.search_preparation_batch_id ?? current.batchId,
    } : current);
  }, {
    active: Boolean(preparationReceipt?.importRef
      && (!importSessionIsComplete(importSessionStatus)
        || importSessionNeedsPreparationPlan(importSessionStatus))),
    intervalMs: 1_000,
    guardOverlap: true,
    immediate: true,
  });

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
    selectionPreparationGeneration.current += 1;
    setSelectionPreparing(false);
    resetBulkPlan();
    setPdfPacketContext({ active: false, stage: 'check', subtitle: null, busy: false });
    pdfPacketCloseGuardRef.current = null;
    if (nextMode !== 'csv') {
      setSingleFileFormat('csv');
      setFtmDatasetName('');
    }
    if (nextMode !== 'pdf_split') setPdfPacketInitialFile(null);
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

  const executeBulkPlan = useCallback((
    plan: BulkImportPlan,
    decisions: Record<string, string>,
  ) => {
    invalidateBulkRequests();
    setBulkPreparing(false);
    const generation = bulkGenerationRef.current;
    const controller = new AbortController();
    bulkExecuteControllerRef.current = controller;
    setBulkBusy(true);
    setBulkPlan(null);
    setBulkError(null);
    void executeBulkImport(
      projectId,
      plan.plan_id,
      decisions,
      { signal: controller.signal },
    ).then(async (result) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      const hasFailures = result.failed.length > 0 || result.failed_omitted > 0;
      const bulkOutputs = result.created.map((output) => ({
        sheetId: output.sheet_id,
        sheetName: output.sheet_name,
        batchId: output.search_preparation_batch_id ?? null,
      }));
      const firstPreparation = bulkOutputs.find((output) => output.batchId !== null);
      if (firstPreparation && typeof result.first_sheet_id === 'number') {
        await onImported(result.first_sheet_id);
        if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
        setBulkPlan(null);
        setBulkDecisions({});
        setBulkError(hasFailures ? result.message : null);
        setBulkSuccessMessage(hasFailures ? null : result.message);
        setBulkWarnings(result.warnings);
        setBulkFailures(result.failed);
        setBulkFailuresOmitted(result.failed_omitted);
        setPreparationStep('summary');
        setPreparationReceipt({ ...firstPreparation, importRef: null, bulkOutputs });
        return;
      }
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

  const acceptPreparedImport = useCallback((prepared: PreparedImportFiles) => {
    if (prepared.kind === 'pdf_import') {
      setPdfBatchFiles(null);
      setPdfBatchResumeId(prepared.draft.draft_id);
      return;
    }
    const nextPlan = prepared.plan;
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
  }, [executeBulkPlan]);

  const startBulkPlan = useCallback((files: File[], expandArchive: boolean) => {
    if (busy || !files.length) return;
    const expandsArchives = expandArchive || selectionExpandsArchives(files);
    if (!expandsArchives && canHandlePdfBatch(files)) {
      setPdfBatchResumeId(null);
      setPdfBatchFiles(files);
      return;
    }
    setImportSessionStatus(null);
    setFileSummary({ names: files.slice(0, 20).map(logicalPathForImport), count: files.length });
    setPreparationStep('summary');
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
    setBulkPreparing(true);
    void prepareImportFiles(
      projectId,
      files,
      files.map(logicalPathForImport),
      expandsArchives,
      { signal: controller.signal },
    ).then((prepared) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      if (bulkPrepareDiscardPromptRef.current) {
        heldPreparedImport.current = prepared;
        return;
      }
      acceptPreparedImport(prepared);
    }).catch((error: unknown) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      setBulkError(`Import planning failed: ${error instanceof Error ? error.message : String(error)}`);
    }).finally(() => {
      if (bulkPlanControllerRef.current === controller && bulkGenerationRef.current === generation) {
        bulkPlanControllerRef.current = null;
        setBulkPreparing(false);
        setBulkBusy(false);
      }
    });
  }, [acceptPreparedImport, busy, invalidateBulkRequests, projectId]);

  const keepPreparingImport = () => {
    bulkPrepareDiscardPromptRef.current = false;
    setBulkPrepareDiscardPrompt(false);
    const prepared = heldPreparedImport.current;
    heldPreparedImport.current = null;
    if (prepared) acceptPreparedImport(prepared);
  };

  const discardPreparingImport = async () => {
    if (bulkPrepareDiscarding) return;
    const prepared = heldPreparedImport.current;
    if (!prepared || prepared.kind === 'bulk') {
      closeWorkspace();
      return;
    }
    setBulkPrepareDiscarding(true);
    try {
      await pdfImportsApi.discard(projectId, prepared.draft.draft_id);
      closeWorkspace();
    } catch (problem) {
      const message = problem instanceof Error ? problem.message : String(problem);
      setBulkError(message);
      onError(message);
      setBulkPrepareDiscarding(false);
    }
  };

  const handlePickedFiles = useCallback((files: File[]) => {
    if (!files.length) return;
    if (!selectionExpandsArchives(files) && canHandlePdfBatch(files)) {
      setPdfBatchResumeId(null);
      setPdfBatchFiles(files);
      return;
    }
    prepareSelection(files, (prepared) => {
      if (prepared.directNeedsBulkPlan) {
        startBulkPlan(files, selectionExpandsArchives(files));
      } else {
        resetBulkPlan();
        void uploadFiles(
          singleFileFormat === 'ftm' ? 'ftm' : fileMode,
          files, undefined, ftmDatasetName, undefined, undefined, undefined, prepared,
        ).then(finishFileImport);
      }
    });
  }, [
    fileMode,
    finishFileImport,
    ftmDatasetName,
    prepareSelection,
    resetBulkPlan,
    singleFileFormat,
    startBulkPlan,
    uploadFiles,
  ]);

  useEffect(() => {
    if (initialFilesConsumedRef.current || !initialFiles?.length) return;
    initialFilesConsumedRef.current = true;
    startBulkPlan(initialFiles, selectionExpandsArchives(initialFiles));
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

  const bulkReceiptSummary = preparationReceipt?.bulkOutputs ? (
    <>
      {preparationStep === 'summary' && <BulkImportPlanPanel
        plan={null} decisions={{}} busy={false} error={bulkError}
        successMessage={bulkSuccessMessage} warnings={bulkWarnings}
        failures={bulkFailures} failuresOmitted={bulkFailuresOmitted}
        onDecisionChange={() => {}} onExecute={() => {}}
      />}
      {preparationReceipt.bulkOutputs.length > 1 && (
        <label className="form-field">
          Review imported sheet
          <PanelSelect
            ariaLabel="Review imported sheet"
            topLayer
            value={preparationReceipt.sheetId ?? ''}
            onValueChange={(value) => {
              const selected = preparationReceipt.bulkOutputs?.find((output) => output.sheetId === Number(value));
              if (selected) setPreparationReceipt((current) => current ? { ...current, ...selected } : current);
            }}
          >
            {preparationReceipt.bulkOutputs.map((output) => (
              <option key={output.sheetId} value={output.sheetId}>{output.sheetName}</option>
            ))}
          </PanelSelect>
        </label>
      )}
    </>
  ) : null;

  const importInProgress = Boolean(fileSummary && ((uploadBusy && railMode === 'files') || bulkBusy)
    && !bulkPlan);
  const showPreparation = preparationReceipt !== null || importInProgress;
  const tableImportActive = tableImportSelected && !showPreparation;
  const fileEntry = railMode === 'files' && !showPreparation && !pdfBatchActive && !downloadPrompt && !nerPrompt;
  const EntryStep = fileEntry ? ImportStep : Fragment;
  const EntryBody = fileEntry ? ImportStepBody : Fragment;
  const tableImportChoosingSource = tableImportActive
    && (tableImportContext?.canChangeMode ?? true)
    && (tableImportContext?.stage ?? 'add') === 'add';
  const showSummary = showPreparation && preparationStep === 'summary';
  const importComplete = importSessionIsComplete(importSessionStatus);
  const preparationPending = importSessionNeedsPreparationPlan(importSessionStatus);
  const importingSession = importSessionStatus !== null && !importComplete;
  const summaryWaiting = importInProgress || importingSession || preparationPending;
  const canReviewPreparation = Boolean(preparationReceipt?.batchId
    && !preparationPending
    && (importSessionStatus === null
      || importSessionStatus.state === 'completed'
      || importSessionStatus.state === 'kept'));
  const summaryHeading = importSessionStatus?.state === 'paused'
    ? 'Import paused'
    : importSessionStatus?.state === 'cancelled'
      ? 'Import cancelled'
      : importSessionStatus?.state === 'removed'
        ? 'Import removed'
        : summaryWaiting && !preparationPending
          ? 'Importing files'
          : 'Files imported';

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
        className={`import-workspace${pdfPacketContext.active || pdfBatchActive ? ' pdf-packet-workspace' : ''}${pdfBatchActive ? ' pdf-batch-workspace' : ''}${tableImportActive ? ' table-import-workspace' : ''}${tableImportChoosingSource ? ' table-import-workspace-mode-selection' : ''}`}
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
            const files = snapshotImportFiles(e.target.files);
            e.target.value = '';
            handlePickedFiles(files);
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
            const files = snapshotImportFiles(e.target.files);
            e.target.value = '';
            if (!files.length) return;
            if (!selectionExpandsArchives(files) && canHandlePdfBatch(files)) {
              setPdfBatchResumeId(null);
              setPdfBatchFiles(files);
              return;
            }
            prepareSelection(files, (prepared) => {
              if (prepared.usesAttachmentSession) {
                resetBulkPlan();
                void uploadFiles(
                  'files', files, undefined, undefined, undefined, undefined, undefined, prepared,
                ).then(finishFileImport);
              } else {
                startBulkPlan(files, false);
              }
            });
          }}
        />
        <input
          aria-label="Choose ZIP"
          ref={zipInputRef}
          type="file"
          multiple
          accept=".zip,application/zip,application/x-zip-compressed"
          style={{ display: 'none' }}
          data-testid="import-zip-input"
          onChange={(e) => {
            const files = snapshotImportFiles(e.target.files);
            if (!files.length || files.some((file) => !file.name.toLowerCase().endsWith('.zip'))) {
              resetBulkPlan();
              setBulkError('Choose a ZIP archive to import its contents.');
            } else {
              startBulkPlan(files, true);
            }
            e.target.value = '';
          }}
        />
        <header className="import-workspace-header">
          <div>
            <div className="import-workspace-title">Import</div>
            <div className="import-workspace-kicker">
              {tableImportContext ? tableImportContext.subtitle : pdfBatchContext ? pdfBatchContext.subtitle : pdfPacketContext.active
                ? pdfPacketContext.subtitle
                : showPreparation ? (showSummary ? 'Imported files' : 'Review text and search setup') : 'Project data intake'}
            </div>
          </div>
          {tableImportContext ? (
            <ImportStageStepper stages={[...tableImportContext.stages]} stage={tableImportContext.stage} />
          ) : pdfBatchContext ? (
            <ImportStageStepper stages={[...pdfBatchContext.stages]} stage={pdfBatchContext.stage} />
          ) : pdfPacketContext.active && !preparationReceipt ? (
            <ImportStageStepper stages={[...PDF_PACKET_STAGES]} stage={pdfPacketContext.stage} />
          ) : null}
          <button
            type="button"
            className="icon-btn"
            aria-label="Close import"
            data-testid="import-workspace-close"
            disabled={!pdfBatchActive && (
              selectionPreparing
              || (busy && !sessionBusy)
              || (bulkBusy && !bulkPreparing)
              || pdfPacketContext.busy
            )}
            onClick={closeWorkspaceFromUser}
          >
            <X size={16} />
          </button>
        </header>
        <div className={`import-workspace-main${pdfPacketContext.active || pdfBatchActive ? ' pdf-packet-workspace-main' : ''}${showPreparation ? ' import-workspace-main-preparation' : ''}`}>
          {!pdfPacketContext.active && !pdfBatchActive && (!tableImportActive || tableImportChoosingSource) && !showPreparation ? (
            <aside className="import-workspace-sidebar">
              <ImportModeRail modes={IMPORT_MODES} mode={railMode} onSelectMode={selectMode} />
            </aside>
          ) : null}
          <main className="import-workspace-content">
            <div className={`import-workspace-panel${mode === 'pdf_split' || pdfBatchActive || tableImportActive || fileEntry || mode === 'feed' ? ' import-workspace-panel-flush' : ''}`}>
              {tableImportActive ? (
                <TableImportFlow
                  projectId={projectId}
                  initialFile={initialTableFile}
                  initialRerunDraftId={initialTableRerunDraftId}
                  preferredDestinationSheetId={initialDestinationSheetId}
                  followTheMoneyEnabled={ftmImportEnabled}
                  destinationSheets={appendDestinationSheets.map((sheet) => ({
                    id: Number(sheet.id),
                    name: sheet.name,
                    rowCount: sheet.rowCount,
                    columns: sheet.columns.map((column) => ({ id: Number(column.id), name: column.name })),
                  }))}
                  onContextChange={setTableImportContext}
                  registerCloseGuard={registerTableImportCloseGuard}
                  onImported={(sheetId) => { onImported(sheetId); closeWorkspace(); }}
                  onDiscarded={closeWorkspace}
                  onError={onError}
                  onUseMultipleFiles={(files) => {
                    setSingleFileFormat('csv');
                    selectMode('files');
                    if (files?.length) startBulkPlan(files, selectionExpandsArchives(files));
                  }}
                  onUsePdfSplit={(file) => {
                    setPdfPacketInitialFile(file);
                    selectMode('pdf_split');
                  }}
                  onUseFollowTheMoney={(file) => {
                    setFtmFile(file);
                    setSingleFileFormat('ftm');
                    setFtmDatasetName('');
                  }}
                />
              ) : pdfBatchActive ? (
                <PdfBatchImportFlow
                  projectId={projectId}
                  files={pdfBatchFiles ?? undefined}
                  resumeDraftId={pdfBatchResumeId}
                  initialDestinationSheetId={destinationSheets.some((sheet) => sheet.parent === undefined && Number(sheet.id) === initialDestinationSheetId) ? initialDestinationSheetId : null}
                  destinationSheets={destinationSheets.filter((sheet) => sheet.parent === undefined).map((sheet) => ({ id: Number(sheet.id), name: sheet.name }))}
                  onContextChange={setPdfBatchContext}
                  registerCloseGuard={registerPdfBatchCloseGuard}
                  onImported={(sheetId) => { onImported(sheetId); closeWorkspace(); }}
                  onDiscarded={closeWorkspace}
                  onError={onError}
                />
              ) : <EntryStep><EntryBody {...(fileEntry ? { className: 'import-entry-body' } : {})}>
              {railMode === 'files' && !showPreparation && <PdfBatchDrafts projectId={projectId} onResume={setPdfBatchResumeId} />}
              {railMode === 'files' && !showPreparation ? (
                <SegmentedToggle
                  fullWidth={false}
                  ariaLabel="File import source"
                  value={mode === 'urls' ? 'urls' : 'files'}
                  onValueChange={(value) => selectMode(value as 'files' | 'urls')}
                  options={[{ value: 'files', label: 'Files' }, { value: 'urls', label: 'URLs' }]}
                  buttonTestId={(value) => `import-source-${value}`}
                  testId="import-source-toggle"
                  disabled={busy || bulkBusy || selectionPreparing}
                />
              ) : null}
              {selectionPreparing ? <div role="status">Preparing selected files…</div> : null}
              {showSummary ? (
                <section className="search-preparation-receipt" data-testid="import-files-summary">
                  <div className="search-preparation-receipt-body">
                    <h2>{summaryHeading}</h2>
                    {fileSummary && <>
                      <p>{fileSummary.count.toLocaleString()} {fileSummary.count === 1 ? 'file' : 'files'}</p>
                      <ul className="import-files-summary-list">
                        {fileSummary.names.map((name, index) => <li key={`${index}:${name}`}>
                          <FileText size={14} aria-hidden />
                          <span>{name}</span>
                        </li>)}
                      </ul>
                      {fileSummary.count > fileSummary.names.length && <p className="form-hint">
                        Showing the first {fileSummary.names.length} filenames.
                      </p>}
                    </>}
                    {bulkReceiptSummary}
                    <ImportSessionProgress
                      status={importSessionStatus}
                      selectedFiles={fileSummary?.count ?? 0}
                      active={importInProgress}
                      complete={importComplete}
                      preparationPending={preparationPending}
                    />
                    {!summaryWaiting && !preparationReceipt?.batchId
                      && importSessionStatus?.state !== 'cancelled'
                      && importSessionStatus?.state !== 'removed' && <p>
                      Files are saved. Search preparation is not available for this import.
                    </p>}
                    {importSessionStatus?.error && <div className="panel-error" role="alert">{importSessionStatus.error}</div>}
                    {preparationFollowupError && <div className="panel-error" role="alert">{preparationFollowupError}</div>}
                  </div>
                  <footer className="search-preparation-actions">
                    {canReviewPreparation ? (
                      <Button type="button" className="btn-primary" data-testid="import-files-next"
                        onClick={() => setPreparationStep('review')}>Next</Button>
                    ) : !summaryWaiting ? (
                      <Button type="button" className="btn-primary" data-testid="search-preparation-done"
                        onClick={closeWorkspace}>Done</Button>
                    ) : null}
                  </footer>
                </section>
              ) : preparationReceipt ? (
                preparationReceipt.batchId ? (
                  <SearchPreparationReceipt
                    key={`${projectId}:${preparationReceipt.sheetId ?? initialPreparationBatchId ?? 'import'}`}
                    projectId={projectId}
                    batchId={preparationReceipt.batchId}
                    importSummary={bulkReceiptSummary}
                    onBack={initialPreparationBatchId ? undefined : () => setPreparationStep('summary')}
                    onBatchChange={(batch) => setPreparationReceipt((current) => current ? {
                      ...current,
                      batchId: current.sheetId === null || current.sheetId === batch.sheet_id
                        ? batch.id : current.batchId,
                      bulkOutputs: current.bulkOutputs?.map((output) => output.sheetId === batch.sheet_id
                        ? { ...output, batchId: batch.id } : output),
                    } : current)}
                    onDone={closeWorkspace}
                    onError={onError}
                  />
                ) : (
                  <section className="search-preparation-receipt" data-testid="search-preparation-receipt">
                    <div className="search-preparation-receipt-body">
                      {bulkReceiptSummary}
                      <p>Files imported. Search preparation is not available for this sheet.</p>
                    </div>
                    <footer className="search-preparation-actions">
                      <Button type="button" className="btn-primary" data-testid="search-preparation-done"
                        onClick={closeWorkspace}>Done</Button>
                    </footer>
                  </section>
                )
              ) : downloadPrompt ? (
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
              ) : singleFileFormat === 'ftm' && ftmFile ? (
                <section className="table-import-ftm-handoff">
                  <FileText size={26} aria-hidden />
                  <h2>Import FollowTheMoney data</h2>
                  <p>{ftmFile.name}</p>
                  <label className="import-field-label" htmlFor="import-ftm-dataset-name">Dataset name (optional)</label>
                  <TextInput
                    id="import-ftm-dataset-name"

                    data-testid="import-ftm-dataset-name"
                    value={ftmDatasetName}
                    onChange={(event) => setFtmDatasetName(event.currentTarget.value)}
                  />
                  <div className="import-step-actions">
                    <Button type="button" disabled={busy} onClick={() => {
                      setFtmFile(null);
                      setSingleFileFormat('csv');
                    }}>Back</Button>
                    <Button type="button" className="btn-primary" disabled={busy} onClick={() => {
                      void uploadFiles('ftm', [ftmFile], undefined, ftmDatasetName).then(finishFileImport);
                    }}>{busy ? 'Importing…' : 'Import'}</Button>
                  </div>
                </section>
              ) : mode === 'pdf_split' ? (
                <PdfPacketImportFlow
                  initialFile={pdfPacketInitialFile}
                  projectId={projectId}
                  onContextChange={updatePdfPacketContext}
                  registerCloseGuard={registerPdfPacketCloseGuard}
                  onImported={(sheetId) => {
                    onImported(sheetId);
                    closeWorkspace();
                  }}
                  onError={onError}
                  onCancel={closeWorkspaceFromUser}
                />
              ) : mode === 'urls' ? (
                <>
                  <label className="import-field-label" htmlFor="import-url-list">
                    URLs
                  </label>
                  <TextArea
                    id="import-url-list"
                    className="import-url-list"
                    data-testid="import-url-list"
                    rows={8}
                    value={urlText}
                    placeholder="https://example.com/file.pdf"
                    onChange={(e) => dispatch({ type: 'setUrlText', value: e.target.value })}
                  />
                </>
              ) : mode === 'feed' ? (
                <FeedSourceForm onCreated={onClose} onImported={onImported} onError={onError} onCancel={closeWorkspaceFromUser} />
              ) : (
                <>
                  <ImportDropArea title="Drop files here" description="Drop files or a ZIP archive here, or choose a folder."
                    testId="import-files-chooser" disabled={busy || bulkBusy || selectionPreparing} onFiles={handlePickedFiles}>
                    <div className="import-files-chooser-actions">
                      <Button type="button" className="btn-primary" data-testid="import-file-picker" disabled={busy || bulkBusy || selectionPreparing} onClick={() => inputRef.current?.click()}>Pick files</Button>
                      <Button type="button" disabled={busy || bulkBusy || selectionPreparing} onClick={() => folderInputRef.current?.click()}>Choose folder</Button>
                      <Button type="button" disabled={busy || bulkBusy || selectionPreparing} onClick={() => zipInputRef.current?.click()}>Upload ZIP</Button>
                    </div>
                  </ImportDropArea>
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
              </EntryBody>
              {fileEntry && <ImportStepFooter>
                <Button type="button" disabled={busy || bulkBusy || selectionPreparing} onClick={closeWorkspaceFromUser}>Cancel</Button>
                <span>{mode === 'urls' ? 'Enter URLs to import their contents.' : 'Choose or drop files to start preparing the import.'}</span>
                {mode === 'urls' && <Button type="button" className="btn-primary" data-testid="import-url-submit"
                  disabled={busy} onClick={() => {
                    void uploadUrls(urlText).then((sheetId) => {
                      if (sheetId != null) void finishImport(sheetId);
                    });
                  }}><Link2 size={13} /> Import URLs</Button>}
              </ImportStepFooter>}
              </EntryStep>}
            </div>
          </main>
        </div>
      </section>
      {bulkPrepareDiscardPrompt && (
        <DiscardImportConfirm
          title="Cancel this import?"
          message="Files won’t be added to the project."
          confirmLabel="Cancel import"
          busy={bulkPrepareDiscarding}
          testId="bulk-prepare-discard-dialog"
          onKeep={keepPreparingImport}
          onConfirm={() => void discardPreparingImport()}
        />
      )}
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
        {error ? <div className="panel-error" role="alert">{error}</div> : null}
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
    return <div className="panel-error" role="alert">{validationError}</div>;
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
      {error ? <div className="panel-error" role="alert">{error}</div> : null}
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
      <Button
        type="button"
        className="btn-primary import-primary"
        disabled={busy}
        onClick={onExecute}
      >
        {busy ? 'Importing…' : 'Execute import'}
      </Button>
    </section>
  );
}

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
    <div className="content-stack import-feed-form" data-testid="media-download-prompt">
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
        <Button
          type="button"

          data-testid="media-download-dismiss"
          onClick={onDismiss}
        >
          Not now
        </Button>
        <Button
          type="button"
          className="btn-primary import-primary"
          data-testid="media-download-run"
          onClick={onRun}
        >
          <ListVideo size={13} />
          Download
        </Button>
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
    <div className="content-stack import-feed-form" data-testid="ner-import-prompt">
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
        <Button
          type="button"

          data-testid="ner-import-dismiss"
          onClick={onDismiss}
        >
          Not now
        </Button>
        <Button
          type="button"
          className="btn-primary import-primary"
          data-testid="ner-import-run"
          onClick={onRun}
        >
          <AtSign size={13} />
          Extract entities
        </Button>
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
  onCancel,
}: {
  onCreated?(): void;
  onImported(sheetId: number): void;
  onError(message: string): void;
  onCancel(): void;
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
      <ImportStep>
      <ImportStepBody className="content-stack" data-testid="feed-populate-prompt">
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
          <div className="panel-error" role="alert">{populate.error}</div>
        )}
      </ImportStepBody>
      <ImportStepFooter>
          <Button
            type="button"

            data-testid="feed-populate-dismiss"
            disabled={populate.busy}
            onClick={dismissPopulate}
          >
            Not now
          </Button>
          <span>The feed is saved. Fetch its rows now or later.</span>
          <Button
            type="button"
            className="btn-primary import-primary"
            data-testid="feed-populate-run"
            disabled={populate.busy}
            onClick={() => void runPopulate()}
          >
            <RefreshCw size={13} className={populate.busy ? 'spin' : undefined} />
            {populate.busy ? 'Running...' : 'Run'}
          </Button>
      </ImportStepFooter>
      </ImportStep>
    );
  }

  return (
    <ImportStep>
    <ImportStepBody>
    <div className="content-stack" data-testid="source-form">
      <div className="import-mode-summary">
        {feedKindIcon(kind)}
        <strong>Feed</strong>
        <span>{feedKindLabel(kind)}</span>
      </div>
      <FormField label="URL or reference" htmlFor="source-url-input">
        <TextInput
          id="source-url-input"

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
      </FormField>
      <FormField label="Type" htmlFor="source-kind-input">
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
      </FormField>
      <FormField label="Name" htmlFor="source-name-input">
        <TextInput
          id="source-name-input"

          placeholder={defaultFeedName(kind, url)}
          aria-label="Source name"
          data-testid="source-name"
          value={name}
          onChange={(e) => dispatch({ type: 'setName', value: e.target.value })}
        />
      </FormField>

      {(kind === 'youtube_playlist' || kind === 'youtube_channel') && (
        <div className="sources-form-grid">
          <FormField label="Max pages" >

            <TextInput
              aria-label="YouTube max pages"
              data-testid="source-youtube-max-pages"
              value={youtubeMaxPages}
              onChange={(e) => dispatch({ type: 'setYoutubeMaxPages', value: e.target.value })}
            />
          </FormField>
          <FormField label="Max items" >

            <TextInput
              aria-label="YouTube max items"
              data-testid="source-youtube-max-items"
              value={youtubeMaxItems}
              onChange={(e) => dispatch({ type: 'setYoutubeMaxItems', value: e.target.value })}
            />
          </FormField>
        </div>
      )}

      {kind === 'api_list_dicts' && (
        <>
          <div className="sources-form-grid">
            <FormField label="Response list path" >

              <TextInput
                aria-label="API response list path"
                data-testid="source-api-list-path"
                value={apiListPath}
                onChange={(e) => dispatch({ type: 'setApiListPath', value: e.target.value })}
              />
            </FormField>
            <FormField label="Item ID path" >

              <TextInput
                aria-label="API item ID path"
                data-testid="source-api-item-id-path"
                value={apiItemIdPath}
                onChange={(e) => dispatch({ type: 'setApiItemIdPath', value: e.target.value })}
              />
            </FormField>
          </div>
          <div className="sources-form-grid">
            <FormField label="Updated path" >

              <TextInput
                aria-label="API updated-at path"
                data-testid="source-api-updated-at-path"
                value={apiUpdatedAtPath}
                onChange={(e) => dispatch({ type: 'setApiUpdatedAtPath', value: e.target.value })}
              />
            </FormField>
            <FormField label="Max items" >

              <TextInput
                aria-label="API max items"
                data-testid="source-api-max-items"
                value={apiMaxItems}
                onChange={(e) => dispatch({ type: 'setApiMaxItems', value: e.target.value })}
              />
            </FormField>
          </div>
        </>
      )}

      {kind === 'courtlistener_docket' && (
        <>
          <TextArea
            className="source-textarea"
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
          <FormField label="Max entries">
            <TextInput
              aria-label="CourtListener max entries"
              data-testid="source-court-max-entries"
              value={courtMaxEntries}
              onChange={(e) => dispatch({ type: 'setCourtMaxEntries', value: e.target.value })}
            />
          </FormField>
        </>
      )}

      <FormField label="Update frequency" htmlFor="source-interval-input">
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
      </FormField>
      {(validationError || error) && (
        <div className="panel-error" role="alert">
          {validationError ?? error}
        </div>
      )}
    </div>
    </ImportStepBody>
    <ImportStepFooter>
      <Button type="button" disabled={busy} onClick={onCancel}>Cancel</Button>
      <span>Add a feed to keep its source available for updates.</span>
      <Button
        type="button"
        className="btn-primary import-primary"
        data-testid="source-create"
        disabled={busy || !url.trim() || Boolean(validationError)}
        onClick={() => void create()}
      >
        {busy ? 'Adding...' : 'Add feed'}
      </Button>
    </ImportStepFooter>
    </ImportStep>
  );
}

/** Empty-project state: a table file opens the single global Import workspace
 *  with the original File. Its server-backed reader owns preview and import. */
export function ImportDropzone(props: ImportHandlers & {
  onOpenWorkspace(): void;
  onOpenTable(file: File): void;
  onOpenBulk(files: File[]): void;
  ftmImportEnabled?: boolean;
}) {
  const { onOpenWorkspace, onOpenTable, onOpenBulk, ftmImportEnabled = false, ...handlers } = props;
  const rich = useImportUpload(handlers);
  const [over, setOver] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const preparationGeneration = useRef(0);

  useEffect(() => () => {
    preparationGeneration.current += 1;
  }, []);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (rich.busy || preparing) return;
    const files = snapshotImportFiles(e.dataTransfer.files);
    if (!files.length) return;
    if (!selectionExpandsArchives(files) && canHandlePdfBatch(files)) {
      onOpenBulk(files);
      return;
    }
    if (files.length === 1 && /\.(?:csv|tsv|txt|xlsx|xls|ods)$/i.test(files[0].name)) {
      onOpenTable(files[0]);
      return;
    }
    const generation = ++preparationGeneration.current;
    setPreparing(true);
    void prepareFileSelection(files).then((prepared) => {
      if (generation !== preparationGeneration.current) return;
      const dropMode = inferDroppedFileMode(files, 'csv');
      if (ftmImportEnabled && files.length === 1 && /\.(?:json|jsonl)$/i.test(files[0].name)) {
        void rich.uploadFiles(
          'ftm', files, undefined, undefined, undefined, undefined, undefined, prepared,
        ).then((completion) => { if (completion?.sheetId != null) handlers.onImported(completion.sheetId); });
      } else if (prepared.directNeedsBulkPlan) {
        onOpenBulk(files);
      } else if (dropMode) {
        void rich.uploadFiles(
          dropMode, files, undefined, undefined, undefined, undefined, undefined, prepared,
        ).then((completion) => { if (completion?.sheetId != null) handlers.onImported(completion.sheetId); });
      }
    }).finally(() => {
      if (generation === preparationGeneration.current) setPreparing(false);
    });
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
        {preparing ? 'Preparing selected files…' : rich.busy ? 'Importing...' : 'Start with data'}
      </div>
      <div className="dropzone-sub">Files, URLs, pasted rows, and feeds</div>
      <Button
        type="button"
        className="btn-primary import-primary dropzone-import-open"
        data-testid="import-workspace-open"
        disabled={rich.busy}
        onClick={() => onOpenWorkspace()}
      >
        <Upload size={13} /> Open import
      </Button>
    </div>
  );
}
