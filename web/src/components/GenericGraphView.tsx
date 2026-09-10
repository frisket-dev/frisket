import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type WheelEvent as ReactWheelEvent,
} from 'react';
import { ArrowLeft, ChevronRight } from 'lucide-react';
import {
  ApiError,
  type SheetGraphEdge,
  type SheetGraphNode,
  type SheetGraphResult,
  type SheetMeta,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';
import { EntityDetailPanel, type EntityDetailConnection } from './EntityDetailPanel';
import { PanelSelect } from './PanelSelect';

// GenericGraphView is the generic node/edge graph work-view.
//
// Reads the materialized edge/join substrate via GET /sheets/:id/graph
// (getSheetGraph), NOT the FtM /graph/neighborhood service. It REPLACES the
// retired FtM GraphNeighborhoodView on the same `graph` work-view segment and
// contribution id, under the canonical root testid `graph-view`. It does not
// expose the FtM-specific anchor/schema UI. The FtM backend service survives for
// the entity-detail connections tab and is untouched here.

const GRAPH_WIDTH = 900;
const GRAPH_HEIGHT = 620;
const NODE_BASE_RADIUS = 22;

// The side-list rows NAVIGATE (open the row drawer) while the SVG dots/edges
// only SELECT in place (see the "SVG click = SELECT" comment below) -- the
// split is intentional but was visually undifferentiated, so users clicked a
// list row expecting an in-place selection and got navigated away instead.
// This inline style keeps the title/meta text pinned to the left of the
// row while the open-row chevron sits flush right, preserving the
// existing `.graph-list-item` flex/space-between layout with an added child.
const GRAPH_LIST_ROW_TEXT_STYLE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
  flex: '1 1 auto',
};

// Visible + accessible cue that a side-list row navigates away (opens the row
// drawer) rather than selecting in place.
function OpenRowCue() {
  return (
    <ChevronRight
      size={14}
      aria-hidden="true"
      data-testid="graph-list-open-cue"
      style={{ flexShrink: 0, opacity: 0.6 }}
    />
  );
}

// Deterministic categorical palette (stable across reloads for screenshots).
const NODE_PALETTE = [
  '#4f7cff',
  '#e8663c',
  '#2ea36b',
  '#b45cd6',
  '#d6a72e',
  '#38b6c4',
  '#d64d7a',
  '#6b8f2e',
];

interface GenericGraphViewProps {
  sheet: SheetMeta;
  onClose(): void;
  onOpenRowRef(sheetId: number | string, rowId: number | string): void;
  /** Resolved entityDetail contributions + the plugin-detail renderer — the
   *  entity-detail host preserved across the FtM-view retirement. A
   *  selected node is the entity subject; the panel renders only when a node
   *  is selected and contributions exist. */
  entityDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?(
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ): ReactNode;
}

type LoadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; graph: SheetGraphResult }
  | { status: 'error'; message: string };

interface GraphViewConfig {
  direction: '' | 'directed' | 'undirected'; // '' = auto (per role family)
  nodeLabelColumnId: string;
  nodeColorColumnId: string;
  nodeSizeColumnId: string;
  edgeLabelColumnId: string;
}

const EMPTY_CONFIG: GraphViewConfig = {
  direction: '',
  nodeLabelColumnId: '',
  nodeColorColumnId: '',
  nodeSizeColumnId: '',
  edgeLabelColumnId: '',
};

interface ViewBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

const FULL_VIEWBOX: ViewBox = { x: 0, y: 0, w: GRAPH_WIDTH, h: GRAPH_HEIGHT };

function computeLayout(
  nodes: SheetGraphNode[],
  edges: SheetGraphEdge[],
): Map<string, { x: number; y: number }> {
  const ids = nodes.map((n) => n.id).sort();
  const n = ids.length;
  const pos = new Map<string, { x: number; y: number }>();
  const cx = GRAPH_WIDTH / 2;
  const cy = GRAPH_HEIGHT / 2;
  const radius = Math.min(GRAPH_WIDTH, GRAPH_HEIGHT) / 2 - 90;
  ids.forEach((id, i) => {
    // Deterministic seed on a circle by SORTED node id — no randomness, so a
    // fixed-iteration spring relaxation is stable across reloads.
    const a = (2 * Math.PI * i) / Math.max(1, n);
    pos.set(id, { x: cx + radius * Math.cos(a), y: cy + radius * Math.sin(a) });
  });
  if (n <= 1) return pos;
  const k = Math.sqrt((GRAPH_WIDTH * GRAPH_HEIGHT) / n);
  const adj: (readonly [string, string])[] = [];
  for (const e of edges) {
    if (pos.has(e.source) && pos.has(e.target)) adj.push([e.source, e.target] as const);
  }
  const ITER = 140;
  for (let it = 0; it < ITER; it++) {
    const disp = new Map(ids.map((id) => [id, { x: 0, y: 0 }]));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = pos.get(ids[i])!;
        const b = pos.get(ids[j])!;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const dist = Math.hypot(dx, dy) || 0.01;
        const force = (k * k) / dist;
        const ux = dx / dist;
        const uy = dy / dist;
        const di = disp.get(ids[i])!;
        const dj = disp.get(ids[j])!;
        di.x += ux * force;
        di.y += uy * force;
        dj.x -= ux * force;
        dj.y -= uy * force;
      }
    }
    for (const [s, t] of adj) {
      const a = pos.get(s)!;
      const b = pos.get(t)!;
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const force = (dist * dist) / k;
      const ux = dx / dist;
      const uy = dy / dist;
      const ds = disp.get(s)!;
      const dt = disp.get(t)!;
      ds.x -= ux * force;
      ds.y -= uy * force;
      dt.x += ux * force;
      dt.y += uy * force;
    }
    const temp = (1 - it / ITER) * (Math.min(GRAPH_WIDTH, GRAPH_HEIGHT) / 10);
    for (const id of ids) {
      const d = disp.get(id)!;
      const p = pos.get(id)!;
      d.x += (cx - p.x) * 0.012;
      d.y += (cy - p.y) * 0.012;
      const dl = Math.hypot(d.x, d.y) || 0.01;
      const limited = Math.min(dl, temp);
      p.x += (d.x / dl) * limited;
      p.y += (d.y / dl) * limited;
      p.x = Math.max(48, Math.min(GRAPH_WIDTH - 48, p.x));
      p.y = Math.max(48, Math.min(GRAPH_HEIGHT - 48, p.y));
    }
  }
  return pos;
}

function nodeColor(node: SheetGraphNode, palette: Map<string, string>): string {
  if (node.color_value === null || node.color_value === undefined) return '#8a94a6';
  return palette.get(String(node.color_value)) ?? '#8a94a6';
}

function nodeRadius(node: SheetGraphNode, sizeRange: [number, number] | null): number {
  if (!sizeRange) return NODE_BASE_RADIUS;
  const value = Number(node.size_value);
  if (!Number.isFinite(value)) return NODE_BASE_RADIUS;
  const [min, max] = sizeRange;
  if (max <= min) return NODE_BASE_RADIUS;
  const t = (value - min) / (max - min);
  return 14 + t * 22;
}

interface GraphCamera {
  viewBox: ViewBox;
  resetCamera(): void;
  onWheel(event: ReactWheelEvent<SVGSVGElement>): void;
  onPointerDown(event: ReactPointerEvent<SVGSVGElement>): void;
  onPointerMove(event: ReactPointerEvent<SVGSVGElement>): void;
  endPan(): void;
}

// Pan/zoom camera for the graph SVG: wheel zooms toward the box center
// (deterministic, no cursor math) and left-drag pans. Extracted from
// GenericGraphView so that component reads as data-load + render.
function useGraphCamera(): GraphCamera {
  const [viewBox, setViewBox] = useState<ViewBox>(FULL_VIEWBOX);
  const panRef = useRef<{ x: number; y: number; box: ViewBox } | null>(null);

  const onWheel = useCallback((event: ReactWheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    setViewBox((box) => {
      const scale = event.deltaY > 0 ? 1.1 : 1 / 1.1;
      const newW = Math.min(GRAPH_WIDTH * 3, Math.max(GRAPH_WIDTH / 6, box.w * scale));
      const newH = newW * (GRAPH_HEIGHT / GRAPH_WIDTH);
      // zoom toward center of current box (deterministic, no cursor math needed)
      const cx = box.x + box.w / 2;
      const cy = box.y + box.h / 2;
      return { x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH };
    });
  }, []);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<SVGSVGElement>) => {
      if (event.button !== 0) return;
      panRef.current = { x: event.clientX, y: event.clientY, box: viewBox };
      (event.target as Element).setPointerCapture?.(event.pointerId);
    },
    [viewBox],
  );
  const onPointerMove = useCallback((event: ReactPointerEvent<SVGSVGElement>) => {
    const pan = panRef.current;
    if (!pan) return;
    const svg = event.currentTarget;
    const rect = svg.getBoundingClientRect();
    const scaleX = pan.box.w / rect.width;
    const scaleY = pan.box.h / rect.height;
    setViewBox({
      x: pan.box.x - (event.clientX - pan.x) * scaleX,
      y: pan.box.y - (event.clientY - pan.y) * scaleY,
      w: pan.box.w,
      h: pan.box.h,
    });
  }, []);
  const endPan = useCallback(() => {
    panRef.current = null;
  }, []);

  const resetCamera = useCallback(() => setViewBox(FULL_VIEWBOX), []);

  return { viewBox, resetCamera, onWheel, onPointerDown, onPointerMove, endPan };
}

export function GenericGraphView({
  sheet,
  onClose,
  onOpenRowRef,
  entityDetailContributions,
  renderPluginDetailTab,
}: GenericGraphViewProps) {
  const { projectApi } = useWorkspaceStores();
  const [config, setConfig] = useState<GraphViewConfig>(EMPTY_CONFIG);
  // useReducer (not useState) so the fetch effect can dispatch 'loading'
  // synchronously without tripping react-hooks/set-state-in-effect. Per-sheet
  // reset happens by remount: the render sites key this component on sheet.id.
  const [load, dispatchLoad] = useReducer(
    (_prev: LoadState, next: LoadState) => next,
    { status: 'idle' } as LoadState,
  );
  const [allSheets, setAllSheets] = useState<SheetMeta[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { viewBox, resetCamera, onWheel, onPointerDown, onPointerMove, endPan } = useGraphCamera();

  useEffect(() => {
    let cancelled = false;
    projectApi
      .listSheets()
      .then((sheets) => {
        if (!cancelled) setAllSheets(sheets);
      })
      .catch(() => {
        /* column options degrade gracefully to empty */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    dispatchLoad({ status: 'loading' });
    projectApi
      .getSheetGraph({
        sheetId: sheet.id,
        direction: config.direction || undefined,
        nodeLabelColumnId: config.nodeLabelColumnId || undefined,
        nodeColorColumnId: config.nodeColorColumnId || undefined,
        nodeSizeColumnId: config.nodeSizeColumnId || undefined,
        edgeLabelColumnId: config.edgeLabelColumnId || undefined,
      })
      .then((graph) => {
        if (!cancelled) dispatchLoad({ status: 'loaded', graph });
      })
      .catch((err) => {
        if (cancelled) return;
        const message =
          err instanceof ApiError ? err.message : 'Failed to load graph.';
        dispatchLoad({ status: 'error', message });
      });
    return () => {
      cancelled = true;
    };
  }, [
    sheet.id,
    config.direction,
    config.nodeLabelColumnId,
    config.nodeColorColumnId,
    config.nodeSizeColumnId,
    config.edgeLabelColumnId,
  ]);

  const graph = load.status === 'loaded' ? load.graph : null;

  const positions = useMemo(
    () => (graph ? computeLayout(graph.nodes, graph.edges) : new Map()),
    [graph],
  );

  const colorPalette = useMemo(() => {
    const palette = new Map<string, string>();
    if (!graph) return palette;
    const values = Array.from(
      new Set(
        graph.nodes
          .map((n) => n.color_value)
          .filter((v) => v !== null && v !== undefined)
          .map((v) => String(v)),
      ),
    ).sort();
    values.forEach((v, i) => palette.set(v, NODE_PALETTE[i % NODE_PALETTE.length]));
    return palette;
  }, [graph]);

  const sizeRange = useMemo<[number, number] | null>(() => {
    if (!graph || !config.nodeSizeColumnId) return null;
    const nums: number[] = [];
    for (const n of graph.nodes) {
      const v = Number(n.size_value);
      if (Number.isFinite(v)) nums.push(v);
    }
    if (nums.length === 0) return null;
    return [Math.min(...nums), Math.max(...nums)];
  }, [graph, config.nodeSizeColumnId]);

  // Config select options. Node bindings resolve on the ENDPOINT sheets,
  // so they are drawn from the columns of the sheets the graph nodes live on;
  // edge bindings are columns on the active (edge) sheet.
  const sheetsById = useMemo(() => {
    const map = new Map<string, SheetMeta>();
    for (const s of allSheets) map.set(String(s.id), s);
    return map;
  }, [allSheets]);

  const nodeColumnOptions = useMemo(() => {
    if (!graph) return [] as { id: string; label: string }[];
    const endpointSheetIds = Array.from(
      new Set(graph.nodes.map((n) => String(n.sheet_id))),
    ).sort();
    const options: { id: string; label: string }[] = [];
    for (const sid of endpointSheetIds) {
      const meta = sheetsById.get(sid);
      if (!meta) continue;
      const prefix = endpointSheetIds.length > 1 ? `${meta.name} · ` : '';
      for (const col of meta.columns) {
        options.push({ id: col.id, label: `${prefix}${col.name}` });
      }
    }
    return options;
  }, [graph, sheetsById]);

  const numericNodeColumnOptions = useMemo(() => {
    if (!graph) return [] as { id: string; label: string }[];
    const endpointSheetIds = new Set(graph.nodes.map((n) => String(n.sheet_id)));
    const options: { id: string; label: string }[] = [];
    for (const sid of endpointSheetIds) {
      const meta = sheetsById.get(sid);
      if (!meta) continue;
      for (const col of meta.columns) {
        if (col.type === 'integer' || col.type === 'number') {
          options.push({ id: col.id, label: col.name });
        }
      }
    }
    return options;
  }, [graph, sheetsById]);

  // SVG click = SELECT (surfaces entity detail in-place, no navigation);
  // the side-panel row lists = OPEN the row drawer (openRowRef navigates).
  const selectNode = useCallback((node: SheetGraphNode) => setSelectedId(node.id), []);
  const selectEdge = useCallback((edge: SheetGraphEdge) => setSelectedId(edge.id), []);
  const openNodeRow = useCallback(
    (node: SheetGraphNode) => {
      setSelectedId(node.id);
      onOpenRowRef(node.row_ref.sheet_id, node.row_ref.row_id);
    },
    [onOpenRowRef],
  );
  const openEdgeRow = useCallback(
    (edge: SheetGraphEdge) => {
      setSelectedId(edge.id);
      onOpenRowRef(edge.row_ref.sheet_id, edge.row_ref.row_id);
    },
    [onOpenRowRef],
  );

  const selectedNode = useMemo(
    () => (graph ? graph.nodes.find((n) => n.id === selectedId) ?? null : null),
    [graph, selectedId],
  );
  const entitySubject = useMemo<PluginDetailSubject | null>(
    () =>
      selectedNode
        ? { kind: 'entity', entityId: selectedNode.id, label: selectedNode.label }
        : null,
    [selectedNode],
  );
  const nodeConnections = useMemo<EntityDetailConnection[]>(() => {
    if (!graph || !selectedNode) return [];
    const byId = new Map(graph.nodes.map((n) => [n.id, n] as const));
    const connections: EntityDetailConnection[] = [];
    for (const e of graph.edges) {
      if (e.source !== selectedNode.id && e.target !== selectedNode.id) continue;
      const otherId = e.source === selectedNode.id ? e.target : e.source;
      connections.push({ key: e.id, label: e.label, detail: byId.get(otherId)?.label });
    }
    return connections;
  }, [graph, selectedNode]);

  return (
    <div className="graph-view" data-testid="graph-view">
      <header className="graph-view-header">
        <button
          type="button"
          className="graph-back-button"
          onClick={onClose}
          data-testid="graph-close"
          aria-label="Close graph view"
        >
          <ArrowLeft size={16} />
        </button>
        <h2 className="graph-view-title">Graph of {sheet.name}</h2>
        <button
          type="button"
          className="graph-toolbar-button"
          onClick={resetCamera}
          data-testid="graph-reset-camera"
        >
          Reset view
        </button>
      </header>

      <div className="graph-view-body">
        <div className="graph-canvas-column">
          <GraphCanvas
            load={load}
            graph={graph}
            positions={positions}
            colorPalette={colorPalette}
            sizeRange={sizeRange}
            viewBox={viewBox}
            selectedId={selectedId}
            onOpenNode={selectNode}
            onOpenEdge={selectEdge}
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endPan}
            onPointerLeave={endPan}
          />
        </div>

        <GraphSidePanel
          graph={graph}
          sheet={sheet}
          config={config}
          setConfig={setConfig}
          nodeColumnOptions={nodeColumnOptions}
          numericNodeColumnOptions={numericNodeColumnOptions}
          entitySubject={entitySubject}
          selectedNode={selectedNode}
          nodeConnections={nodeConnections}
          entityDetailContributions={entityDetailContributions}
          renderPluginDetailTab={renderPluginDetailTab}
          onOpenNodeRow={openNodeRow}
          onOpenEdgeRow={openEdgeRow}
        />
      </div>
    </div>
  );
}

// The right-hand panel: selected-entity detail + graph config + diagnostics +
// node/edge lists. Presentational; extracted from GenericGraphView.
function GraphSidePanel({
  graph,
  sheet,
  config,
  setConfig,
  nodeColumnOptions,
  numericNodeColumnOptions,
  entitySubject,
  selectedNode,
  nodeConnections,
  entityDetailContributions,
  renderPluginDetailTab,
  onOpenNodeRow,
  onOpenEdgeRow,
}: {
  graph: SheetGraphResult | null;
  sheet: SheetMeta;
  config: GraphViewConfig;
  setConfig: React.Dispatch<React.SetStateAction<GraphViewConfig>>;
  nodeColumnOptions: { id: string; label: string }[];
  numericNodeColumnOptions: { id: string; label: string }[];
  entitySubject: PluginDetailSubject | null;
  selectedNode: SheetGraphNode | null;
  nodeConnections: EntityDetailConnection[];
  entityDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?(
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ): ReactNode;
  onOpenNodeRow(node: SheetGraphNode): void;
  onOpenEdgeRow(edge: SheetGraphEdge): void;
}) {
  return (
    <aside className="graph-side-panel">
      {entitySubject && selectedNode && entityDetailContributions && (
        <EntityDetailPanel
          subject={entitySubject}
          summary={{
            label: selectedNode.label,
            meta: `sheet ${selectedNode.sheet_id} · row ${selectedNode.row_id}`,
          }}
          connections={nodeConnections}
          contributions={entityDetailContributions}
          renderPluginDetailTab={renderPluginDetailTab}
        />
      )}
      <GraphConfigPanel
        config={config}
        setConfig={setConfig}
        nodeColumnOptions={nodeColumnOptions}
        numericNodeColumnOptions={numericNodeColumnOptions}
        edgeColumnOptions={sheet.columns.map((c) => ({ id: c.id, label: c.name }))}
      />

      {graph?.truncated && (
        <div className="graph-diagnostic graph-diagnostic-limit" data-testid="graph-truncated">
          Result limited at {graph.limits.returned_nodes}/{graph.limits.nodes} nodes and{' '}
          {graph.limits.returned_edges}/{graph.limits.edges} edges.
        </div>
      )}
      {graph?.diagnostics.map((item) => (
        <div
          className="graph-diagnostic"
          data-testid="graph-diagnostic"
          key={`${item.code}:${item.message}`}
        >
          <strong>{item.code}</strong>: {item.message}
        </div>
      ))}

      <h3>Nodes {graph ? `(${graph.nodes.length})` : ''}</h3>
      <div className="graph-list" data-testid="graph-node-list">
        {graph?.nodes.map((node) => (
          <button
            type="button"
            key={node.id}
            className="graph-list-item"
            data-testid={`graph-node-row-${node.id}`}
            onClick={() => onOpenNodeRow(node)}
            aria-label={`Open row for ${node.label}`}
            title="Open row"
          >
            <span style={GRAPH_LIST_ROW_TEXT_STYLE}>
              <span className="graph-list-title">{node.label}</span>
              <span className="graph-list-meta">degree {node.degree}</span>
            </span>
            <OpenRowCue />
          </button>
        ))}
      </div>

      <h3>Edges {graph ? `(${graph.edges.length})` : ''}</h3>
      <div className="graph-list" data-testid="graph-edge-list">
        {graph?.edges.map((edge) => (
          <button
            type="button"
            key={edge.id}
            className="graph-list-item"
            data-testid={`graph-edge-row-${edge.id}`}
            onClick={() => onOpenEdgeRow(edge)}
            aria-label={`Open row for ${edge.label}`}
            title="Open row"
          >
            <span style={GRAPH_LIST_ROW_TEXT_STYLE}>
              <span className="graph-list-title">{edge.label}</span>
              <span className="graph-list-meta">{edge.direction}</span>
            </span>
            <OpenRowCue />
          </button>
        ))}
      </div>
    </aside>
  );
}

function GraphCanvas({
  load,
  graph,
  positions,
  colorPalette,
  sizeRange,
  viewBox,
  selectedId,
  onOpenNode,
  onOpenEdge,
  onWheel,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerLeave,
}: {
  load: LoadState;
  graph: SheetGraphResult | null;
  positions: Map<string, { x: number; y: number }>;
  colorPalette: Map<string, string>;
  sizeRange: [number, number] | null;
  viewBox: ViewBox;
  selectedId: string | null;
  onOpenNode(node: SheetGraphNode): void;
  onOpenEdge(edge: SheetGraphEdge): void;
  onWheel(event: ReactWheelEvent<SVGSVGElement>): void;
  onPointerDown(event: ReactPointerEvent<SVGSVGElement>): void;
  onPointerMove(event: ReactPointerEvent<SVGSVGElement>): void;
  onPointerUp(): void;
  onPointerLeave(): void;
}) {
  if (load.status === 'loading' || load.status === 'idle') {
    return (
      <div className="graph-empty" data-testid="graph-loading">
        Loading graph…
      </div>
    );
  }
  if (load.status === 'error') {
    return (
      <div className="graph-empty graph-error" data-testid="graph-error">
        {load.message}
      </div>
    );
  }
  if (!graph || graph.nodes.length === 0) {
    return (
      <div className="graph-empty" data-testid="graph-empty">
        This sheet has no materialized edges to graph.
      </div>
    );
  }
  return (
    <div className="graph-canvas-wrap">
      <svg
        className="graph-svg"
        data-testid="graph-svg"
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
        role="img"
        aria-label={`Graph of sheet ${graph.sheet_id}`}
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerLeave}
      >
        <defs>
          <marker
            id="graph-arrow"
            markerWidth="10"
            markerHeight="10"
            refX="18"
            refY="3"
            orient="auto"
            markerUnits="strokeWidth"
          >
            <path d="M0,0 L0,6 L9,3 z" className="graph-arrow" />
          </marker>
        </defs>
        {graph.edges.map((edge) => {
          const source = positions.get(edge.source);
          const target = positions.get(edge.target);
          if (!source || !target) return null;
          const midX = (source.x + target.x) / 2;
          const midY = (source.y + target.y) / 2;
          return (
            <g
              key={edge.id}
              data-testid={`graph-edge-${edge.id}`}
              className={`graph-edge-group${selectedId === edge.id ? ' is-selected' : ''}`}
              onClick={() => onOpenEdge(edge)}
            >
              <line
                className="graph-edge-line"
                x1={source.x}
                y1={source.y}
                x2={target.x}
                y2={target.y}
                markerEnd={edge.direction === 'directed' ? 'url(#graph-arrow)' : undefined}
              />
              <text className="graph-edge-label" x={midX} y={midY - 8}>
                {edge.label}
              </text>
            </g>
          );
        })}
        {graph.nodes.map((node) => {
          const point = positions.get(node.id);
          if (!point) return null;
          const r = nodeRadius(node, sizeRange);
          return (
            <g
              key={node.id}
              data-testid={`graph-node-${node.id}`}
              className={`graph-node-group${selectedId === node.id ? ' is-selected' : ''}`}
              onClick={() => onOpenNode(node)}
            >
              <circle
                className="graph-node"
                cx={point.x}
                cy={point.y}
                r={r}
                fill={nodeColor(node, colorPalette)}
              />
              <text className="graph-node-label" x={point.x} y={point.y + r + 16}>
                {node.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function GraphConfigPanel({
  config,
  setConfig,
  nodeColumnOptions,
  numericNodeColumnOptions,
  edgeColumnOptions,
}: {
  config: GraphViewConfig;
  setConfig: React.Dispatch<React.SetStateAction<GraphViewConfig>>;
  nodeColumnOptions: { id: string; label: string }[];
  numericNodeColumnOptions: { id: string; label: string }[];
  edgeColumnOptions: { id: string; label: string }[];
}) {
  const set = (patch: Partial<GraphViewConfig>) =>
    setConfig((prev) => ({ ...prev, ...patch }));
  return (
    <div className="graph-config-panel" data-testid="graph-config-panel">
      <h3>Configure</h3>
      <label className="graph-config-field">
        <span>Direction</span>
        <PanelSelect
          className="row-height-select"
          data-testid="graph-config-direction"
          value={config.direction}
          onChange={(e) => set({ direction: e.target.value as GraphViewConfig['direction'] })}
        >
          <option value="">Auto</option>
          <option value="directed">Directed</option>
          <option value="undirected">Undirected</option>
        </PanelSelect>
      </label>
      <label className="graph-config-field">
        <span>Node label</span>
        <PanelSelect
          className="row-height-select"
          data-testid="graph-config-node-label"
          value={config.nodeLabelColumnId}
          onChange={(e) => set({ nodeLabelColumnId: e.target.value })}
        >
          <option value="">Default</option>
          {nodeColumnOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.label}
            </option>
          ))}
        </PanelSelect>
      </label>
      <label className="graph-config-field">
        <span>Node color</span>
        <PanelSelect
          className="row-height-select"
          data-testid="graph-config-node-color"
          value={config.nodeColorColumnId}
          onChange={(e) => set({ nodeColorColumnId: e.target.value })}
        >
          <option value="">None</option>
          {nodeColumnOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.label}
            </option>
          ))}
        </PanelSelect>
      </label>
      <label className="graph-config-field">
        <span>Node size</span>
        <PanelSelect
          className="row-height-select"
          data-testid="graph-config-node-size"
          value={config.nodeSizeColumnId}
          onChange={(e) => set({ nodeSizeColumnId: e.target.value })}
        >
          <option value="">Degree</option>
          {numericNodeColumnOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.label}
            </option>
          ))}
        </PanelSelect>
      </label>
      <label className="graph-config-field">
        <span>Edge label</span>
        <PanelSelect
          className="row-height-select"
          data-testid="graph-config-edge-label"
          value={config.edgeLabelColumnId}
          onChange={(e) => set({ edgeLabelColumnId: e.target.value })}
        >
          <option value="">Default</option>
          {edgeColumnOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.label}
            </option>
          ))}
        </PanelSelect>
      </label>
    </div>
  );
}
