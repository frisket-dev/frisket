import type { CellValue } from '../../api/types';
import { resolveMediaValue, type ResolvedMediaValue } from '../../media/resolveMediaValue';
import { documentMediaKind } from '../../workbench/documentMedia';

/** A review source is only opened as a document when it is an admitted blob
 * envelope or an absolute PDF URL. A filename-shaped text value is evidence,
 * not a browser navigation target. */
export function reviewPdfMedia(value: CellValue, projectId: string): ResolvedMediaValue | null {
  const raw = typeof value === 'string' ? value : null;
  if (!raw) return null;
  const isBlobEnvelope = (() => {
    if (!raw.trimStart().startsWith('{')) return false;
    try {
      const envelope = JSON.parse(raw) as { blob?: unknown };
      return typeof envelope.blob === 'string' && /^[a-f0-9]{64}$/i.test(envelope.blob);
    } catch {
      return false;
    }
  })();
  const isPdfUrl = (() => {
    try {
      const url = new URL(raw);
      return (url.protocol === 'https:' || url.protocol === 'http:')
        && url.pathname.toLowerCase().endsWith('.pdf');
    } catch {
      return false;
    }
  })();
  if (!isBlobEnvelope && !isPdfUrl) return null;
  const media = resolveMediaValue(raw, projectId);
  return documentMediaKind(media, 'file') === 'pdf' ? media : null;
}

