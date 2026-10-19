import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { MapPin, Pause, Play } from 'lucide-react';
import type { CellValue, ColumnDef, Row } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { useSelector } from '../../bind/useSelector';
import { decodeEscapedText, looksLikeHtml } from '../../displayText';
import { imageRegionsForRow, type RegionBox } from '../../grid/cells';
import { presentationFor } from '../../grid/typeRegistry';
import { formatGeoPoint, parseGeoPointValue } from '../../grid/geo';
import { coerceFiniteNumber, formatCell, formatDefaultNumberDisplay } from '../../format';
import { resolveMediaValue } from '../../media/resolveMediaValue';
import { formatTimecode, isTemporalColumnType, parseTimelineValue, type TemporalColumnType } from '../../temporal/model';
import { HtmlView, MarkdownView } from '../../markdown';
import { createAudioPlaybackSource } from '../../state/audioPlaybackStore';
import type { RowTextTarget } from '../../state/detailStore';
import './FieldValue.css';
import { Button } from '../Button';
import { LargeCellValue } from '../LargeCellValue';
import { captionColumnScore, fieldValueDependencyColumnIds } from '../fieldValueDependencies';
import { JsonValue } from './JsonValue';
import { linkify, safeHttpUrl } from './ValueLinks';

/** Every detail field's value is capped at half the viewport height so a long
 *  transcript, entity table, or text blob can't push the rest of the Inspect
 *  column off-screen. When the value overflows the cap, a subtle "Show more"
 *  reveals the rest in place (and "Show less" re-collapses it). Content that
 *  fits shows no toggle at all. */
export function ClampedField({ children }: { children: ReactNode }) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  // Measure only while collapsed: expanding removes the max-height so
  // scrollHeight would equal clientHeight and falsely read as "fits". Keeping
  // `overflowing` latched true while expanded is what keeps "Show less" shown.
  useEffect(() => {
    if (expanded) return;
    const el = contentRef.current;
    if (!el) return;
    const measure = () => setOverflowing(el.scrollHeight - el.clientHeight > 1);
    measure();
    // jsdom (unit tests) has no ResizeObserver and no layout, so the one-shot
    // measure above is enough there; guard so the component still mounts.
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [expanded]);

  return (
    <div className="row-field-clamp" data-clamped={!expanded && overflowing ? 'true' : undefined}>
      <div
        ref={contentRef}
        className={`row-field-clamp-content${expanded ? ' row-field-clamp-expanded' : ''}`}
        data-testid="row-field-clamp"
      >
        {children}
      </div>
      {overflowing && (
        <Button
          type="button"
          className="row-field-clamp-toggle"
          data-testid="row-field-clamp-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show less' : 'Show more'}
        </Button>
      )}
    </div>
  );
}

export function FieldValue({
  col,
  columns,
  row,
  sheetId = '',
  value,
  onSelectItem,
  selectableItemIndices,
  textTarget,
}: {
  col: ColumnDef;
  columns: ColumnDef[];
  row: Row;
  sheetId?: string;
  value: Row['cells'][string];
  onSelectItem?(index: number): void;
  selectableItemIndices?: ReadonlySet<number>;
  textTarget?: RowTextTarget;
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  if (row.loadedColumnIds && !row.loadedColumnIds.includes(col.id)) return <p role="status">Loading value…</p>;
  const bounded = row.boundedValues?.[col.id];
  if (bounded || textTarget) return <LargeCellValue projectId={projectId} rowId={row.id} columnId={col.id} descriptor={bounded} textTarget={textTarget} />;
  // Explicit "(empty)" for a real null/empty value so the detail panel never
  // shows blank (or, pre-coercion, the literal string "null") as if it were
  // text. Styled by .row-field-empty (dim, italic, --text-light).
  if (value === null || value === '') return <div className="row-field-empty">(empty)</div>;
  // Imported email bodies explicitly mark HTML-looking source as literal text.
  // This must precede every rich/text enhancement: Markdown, HTML detection,
  // and URL linkification can each create elements or fetchable resources.
  if (col.format === 'plain_text') {
    return <div className="row-field-value">{String(value)}</div>;
  }
  const presentation = presentationFor(col.type);
  if (presentation.renderer === 'stars') {
    return <StarsValue value={value} />;
  }
  if (col.format === 'markdown') {
    return (
      <MarkdownView
        className="row-field-value row-field-markdown"
        source={String(value)}
        testId="markdown-value"
        decodeEscapes={Boolean(col.ai)}
      />
    );
  }
  if (isTemporalColumnType(col.type)) {
    return <TemporalValue type={col.type} value={value} />;
  }
  switch (col.type) {
    case 'image': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      const regions = imageRegionsForRow(row, col);
      if (regions.length === 0) {
        return <img className="row-field-media" src={media.url} alt={col.name} loading="lazy" />;
      }
      return <RegionImageValue url={media.url} alt={col.name} regions={regions} />;
    }
    case 'video': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      const captionTrack = mediaCaptionTrack(row, columns, col);
      return (
        <>
          <video
            className="row-field-media"
            src={media.url}
            controls
            preload="metadata"
            aria-label={`${col.name} video: ${media.label}`}
          >
            <track
              kind="captions"
              src={captionTrack.src}
              srcLang="en"
              label={captionTrack.label}
              default
            />
          </video>
          <div className="row-field-media-label">{media.label}</div>
        </>
      );
    }
    case 'audio': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      return (
        <AudioFieldControl
          columnId={col.id}
          columnName={col.name}
          label={media.label}
          rowId={row.id}
          sheetId={sheetId}
          url={media.url}
        />
      );
    }
    case 'file': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      return (
        <a className="row-field-link" href={media.url} target="_blank" rel="noopener noreferrer">
          {media.label}
        </a>
      );
    }
    case 'link':
      return <LinkValue value={String(value)} />;
    case 'geo_point':
      return <GeoPointValue value={value} />;
    case 'boolean':
      return <div className="row-field-value">{value ? 'true' : 'false'}</div>;
    case 'json':
      return <JsonValue value={value} isEmailAttachments={col.name === 'attachments'}
        semanticType={col.semanticType} onSelectItem={onSelectItem} selectableItemIndices={selectableItemIndices} />;
  }

  if (col.type === 'number' || col.type === 'integer') {
    const n = coerceFiniteNumber(value);
    if (n !== null) {
      const formattedNumber =
        formatCell(n, col.format) ?? formatDefaultNumberDisplay(n, col.type);
      return <div className="row-field-value">{formattedNumber}</div>;
    }
  }

  const formatted = formatCell(value, col.format);
  if (formatted) return <div className="row-field-value">{formatted}</div>;
  if (typeof value === 'object') {
    return <JsonValue value={value} semanticType={col.semanticType}
      onSelectItem={onSelectItem} selectableItemIndices={selectableItemIndices} />;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
      return <JsonValue value={value} semanticType={col.semanticType}
        onSelectItem={onSelectItem} selectableItemIndices={selectableItemIndices} />;
    }
    const rendered = col.ai ? decodeEscapedText(value) : value;
    if (looksLikeHtml(value) || looksLikeHtml(rendered)) {
      return <HtmlValue raw={value} source={rendered} />;
    }
    return <div className="row-field-value">{linkify(rendered)}</div>;
  }
  return <div className="row-field-value">{linkify(String(value))}</div>;
}

function LinkValue({ value }: { value: string }) {
  const href = safeHttpUrl(value);
  if (!href) return <div className="row-field-value">{value}</div>;
  return (
    <a className="row-field-link" href={href} target="_blank" rel="noopener noreferrer">
      {value}
    </a>
  );
}

function AudioFieldControl({
  columnId,
  columnName,
  label,
  rowId,
  sheetId,
  url,
}: {
  columnId: string;
  columnName: string;
  label: string;
  rowId: string;
  sheetId: string;
  url: string;
}) {
  const { audioPlayback } = useWorkspaceStores();
  const source = createAudioPlaybackSource({
    url,
    label,
    sheetId,
    rowId,
    columnId,
  });
  const playback = useSelector(audioPlayback.store, (state) => (
    state.source?.key === source.key && state.playing ? 'playing' : 'paused'
  ));

  return (
    <button
      type="button"
      className="row-field-audio-control"
      data-testid="row-audio"
      aria-label={`${playback === 'playing' ? 'Pause' : 'Play'} ${columnName} audio: ${label}`}
      onClick={() => audioPlayback.toggle(source)}
    >
      {playback === 'playing' ? <Pause size={14} aria-hidden /> : <Play size={14} aria-hidden />}
      <span>{label}</span>
    </button>
  );
}

function TemporalValue({
  type,
  value,
}: {
  type: TemporalColumnType;
  value: CellValue;
}) {
  const parsed = parseTimelineValue(value, type);
  if (!parsed) {
    return (
      <div className="row-field-temporal-invalid" role="status" data-testid="temporal-value-invalid">
        Invalid temporal value
      </div>
    );
  }
  const points = parsed.schema_version === 'frisket.timeline_point.v1'
    ? [parsed.item]
    : parsed.schema_version === 'frisket.timeline_points.v1'
      ? parsed.items
      : [];
  const ranges = parsed.schema_version === 'frisket.timeline_range.v1'
    ? [parsed.item]
    : parsed.schema_version === 'frisket.timeline_ranges.v1'
      ? parsed.items
      : [];
  return (
    <div className="row-field-temporal" data-testid="temporal-value">
      <div className="row-field-temporal-anchor">
        <span title={parsed.timeline.artifact_stable_id}>Source timeline</span>
        {parsed.timeline.duration_ms != null && (
          <span>{formatTimecode(parsed.timeline.duration_ms)} total</span>
        )}
      </div>
      {points.length === 0 && ranges.length === 0 ? (
        <div className="row-field-empty">No temporal selections</div>
      ) : (
        <ol className="row-field-temporal-items">
          {points.map((point) => (
            <li key={point.id} title={point.id}>
              <strong>{formatTimecode(point.at_ms)}</strong>
              {point.label ? <span>{point.label}</span> : null}
            </li>
          ))}
          {ranges.map((range) => (
            <li key={range.id} title={range.id}>
              <strong>{formatTimecode(range.start_ms)} – {formatTimecode(range.end_ms)}</strong>
              {range.label ? <span>{range.label}</span> : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// Exported as the render seam for StarsValue.test.tsx; plugin lifecycle and
// value validation remain browser-covered separately.
export function StarsValue({ value }: { value: unknown }) {
  const n = coerceFiniteNumber(value);
  if (n === null) return <div className="row-field-value">{String(value)}</div>;
  const clamped = Math.max(0, Math.min(5, n));
  const whole = Math.floor(clamped);
  const half = clamped - whole >= 0.5 && whole < 5;
  const stars = `${'★'.repeat(whole)}${half ? '½' : ''}${'☆'.repeat(5 - whole - (half ? 1 : 0))}`;
  return (
    <div className="row-field-value" data-testid="stars-value">
      <span aria-label={`${n} out of 5`}>{stars}</span>{' '}
      <span data-testid="stars-raw-value">{formatDefaultNumberDisplay(n, 'number')}</span>
    </div>
  );
}

function RegionImageValue({
  url,
  alt,
  regions,
}: {
  url: string;
  alt: string;
  regions: RegionBox[];
}) {
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const [imageSize, setImageSize] = useState<{ naturalWidth: number; naturalHeight: number } | null>(null);
  const [frameSize, setFrameSize] = useState<{ width: number; height: number } | null>(null);

  const setFrameRef = useCallback((frame: HTMLDivElement | null) => {
    resizeObserverRef.current?.disconnect();
    resizeObserverRef.current = null;
    if (!frame) return;
    const updateSize = () => {
      const rect = frame.getBoundingClientRect();
      setFrameSize({ width: rect.width, height: rect.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(frame);
    resizeObserverRef.current = observer;
  }, []);

  const imageRect = imageSize && frameSize
    ? containedImageRect(frameSize.width, frameSize.height, imageSize.naturalWidth, imageSize.naturalHeight)
    : null;

  return (
    <div className="row-field-region-media" data-testid="row-region-image" ref={setFrameRef}>
      <img
        className="row-field-media"
        src={url}
        alt={alt}
        loading="lazy"
        onLoad={(event) => {
          const img = event.currentTarget;
          setImageSize({ naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight });
        }}
      />
      {imageRect && regions.map((region, index) => (
        <span
          key={`${region.x}:${region.y}:${region.w}:${region.h}:${index}`}
          className="row-field-region-box"
          data-testid="row-region-box"
          title={region.label}
          style={regionBoxStyle(region, imageRect)}
        />
      ))}
    </div>
  );
}

function containedImageRect(
  frameWidth: number,
  frameHeight: number,
  naturalWidth: number,
  naturalHeight: number,
): { left: number; top: number; scale: number } {
  if (frameWidth <= 0 || frameHeight <= 0 || naturalWidth <= 0 || naturalHeight <= 0) {
    return { left: 0, top: 0, scale: 0 };
  }
  const scale = Math.min(frameWidth / naturalWidth, frameHeight / naturalHeight);
  return {
    left: (frameWidth - naturalWidth * scale) / 2,
    top: (frameHeight - naturalHeight * scale) / 2,
    scale,
  };
}

function regionBoxStyle(
  region: RegionBox,
  imageRect: { left: number; top: number; scale: number },
): CSSProperties {
  return {
    left: `${imageRect.left + region.x * imageRect.scale}px`,
    top: `${imageRect.top + region.y * imageRect.scale}px`,
    width: `${region.w * imageRect.scale}px`,
    height: `${region.h * imageRect.scale}px`,
  };
}

type CaptionTrack = {
  src: string;
  label: string;
};

const MAX_CAPTION_TRACK_CHARS = 40000;

function mediaCaptionTrack(row: Row, columns: ColumnDef[], mediaCol: ColumnDef): CaptionTrack {
  let best: { score: number; columnName: string; text: string } | null = null;
  const dependencyIds = new Set(fieldValueDependencyColumnIds(columns, mediaCol));
  for (const candidate of columns) {
    if (!dependencyIds.has(String(candidate.id))) continue;
    const score = captionColumnScore(candidate.name, mediaCol);
    if (score === null) continue;
    const text = captionTrackText(row.cells[candidate.id]);
    if (!text) continue;
    if (!best || score < best.score) {
      best = { score, columnName: candidate.name, text };
    }
  }
  if (!best) {
    return {
      src: webVttDataUrl(`No transcript or caption text is available for ${mediaCol.name}.`),
      label: 'No captions available',
    };
  }
  return {
    src: webVttDataUrl(best.text),
    label: best.columnName,
  };
}

function captionTrackText(value: CellValue | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) return null;
  return trimmed.slice(0, MAX_CAPTION_TRACK_CHARS);
}

function webVttDataUrl(text: string): string {
  const cueText = text.replace(/\r\n?/g, '\n').replace(/-->/g, '->').trim();
  const vtt = `WEBVTT\n\n00:00:00.000 --> 99:59:59.000\n${cueText}\n`;
  return `data:text/vtt;charset=utf-8,${encodeURIComponent(vtt)}`;
}

function HtmlValue({ raw, source }: { raw: string; source: string }) {
  return (
    <div className="row-field-rich-text">
      <HtmlView className="row-field-value row-field-html" source={source} testId="html-value" />
      <details className="row-field-raw">
        <summary>Raw</summary>
        <pre>{raw}</pre>
      </details>
    </div>
  );
}

function GeoPointValue({ value }: { value: CellValue }) {
  const point = parseGeoPointValue(value);
  if (!point) return <div className="row-field-value">{String(value)}</div>;
  const href =
    `https://www.openstreetmap.org/?mlat=${point.lat}&mlon=${point.lon}` +
    `#map=14/${point.lat}/${point.lon}`;
  return (
    <div className="geo-point-detail" data-testid="geo-point-value">
      <div className="geo-point-coords" data-testid="geo-point-coordinates">
        <MapPin size={15} />
        <span>{formatGeoPoint(point)}</span>
      </div>
      <a
        className="row-field-link"
        data-testid="geo-point-map-link"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
      >
        Open map
      </a>
    </div>
  );
}
