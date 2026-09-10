// Cell construction + the custom renderer for media types (video/audio/file).
// This is the spot where Glide's custom-cell model gets stress-tested: we draw
// a badge (type icon + filename) straight onto the canvas.

import { coerceFiniteNumber, countLabel, formatNumberDisplay } from '../format';
import { textDisplayValue } from '../displayText';
import {
  GridCellKind,
  type CustomCell,
  type CustomRenderer,
  type GridCell,
  type SpriteMap,
  type Theme,
} from '@glideapps/glide-data-grid';
import { resolveMediaValue } from '../media/resolveMediaValue';
import type { CellValue, ColumnDef, Row } from './dataTypes';
import { formatGeoPoint, parseGeoPointValue, type GeoPoint } from './geo';
import {
  facetBehaviorFor,
  presentationFor,
  type TypeFacetBehavior,
  type TypePresentation,
} from './typeRegistry';
import {
  isTemporalColumnType,
  summarizeTemporalCell,
} from '../temporal/model';
import type { GridCellPalette as GridCellPaletteValue } from './gridTheme';

export type MediaKind = 'video' | 'audio' | 'file';

export interface MediaCellData {
  kind: 'frisket-media';
  mediaType: MediaKind;
  url: string | null;
  label: string;
  playback?: 'playing' | 'paused';
}

export type MediaCell = CustomCell<MediaCellData>;

/** Playback may only use the media already resolved into the grid cell. */
export function audioMedia(cell: GridCell): { url: string; label: string } | null {
  if (cell.kind !== GridCellKind.Custom) return null;
  const data = cell.data as Partial<MediaCellData>;
  return data.kind === 'frisket-media'
    && data.mediaType === 'audio'
    && typeof data.url === 'string'
    ? { url: data.url, label: typeof data.label === 'string' ? data.label : '' }
    : null;
}

export function withAudioPlayback(cell: GridCell, playing: boolean): GridCell {
  if (!audioMedia(cell) || cell.kind !== GridCellKind.Custom) return cell;
  return {
    ...cell,
    data: {
      ...(cell.data as MediaCellData),
      playback: playing ? 'playing' : 'paused',
    },
  } satisfies MediaCell;
}

/** The canvas badge remains a selectable cell; only its leading icon is the
 * pointer play target. Keyboard activation still operates on the whole cell. */
export function isAudioPlayButtonHit(x: number, y: number, height: number): boolean {
  return x >= 4 && x <= 40 && y >= 0 && y <= height;
}

export interface RegionBox {
  x: number;
  y: number;
  w: number;
  h: number;
  label?: string;
}

export interface RegionImageCellData {
  kind: 'frisket-region-image';
  url: string | null;
  label: string;
  regions: RegionBox[];
}

export type RegionImageCell = CustomCell<RegionImageCellData>;

export interface MapPinCellData {
  kind: 'frisket-map-pin';
  point: GeoPoint | null;
  label: string;
}

export type MapPinCell = CustomCell<MapPinCellData>;

export interface TimelineCellData {
  kind: 'frisket-timeline';
  label: string;
  invalid: boolean;
}

export type TimelineCell = CustomCell<TimelineCellData>;

const MEDIA_COLORS: Record<MediaKind, { bg: string; fg: string }> = {
  video: { bg: '#fdeeee', fg: '#b3392f' },
  audio: { bg: '#eaf3ec', fg: '#2f7d46' },
  file: { bg: '#eef0f4', fg: '#4a5468' },
};

const GEO_COLORS = { bg: '#e8f3f1', fg: '#1d766e' };
const TIMELINE_COLORS = {
  normal: { bg: '#eef1fb', fg: '#3854a6' },
  invalid: { bg: '#fdecec', fg: '#b42318' },
};

function formatStarsDisplay(value: number): string {
  const clamped = Math.max(0, Math.min(5, value));
  const whole = Math.floor(clamped);
  const half = clamped - whole >= 0.5 && whole < 5;
  return `${'★'.repeat(whole)}${half ? '½' : ''}${'☆'.repeat(5 - whole - (half ? 1 : 0))} ${clamped.toFixed(clamped % 1 === 0 ? 0 : 1)}`;
}

function drawMediaIcon(
  ctx: CanvasRenderingContext2D,
  kind: MediaKind,
  x: number,
  y: number,
  size: number,
  color: string,
  playback: 'playing' | 'paused' | undefined,
  hovered: boolean,
) {
  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  if (kind === 'video') {
    ctx.beginPath();
    ctx.moveTo(x + size * 0.22, y + size * 0.12);
    ctx.lineTo(x + size * 0.88, y + size * 0.5);
    ctx.lineTo(x + size * 0.22, y + size * 0.88);
    ctx.closePath();
    ctx.fill();
  } else if (kind === 'audio') {
    if (playback === 'playing') {
      const barWidth = Math.max(1.5, size * 0.2);
      ctx.fillRect(x + size * 0.22, y + size * 0.18, barWidth, size * 0.64);
      ctx.fillRect(x + size * 0.58, y + size * 0.18, barWidth, size * 0.64);
    } else if (hovered) {
      ctx.beginPath();
      ctx.moveTo(x + size * 0.28, y + size * 0.16);
      ctx.lineTo(x + size * 0.82, y + size * 0.5);
      ctx.lineTo(x + size * 0.28, y + size * 0.84);
      ctx.closePath();
      ctx.fill();
    } else {
      const bars = [0.45, 0.8, 0.55, 1.0, 0.6, 0.35];
      const bw = size * 0.62 / (bars.length * 1.6);
      bars.forEach((h, i) => {
        const bh = size * h;
        const bx = x + i * bw * 1.6;
        ctx.fillRect(bx, y + (size - bh) / 2, bw, bh);
      });
    }
  } else {
    const w = size * 0.72;
    const fold = size * 0.26;
    const bx = x + (size - w) / 2;
    ctx.beginPath();
    ctx.moveTo(bx, y);
    ctx.lineTo(bx + w - fold, y);
    ctx.lineTo(bx + w, y + fold);
    ctx.lineTo(bx + w, y + size);
    ctx.lineTo(bx, y + size);
    ctx.closePath();
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(bx + w - fold, y);
    ctx.lineTo(bx + w - fold, y + fold);
    ctx.lineTo(bx + w, y + fold);
    ctx.stroke();
  }
  ctx.restore();
}

const mediaCellRenderer: CustomRenderer<MediaCell> = {
  kind: GridCellKind.Custom,
  isMatch: (c): c is MediaCell =>
    (c.data as Partial<MediaCellData>).kind === 'frisket-media',
  needsHover: (cell) => cell.data.mediaType === 'audio' && Boolean(cell.data.url),
  needsHoverPosition: true,
  draw: (args, cell) => {
    const { ctx, rect, theme, hoverAmount, hoverX, hoverY, overrideCursor } = args;
    const { mediaType, url, label, playback } = cell.data;
    if (!url) {
      ctx.fillStyle = theme.textLight;
      ctx.fillText('—', rect.x + theme.cellHorizontalPadding, rect.y + rect.height / 2 + 4);
      return;
    }
    const colors = MEDIA_COLORS[mediaType];
    const padX = theme.cellHorizontalPadding;
    const badgeH = Math.min(22, rect.height - 8);
    const y = rect.y + (rect.height - badgeH) / 2;
    const iconSize = badgeH - 10;

    ctx.font = `11px ${theme.fontFamily}`;
    const maxTextW = rect.width - padX * 2 - iconSize - 16;
    let text = label;
    while (text.length > 3 && ctx.measureText(text).width > maxTextW) {
      text = text.slice(0, -2);
    }
    if (text !== label) text += '…';
    const textW = Math.max(0, ctx.measureText(text).width);
    const badgeW = Math.min(rect.width - padX * 2, iconSize + textW + 16);

    ctx.beginPath();
    ctx.roundRect(rect.x + padX, y, badgeW, badgeH, badgeH / 2);
    ctx.fillStyle = colors.bg;
    ctx.fill();

    const audioControlHovered = mediaType === 'audio'
      && hoverX !== undefined
      && hoverY !== undefined
      && isAudioPlayButtonHit(hoverX, hoverY, rect.height);
    if (audioControlHovered) overrideCursor?.('pointer');
    drawMediaIcon(
      ctx,
      mediaType,
      rect.x + padX + 7,
      y + 5,
      iconSize,
      colors.fg,
      playback,
      audioControlHovered && hoverAmount > 0,
    );
    ctx.fillStyle = colors.fg;
    ctx.textBaseline = 'middle';
    ctx.fillText(text, rect.x + padX + iconSize + 11, y + badgeH / 2 + 0.5);
  },
  provideEditor: undefined,
};

function drawRegionImage(
  ctx: CanvasRenderingContext2D,
  rect: { x: number; y: number; width: number; height: number },
  theme: Theme,
  image: HTMLImageElement | ImageBitmap,
  regions: RegionBox[],
) {
  const imgHeight = rect.height - theme.cellVerticalPadding * 2;
  const imgWidth = image.width * (imgHeight / image.height);
  const drawX = rect.x + theme.cellHorizontalPadding;
  const drawY = rect.y + theme.cellVerticalPadding;
  const width = Math.min(imgWidth, rect.width - theme.cellHorizontalPadding * 2);
  const height = width < imgWidth ? image.height * (width / image.width) : imgHeight;
  const scaleX = width / image.width;
  const scaleY = height / image.height;

  ctx.save();
  ctx.beginPath();
  ctx.roundRect(drawX, drawY, width, height, 4);
  ctx.clip();
  ctx.drawImage(image, drawX, drawY, width, height);
  ctx.restore();

  ctx.save();
  ctx.lineWidth = 2;
  ctx.strokeStyle = '#f04438';
  ctx.fillStyle = 'rgba(240, 68, 56, 0.14)';
  for (const region of regions) {
    const x = drawX + region.x * scaleX;
    const y = drawY + region.y * scaleY;
    const w = region.w * scaleX;
    const h = region.h * scaleY;
    if (w <= 0 || h <= 0) continue;
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
  }
  ctx.restore();
}

const regionImageCellRenderer: CustomRenderer<RegionImageCell> = {
  kind: GridCellKind.Custom,
  isMatch: (c): c is RegionImageCell =>
    (c.data as Partial<RegionImageCellData>).kind === 'frisket-region-image',
  draw: (args, cell) => {
    const { ctx, rect, theme, imageLoader, col, row } = args;
    const { url, label, regions } = cell.data;
    if (!url) {
      ctx.fillStyle = theme.textLight;
      ctx.fillText('—', rect.x + theme.cellHorizontalPadding, rect.y + rect.height / 2 + 4);
      return;
    }
    const img = imageLoader.loadOrGetImage(url, col, row);
    if (!img) {
      ctx.fillStyle = theme.textLight;
      ctx.fillText(label || 'image', rect.x + theme.cellHorizontalPadding, rect.y + rect.height / 2 + 4);
      return;
    }
    drawRegionImage(ctx, rect, theme, img, regions);
  },
  provideEditor: undefined,
};

function drawPinIcon(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  size: number,
  color: string,
) {
  ctx.save();
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x + size / 2, y + size * 0.42, size * 0.32, 0, Math.PI * 2);
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(x + size / 2, y + size);
  ctx.lineTo(x + size * 0.22, y + size * 0.56);
  ctx.lineTo(x + size * 0.78, y + size * 0.56);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = '#ffffff';
  ctx.beginPath();
  ctx.arc(x + size / 2, y + size * 0.42, size * 0.12, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

const mapPinCellRenderer: CustomRenderer<MapPinCell> = {
  kind: GridCellKind.Custom,
  isMatch: (c): c is MapPinCell =>
    (c.data as Partial<MapPinCellData>).kind === 'frisket-map-pin',
  draw: (args, cell) => {
    const { ctx, rect, theme } = args;
    const { point, label } = cell.data;
    if (!point) {
      ctx.fillStyle = theme.textLight;
      ctx.fillText('—', rect.x + theme.cellHorizontalPadding, rect.y + rect.height / 2 + 4);
      return;
    }
    const padX = theme.cellHorizontalPadding;
    const badgeH = Math.min(22, rect.height - 8);
    const y = rect.y + (rect.height - badgeH) / 2;
    const iconSize = badgeH - 8;

    ctx.font = `11px ${theme.fontFamily}`;
    const maxTextW = rect.width - padX * 2 - iconSize - 14;
    let text = label;
    while (text.length > 3 && ctx.measureText(text).width > maxTextW) {
      text = text.slice(0, -2);
    }
    if (text !== label) text += '...';
    const textW = Math.max(0, ctx.measureText(text).width);
    const badgeW = Math.min(rect.width - padX * 2, iconSize + textW + 16);

    ctx.beginPath();
    ctx.roundRect(rect.x + padX, y, badgeW, badgeH, badgeH / 2);
    ctx.fillStyle = GEO_COLORS.bg;
    ctx.fill();

    drawPinIcon(ctx, rect.x + padX + 6, y + 4, iconSize, GEO_COLORS.fg);
    ctx.fillStyle = GEO_COLORS.fg;
    ctx.textBaseline = 'middle';
    ctx.fillText(text, rect.x + padX + iconSize + 10, y + badgeH / 2 + 0.5);
  },
  provideEditor: undefined,
};

const timelineCellRenderer: CustomRenderer<TimelineCell> = {
  kind: GridCellKind.Custom,
  isMatch: (cell): cell is TimelineCell =>
    (cell.data as Partial<TimelineCellData>).kind === 'frisket-timeline',
  draw: (args, cell) => {
    const { ctx, rect, theme } = args;
    const { label, invalid } = cell.data;
    if (!label) {
      ctx.fillStyle = theme.textLight;
      ctx.fillText('—', rect.x + theme.cellHorizontalPadding, rect.y + rect.height / 2 + 4);
      return;
    }
    const colors = invalid ? TIMELINE_COLORS.invalid : TIMELINE_COLORS.normal;
    const padX = theme.cellHorizontalPadding;
    const badgeH = Math.min(22, rect.height - 8);
    const y = rect.y + (rect.height - badgeH) / 2;
    ctx.font = `11px ${theme.fontFamily}`;
    const maxTextW = Math.max(0, rect.width - padX * 2 - 16);
    let text = label;
    while (text.length > 3 && ctx.measureText(text).width > maxTextW) text = text.slice(0, -2);
    if (text !== label) text += '…';
    const badgeW = Math.min(rect.width - padX * 2, ctx.measureText(text).width + 16);
    ctx.beginPath();
    ctx.roundRect(rect.x + padX, y, badgeW, badgeH, badgeH / 2);
    ctx.fillStyle = colors.bg;
    ctx.fill();
    ctx.fillStyle = colors.fg;
    ctx.textBaseline = 'middle';
    ctx.fillText(text, rect.x + padX + 8, y + badgeH / 2 + 0.5);
  },
  // Temporal values require source-aware contextual validation. Keeping the
  // grid cell read-only prevents Glide's generic text overlay from turning a
  // bound value into unvalidated JSON.
  provideEditor: undefined,
};

// Pending cell: a subtle animated shimmer drawn while a run is streaming
// results into this column (the cell has no value yet).

export interface PendingCellData {
  kind: 'frisket-pending';
  palette: GridCellPaletteValue['pending'];
}

export type PendingCell = CustomCell<PendingCellData>;

// The pulse animates unless the viewer asked for reduced motion, in which case
// it renders a single static band (still a legible "pending" affordance, no rAF
// loop). Read once and kept live via a listener — the query is cheap but draw
// runs every animation frame for every pending cell, so we never re-query it in
// the hot path.
const reducedMotionQuery =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : null;
let reducedMotion = reducedMotionQuery?.matches ?? false;
reducedMotionQuery?.addEventListener?.('change', (e) => {
  reducedMotion = e.matches;
});

/** Canvas needs a concrete rgba() value; CSS token colors are documented hex
 * values, and an unexpected token format falls back to the opaque color. */
function colorWithOpacity(color: string, opacity: number): string {
  const match = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!match) return color;
  const value = parseInt(match[1], 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${opacity})`;
}

const pendingCellRenderer: CustomRenderer<PendingCell> = {
  kind: GridCellKind.Custom,
  isMatch: (c): c is PendingCell =>
    (c.data as Partial<PendingCellData>).kind === 'frisket-pending',
  draw: (args, cell) => {
    const { ctx, rect, theme, frameTime, requestAnimationFrame } = args;
    const { background, highlight } = cell.data.palette;
    const padX = theme.cellHorizontalPadding;
    const h = Math.min(10, rect.height - 10);
    const y = rect.y + (rect.height - h) / 2;
    const w = Math.max(24, Math.min(rect.width - padX * 2, 96));
    ctx.save();
    ctx.beginPath();
    ctx.roundRect(rect.x + padX, y, w, h, h / 2);
    ctx.fillStyle = background;
    ctx.fill();
    ctx.clip();
    if (reducedMotion) {
      // Static band: a subtle fixed highlight, no animation frame requested.
      const grad = ctx.createLinearGradient(rect.x + padX, 0, rect.x + padX + w, 0);
      grad.addColorStop(0, colorWithOpacity(highlight, 0.12));
      grad.addColorStop(0.5, colorWithOpacity(highlight, 0.22));
      grad.addColorStop(1, colorWithOpacity(highlight, 0.12));
      ctx.fillStyle = grad;
      ctx.fillRect(rect.x + padX, y, w, h);
      ctx.restore();
      return;
    }
    const t = (frameTime % 1200) / 1200;
    const bandW = w * 0.5;
    const bx = rect.x + padX - bandW + (w + bandW * 2) * t;
    const grad = ctx.createLinearGradient(bx, 0, bx + bandW, 0);
    grad.addColorStop(0, colorWithOpacity(highlight, 0));
    grad.addColorStop(0.5, colorWithOpacity(highlight, 0.28));
    grad.addColorStop(1, colorWithOpacity(highlight, 0));
    ctx.fillStyle = grad;
    ctx.fillRect(bx, y, bandW, h);
    ctx.restore();
    requestAnimationFrame();
  },
  provideEditor: undefined,
};

export const customRenderers = [
  mediaCellRenderer,
  regionImageCellRenderer,
  pendingCellRenderer,
  mapPinCellRenderer,
  timelineCellRenderer,
];

// ---------------------------------------------------------------------------
// Header icon for AI columns (Glide sprite map: name -> svg string)

const tag = (glyph: string): SpriteMap[string] =>
  ({ fgColor, bgColor }) => `
    <svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
      <rect width="20" height="20" rx="4" fill="${bgColor}"/>
      <text x="10" y="14" text-anchor="middle" font-family="IBM Plex Mono, monospace"
        font-size="10" font-weight="600" fill="${fgColor}">${glyph}</text>
    </svg>`;

export const headerIcons: SpriteMap = {
  aiBolt: ({ fgColor, bgColor }) => `
    <svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
      <rect width="20" height="20" rx="4" fill="${bgColor}"/>
      <path d="M11.2 3 5.6 11h3.3l-1 6 5.5-8h-3.2l1-6z" fill="${fgColor}"/>
    </svg>`,
  // Column TYPE-TAG pills. Glide renders headers on canvas, so the type "pill"
  // is delivered as a header sprite glyph next to the label.
  typeText: tag('T'),
  typeNumber: tag('#'),
  typeBoolean: tag('✓'),
  typeDate: tag('◷'),
  typeLink: tag('↗'),
  typeImage: tag('▦'),
  typeGeo: tag('◉'),
  typeJson: tag('{}'),
  typeCategory: tag('◈'),
  typeTimeline: tag('↦'),
  // A filtered column swaps its type glyph for a funnel so the active-filter
  // state is visible on the header itself.
  funnel: ({ fgColor, bgColor }) => `
    <svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
      <rect width="20" height="20" rx="4" fill="${bgColor}"/>
      <path d="M4 5h12l-4.6 5.4V15l-2.8 1.4v-6L4 5z" fill="${fgColor}"/>
    </svg>`,
};

/** Sprite name for a column's type tag, or undefined for unknown/plugin types
 *  (which fall back to no glyph). Resolved from the presentation renderer so
 *  plugin-registered types (geo_point → map-pin, image, etc.) map correctly. */
export function typeHeaderIcon(type: string): string | undefined {
  const renderer = presentationFor(type).renderer ?? type;
  switch (renderer) {
    case 'text':
      return 'typeText';
    case 'number':
    case 'integer':
    case 'stars':
      return 'typeNumber';
    case 'boolean':
      return 'typeBoolean';
    case 'link':
      return 'typeLink';
    case 'image':
    case 'media':
      return 'typeImage';
    case 'map-pin':
      return 'typeGeo';
    case 'json':
      return 'typeJson';
    case 'category':
      return 'typeCategory';
    case 'timeline':
      return 'typeTimeline';
    default:
      break;
  }
  if (type === 'date') return 'typeDate';
  return undefined;
}

// AI-column tint is derived from the CSS token palette at runtime — see
// readAiColumnTheme in ./gridTheme (kept in sync with :root so canvas and CSS
// cannot drift).

// ---------------------------------------------------------------------------
// Value -> GridCell

function parseUnknownJson(v: CellValue): unknown {
  if (v === null || v === '') return null;
  if (typeof v !== 'string') return v;
  const trimmed = v.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null;
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return null;
  }
}

function finiteNumber(v: unknown): number | null {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function normalizeRegion(candidate: unknown): RegionBox | null {
  if (candidate === null || typeof candidate !== 'object') return null;
  const obj = candidate as Record<string, unknown>;
  if (Array.isArray(obj.bbox) && obj.bbox.length >= 4) {
    const xs = obj.bbox.map((p) =>
      Array.isArray(p) ? finiteNumber(p[0]) : null,
    );
    const ys = obj.bbox.map((p) =>
      Array.isArray(p) ? finiteNumber(p[1]) : null,
    );
    if (xs.every((n) => n !== null) && ys.every((n) => n !== null)) {
      const minX = Math.min(...(xs as number[]));
      const minY = Math.min(...(ys as number[]));
      const maxX = Math.max(...(xs as number[]));
      const maxY = Math.max(...(ys as number[]));
      return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
    }
  }

  if (Array.isArray(obj.bbox) && obj.bbox.length === 4) {
    const [x, y, w, h] = obj.bbox.map(finiteNumber);
    if (x !== null && y !== null && w !== null && h !== null) return { x, y, w, h };
  }

  const x = finiteNumber(obj.x ?? obj.left);
  const y = finiteNumber(obj.y ?? obj.top);
  const w = finiteNumber(obj.w ?? obj.width);
  const h = finiteNumber(obj.h ?? obj.height);
  if (x !== null && y !== null && w !== null && h !== null) {
    return {
      x,
      y,
      w,
      h,
      label: typeof obj.label === 'string' ? obj.label : undefined,
    };
  }
  return null;
}

function collectRegions(value: unknown, acc: RegionBox[]) {
  if (Array.isArray(value)) {
    for (const item of value) collectRegions(item, acc);
    return;
  }
  const region = normalizeRegion(value);
  if (region) acc.push(region);
}

export function imageRegionsForRow(row: Row, imageColumn: ColumnDef): RegionBox[] {
  const regions: RegionBox[] = [];
  for (const [columnId, raw] of Object.entries(row.cells)) {
    if (columnId === imageColumn.id) continue;
    const parsed = parseUnknownJson(raw);
    if (parsed === null) continue;
    collectRegions(parsed, regions);
  }
  return regions.slice(0, 200);
}

/** Parse a json cell into an array, if that's what it holds. */
export function parseJsonArray(v: CellValue): unknown[] | null {
  if (typeof v !== 'string' || !v.trimStart().startsWith('[')) return null;
  try {
    const arr = JSON.parse(v) as unknown;
    return Array.isArray(arr) ? arr : null;
  } catch {
    return null;
  }
}

export interface BuildCellOptions {
  projectId?: string;
  /** Wrap long text in text-ish cells (pair with taller rows). */
  wrap?: boolean;
  /** Run is streaming into this column and the cell is still empty. */
  pending?: boolean;
  /** Theme snapshot supplied by SheetGrid's CSS-token observer. */
  gridCellPalette?: GridCellPaletteValue;
  /** This column is eligible for in-place (spreadsheet-style) editing —
   *  see cellEditability below. Only SheetGrid's real getCellContent passes
   *  this; the in-memory preview overlay (previewGridCell) never does, so a
   *  sampled/unwritten preview value can never look editable. */
  editable?: boolean;
  /** A marked entity-mention cell was explicitly opened in the grid. Generic
   * JSON arrays deliberately remain their compact count bubbles. */
  expandEntityMentions?: boolean;
}

const ENTITY_MENTIONS_SEMANTIC_TYPE = 'entity_mentions';
const MAX_EXPANDED_ENTITY_MENTION_PILLS = 50;

interface EntityMentionSummary {
  /** Number of well-formed mentions in the persisted JSON array. */
  count: number;
  /** The labels we can safely put on a canvas cell without an unbounded cost. */
  labels: string[];
}

/** Scan the persisted value once. Arbitrary JSON objects do not become
 * mentions merely because they happen to include a `text` property. */
function summarizeEntityMentions(items: unknown[]): EntityMentionSummary {
  const labels: string[] = [];
  let count = 0;
  for (const item of items) {
    if (
      item !== null
      && typeof item === 'object'
      && !Array.isArray(item)
      && typeof (item as { type?: unknown }).type === 'string'
      && (item as { type: string }).type.trim() !== ''
      && typeof (item as { text?: unknown }).text === 'string'
      && (item as { text: string }).text.trim() !== ''
    ) {
      count += 1;
      if (labels.length < MAX_EXPANDED_ENTITY_MENTION_PILLS) {
        labels.push((item as { text: string }).text);
      }
    }
  }
  return { count, labels };
}

type EntityMentionToggleCell = GridCell & { entityMentionToggle?: true };

/** The displayed cell, rather than its raw input, is the interaction
 * authority. This prevents error, incomplete, withheld, pending, and preview
 * stand-ins from inheriting interactivity from a value they do not display. */
export function isExpandableEntityMentionCell(col: ColumnDef, cell: GridCell): boolean {
  return col.type === 'json'
    && col.semanticType === ENTITY_MENTIONS_SEMANTIC_TYPE
    && (cell as EntityMentionToggleCell).entityMentionToggle === true;
}

function entityMentionPills(summary: EntityMentionSummary): string[] {
  if (summary.count <= MAX_EXPANDED_ENTITY_MENTION_PILLS) return summary.labels;
  return [
    ...summary.labels,
    `+${summary.count - MAX_EXPANDED_ENTITY_MENTION_PILLS} more`,
  ];
}

function withEntityMentionToggle(cell: GridCell, expandable: boolean): GridCell {
  if (!expandable) return cell;
  return {
    ...cell,
    cursor: 'pointer',
    entityMentionToggle: true,
  } as EntityMentionToggleCell;
}

export interface OneClickFacetValue {
  value: string;
  operator: Extract<TypeFacetBehavior, { kind: 'categorical' }>['operator'];
}

/** Exact value represented by a registry-declared one-click facet cell.
 * Synthetic error/incomplete/withheld bubbles never become filters, and a
 * blank category remains a selectable cell rather than an invisible filter. */
export function oneClickFacetValue(
  col: ColumnDef,
  row: Row,
): OneClickFacetValue | null {
  const facet = facetBehaviorFor(col.type);
  if (!facet?.oneClick) return null;
  if (row.cellErrors?.[col.id]) return null;
  if (row.cellOutcomes?.[col.id] === 'withheld_unverified') return null;
  if (row.cellStates?.[col.id] === 'incomplete') return null;
  const raw = row.cells[col.id];
  if (raw === null || raw === undefined || raw === '') return null;
  if (typeof raw !== 'string' && typeof raw !== 'number' && typeof raw !== 'boolean') {
    return null;
  }
  return { value: String(raw), operator: facet.operator };
}

/** Decorate only committed cells whose exact value the grid click handler
 * turns into a facet. Preview cells deliberately do not pass through here. */
export function withOneClickFacetCursor(
  cell: GridCell,
  col: ColumnDef,
  row: Row | undefined,
): GridCell {
  return row && oneClickFacetValue(col, row)
    ? { ...cell, cursor: 'pointer' }
    : cell;
}

// ---------------------------------------------------------------------------
// In-place (spreadsheet-style) cell editing (grid-in-place-edit-v1).
//
// Double-click/Enter on a grid cell edits it in place via glide's own overlay
// editor instead of opening the row drawer — the drawer stays reachable via a
// floating "open row details" affordance (SheetGrid.tsx) or the Space key.
//
// SCOPE (post-review retreat): text renderer ONLY this round. number/integer/
// stars/boolean were dropped after review, for three concrete reasons, not
// just "be conservative":
//   1. glide's BooleanCell overlay toggles on a SINGLE click — that click is
//      also the one the drawer-repointing flow (onCellClicked/
//      onGridSelectionChange, and tests/e2e/helpers.ts's openCellDrawer) fires
//      routinely, so making a boolean column overlay-editable turns an
//      ordinary selection click into a silent data mutation.
//   2. buildCell's empty-marker branch renders EVERY empty scalar (including
//      number/integer/stars) as a GridCellKind.Text cell with data: '' — so an
//      empty number/integer/stars cell edited via that branch would commit a
//      STRING through cell.edit, with the destination column's type erased.
//   3. glide's built-in Number editor enforces neither integer-ness nor a
//      stars 0-5 range, so `1.5` in an integer column or `99` in a stars
//      column would reach cell.edit completely unvalidated.
// Those columns keep the pre-feature behavior (activation always opens the
// drawer; editing happens there) until a registry-declared inline-editor
// capability exists per column type (tracked separately, not this feature).
const EDITABLE_RENDERERS = new Set(['text']);

/** Whether a column's cells should open glide's native overlay editor on
 *  double-click/Enter (vs. staying read-only, with the row drawer as the only
 *  edit surface). AI-generated columns are never editable in the grid — their
 *  value is the run's output, edited only via re-running or the drawer. A
 *  markdown-formatted text column keeps its existing read-only preview-on-open
 *  behavior (the overlay renders the rendered markdown, not an editor).
 *  This is a COLUMN-level check only — see isOverlayEditable below for the
 *  per-CELL check activation actually branches on (an error/pending/withheld
 *  cell in an eligible column is still not overlay-editable). */
export function cellEditability(col: ColumnDef): boolean {
  if (col.ai) return false;
  if (col.format === 'markdown') return false;
  const renderer = presentationFor(col.type).renderer ?? col.type;
  return EDITABLE_RENDERERS.has(renderer);
}

/** Cell kinds `editable` is allowed to flip to allowOverlay+writable — the
 *  kind the text builder actually returns for a NORMAL (non-error, non-
 *  pending, non-marker) cell. Never applied to Bubble/Custom/Image/Loading
 *  cells even on an editable column: a withheld/incomplete/pending
 *  placeholder is not a value to type over. (Number/Boolean kinds are never
 *  reached here post-retreat — EDITABLE_RENDERERS no longer produces
 *  opts.editable: true for those columns — but the kind check stays as a
 *  second line of defense.) */
function withEditableOverlay(cell: GridCell, editable: boolean | undefined): GridCell {
  if (!editable) return cell;
  if (cell.kind === GridCellKind.Text) {
    return { ...cell, allowOverlay: true, readonly: false };
  }
  return cell;
}

/** The per-CELL check activation (double-click/Enter) actually branches on:
 *  is THIS specific rendered cell overlay-editable right now, as opposed to
 *  "is the column eligible" (cellEditability). buildCell deliberately keeps
 *  an error cell, a pending/incomplete/withheld Bubble, and a preview cell
 *  read-only even in an eligible column — those must still fall through to
 *  the drawer on activation, not go dead (no editor, no drawer). A markdown
 *  cell (allowOverlay: true, readonly: true — the read-only rendered preview)
 *  is deliberately NOT overlay-editable by this check either, preserving its
 *  pre-existing dual behavior (the overlay preview opens AND the drawer
 *  opens on activation). */
export function isOverlayEditable(cell: GridCell): boolean {
  return Boolean(cell.allowOverlay) && !(cell as { readonly?: boolean }).readonly;
}

/** Whether a keydown on the currently-selected cell should open the row
 *  drawer, given the raw key and whether that SPECIFIC cell (isOverlayEditable
 *  above) is overlay-editable right now:
 *  - Space always opens the drawer, editable or not — the guaranteed keyboard
 *    route to row detail now that Enter is reserved for editing where
 *    possible.
 *  - Enter opens the drawer only when the cell is NOT overlay-editable —
 *    otherwise it's left alone so glide's own Enter-to-edit handling fires.
 *  - Any other key is not this feature's concern. */
export function shouldOpenDrawerOnKey(key: string, cellOverlayEditable: boolean): boolean {
  if (key === ' ') return true;
  if (key === 'Enter') return !cellOverlayEditable;
  return false;
}

/** One grid-cell builder, resolved by the type registry's
 *  presentation.renderer (NOT by switching on the column type). */
export type CellBuilder = (
  col: ColumnDef,
  v: CellValue,
  opts: BuildCellOptions,
  pres: TypePresentation,
) => GridCell;

const textBuilder: CellBuilder = (col, v, opts) => {
  // Markdown-formatted text (sniffed on import or set via v1 column.patch)
  // uses Glide's native Markdown cell: the canvas shows the source text,
  // and opening the cell (double-click / Enter) renders the markdown in
  // the overlay preview. Glide's Markdown cell does not support wrapping, so
  // the global wrap mode uses its read-only Text cell instead. The row drawer
  // continues to provide the rendered Markdown view in either mode.
  if (col.format === 'markdown') {
    if (opts.wrap) {
      const display = textDisplayValue(v);
      return {
        kind: GridCellKind.Text,
        data: v === null ? '' : String(v),
        displayData: display,
        allowOverlay: false,
        allowWrapping: true,
        readonly: true,
      };
    }
    return {
      kind: GridCellKind.Markdown,
      data: textDisplayValue(v),
      allowOverlay: true,
      readonly: true,
    };
  }
  const display = textDisplayValue(v, { decodeEscapes: Boolean(col.ai) });
  return {
    kind: GridCellKind.Text,
    data: v === null ? '' : String(v),
    displayData: display,
    allowOverlay: false,
    allowWrapping: opts.wrap,
  };
};

/** renderer name (presentation.renderer in the column-type registry) →
 *  builder. Unknown renderer names fall back to text until frontend plugin
 *  renderer modules are wired. */
const CELL_BUILDERS: Record<string, CellBuilder> = {
  text: textBuilder,

  number: (col, v) => {
    const n = coerceFiniteNumber(v);
    return {
      kind: GridCellKind.Number,
      data: n ?? undefined,
      displayData: n === null ? '' : formatNumberDisplay(n, col.format) ?? String(n),
      allowOverlay: false,
      contentAlign: 'right',
    };
  },

  integer: (col, v) => {
    const n = coerceFiniteNumber(v);
    const display = n === null ? '' : formatNumberDisplay(n, col.format) ?? n.toLocaleString();
    return {
      kind: GridCellKind.Number,
      data: n ?? undefined,
      displayData: display,
      allowOverlay: false,
      contentAlign: 'right',
    };
  },

  stars: (_col, v) => {
    const n = coerceFiniteNumber(v);
    return {
      kind: GridCellKind.Number,
      data: n ?? undefined,
      displayData: n === null ? '' : formatStarsDisplay(n),
      allowOverlay: false,
      contentAlign: 'right',
    };
  },

  boolean: (_col, v) => ({
    kind: GridCellKind.Boolean,
    data: typeof v === 'boolean' ? v : undefined,
    allowOverlay: false,
  }),

  category: (_col, v) => ({
    kind: GridCellKind.Bubble,
    data: v === null ? [] : [String(v)],
    allowOverlay: false,
  }),

  image: (_col, v, opts) => {
    const media = resolveMediaValue(v, opts.projectId ?? '');
    return {
      kind: GridCellKind.Image,
      data: media ? [media.url] : [],
      allowOverlay: true,
      rounding: 4,
    };
  },

  link: (_col, v) => ({
    kind: GridCellKind.Uri,
    data: v === null ? '' : String(v),
    allowOverlay: false,
    hoverEffect: true,
  }),

  'map-pin': (_col, v) => {
    const point = parseGeoPointValue(v);
    const label = point ? formatGeoPoint(point) : '';
    return {
      kind: GridCellKind.Custom,
      data: {
        kind: 'frisket-map-pin',
        point,
        label,
      },
      copyData: label,
      allowOverlay: false,
    } satisfies MapPinCell;
  },

  timeline: (col, v) => {
    const summary = isTemporalColumnType(col.type)
      ? summarizeTemporalCell(v, col.type)
      : { label: 'Invalid temporal value', copyText: String(v ?? ''), invalid: true };
    return {
      kind: GridCellKind.Custom,
      data: {
        kind: 'frisket-timeline',
        label: summary.label,
        invalid: summary.invalid,
      },
      copyData: summary.copyText,
      allowOverlay: false,
      readonly: true,
    } satisfies TimelineCell;
  },

  // video / audio / file share one builder; which badge to draw comes from
  // the type's presentation hints ({renderer: 'media', mediaType: ...}).
  media: (_col, v, opts, pres) => {
    const media = resolveMediaValue(v, opts.projectId ?? '');
    const mt = pres.mediaType;
    const mediaType: MediaKind =
      mt === 'video' || mt === 'audio' || mt === 'file' ? mt : 'file';
    return {
      kind: GridCellKind.Custom,
      data: {
        kind: 'frisket-media',
        mediaType,
        url: media?.url ?? null,
        label: media?.label ?? '',
      },
      copyData: media?.url ?? '',
      allowOverlay: false,
    } satisfies MediaCell;
  },

  json: (col, v, opts) => {
    // Arrays (e.g. web_search results) render as a compact count badge;
    // the row drawer holds the full mini-table.
    const arr = parseJsonArray(v);
    if (arr) {
      const mentions = col.type === 'json' && col.semanticType === ENTITY_MENTIONS_SEMANTIC_TYPE
        ? summarizeEntityMentions(arr)
        : null;
      if (mentions?.count && opts.expandEntityMentions) {
        return withEntityMentionToggle({
          kind: GridCellKind.Bubble,
          data: entityMentionPills(mentions),
          allowOverlay: false,
        }, true);
      }
      // Use the column's own name as the noun ("faces" → "1 face" / "3 faces",
      // "people" → "2 people" not "2 peoples") so derived/list columns read
      // naturally instead of "1 result".
      const count = mentions ? mentions.count : arr.length;
      const label = countLabel(count, col.name);
      return withEntityMentionToggle({
        kind: GridCellKind.Bubble,
        data: count === 0 ? [] : [label],
        allowOverlay: false,
      }, Boolean(mentions?.count));
    }
    return {
      kind: GridCellKind.Text,
      data: v === null ? '' : String(v),
      displayData: v === null ? '' : String(v),
      allowOverlay: false,
      allowWrapping: opts.wrap,
    };
  },
};

// Standalone cell builders (unit tests and non-grid previews) retain the
// previous appearance. SheetGrid always supplies the token-derived snapshot.
const DEFAULT_GRID_CELL_PALETTE: GridCellPaletteValue = {
  error: { textDark: '#b91c1c', textLight: '#b91c1c' },
  empty: { textDark: '#8a8375', textLight: '#8a8375' },
  pending: { background: '#ece4f9', highlight: '#7c5ce6' },
};
const EMPTY_CELL_MARKER = '—';
// Text-like renderers whose blank would otherwise be indistinguishable from
// text. Renderers with their own empty affordance (boolean checkbox, category
// bubble, image, media/map-pin/timeline custom cells, json count badge) are
// intentionally excluded.
const EMPTY_MARKER_RENDERERS = new Set(['text', 'number', 'integer', 'stars', 'link', 'date']);

export function buildCell(col: ColumnDef, row: Row | undefined, opts: BuildCellOptions = {}): GridCell {
  if (row === undefined) {
    return { kind: GridCellKind.Loading, allowOverlay: false };
  }
  const palette = opts.gridCellPalette ?? DEFAULT_GRID_CELL_PALETTE;
  // A deliberately withheld value (citation-required grounding, or a required
  // field the model returned null/missing for) is a POLICY HOLD, not a
  // failure: it carries an `error` reason but a terminal `withheld_unverified`
  // outcome. Surface it as its own subdued badge -- taking precedence over the
  // red error branch below so a hold never masquerades as a failure -- the same
  // muted-bubble family as the `incomplete` marker.
  if (row.cellOutcomes?.[col.id] === 'withheld_unverified') {
    return {
      kind: GridCellKind.Bubble,
      data: ['withheld'],
      allowOverlay: false,
    };
  }
  // Terminal per-cell ordering is pulse -> value | error: a landed error wins
  // over the pending pulse (the cell is done, just failed) and over the empty
  // value it would otherwise show as a dash.
  const errorText = row.cellErrors?.[col.id];
  if (errorText) {
    // Never overlay-editable, even on an editable column: the displayed text
    // is a synthetic "⚠ message" rendering of the failure, not the real
    // underlying value — typing into it would risk committing the error
    // message itself as the cell's value. Fixing an errored cell goes through
    // the row drawer (or a re-run), same as before this feature.
    const text = `⚠ ${errorText}`;
    return {
      kind: GridCellKind.Text,
      data: text,
      displayData: text,
      allowOverlay: false,
      allowWrapping: opts.wrap,
      themeOverride: palette.error,
    };
  }
  if (row.cellStates?.[col.id] === 'incomplete') {
    return {
      kind: GridCellKind.Bubble,
      data: ['incomplete'],
      allowOverlay: false,
    };
  }
  const v: CellValue = row.cells[col.id] ?? null;
  if (opts.pending && (v === null || v === '')) {
    return {
      kind: GridCellKind.Custom,
      data: { kind: 'frisket-pending', palette: palette.pending },
      copyData: '',
      allowOverlay: false,
    } satisfies PendingCell;
  }

  // Resolve the renderer from the column-type registry's presentation hints;
  // a type with no (or an unknown) renderer falls back to plain text.
  const pres = presentationFor(col.type);
  if ((v === null || v === '') && EMPTY_MARKER_RENDERERS.has(pres.renderer ?? col.type)) {
    return withEditableOverlay(
      {
        kind: GridCellKind.Text,
        data: '',
        displayData: EMPTY_CELL_MARKER,
        allowOverlay: false,
        themeOverride: palette.empty,
      },
      opts.editable,
    );
  }
  if ((pres.renderer ?? col.type) === 'image') {
    const media = resolveMediaValue(v, opts.projectId ?? '');
    const regions = media ? imageRegionsForRow(row, col) : [];
    if (regions.length > 0) {
      return {
        kind: GridCellKind.Custom,
        data: {
          kind: 'frisket-region-image',
          url: media?.url ?? null,
          label: media?.label ?? '',
          regions,
        },
        copyData: media?.url ?? '',
        allowOverlay: false,
      } satisfies RegionImageCell;
    }
  }
  // A research answer produced from the model's parametric memory with no
  // source consulted is kept but marked `unverified_memory` (engine
  // store/runs.py). Where its empty sources array would otherwise render as a
  // blank cell, show a subdued "no sources" badge so the absence of grounding
  // is visible instead of indistinguishable from "not yet run".
  if (row.cellOutcomes?.[col.id] === 'unverified_memory') {
    const arr = parseJsonArray(v);
    if (arr && arr.length === 0) {
      return {
        kind: GridCellKind.Bubble,
        data: ['no sources'],
        allowOverlay: false,
      };
    }
  }
  const build = CELL_BUILDERS[pres.renderer ?? 'text'] ?? textBuilder;
  return withEditableOverlay(build(col, v, opts, pres), opts.editable);
}
