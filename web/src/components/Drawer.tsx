import { useCallback, useState, type CSSProperties, type ReactNode, type Ref } from 'react';
import { useResizable } from './useResizable';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { PanelHeader } from './PanelPrimitives';

export interface DrawerProps {
  title: ReactNode;
  side?: 'left' | 'right';
  onClose(): void;
  children: ReactNode;
  testId?: string;
  bodyRef?: Ref<HTMLDivElement>;
}

const MIN_WIDTH = 280;
const MAX_WIDTH = 760;
const DEFAULT_WIDTH = 392;
const CLOSE_MS = 110; // keep just under the 120ms animation so it never pops
const RESIZE_HANDLE_STYLE: CSSProperties = { border: 0, margin: 0, padding: 0 };

/**
 * Shared slide-in drawer chrome (right: row/column detail; left: history).
 * Drag the inner edge to resize; the width persists per side in localStorage.
 */
export function Drawer({ title, side = 'right', onClose, children, testId, bodyRef }: DrawerProps) {
  const { width, resizing, onResizeStart } = useResizable({
    storageKey: `frisket:drawer-width:${side}`,
    minWidth: MIN_WIDTH,
    maxWidth: MAX_WIDTH,
    defaultWidth: DEFAULT_WIDTH,
    // the handle sits on the drawer's inner edge, opposite its anchored side
    handleEdge: side === 'right' ? 'left' : 'right',
  });
  const [closing, setClosing] = useState(false);

  const requestClose = useCallback(() => {
    setClosing(true);
    window.setTimeout(onClose, CLOSE_MS);
  }, [onClose]);

  // Esc closes the drawer (with the slide-out, like the X button). The hook's
  // capture-phase keydown handles the grid canvas swallowing Escape when focused.
  // Now top-layer-aware: an Escape meant for a
  // popover/modal layered above this drawer defers to that top-layer element
  // instead of also closing the drawer — the old `outside:false`-only call had
  // no such guard.
  useEscapeDismiss(requestClose);

  return (
    <aside
      className={`drawer drawer-${side}${closing ? ' drawer-closing' : ''}${resizing ? ' drawer-resizing' : ''}`}
      style={{ width }}
      data-testid={testId}
    >
      <hr
        className="drawer-resize-handle"
        data-testid={testId ? `${testId}-resize` : undefined}
        onPointerDown={onResizeStart}
        aria-orientation="vertical"
        aria-label="Resize drawer"
        style={RESIZE_HANDLE_STYLE}
      />
      <PanelHeader
        className="panel-frame-header drawer-header"
        title={<div className="drawer-title">{title}</div>}
        onClose={requestClose}
        closeAriaLabel="Close drawer"
      />
      <div className="drawer-body" ref={bodyRef}>
        {children}
      </div>
    </aside>
  );
}
