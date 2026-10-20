import { useState } from 'react';
import { createImportSession, importFiles, importFollowTheMoney, importCsv, importUrls, importXlsx, uploadFilesToImportSession, type ImportSessionStatus, type BulkImportPlan } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { notifyImportSessionChanged, rememberImportSessionFiles, setImportSessionBrowserUploading } from '../importProgressSession';
import { byteBucket, columnBucket, durationBucket, failureCategory, rowBucket, sendProductTelemetry, type ImportFormat } from '../../telemetry/productTelemetry';
import type { FileImportMode, ImportHandlers } from './model';

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

export interface PreparedFileSelection {
  bytes: number;
  directNeedsBulkPlan: boolean;
  format: ImportFormat;
  usesAttachmentSession: boolean;
}

export async function prepareFileSelection(files: File[]): Promise<PreparedFileSelection> {
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

export const ACCEPTS: Record<FileImportMode, string> = {
  csv: '.csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  xlsx: '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  files: 'image/*,audio/*,video/*,application/pdf,text/plain,application/json,.csv,.xlsx,.json,.txt,.eml,.mbox,.zip,message/rfc822,application/mbox,application/x-mbox,application/zip,application/x-zip-compressed,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

export const FTM_ACCEPT = '.json,.jsonl,application/json,application/x-ndjson';
export type SingleFileFormat = 'csv' | 'ftm';

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

export function logicalPathForImport(file: File): string {
  const relativePath = (file as File & { webkitRelativePath?: unknown }).webkitRelativePath;
  return typeof relativePath === 'string' && relativePath ? relativePath : file.name;
}

export function selectionExpandsArchives(files: readonly File[]): boolean {
  return files.some((file) => file.name.toLowerCase().endsWith('.zip'));
}

export function bulkPlanValidationError(plan: BulkImportPlan): string | null {
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

export const inferDroppedFileMode = (
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

export interface FileImportCompletion {
  sheetId: number | null;
  searchPreparationBatchId: string | null;
  importRef: string | null;
  sessionStatus: ImportSessionStatus | null;
}

const TERMINAL_IMPORT_STATES = new Set<ImportSessionStatus['state']>([
  'completed', 'kept', 'cancelled', 'removed',
]);

export function importSessionIsComplete(status: ImportSessionStatus | null): boolean {
  return status !== null && TERMINAL_IMPORT_STATES.has(status.state);
}

export function importSessionNeedsPreparationPlan(status: ImportSessionStatus | null): boolean {
  return status !== null
    && (status.state === 'completed' || status.state === 'kept')
    && status.search_preparation_status === 'pending';
}

export function useImportUpload({
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
