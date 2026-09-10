// The bundled geo plugin's map view.
//
// This is GLUE, not a bundled map stack: React arrives by injection
// (TrustedLocalPluginComponent), deck.gl arrives host-injected as
// `ctx.libs.deckgl` (plugin-host-library-injection-v1 — the 256KB
// trusted-module cap is why deck is never bundled here), point data arrives
// as raw Arrow IPC bytes through `ctx.projection.fetchData`
// (projection.data.read), "filter to this area" goes through
// `ctx.gridFilter.applyBbox`, and click-to-row goes through
// `ctx.navigation.openRow`. The DOM mirrors the first-party MapView it
// replaces — same data-testids and classNames — so the deckgl Playwright
// suite remains the parity oracle.
//
// The Arrow decoder below is a minimal reader for exactly the
// frisket.map_points.arrow.v1 stream (row_id int64, lon/lat float32,
// attr:<id> float64|utf8, uncompressed pyarrow IPC stream): plugin modules
// cannot bundle apache-arrow (bare npm specifiers are rejected at build and
// the cap forbids the bytes), and the wire format is versioned and fixed.

type ReactRuntime = typeof import('react');

// ---------------------------------------------------------------------------
// Minimal types for the injected context (structural; the host owns truth).

interface ColumnDef {
  id: string | number;
  name: string;
  type: string;
}

interface DeckglNamespace {
  Deck: new (props: Record<string, unknown>) => {
    setProps(props: Record<string, unknown>): void;
    finalize(): void;
  };
  WebMercatorViewport: new (props?: Record<string, unknown>) => {
    unproject(xy: [number, number]): [number, number];
  };
  ScatterplotLayer: new (props: Record<string, unknown>) => unknown;
  BitmapLayer: new (...args: unknown[]) => unknown;
  TileLayer: new (props: Record<string, unknown>) => unknown;
  HeatmapLayer: new (props: Record<string, unknown>) => unknown;
}

interface GridFilterSpecLike {
  [column: string]: unknown;
}

interface MapPluginCtx {
  schemaVersion: string;
  contributionId: string;
  sheet: { id: string; name: string; rowCount: number; columns: ColumnDef[] };
  projection: {
    kind: string;
    target: Record<string, unknown>;
    params: Record<string, unknown>;
    status(): Promise<unknown>;
    fetchData?(params: {
      columnId: string;
      bbox?: [number, number, number, number];
      attrs?: string[];
    }): Promise<ArrayBuffer>;
  };
  navigation: {
    openRow(rowId: string): void;
    closeView?(): void;
  };
  gridState?: {
    filter: GridFilterSpecLike | null;
    sort: unknown;
  };
  gridFilter?: {
    applyBbox(columnId: string, bbox: [number, number, number, number]): void;
  };
  libs?: {
    deckgl: Promise<DeckglNamespace>;
  };
}

// ---------------------------------------------------------------------------
// Arrow IPC (stream format) decoder for frisket.map_points.arrow.v1.

interface DecodedMapPoints {
  count: number;
  positions: Float32Array;
  rowIds: string[];
  attributes: Record<string, Array<number | string | null>>;
  transient: boolean;
  generation: string;
}

function decodeError(detail: string): Error {
  return new Error(`invalid Arrow map-points payload: ${detail}`);
}

/** Flatbuffers table reader (read-only, little-endian). */
class FbTable {
  constructor(
    readonly dv: DataView,
    readonly pos: number,
  ) {}

  private fieldOffset(index: number): number {
    const vtablePos = this.pos - this.dv.getInt32(this.pos, true);
    const vtableSize = this.dv.getUint16(vtablePos, true);
    const entry = 4 + index * 2;
    if (entry >= vtableSize) return 0;
    const rel = this.dv.getUint16(vtablePos + entry, true);
    return rel === 0 ? 0 : this.pos + rel;
  }

  int16(index: number, fallback: number): number {
    const off = this.fieldOffset(index);
    return off === 0 ? fallback : this.dv.getInt16(off, true);
  }

  int32(index: number, fallback: number): number {
    const off = this.fieldOffset(index);
    return off === 0 ? fallback : this.dv.getInt32(off, true);
  }

  int64(index: number, fallback: bigint): bigint {
    const off = this.fieldOffset(index);
    return off === 0 ? fallback : this.dv.getBigInt64(off, true);
  }

  uint8(index: number, fallback: number): number {
    const off = this.fieldOffset(index);
    return off === 0 ? fallback : this.dv.getUint8(off);
  }

  bool(index: number, fallback: boolean): boolean {
    const off = this.fieldOffset(index);
    return off === 0 ? fallback : this.dv.getUint8(off) !== 0;
  }

  table(index: number): FbTable | null {
    const off = this.fieldOffset(index);
    if (off === 0) return null;
    return new FbTable(this.dv, off + this.dv.getInt32(off, true));
  }

  string(index: number): string | null {
    const off = this.fieldOffset(index);
    if (off === 0) return null;
    const strPos = off + this.dv.getInt32(off, true);
    const length = this.dv.getInt32(strPos, true);
    const bytes = new Uint8Array(this.dv.buffer, this.dv.byteOffset + strPos + 4, length);
    return new TextDecoder('utf-8').decode(bytes);
  }

  /** Vector of table offsets. */
  tableVector(index: number): FbTable[] {
    const off = this.fieldOffset(index);
    if (off === 0) return [];
    const vecPos = off + this.dv.getInt32(off, true);
    const length = this.dv.getInt32(vecPos, true);
    const out: FbTable[] = [];
    for (let i = 0; i < length; i += 1) {
      const elemOff = vecPos + 4 + i * 4;
      out.push(new FbTable(this.dv, elemOff + this.dv.getInt32(elemOff, true)));
    }
    return out;
  }

  /** Vector of fixed-size structs; returns (structPos, count). */
  structVector(index: number): { pos: number; length: number } | null {
    const off = this.fieldOffset(index);
    if (off === 0) return null;
    const vecPos = off + this.dv.getInt32(off, true);
    return { pos: vecPos + 4, length: this.dv.getInt32(vecPos, true) };
  }
}

type WireFieldType =
  | { kind: 'int64' }
  | { kind: 'float'; precision: 'single' | 'double' }
  | { kind: 'utf8' };

interface WireField {
  name: string;
  type: WireFieldType;
}

function parseSchemaField(field: FbTable): WireField {
  const name = field.string(0) ?? '';
  const typeType = field.uint8(2, 0);
  // Type union discriminants (Arrow Schema.fbs): Int=2, FloatingPoint=3, Utf8=5.
  if (typeType === 2) {
    const intType = field.table(3);
    const bitWidth = intType ? intType.int32(0, 0) : 0;
    if (bitWidth !== 64) throw decodeError(`unsupported int bit width ${bitWidth}`);
    return { name, type: { kind: 'int64' } };
  }
  if (typeType === 3) {
    const floatType = field.table(3);
    const precision = floatType ? floatType.int16(0, 0) : 0;
    if (precision === 1) return { name, type: { kind: 'float', precision: 'single' } };
    if (precision === 2) return { name, type: { kind: 'float', precision: 'double' } };
    throw decodeError('unsupported float precision');
  }
  if (typeType === 5) {
    return { name, type: { kind: 'utf8' } };
  }
  throw decodeError(`unsupported column type discriminant ${typeType}`);
}

function readValidity(
  dv: DataView,
  bodyStart: number,
  buffer: { offset: number; length: number },
  count: number,
): ((i: number) => boolean) | null {
  if (buffer.length === 0) return null;
  const base = bodyStart + buffer.offset;
  return (i: number) => {
    if (i >= count) return false;
    const byte = dv.getUint8(base + (i >> 3));
    return (byte & (1 << (i % 8))) !== 0;
  };
}

export function decodeMapPointsArrow(buf: ArrayBuffer): DecodedMapPoints {
  const dv = new DataView(buf);
  let offset = 0;
  let fields: WireField[] | null = null;
  const metadata = new Map<string, string>();
  const batches: Array<{ recordBatch: FbTable; bodyStart: number }> = [];

  const readMessage = () => {
    if (offset + 8 > dv.byteLength) throw decodeError('truncated stream');
    const continuation = dv.getUint32(offset, true);
    if (continuation !== 0xffffffff) throw decodeError('missing continuation marker');
    const metaLength = dv.getInt32(offset + 4, true);
    if (metaLength === 0) {
      offset += 8;
      return null; // end-of-stream marker
    }
    const metaStart = offset + 8;
    if (metaStart + metaLength > dv.byteLength) throw decodeError('truncated metadata');
    const root = new FbTable(dv, metaStart + dv.getInt32(metaStart, true));
    // Message fields: 0 version, 1 header_type, 2 header, 3 bodyLength.
    const headerType = root.uint8(1, 0);
    const header = root.table(2);
    const bodyLength = Number(root.int64(3, 0n));
    const bodyStart = metaStart + metaLength;
    if (bodyStart + bodyLength > dv.byteLength) throw decodeError('truncated body');
    offset = bodyStart + bodyLength;
    return { headerType, header, bodyStart };
  };

  // Schema message first.
  const first = readMessage();
  if (!first || first.headerType !== 1 || !first.header) {
    throw decodeError('expected schema message');
  }
  fields = first.header.tableVector(1).map(parseSchemaField);
  for (const kv of first.header.tableVector(2)) {
    const key = kv.string(0);
    const value = kv.string(1);
    if (key !== null && value !== null) metadata.set(key, value);
  }
  if (metadata.get('frisket_schema') !== 'frisket.map_points.arrow.v1') {
    throw decodeError('unexpected wire schema');
  }

  while (offset < dv.byteLength) {
    const message = readMessage();
    if (message === null) break;
    if (message.headerType === 3 && message.header) {
      // RecordBatch — reject compressed batches (field 3): the wire format
      // is written uncompressed by pyarrow's default stream writer.
      if (message.header.table(3) !== null) throw decodeError('compressed batch');
      batches.push({ recordBatch: message.header, bodyStart: message.bodyStart });
    } else if (message.headerType === 2) {
      throw decodeError('unexpected dictionary batch');
    }
  }

  const byName = new Map<string, Array<number | string | null | bigint>>();
  for (const field of fields) byName.set(field.name, []);
  let total = 0;

  for (const { recordBatch, bodyStart } of batches) {
    const rowCount = Number(recordBatch.int64(0, 0n));
    const buffers = recordBatch.structVector(2);
    if (!buffers) throw decodeError('record batch has no buffers');
    const readBuffer = (index: number) => {
      if (index >= buffers.length) throw decodeError('buffer index out of range');
      const pos = buffers.pos + index * 16;
      return {
        offset: Number(dv.getBigInt64(pos, true)),
        length: Number(dv.getBigInt64(pos + 8, true)),
      };
    };
    let bufferIndex = 0;
    for (const field of fields) {
      const validity = readValidity(dv, bodyStart, readBuffer(bufferIndex), rowCount);
      bufferIndex += 1;
      const column = byName.get(field.name)!;
      if (field.type.kind === 'utf8') {
        const offsets = readBuffer(bufferIndex);
        bufferIndex += 1;
        const data = readBuffer(bufferIndex);
        bufferIndex += 1;
        const offsetsBase = bodyStart + offsets.offset;
        const dataBase = bodyStart + data.offset;
        const decoder = new TextDecoder('utf-8');
        for (let i = 0; i < rowCount; i += 1) {
          if (validity && !validity(i)) {
            column.push(null);
            continue;
          }
          const start = dv.getInt32(offsetsBase + i * 4, true);
          const end = dv.getInt32(offsetsBase + (i + 1) * 4, true);
          column.push(
            decoder.decode(
              new Uint8Array(dv.buffer, dv.byteOffset + dataBase + start, end - start),
            ),
          );
        }
      } else {
        const data = readBuffer(bufferIndex);
        bufferIndex += 1;
        const dataBase = bodyStart + data.offset;
        for (let i = 0; i < rowCount; i += 1) {
          if (validity && !validity(i)) {
            column.push(null);
            continue;
          }
          if (field.type.kind === 'int64') {
            column.push(dv.getBigInt64(dataBase + i * 8, true));
          } else if (field.type.precision === 'single') {
            column.push(dv.getFloat32(dataBase + i * 4, true));
          } else {
            column.push(dv.getFloat64(dataBase + i * 8, true));
          }
        }
      }
    }
    total += rowCount;
  }

  const rowIdColumn = byName.get('row_id');
  const lonColumn = byName.get('lon');
  const latColumn = byName.get('lat');
  if (!rowIdColumn || !lonColumn || !latColumn) {
    throw decodeError('missing columns');
  }
  const positions = new Float32Array(total * 2);
  const rowIds: string[] = [];
  for (let i = 0; i < total; i += 1) {
    const rowId = rowIdColumn[i];
    const lon = lonColumn[i];
    const lat = latColumn[i];
    if (rowId == null || lon == null || lat == null) {
      throw decodeError('null values');
    }
    positions[i * 2] = Number(lon);
    positions[i * 2 + 1] = Number(lat);
    rowIds.push(rowId.toString());
  }
  const attributes: Record<string, Array<number | string | null>> = {};
  for (const field of fields) {
    if (!field.name.startsWith('attr:')) continue;
    const raw = byName.get(field.name) ?? [];
    attributes[field.name.slice('attr:'.length)] = raw.map((value) =>
      value == null ? null : typeof value === 'bigint' ? Number(value) : value,
    );
  }
  return {
    count: total,
    positions,
    rowIds,
    attributes,
    transient: metadata.get('frisket_transient') === '1',
    generation: metadata.get('frisket_generation') ?? '',
  };
}

// ---------------------------------------------------------------------------
// Map style (port of web/src/components/map/mapStyle.ts — the host CSS keys
// off the same classNames).

type RgbColor = [number, number, number];

type MapLegendModel =
  | { kind: 'numeric'; name: string; min: number; max: number }
  | { kind: 'category'; name: string; items: Array<{ label: string; color: RgbColor }> };

interface MapPointStyle {
  getFillColor: (_d: unknown, info: { index: number }) => RgbColor;
  getRadius: (_d: unknown, info: { index: number }) => number;
  legend: MapLegendModel | null;
  trigger: string;
}

const NUMERIC_LOW: RgbColor = [222, 235, 247];
const NUMERIC_HIGH: RgbColor = [8, 81, 156];
const CATEGORY_PALETTE: RgbColor[] = [
  [31, 119, 180],
  [255, 127, 14],
  [44, 160, 44],
  [214, 39, 40],
  [148, 103, 189],
  [140, 86, 75],
  [227, 119, 194],
  [127, 127, 127],
  [188, 189, 34],
  [23, 190, 207],
];
const GEO_FILL: RgbColor = [29, 118, 110];
const RADIUS_MIN = 5;
const RADIUS_MAX = 18;

function isNumericColumn(type: string): boolean {
  return type === 'number' || type === 'integer';
}

function lerp(a: number, b: number, t: number): number {
  return Math.round(a + (b - a) * t);
}

function buildPointStyle({
  result,
  columns,
  colorBy,
  sizeBy,
}: {
  result: DecodedMapPoints | null;
  columns: ColumnDef[];
  colorBy: string;
  sizeBy: string;
}): MapPointStyle {
  const attrs = result?.attributes ?? {};
  const colorVals = colorBy ? attrs[colorBy] : undefined;
  const sizeVals = sizeBy ? attrs[sizeBy] : undefined;
  const colorCol = columns.find((c) => String(c.id) === colorBy);
  const colorNumeric = colorCol ? isNumericColumn(colorCol.type) : false;

  const catColor = new Map<string, RgbColor>();
  let colorMin = Infinity;
  let colorMax = -Infinity;
  if (colorVals && colorNumeric) {
    for (const v of colorVals) {
      if (typeof v === 'number') {
        if (v < colorMin) colorMin = v;
        if (v > colorMax) colorMax = v;
      }
    }
  } else if (colorVals) {
    let next = 0;
    for (const v of colorVals) {
      if (v == null) continue;
      const k = String(v);
      if (!catColor.has(k)) {
        catColor.set(k, CATEGORY_PALETTE[next % CATEGORY_PALETTE.length]);
        next += 1;
      }
    }
  }

  let sizeMin = Infinity;
  let sizeMax = -Infinity;
  if (sizeVals) {
    for (const v of sizeVals) {
      if (typeof v === 'number') {
        if (v < sizeMin) sizeMin = v;
        if (v > sizeMax) sizeMax = v;
      }
    }
  }

  const getFillColor = (_d: unknown, info: { index: number }): RgbColor => {
    if (!colorVals) return GEO_FILL;
    const v = colorVals[info.index];
    if (v == null) return [160, 160, 160];
    if (colorNumeric && typeof v === 'number') {
      const t = colorMax > colorMin ? (v - colorMin) / (colorMax - colorMin) : 0.5;
      return [
        lerp(NUMERIC_LOW[0], NUMERIC_HIGH[0], t),
        lerp(NUMERIC_LOW[1], NUMERIC_HIGH[1], t),
        lerp(NUMERIC_LOW[2], NUMERIC_HIGH[2], t),
      ];
    }
    return catColor.get(String(v)) ?? GEO_FILL;
  };

  const getRadius = (_d: unknown, info: { index: number }): number => {
    if (!sizeVals) return 9;
    const v = sizeVals[info.index];
    if (typeof v !== 'number' || sizeMax <= sizeMin) return 9;
    const t = (v - sizeMin) / (sizeMax - sizeMin);
    return RADIUS_MIN + t * (RADIUS_MAX - RADIUS_MIN);
  };

  const colorName = colorCol?.name ?? colorBy;
  let legend: MapLegendModel | null = null;
  if (colorVals && colorNumeric && colorMax >= colorMin && colorMin !== Infinity) {
    legend = { kind: 'numeric', name: colorName, min: colorMin, max: colorMax };
  } else if (colorVals && catColor.size > 0) {
    legend = {
      kind: 'category',
      name: colorName,
      items: Array.from(catColor.entries())
        .slice(0, 10)
        .map(([label, color]) => ({ label, color })),
    };
  }

  return {
    getFillColor,
    getRadius,
    legend,
    trigger: `${colorBy}|${sizeBy}|${result?.generation ?? ''}`,
  };
}

// --- option/tooltip helpers (ports of mapOptions.ts / tooltipModel.ts) ---

function mapAttributeIds(colorBy: string, sizeBy: string): string[] {
  const attrIds: string[] = [];
  if (colorBy) attrIds.push(colorBy);
  if (sizeBy && sizeBy !== colorBy) attrIds.push(sizeBy);
  return attrIds;
}

function mapColumnName(columns: ColumnDef[], columnId: string): string {
  return columns.find((c) => String(c.id) === columnId)?.name ?? columnId;
}

function colorByColumns(columns: ColumnDef[], mapColumnId: string): ColumnDef[] {
  return columns.filter(
    (column) => String(column.id) !== mapColumnId && column.type !== 'geo_point',
  );
}

function sizeByColumns(columns: ColumnDef[], mapColumnId: string): ColumnDef[] {
  return columns.filter(
    (column) => String(column.id) !== mapColumnId && isNumericColumn(column.type),
  );
}

interface MapHover {
  index: number;
  x: number;
  y: number;
}

interface MapTooltipModel {
  lat: number;
  lon: number;
  lines: Array<{ label: string; value: string }>;
  x: number;
  y: number;
}

function buildMapTooltip({
  hover,
  result,
  columns,
  colorBy,
  sizeBy,
}: {
  hover: MapHover | null;
  result: DecodedMapPoints | null;
  columns: ColumnDef[];
  colorBy: string;
  sizeBy: string;
}): MapTooltipModel | null {
  if (!hover || !result || hover.index >= result.count) return null;
  const lon = result.positions[hover.index * 2];
  const lat = result.positions[hover.index * 2 + 1];
  const lines: Array<{ label: string; value: string }> = [];
  for (const colId of mapAttributeIds(colorBy, sizeBy)) {
    const vals = result.attributes[colId];
    if (!vals) continue;
    const v = vals[hover.index];
    lines.push({
      label: mapColumnName(columns, colId),
      value: v == null ? '—' : String(v),
    });
  }
  return { lat, lon, lines, x: hover.x, y: hover.y };
}

// ---------------------------------------------------------------------------
// Basemap config: the host injects window.__FRISKET_MAP_CONFIG__ (both the
// private hosted layer and the open team server,
// frisket.team.app.create_team_app via
// frisket.server.static_serving.mount_spa_static); default is OSM.

const DEFAULT_BASEMAP_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const DEFAULT_BASEMAP_ATTRIBUTION = '© OpenStreetMap contributors';

function resolveBasemapConfig(): { tileUrl: string; attribution: string } {
  const runtime =
    typeof window !== 'undefined'
      ? ((
          window as unknown as {
            __FRISKET_MAP_CONFIG__?: {
              tileUrlTemplate?: string;
              apiKey?: string;
              attribution?: string;
            };
          }
        ).__FRISKET_MAP_CONFIG__ ?? {})
      : {};
  const apiKey = runtime.apiKey?.trim() ?? '';
  const tileUrl = (runtime.tileUrlTemplate?.trim() || DEFAULT_BASEMAP_TILE_URL)
    .split('{key}')
    .join(apiKey);
  return {
    tileUrl,
    attribution: runtime.attribution?.trim() || DEFAULT_BASEMAP_ATTRIBUTION,
  };
}

// ---------------------------------------------------------------------------
// View state helpers (ports of useMapController.ts).

type Bbox = [number, number, number, number];

interface ViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

function viewStateForPoints(positions: Float32Array, count: number): ViewState {
  if (count === 0) {
    return { longitude: 0, latitude: 20, zoom: 1, pitch: 0, bearing: 0 };
  }
  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;
  for (let i = 0; i < count; i += 1) {
    const lon = positions[i * 2];
    const lat = positions[i * 2 + 1];
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }
  const longitude = (minLon + maxLon) / 2;
  const latitude = (minLat + maxLat) / 2;
  const span = Math.max(maxLon - minLon, maxLat - minLat);
  const zoom = span < 1e-6 ? 6 : Math.min(12, Math.max(1, Math.log2(360 / span) - 1));
  return { longitude, latitude, zoom, pitch: 0, bearing: 0 };
}

function isViewportUserInteraction(interactionState: unknown): boolean {
  if (typeof interactionState !== 'object' || interactionState === null) return false;
  const state = interactionState as Record<string, unknown>;
  return Boolean(
    state.isDragging || state.isPanning || state.isRotating || state.isZooming,
  );
}

function viewportBboxFromElement(
  deckgl: DeckglNamespace,
  el: HTMLElement | null,
  viewState: ViewState,
): Bbox | null {
  if (!el || !el.clientWidth || !el.clientHeight) return null;
  try {
    const vp = new deckgl.WebMercatorViewport({
      width: el.clientWidth,
      height: el.clientHeight,
      longitude: viewState.longitude,
      latitude: viewState.latitude,
      zoom: viewState.zoom,
    });
    const [minLon, maxLat] = vp.unproject([0, 0]);
    const [maxLon, minLat] = vp.unproject([el.clientWidth, el.clientHeight]);
    if (maxLon <= minLon || maxLon - minLon >= 360 || maxLat <= minLat) return null;
    return [minLon, minLat, maxLon, maxLat];
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Chrome (ports of MapToolbar/MapLegend/MapTooltip — same testids/classNames).

type MapViewMode = 'points' | 'heatmap';

function MapToolbar({
  React,
  columnId,
  columnName,
  columns,
  loading,
  pointCount,
  viewMode,
  colorBy,
  sizeBy,
  transient,
  onViewModeChange,
  onColorByChange,
  onSizeByChange,
  onFilterToViewport,
}: {
  React: ReactRuntime;
  columnId: string;
  columnName: string;
  columns: ColumnDef[];
  loading: boolean;
  pointCount: number | null;
  viewMode: MapViewMode;
  colorBy: string;
  sizeBy: string;
  transient: boolean;
  onViewModeChange(mode: MapViewMode): void;
  onColorByChange(columnId: string): void;
  onSizeByChange(columnId: string): void;
  onFilterToViewport?: () => void;
}) {
  // The redundant "← Back to grid" affordance was removed (splitfix): the host
  // split header's × and the work-view switcher's Grid segment are the close
  // paths. The map pane no longer owns a close button.
  return (
    <div className="map-view-toolbar">
      <span className="map-view-title">{columnName}</span>
      <span className="map-view-stats" data-testid="map-points-count">
        {pointCount !== null ? `${pointCount} points` : loading ? 'Loading…' : ''}
      </span>
      <label className="map-view-control">
        View
        <select
          className="row-height-select"
          data-testid="map-view-mode"
          value={viewMode}
          onChange={(e: { target: { value: string } }) =>
            onViewModeChange(e.target.value as MapViewMode)
          }
        >
          <option value="points">Points</option>
          <option value="heatmap">Heatmap</option>
        </select>
      </label>
      <label className="map-view-control">
        Color by
        <select
          className="row-height-select"
          data-testid="map-color-by"
          value={colorBy}
          onChange={(e: { target: { value: string } }) => onColorByChange(e.target.value)}
        >
          <option value="">none</option>
          {colorByColumns(columns, columnId).map((c) => (
            <option key={String(c.id)} value={String(c.id)}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <label className="map-view-control">
        Size by
        <select
          className="row-height-select"
          data-testid="map-size-by"
          value={sizeBy}
          onChange={(e: { target: { value: string } }) => onSizeByChange(e.target.value)}
        >
          <option value="">none</option>
          {sizeByColumns(columns, columnId).map((c) => (
            <option key={String(c.id)} value={String(c.id)}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      {onFilterToViewport && (
        <button
          type="button"
          className="mini-btn"
          data-testid="map-filter-viewport"
          onClick={onFilterToViewport}
        >
          Filter to this area
        </button>
      )}
      {transient && (
        <span className="map-view-transient" data-testid="map-transient">
          updating…
        </span>
      )}
    </div>
  );
}

function MapLegend({ React, legend }: { React: ReactRuntime; legend: MapLegendModel | null }) {
  if (!legend) return null;
  return (
    <div className="map-legend" data-testid="map-legend">
      <div className="map-legend-title">{legend.name}</div>
      {legend.kind === 'numeric' ? (
        <div className="map-legend-numeric">
          <span
            className="map-legend-ramp"
            style={{
              background: `linear-gradient(to right, rgb(${NUMERIC_LOW.join(',')}), rgb(${NUMERIC_HIGH.join(',')}))`,
            }}
          />
          <span className="map-legend-range">
            {legend.min} – {legend.max}
          </span>
        </div>
      ) : (
        legend.items.map((it) => (
          <div className="map-legend-item" key={it.label}>
            <span
              className="map-legend-swatch"
              style={{ background: `rgb(${it.color.join(',')})` }}
            />
            {it.label}
          </div>
        ))
      )}
    </div>
  );
}

function MapTooltip({
  React,
  tooltip,
}: {
  React: ReactRuntime;
  tooltip: MapTooltipModel | null;
}) {
  if (!tooltip) return null;
  return (
    <div
      className="map-tooltip"
      data-testid="map-tooltip"
      style={{ left: tooltip.x + 12, top: tooltip.y + 12 }}
    >
      <div className="map-tooltip-coords">
        {tooltip.lat.toFixed(5)}, {tooltip.lon.toFixed(5)}
      </div>
      {tooltip.lines.map((l) => (
        <div key={l.label}>
          <span className="map-tooltip-label">{l.label}:</span> {l.value}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The map view component.

interface MapControllerState {
  result: DecodedMapPoints | null;
  error: string | null;
  loading: boolean;
  colorBy: string;
  sizeBy: string;
  hover: MapHover | null;
  viewMode: MapViewMode;
  viewState: ViewState;
  bbox: Bbox | null;
}

type MapControllerAction =
  | { type: 'set_color_by'; columnId: string }
  | { type: 'set_size_by'; columnId: string }
  | { type: 'set_hover'; hover: MapHover | null }
  | { type: 'set_view_mode'; viewMode: MapViewMode }
  | { type: 'set_view_state'; viewState: ViewState }
  | { type: 'set_bbox'; bbox: Bbox | null }
  | { type: 'fetch_success'; result: DecodedMapPoints; fittedViewState: ViewState | null }
  | { type: 'fetch_error'; message: string };

function mapControllerReducer(
  state: MapControllerState,
  action: MapControllerAction,
): MapControllerState {
  switch (action.type) {
    case 'set_color_by':
      return { ...state, colorBy: action.columnId };
    case 'set_size_by':
      return { ...state, sizeBy: action.columnId };
    case 'set_hover':
      return { ...state, hover: action.hover };
    case 'set_view_mode':
      return {
        ...state,
        viewMode: action.viewMode,
        hover: action.viewMode === 'points' ? state.hover : null,
      };
    case 'set_view_state':
      return { ...state, viewState: action.viewState };
    case 'set_bbox':
      return { ...state, bbox: action.bbox };
    case 'fetch_success':
      return {
        ...state,
        result: action.result,
        error: null,
        loading: false,
        viewState: action.fittedViewState ?? state.viewState,
      };
    case 'fetch_error':
      return { ...state, error: action.message, loading: false };
    default:
      return state;
  }
}

const initialMapControllerState: MapControllerState = {
  result: null,
  error: null,
  loading: true,
  colorBy: '',
  sizeBy: '',
  hover: null,
  viewMode: 'points',
  viewState: viewStateForPoints(new Float32Array(0), 0),
  bbox: null,
};

export function MapView({ React, ctx }: { React: ReactRuntime; ctx: MapPluginCtx | null }) {
  const { useCallback, useEffect, useMemo, useReducer, useRef, useState } = React;

  const [deckgl, setDeckgl] = useState<DeckglNamespace | null>(null);
  const [deckglError, setDeckglError] = useState<string | null>(null);
  const [state, dispatch] = useReducer(mapControllerReducer, initialMapControllerState);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const deckRef = useRef<InstanceType<DeckglNamespace['Deck']> | null>(null);
  const bboxDebounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fittedGenerationRef = useRef<string | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;

  // --- column scoping: the host route's targetColumnId, else first geo column
  const columns = ctx?.sheet?.columns ?? [];
  const routedColumnId =
    typeof ctx?.projection?.params?.targetColumnId === 'string' ||
    typeof ctx?.projection?.params?.targetColumnId === 'number'
      ? String(ctx.projection.params.targetColumnId)
      : null;
  const routedColumn = routedColumnId
    ? columns.find((c) => String(c.id) === routedColumnId && c.type === 'geo_point') ?? null
    : null;
  const geoColumn = routedColumn ?? columns.find((c) => c.type === 'geo_point') ?? null;
  const columnId = geoColumn ? String(geoColumn.id) : null;
  const columnName = geoColumn?.name ?? '';

  const attrIds = useMemo(
    () => mapAttributeIds(state.colorBy, state.sizeBy),
    [state.colorBy, state.sizeBy],
  );
  const attrsKey = attrIds.join(',');
  const gridStateKey = ctx?.gridState
    ? JSON.stringify([ctx.gridState.filter, ctx.gridState.sort])
    : '';

  // --- host-injected deck.gl namespace
  useEffect(() => {
    let active = true;
    if (!ctx?.libs?.deckgl) {
      setDeckglError('host did not provide the deck.gl namespace');
      return () => {
        active = false;
      };
    }
    ctx.libs.deckgl
      .then((namespace) => {
        if (active) setDeckgl(namespace);
      })
      .catch((err: unknown) => {
        if (active) {
          setDeckglError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      active = false;
    };
    // The promise is host-memoized; re-running on ctx identity is harmless.
  }, [ctx?.libs?.deckgl]);

  // --- Arrow point data through ctx.projection.fetchData
  useEffect(() => {
    if (!ctx || !columnId) return undefined;
    if (typeof ctx.projection?.fetchData !== 'function') {
      dispatch({
        type: 'fetch_error',
        message: 'projection.data.read is unavailable',
      });
      return undefined;
    }
    let cancelled = false;
    ctx.projection
      .fetchData({
        columnId,
        ...(state.bbox ? { bbox: state.bbox } : {}),
        ...(attrIds.length > 0 ? { attrs: attrIds } : {}),
      })
      .then((buf) => {
        if (cancelled) return;
        const decoded = decodeMapPointsArrow(buf);
        const fittedViewState =
          fittedGenerationRef.current !== decoded.generation
            ? viewStateForPoints(decoded.positions, decoded.count)
            : null;
        fittedGenerationRef.current = decoded.generation;
        dispatch({ type: 'fetch_success', result: decoded, fittedViewState });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        dispatch({
          type: 'fetch_error',
          message: err instanceof Error ? err.message : String(err),
        });
      });
    return () => {
      cancelled = true;
    };
    // gridStateKey: the host applies the live grid filter/sort inside
    // fetchData; the key just retriggers the fetch when they change.
  }, [columnId, state.bbox, attrsKey, gridStateKey]);

  const scheduleBboxRefetch = useCallback(
    (viewState: ViewState) => {
      if (!deckgl) return;
      if (bboxDebounce.current) clearTimeout(bboxDebounce.current);
      bboxDebounce.current = setTimeout(() => {
        dispatch({
          type: 'set_bbox',
          bbox: viewportBboxFromElement(deckgl, containerRef.current, viewState),
        });
      }, 250);
    },
    [deckgl],
  );

  useEffect(
    () => () => {
      if (bboxDebounce.current) clearTimeout(bboxDebounce.current);
    },
    [],
  );

  const onViewStateChange = useCallback(
    (params: { viewState: unknown; interactionState?: unknown }) => {
      const viewState = params.viewState as ViewState;
      dispatch({ type: 'set_view_state', viewState });
      if (isViewportUserInteraction(params.interactionState)) {
        scheduleBboxRefetch(viewState);
      }
    },
    [scheduleBboxRefetch],
  );

  const pointStyle = useMemo(
    () =>
      buildPointStyle({
        result: state.result,
        colorBy: state.colorBy,
        sizeBy: state.sizeBy,
        columns,
      }),
    [state.result, state.colorBy, state.sizeBy, columns],
  );

  const { tileUrl: basemapTileUrl, attribution: basemapAttribution } =
    resolveBasemapConfig();

  const layers = useMemo(() => {
    if (!deckgl) return [];
    const basemapLayer = new deckgl.TileLayer({
      id: 'map-basemap',
      data: basemapTileUrl,
      minZoom: 0,
      maxZoom: 19,
      tileSize: 256,
      renderSubLayers: (props: {
        tile: { boundingBox: [[number, number], [number, number]] };
        data: unknown;
      }) => {
        const { boundingBox } = props.tile;
        return new deckgl.BitmapLayer(props, {
          data: undefined,
          image: props.data,
          bounds: [
            boundingBox[0][0],
            boundingBox[0][1],
            boundingBox[1][0],
            boundingBox[1][1],
          ],
        });
      },
    });
    if (!state.result || state.result.count === 0) return [basemapLayer];
    if (state.viewMode === 'heatmap') {
      const heatmapLayer = new deckgl.HeatmapLayer({
        id: 'map-heatmap',
        data: {
          length: state.result.count,
          attributes: { getPosition: { value: state.result.positions, size: 2 } },
        },
        getWeight: 1,
        aggregation: 'SUM',
        radiusPixels: 40,
        intensity: 1,
        threshold: 0.05,
      });
      return [basemapLayer, heatmapLayer];
    }
    const result = state.result;
    const pointLayer = new deckgl.ScatterplotLayer({
      id: 'map-points',
      data: {
        length: result.count,
        attributes: {
          getPosition: { value: result.positions, size: 2 },
        },
      },
      getFillColor: pointStyle.getFillColor,
      getRadius: pointStyle.getRadius,
      radiusUnits: 'pixels',
      radiusMinPixels: 4,
      stroked: true,
      getLineColor: [255, 255, 255],
      lineWidthUnits: 'pixels',
      getLineWidth: 1.5,
      pickable: true,
      updateTriggers: {
        getFillColor: pointStyle.trigger,
        getRadius: pointStyle.trigger,
      },
      onClick: (info: { index: number }) => {
        if (info.index >= 0 && info.index < result.rowIds.length) {
          ctxRef.current?.navigation.openRow(result.rowIds[info.index]);
          return true;
        }
        return false;
      },
      onHover: (info: { index: number; x: number; y: number; picked: boolean }) => {
        if (info.picked && info.index >= 0) {
          dispatch({
            type: 'set_hover',
            hover: { index: info.index, x: info.x, y: info.y },
          });
        } else {
          dispatch({ type: 'set_hover', hover: null });
        }
      },
    });
    return [basemapLayer, pointLayer];
  }, [deckgl, state.result, state.viewMode, pointStyle, basemapTileUrl]);

  // --- imperative Deck lifecycle (deck.gl/core, never deck.gl/react: React
  // is host-injected and a second embedded React is the two-React hazard the
  // spec bans).
  useEffect(() => {
    if (!deckgl || !canvasRef.current || state.error) return undefined;
    const deck = new deckgl.Deck({
      canvas: canvasRef.current,
      controller: true,
      viewState: stateRef.current.viewState,
      layers: [],
      onViewStateChange,
      getCursor: ({ isHovering }: { isHovering: boolean }) =>
        isHovering ? 'pointer' : 'grab',
    });
    deckRef.current = deck;
    return () => {
      deckRef.current = null;
      deck.finalize();
    };
  }, [deckgl, state.error]);

  useEffect(() => {
    deckRef.current?.setProps({ viewState: state.viewState, layers, onViewStateChange });
  }, [state.viewState, layers, onViewStateChange]);

  const currentViewportBbox = useCallback(
    () =>
      deckgl ? viewportBboxFromElement(deckgl, containerRef.current, state.viewState) : null,
    [deckgl, state.viewState],
  );

  const gridFilter = ctx?.gridFilter;
  const filterToViewport = useMemo(() => {
    if (!gridFilter || !columnId) return undefined;
    return () => {
      const bbox = currentViewportBbox();
      if (bbox) {
        gridFilter.applyBbox(columnId, bbox);
        ctxRef.current?.navigation.closeView?.();
      }
    };
  }, [gridFilter, columnId, currentViewportBbox]);

  const tooltip = buildMapTooltip({
    hover: state.hover,
    result: state.result,
    columns,
    colorBy: state.colorBy,
    sizeBy: state.sizeBy,
  });

  if (!ctx || ctx.schemaVersion !== 'frisket.plugin_projection_view_context.v1') {
    return (
      <div className="map-view" data-testid="map-view">
        <div className="map-view-error" data-testid="map-error">
          map view mounted without its projection view context
        </div>
      </div>
    );
  }
  if (!columnId) {
    return (
      <div className="map-view" data-testid="map-view">
        <div className="map-view-empty" data-testid="map-empty">
          No geo_point column in this sheet.
        </div>
      </div>
    );
  }

  const error = deckglError ?? state.error;

  return (
    <div className="map-view" data-testid="map-view">
      <MapToolbar
        React={React}
        columnId={columnId}
        columnName={columnName}
        columns={columns}
        loading={state.loading}
        pointCount={state.result?.count ?? null}
        viewMode={state.viewMode}
        colorBy={state.colorBy}
        sizeBy={state.sizeBy}
        transient={Boolean(state.result?.transient)}
        onViewModeChange={(mode) => dispatch({ type: 'set_view_mode', viewMode: mode })}
        onColorByChange={(id) => dispatch({ type: 'set_color_by', columnId: id })}
        onSizeByChange={(id) => dispatch({ type: 'set_size_by', columnId: id })}
        onFilterToViewport={filterToViewport}
      />
      <div className="map-view-canvas" data-testid="map-canvas-container" ref={containerRef}>
        {error ? (
          <div className="map-view-error" data-testid="map-error">
            {error}
          </div>
        ) : (
          <canvas
            ref={canvasRef}
            style={{
              position: 'absolute',
              width: '100%',
              height: '100%',
              background: '#d7dee2',
            }}
          />
        )}
        {state.viewMode === 'points' && <MapTooltip React={React} tooltip={tooltip} />}
        {!error && <MapLegend React={React} legend={pointStyle.legend} />}
        {!error && (
          <div className="map-view-attribution" data-testid="map-basemap-attribution">
            {basemapAttribution}
          </div>
        )}
        {!error && state.result && state.result.count === 0 && !state.loading && (
          <div className="map-view-empty" data-testid="map-empty">
            No mappable points in this column.
          </div>
        )}
      </div>
    </div>
  );
}
