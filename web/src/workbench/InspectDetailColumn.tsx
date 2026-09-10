import { ChevronDown, ChevronUp } from 'lucide-react';
import { useMemo, useRef, type ReactNode } from 'react';
import { RowDrawerBody, PreviewFieldValue } from '../components/RowDrawer';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { useResizable } from '../components/useResizable';
import { ResizeSeam } from '../components/ResizeSeam';
import { PanelHeader } from '../components/PanelPrimitives';
import type { CellValue, ColumnDef, PreviewCellDetail, Row, SheetMeta } from '../api/open';
import type { WorkbenchResolvedLayoutContribution } from './layout';
import { rowTitle } from './rowTitle';

const INSPECT_DETAIL_MIN_WIDTH = 260;
const INSPECT_DETAIL_MAX_WIDTH = 560;
const INSPECT_DETAIL_DEFAULT_WIDTH = 340;

export interface InspectDetailColumnProps {
  projectId: string;
  sheet: SheetMeta;
  row: Row;
  selectedColumnId?: string | null;
  /** Sampled details bypass committed values and all row-edit/retry controls. */
  preview?: PreviewCellDetail | null;
  onClose(): void;
  onEdit(row: Row, col: ColumnDef, value: CellValue): Promise<void>;
  /** Walk the selection without closing the panel; the grid highlight follows.
   *  Absent/disabled at the ends of the sheet. */
  onPrev?(): void;
  onNext?(): void;
  canPrev: boolean;
  canNext: boolean;
  detailContributions?: WorkbenchResolvedLayoutContribution[];
  renderDetailContribution?(contribution: WorkbenchResolvedLayoutContribution): ReactNode;
  onOpenInDocumentView?(columnId: string): void;
  /** Deliberate per-row retry of a terminal empty_output cell — threaded to
   *  RowDrawerBody's "Retry anyway" affordance. */
  onRetryCell?(columnName: string): Promise<void>;
  /** The grid's current column drag order (NAMES, visible-only) — the
   *  default-column input to `rowTitle()`. Absent/empty falls back to
   *  canonical column order. */
  titleColumnOrder?: readonly string[];
}

/**
 * Inspect region — the resident Detail column. Selecting a grid row opens
 * this dedicated column immediately right of the Work area (it is NOT an
 * overlay drawer, and it coexists with the Discover panel). Header: ROW n
 * (pinned e2e text) · the row's resolved TITLE (via the shared rowTitle
 * helper) · prev/next · close. Body: RowDrawerBody (the re-hosted RowDrawer
 * content). Drag-resizable via the left seam; width persists per user.
 */
export function InspectDetailColumn({
  projectId,
  sheet,
  row,
  selectedColumnId,
  preview,
  onClose,
  onEdit,
  onPrev,
  onNext,
  canPrev,
  canNext,
  detailContributions,
  renderDetailContribution,
  onOpenInDocumentView,
  onRetryCell,
  titleColumnOrder,
}: InspectDetailColumnProps) {
  const title = preview ? `${preview.column.name} · Preview` : rowTitle(sheet, row, { columnOrder: titleColumnOrder });
  const bodyRef = useRef<HTMLDivElement>(null);
  const { width, resizing, onResizeStart, onResizeKeyDown } = useResizable({
    storageKey: `frisket:inspect-detail-width:${projectId}`,
    minWidth: INSPECT_DETAIL_MIN_WIDTH,
    maxWidth: INSPECT_DETAIL_MAX_WIDTH,
    defaultWidth: INSPECT_DETAIL_DEFAULT_WIDTH,
    handleEdge: 'left',
  });

  // Esc closes the Detail column (parity with the retired overlay drawer's
  // dismiss). Detail is RESIDENT, so the guards matter: the typing guard keeps
  // an inline editor's cancel-edit from also closing Detail, and top-layer
  // awareness (`:popover-open, :modal`) keeps a menu/palette/modal Escape from
  // double-dismissing this panel. Capture phase is the hook's default (the grid
  // canvas swallows Escape when focused).
  useEscapeDismiss(onClose, { typingGuard: true });

  // jsx-no-jsx-as-prop: memoized so PanelHeader's kicker/chips don't get
  // fresh JSX elements every render.
  const headerKicker = useMemo(
    () => (
      <span className="inspect-detail-rownum" data-testid="inspect-detail-rownum">
        ROW {row.index + 1}
      </span>
    ),
    [row.index],
  );
  const headerChips = useMemo(
    () => (
      <span className="inspect-detail-sheet muted" title={sheet.name}>
        {sheet.name}
      </span>
    ),
    [sheet.name],
  );

  return (
    <aside
      className={`inspect-detail-column${resizing ? ' inspect-detail-column-resizing' : ''}`}
      style={{ width }}
      // `row-drawer` is the pinned e2e hook carried over from the retired
      // overlay (the content re-hosts here); `inspect-detail-column` names the
      // new resident frame.
      data-testid="row-drawer"
      data-inspect-detail-column="true"
      aria-label={`Row ${row.index + 1} detail`}
    >
      <ResizeSeam
        className="inspect-detail-seam"
        ariaLabel="Resize the Detail column"
        testId="inspect-detail-seam"
        width={width}
        min={INSPECT_DETAIL_MIN_WIDTH}
        max={INSPECT_DETAIL_MAX_WIDTH}
        onResizeStart={onResizeStart}
        onResizeKeyDown={onResizeKeyDown}
      />
      <PanelHeader
        className="panel-frame-header inspect-detail-header"
        kicker={headerKicker}
        title={
          <div className="inspect-detail-walk">
            <span
              className="inspect-detail-row-title"
              data-testid="inspect-detail-row-title"
              title={title}
            >
              {title}
            </span>
            <button
              type="button"
              className="inspect-detail-chip"
              data-testid="inspect-detail-prev"
              aria-label="Previous row"
              title="Previous row"
              disabled={!canPrev}
              onClick={() => onPrev?.()}
            >
              <ChevronUp size={14} />
            </button>
            <button
              type="button"
              className="inspect-detail-chip"
              data-testid="inspect-detail-next"
              aria-label="Next row"
              title="Next row"
              disabled={!canNext}
              onClick={() => onNext?.()}
            >
              <ChevronDown size={14} />
            </button>
          </div>
        }
        chips={headerChips}
        onClose={onClose}
        closeTestId="inspect-detail-close"
        closeClassName="panel-frame-close inspect-detail-chip inspect-detail-close"
        // "Close drawer" is the pinned label carried over from the retired
        // overlay Drawer (many specs target it by role/name).
        closeAriaLabel="Close drawer"
        closeTitle="Close"
      />
      {/* `drawer-body` is retained as the scoped scroll container class the
          selected-cell smooth-scroll effect (and its e2e probe) targets. */}
      <div className="inspect-detail-body drawer-body" ref={bodyRef} data-testid="inspect-detail-body">
        {preview ? (
          <PreviewFieldValue preview={preview} />
        ) : (
          <RowDrawerBody
            sheet={sheet}
            row={row}
            selectedColumnId={selectedColumnId}
            onEdit={onEdit}
            detailContributions={detailContributions}
            renderDetailContribution={renderDetailContribution}
            onOpenInDocumentView={onOpenInDocumentView}
            onRetryCell={onRetryCell}
            bodyRef={bodyRef}
          />
        )}
      </div>
    </aside>
  );
}
