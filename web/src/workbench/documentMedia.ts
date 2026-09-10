import type { ColumnDef } from '../api/types';
import type { ResolvedMediaValue } from '../media/resolveMediaValue';

export type DocumentMediaKind = 'pdf' | 'image' | 'video' | 'audio' | 'text' | 'other';

/** Filename suffixes read as text when the blob envelope carries no mime.
 *  Deliberately short: a suffix we do not recognize falls through to `other`
 *  (a download link), which is wrong-but-honest, whereas rendering an unknown
 *  binary as text produces mojibake. */
const TEXT_FILE_SUFFIXES = ['.txt', '.md', '.markdown', '.log', '.csv', '.tsv', '.json'];

/** Classify a media cell for the reader: PDFs get the pdf.js page reader with
 *  header page controls; text files render as their own contents; everything
 *  else renders centered with native controls.
 *
 *  `text` is checked AFTER pdf/image/video/audio and before the `other`
 *  fallback: a `text/plain` file used to land on `other` and render as a
 *  download-link icon, which is the one document kind the Document view could
 *  not actually show you. */
export function documentMediaKind(
  media: ResolvedMediaValue | null,
  columnType: string,
): DocumentMediaKind | null {
  if (!media) return null;
  const mime = (media.mime ?? '').toLowerCase();
  const name = (media.filename ?? media.label ?? '').toLowerCase();
  if (mime.includes('pdf') || name.endsWith('.pdf')) return 'pdf';
  if (columnType === 'image' || mime.startsWith('image/')) return 'image';
  if (columnType === 'video' || mime.startsWith('video/')) return 'video';
  if (columnType === 'audio' || mime.startsWith('audio/')) return 'audio';
  if (mime.startsWith('text/')) return 'text';
  // Only trust the suffix when the envelope declared no mime at all — a
  // declared `application/octet-stream` on a `.txt` is still a claim, and
  // guessing past it is how a binary ends up rendered as text.
  if (!mime && TEXT_FILE_SUFFIXES.some((suffix) => name.endsWith(suffix))) return 'text';
  return 'other';
}

/** Column types that can be read as a document. `file` covers PDFs — there
 *  is no dedicated `pdf` column type in the registry. DocumentReader already
 *  has a native `<audio controls>` rendering branch for `documentMediaKind`'s
 *  `'audio'` case. */
const DOCUMENT_MEDIA_COLUMN_TYPES = ['file', 'image', 'video', 'audio'] as const;

function isDocumentMediaColumn(column: { type: string }): boolean {
  return (DOCUMENT_MEDIA_COLUMN_TYPES as readonly string[]).includes(column.type);
}

/** The sheet's media columns in priority order (files/PDFs, then images, then
 *  video), so the default source column and the switcher availability agree. */
export function documentMediaColumns(sheet: { columns: readonly ColumnDef[] }): ColumnDef[] {
  const byPriority = (type: string) => DOCUMENT_MEDIA_COLUMN_TYPES.indexOf(type as never);
  return sheet.columns
    .filter((column) => isDocumentMediaColumn(column))
    .sort((a, b) => byPriority(a.type) - byPriority(b.type));
}

/** What the Document view is reading: a media/file column, or a text column
 *  carrying annotation layers. A discriminated union rather than a nullable
 *  media value — a text cell resolves to `null` at every layer of the media
 *  model (`resolveMediaValue`, `documentMediaKind`), and `null` there means
 *  "render nothing", which is how a text-only sheet ended up with no reader at
 *  all. */
export type DocumentSource =
  | { kind: 'media'; column: ColumnDef }
  | { kind: 'text'; column: ColumnDef };

/** Every column this sheet can be read from: media first (existing priority),
 *  then the ANNOTATED text columns.
 *
 *  Text columns come from the narrow `annotatedTextColumnIds` signal, not from
 *  "column.type === 'text'". Every sheet has a text column; offering all of
 *  them would make the Document view a second, worse grid on every sheet in the
 *  project, and its availability rule would have to lie. */
export function documentSources(
  sheet: { columns: readonly ColumnDef[] },
  annotatedTextColumnIds: readonly string[],
): DocumentSource[] {
  const annotated = new Set(annotatedTextColumnIds);
  const media: DocumentSource[] = documentMediaColumns(sheet).map((column) => ({
    kind: 'media',
    column,
  }));
  const text: DocumentSource[] = sheet.columns
    .filter((column) => annotated.has(String(column.id)))
    .map((column) => ({ kind: 'text', column }));
  return [...media, ...text];
}
