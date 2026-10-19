import '../PanelPrimitives.css';
import './JsonValue.css';
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react';
import type { CellValue } from '../../api/open';
import { projectBlobUrl } from '../../api/raw/projectResources';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { formatTimecode } from '../../temporal/model';
import structuredStyles from '../StructuredValue.module.css';
import { linkify } from './ValueLinks';

const isUrl = (s: string): boolean => /^https?:\/\//.test(s);

const cellText = (v: unknown): string =>
  v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);

function jsonKeyBase(value: unknown): string {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    for (const field of ['id', 'url', 'href', 'link', 'name', 'title']) {
      const fieldValue = record[field];
      if (typeof fieldValue === 'string' || typeof fieldValue === 'number') {
        return `${field}:${String(fieldValue)}`;
      }
    }
  }
  return cellText(value);
}

function keyedJsonItems<T>(items: T[]): Array<{ item: T; key: string }> {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = jsonKeyBase(item);
    const duplicateCount = seen.get(base) ?? 0;
    seen.set(base, duplicateCount + 1);
    return {
      item,
      key: duplicateCount === 0 ? base : `${base}#${duplicateCount + 1}`,
    };
  });
}

/** Frames retain their timestamp beside a nested image. Closed image shapes
 * keep other JSON records (including face geometry) in the ordinary table. */
interface ImageBlobEnvelope {
  blob?: string;
  inlineDataUrl?: string;
  omitted?: boolean;
  mime: string;
  filename?: string;
  t?: number;
}

function imageBlobEnvelope(item: unknown): ImageBlobEnvelope | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
  const rec = item as Record<string, unknown>;
  const frame = Object.keys(rec).length === 2 && 't' in rec && 'image' in rec;
  if (frame && (typeof rec.t !== 'number' || !Number.isFinite(rec.t))) return null;
  const value = frame ? rec.image : rec;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const image = value as Record<string, unknown>;
  if (typeof image.mime !== 'string' || !image.mime.startsWith('image/')) return null;
  const keys = Object.keys(image);
  const stored = typeof image.blob === 'string' && /^[0-9a-f]{64}$/.test(image.blob)
    && keys.every((key) => ['blob', 'mime', 'filename'].includes(key));
  const inline = image.mime === 'image/jpeg' && typeof image.inline_data_url === 'string'
    && /^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/.test(image.inline_data_url)
    && image.inline_data_url.length <= 2_796_228
    && keys.length === 3 && keys.every((key) => ['inline_data_url', 'mime', 'filename'].includes(key));
  const omitted = image.mime === 'image/jpeg' && image.preview_omitted === true
    && keys.length === 3 && keys.every((key) => ['preview_omitted', 'mime', 'filename'].includes(key));
  if (!stored && !inline && !omitted) return null;
  if ((inline || omitted) && typeof image.filename !== 'string') return null;
  return {
    blob: stored ? image.blob as string : undefined,
    inlineDataUrl: inline ? image.inline_data_url as string : undefined,
    omitted,
    mime: image.mime,
    filename: typeof image.filename === 'string' && image.filename ? image.filename : undefined,
    t: frame ? rec.t as number : undefined,
  };
}

interface DownloadBlobEnvelope {
  blob: string;
  mime: string;
  filename: string;
}

const BLOB_DIGEST_RE = /^[0-9a-f]{64}$/;

/** Email attachments are deliberately a much narrower shape than media
 * envelopes. Exact keys prevent a producer extension or mixed list from being
 * silently hidden by the filename-only download presentation. */
function downloadBlobEnvelope(item: unknown): DownloadBlobEnvelope | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
  const record = item as Record<string, unknown>;
  const keys = Object.keys(record);
  if (
    keys.length !== 3
    || !keys.includes('blob')
    || !keys.includes('mime')
    || !keys.includes('filename')
    || typeof record.blob !== 'string'
    || !BLOB_DIGEST_RE.test(record.blob)
    || typeof record.mime !== 'string'
    || typeof record.filename !== 'string'
  ) return null;
  return { blob: record.blob, mime: record.mime, filename: record.filename };
}

/** The json-mini-table primitive: an array of objects rendered as a compact
 *  table with linkified cells. Shared by RowDrawer's JSON cell view (6-key cap)
 *  and the PDF-tables picker (per-table_index groups, metadata keys hidden by
 *  default). `hiddenKeys` are dropped from the header/body unless `showHidden`;
 *  `maxKeys` caps the visible column count (null = no cap). A list that is
 *  entirely image-blob envelopes renders as a thumbnail strip instead. */
export function JsonMiniTable({
  items,
  hiddenKeys,
  showHidden = false,
  maxKeys = null,
  testId = 'json-mini-table',
  itemTestId = 'review-result-item',
  disableBlobThumbnails = false,
  onSelectItem,
  selectableItemIndices,
}: {
  items: Record<string, unknown>[];
  hiddenKeys?: ReadonlySet<string>;
  showHidden?: boolean;
  maxKeys?: number | null;
  testId?: string;
  itemTestId?: string;
  /** Email attachment fallbacks retain their generic JSON table rather than
   * turning image-like malformed envelopes into resource-loading previews. */
  disableBlobThumbnails?: boolean;
  onSelectItem?(index: number): void;
  selectableItemIndices?: ReadonlySet<number>;
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  // A homogeneous list of image-blob envelopes (e.g. the `Extract frames`
  // `frames` column) becomes visible thumbnails rather than a table of blob
  // hashes. Any non-image item falls through to the text table below.
  const envelopes = items.map(imageBlobEnvelope);
  if (!disableBlobThumbnails && items.length > 0 && envelopes.every((e) => e !== null)) {
    return (
      <div className="json-blob-thumbs" data-testid="json-blob-thumbs">
        {envelopes.map((e, i) => {
          const env = e as ImageBlobEnvelope;
          return (
            <figure className="json-blob-thumb" key={`${env.blob}-${i}`}
              data-testid={itemTestId} {...resultItemProps(i, onSelectItem, selectableItemIndices)}>
              <ImageBlobContent envelope={env} projectId={projectId} />
            </figure>
          );
        })}
      </div>
    );
  }

  const keys: string[] = [];
  const seenKeys = new Set<string>();
  for (const it of items) {
    for (const k of Object.keys(it)) {
      if (!seenKeys.has(k)) {
        seenKeys.add(k);
        keys.push(k);
      }
    }
  }
  const visibleKeys =
    hiddenKeys && !showHidden ? keys.filter((k) => !hiddenKeys.has(k)) : keys;
  const shownKeys = typeof maxKeys === 'number' ? visibleKeys.slice(0, maxKeys) : visibleKeys;
  return (
    <div className="json-results" data-testid={testId}>
      <table className="json-table">
        <thead>
          <tr>{shownKeys.map((k) => <th key={k}>{k}</th>)}</tr>
        </thead>
        <tbody>
          {keyedJsonItems(items).map(({ item: it, key }, index) => (
            <tr key={key} data-testid={itemTestId} {...resultItemProps(index, onSelectItem, selectableItemIndices)}>
              {shownKeys.map((k) => {
                const v = it[k];
                return (
                  <td key={k}>
                    {typeof v === 'string' && isUrl(v) ? (
                      <a className="row-field-link" href={v} target="_blank" rel="noopener noreferrer">
                        {v.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60)}
                      </a>
                    ) : (
                      linkify(cellText(v))
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Entity-mention plumbing fields hidden from the detail table — only `text`
// and `type` read as content (frisket/ops/entities.py emits
// {text, type, start, end, score, fingerprint?, metadata?}).
const ENTITY_HIDDEN_KEYS: ReadonlySet<string> = new Set([
  'start',
  'end',
  'score',
  'fingerprint',
  'metadata',
]);

// Transcript segments ({segment_index, start, end, text, speaker?},
// frisket/preview/transcribe.py) drop the redundant positional index.
const TRANSCRIPT_SEGMENT_HIDDEN_KEYS: ReadonlySet<string> = new Set(['segment_index']);

/** An array of entity mentions: every item is an object carrying `text` and
 *  `type` strings plus at least one entity-specific offset/scoring field. The
 *  extra-key guard keeps a generic `{text, type}` json list from being treated
 *  as entities and stripped. */
function isEntityArray(items: Record<string, unknown>[]): boolean {
  return items.every(
    (it) =>
      typeof it.text === 'string' &&
      typeof it.type === 'string' &&
      ('start' in it || 'end' in it || 'fingerprint' in it || 'score' in it),
  );
}

/** An array of timestamped transcript segments — every item carries the
 *  `segment_index` the preview normalizer stamps on. */
function isTranscriptSegmentArray(items: Record<string, unknown>[]): boolean {
  return items.every((it) => 'segment_index' in it && 'text' in it);
}

type ResultItemProps = {
  'data-review-result-index': number;
  'data-review-result-selectable'?: 'true';
  'aria-label'?: string;
  tabIndex?: number;
  onClick?: (event: ReactMouseEvent<HTMLElement>) => void;
  onKeyDown?: (event: ReactKeyboardEvent<HTMLElement>) => void;
};

function resultItemProps(
  index: number,
  onSelectItem?: (index: number) => void,
  selectableItemIndices?: ReadonlySet<number>,
): ResultItemProps {
  const selectable = Boolean(onSelectItem && selectableItemIndices?.has(index));
  if (!selectable || !onSelectItem) return { 'data-review-result-index': index };
  const select = () => onSelectItem(index);
  return {
    'data-review-result-index': index,
    'data-review-result-selectable': 'true',
    'aria-label': `Show source for result item ${index + 1}`,
    tabIndex: 0,
    onClick: (event) => {
      const target = event.target;
      if (target instanceof Element && target !== event.currentTarget && target.closest('a, button, input, select, textarea')) return;
      select();
    },
    onKeyDown: (event) => {
      if (event.target !== event.currentTarget || !['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      select();
    },
  };
}

function ImageBlobContent({ envelope: env, projectId }: { envelope: ImageBlobEnvelope; projectId: string }) {
  const time = env.t != null ? formatTimecode(Math.round(env.t * 1000)) : null;
  const caption = env.filename ?? `${env.blob?.slice(0, 10)}…`;
  const alt = time ? `${caption} (${time})` : caption;
  return <>
    {env.omitted ? <span className="muted" data-testid="json-blob-thumb-omitted">
      Preview image omitted (size or image-count limit).
    </span> : <img data-testid="json-blob-thumb" src={env.inlineDataUrl ?? projectBlobUrl(projectId, env.blob!)}
      alt={alt} title={alt} loading="lazy" />}
    <figcaption>{caption}{time ? <span className="json-blob-thumb-time"> {time}</span> : null}</figcaption>
  </>;
}

function isFlatRecord(item: Record<string, unknown>): boolean {
  return Object.values(item).every((value) => value === null || typeof value !== 'object');
}

function StructuredJsonValue({
  value,
  depth = 0,
  onSelectItem,
  selectableItemIndices,
  disableBlobThumbnails = false,
}: {
  value: unknown;
  depth?: number;
  disableBlobThumbnails?: boolean;
  onSelectItem?: (index: number) => void;
  selectableItemIndices?: ReadonlySet<number>;
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="row-field-empty">(empty list)</span>;
    return (
      <ul className={`json-list ${structuredStyles.list}`} data-testid={depth === 0 ? 'json-list' : undefined}>
        {keyedJsonItems(value).map(({ item, key }, index) => (
          <li key={key} data-testid={depth === 0 ? 'review-result-item' : undefined}
            {...(depth === 0 ? resultItemProps(index, onSelectItem, selectableItemIndices) : {})}>
            <StructuredJsonValue value={item} depth={depth + 1} disableBlobThumbnails={disableBlobThumbnails} />
          </li>
        ))}
      </ul>
    );
  }
  if (value !== null && typeof value === 'object') {
    const envelope = disableBlobThumbnails ? null : imageBlobEnvelope(value);
    if (envelope) {
      return <figure className="json-blob-thumb"><ImageBlobContent envelope={envelope} projectId={projectId} /></figure>;
    }
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="row-field-empty">(empty object)</span>;
    return (
      <dl className={`json-object ${structuredStyles.object}`} data-testid={depth === 0 ? 'json-object' : undefined}>
        {entries.map(([key, item]) => <div key={key} className={structuredStyles.objectEntry}>
          <dt className={structuredStyles.term}>{key}</dt>
          <dd className={structuredStyles.description}><StructuredJsonValue value={item} depth={depth + 1} disableBlobThumbnails={disableBlobThumbnails} /></dd>
        </div>)}
      </dl>
    );
  }
  if (value === null || value === undefined || value === '') return <span className="row-field-empty">(empty)</span>;
  return <>{typeof value === 'string' ? linkify(value) : String(value)}</>;
}

export function JsonValue({
  value,
  isEmailAttachments = false,
  semanticType,
  onSelectItem,
  selectableItemIndices,
}: {
  value: CellValue;
  isEmailAttachments?: boolean;
  semanticType?: string | null;
  onSelectItem?: (index: number) => void;
  selectableItemIndices?: ReadonlySet<number>;
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  let parsed: unknown;
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value);
    } catch {
      return <pre className="row-field-json">{linkify(value)}</pre>;
    }
  } else {
    parsed = value;
  }

  if (Array.isArray(parsed) && parsed.length > 0) {
    // The email importer emits an exact homogeneous attachment list. Keep its
    // presentation intentionally small: filenames are same-origin downloads,
    // never previews or new browsing contexts. Anything malformed or mixed
    // remains on the generic JSON renderer below.
    const downloads = isEmailAttachments ? parsed.map(downloadBlobEnvelope) : [];
    if (isEmailAttachments && downloads.every((download) => download !== null)) {
      return (
        <ul className="json-blob-downloads" data-testid="json-blob-downloads">
          {downloads.map((download, index) => {
            const attachment = download as DownloadBlobEnvelope;
            return (
              <li key={attachment.blob} data-testid="review-result-item"
                {...resultItemProps(index, onSelectItem, selectableItemIndices)}>
                <a
                  className="row-field-link"
                  href={projectBlobUrl(projectId, attachment.blob)}
                  download={attachment.filename}
                >
                  {attachment.filename}
                </a>
              </li>
            );
          })}
        </ul>
      );
    }
    // Array of objects (e.g. web_search results) → mini table with links.
    if (parsed.every((x) => x !== null && typeof x === 'object' && !Array.isArray(x))) {
      const items = parsed as Record<string, unknown>[];
      // Entity mentions ({text, type, start, end, score, fingerprint?}) show
      // only the two fields that read as data — text and type. The offsets,
      // model score, and grouping fingerprint are plumbing, not content.
      const entities = semanticType === 'entity_mentions' || isEntityArray(items);
      // Timestamped transcript segments carry a redundant positional index
      // (`segment_index`); the start/end timecodes already order them.
      const transcript = !entities && isTranscriptSegmentArray(items);
      const compact = (!isEmailAttachments && items.every((item) => imageBlobEnvelope(item) !== null))
        || (items.every(isFlatRecord) && new Set(items.flatMap(Object.keys)).size <= 6);
      if (entities || transcript || compact) {
        return (
          <JsonMiniTable
            items={items}
            hiddenKeys={entities ? ENTITY_HIDDEN_KEYS : transcript ? TRANSCRIPT_SEGMENT_HIDDEN_KEYS : undefined}
            testId={entities ? 'entity-mini-table' : transcript ? 'transcript-segment-mini-table' : undefined}
            itemTestId={entities ? 'review-entity-item' : undefined}
            maxKeys={entities || transcript ? null : 6}
            disableBlobThumbnails={isEmailAttachments}
            onSelectItem={onSelectItem}
            selectableItemIndices={selectableItemIndices}
          />
        );
      }
    }
  }

  return <StructuredJsonValue value={parsed} disableBlobThumbnails={isEmailAttachments} onSelectItem={onSelectItem}
    selectableItemIndices={selectableItemIndices} />;
}
