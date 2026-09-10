import type {
  ProjectExportOptions,
  SheetDatasetExportOptions,
  SheetViewExportOptions,
} from '../types';

/** Identifiers accepted by the named browser-navigation resource builders. */
export type ProjectId = string;
export type BlobDigest = string;
export type EmbeddingIndexId = string;
export type EmbeddingArtifactFormat = string;

/**
 * URL builders for browser-native project resources. These deliberately only
 * render the fixed download/navigation routes: callers retain browser handling
 * for streaming responses and Content-Disposition filenames.
 */
function currentProjectUrl(projectId: ProjectId): string {
  return `/api/projects/${encodeURIComponent(projectId)}`;
}

export function projectBlobUrl(projectId: ProjectId, digest: BlobDigest): string {
  return `${currentProjectUrl(projectId)}/blobs/${encodeURIComponent(digest)}`;
}

export function embeddingExportArtifactUrl(
  projectId: ProjectId,
  indexId: EmbeddingIndexId,
  format: EmbeddingArtifactFormat,
): string {
  return `${currentProjectUrl(projectId)}/embeddings/v1/indexes/${encodeURIComponent(
    indexId,
  )}/export/${encodeURIComponent(format)}`;
}

export function projectExportUrl(
  projectId: ProjectId,
  includeMediaOrOptions: boolean | ProjectExportOptions = true,
): string {
  const options = typeof includeMediaOrOptions === 'boolean'
    ? { mode: 'bundle' as const, includeMedia: includeMediaOrOptions }
    : includeMediaOrOptions;
  if (options.mode === 'db') return `${currentProjectUrl(projectId)}/export?mode=db`;

  const includeMedia = options.includeMedia ?? true;
  const includeTraces = options.includeTraces ?? false;
  return `${currentProjectUrl(projectId)}/export?mode=bundle&include_media=${includeMedia ? 'true' : 'false'}&include_traces=${includeTraces ? 'true' : 'false'}`;
}

function sheetViewQuery(options: SheetViewExportOptions | null = {}): string {
  const opts = options ?? {};
  const params = new URLSearchParams();
  if (opts.filter && Object.keys(opts.filter).length > 0) {
    params.set('filter', JSON.stringify(opts.filter));
  }
  if (opts.sort && opts.sort.length > 0) {
    params.set('sort', JSON.stringify(opts.sort));
  }
  return params.toString();
}

export function sheetDatasetExportUrl(
  projectId: ProjectId,
  options: SheetDatasetExportOptions,
): string {
  const sheetIds = [...new Set(options.sheetIds)];
  if (sheetIds.length === 0) throw new Error('select at least one sheet');
  if (options.currentView && sheetIds.length !== 1) {
    throw new Error('current-view export requires exactly one selected sheet');
  }
  const params = new URLSearchParams();
  for (const sheetId of sheetIds) params.append('sheet_id', sheetId);
  params.set('format', options.format);
  const viewQuery = sheetViewQuery(options.currentView);
  if (viewQuery) {
    const viewParams = new URLSearchParams(viewQuery);
    viewParams.forEach((value, key) => params.set(key, value));
  }
  return `${currentProjectUrl(projectId)}/exports/sheets?${params.toString()}`;
}

export function workLogExportUrl(
  projectId: ProjectId,
  format: 'md' | 'html' | 'pdf' = 'md',
): string {
  const ext = format === 'html' ? 'html' : format === 'pdf' ? 'pdf' : 'md';
  return `${currentProjectUrl(projectId)}/export/work-log.${ext}`;
}
