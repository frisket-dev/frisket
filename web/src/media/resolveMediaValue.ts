import type { CellValue } from '../api/types';
import { projectBlobUrl } from '../api/raw/projectResources';

export interface ResolvedMediaValue {
  url: string;
  label: string;
  filename?: string;
  mime?: string;
  blobHash?: string;
}

const fileLabel = (url: string): string => {
  try {
    const path = url.startsWith('http') ? new URL(url).pathname : url;
    return path.split('/').filter(Boolean).pop() ?? url;
  } catch {
    return url;
  }
};

/** The resolved label as a FILENAME, or null when there isn't one.
 *
 *  `fileLabel` above takes a URL's last path segment, which is a filename only
 *  when the URL ends in one. The sample project's photo column is
 *  `https://picsum.photos/seed/frisket-a1/240/160`, whose last segment is the
 *  image height — so every card in the sample gallery captioned itself "160"
 *  (2026-07-26 hand-use pass). A caption that is the same meaningless number on
 *  every row is worse than no caption, so callers that show one ask here and
 *  render nothing when the answer is null. */
export function mediaFilename(media: ResolvedMediaValue): string | null {
  if (media.filename) return media.filename;
  return /\.[a-z0-9]{1,8}$/i.test(media.label) ? media.label : null;
}

/**
 * Media cell values come in two shapes: a plain URL string, or the backend's
 * blob envelope {blob, mime, filename} encoded as JSON by the web adapter.
 */
export function resolveMediaValue(value: CellValue, projectId: string): ResolvedMediaValue | null {
  if (value === null || value === '') return null;
  const raw = String(value);
  if (raw.startsWith('{')) {
    try {
      const envelope = JSON.parse(raw) as {
        blob?: unknown;
        filename?: unknown;
        mime?: unknown;
      };
      if (envelope && typeof envelope.blob === 'string') {
        const filename =
          typeof envelope.filename === 'string' && envelope.filename
            ? envelope.filename
            : undefined;
        const mime =
          typeof envelope.mime === 'string' && envelope.mime ? envelope.mime : undefined;
        return {
          url: projectBlobUrl(projectId, envelope.blob),
          label: filename ?? envelope.blob.slice(0, 12),
          filename,
          mime,
          blobHash: envelope.blob,
        };
      }
    } catch {
      // Not a blob envelope; fall through to the raw string path.
    }
  }
  return { url: raw, label: fileLabel(raw) };
}
