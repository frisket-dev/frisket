// @vitest-environment jsdom
//
// src/hooks/useEscapeDismiss.ts is the one registry every resident overlay-aware panel (ActionDrawer,
// InspectDetailColumn, Drawer, ReviewQueue, CascadeConfirmDialog, ...) calls
// into, after the Popover API / native `<dialog>` migration retired the old
// hand-maintained `OVERLAY_OPEN_SELECTORS` string registry.
// The registry distinguishes Glide's invisible markdown-cell proxy from a
// visible editor and makes a resident panel defer Escape to an open top-layer
// popover or dialog. The first Escape dismisses that overlay; the second reaches
// the resident panel.
//
// useEscapeDismiss's own contract (top-layer defer + opt-in typing guard) is
// exercised directly against a harness, rather than re-mounting the five+ full
// call sites (ActionDrawer, MultiColumnPicker, AddColumnPopover, the copilot
// popover, ...) each of which requires its own large surface -- this is the
// shared mechanism all of them delegate to, so proving IT is correct proves
// what all four specs actually asserted. CascadeConfirmDialog (LineagePanel.tsx)
// is additionally mounted directly, since "Escape cancels, never runs, the
// cascade" is specific to how that component wires useEscapeDismiss's onClose.
//
// jsdom does not implement the Popover API / `<dialog>` modal state (no real
// `:popover-open`/`:modal` match is possible), so "an overlay is currently
// top-layer" is stood in for via a targeted `document.querySelector` spy --
// this exercises the EXACT structural check useEscapeDismiss's source makes
// (`document.querySelector(':popover-open, :modal')`), not a re-implementation
// of it.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useEscapeDismiss } from '../../src/hooks/useEscapeDismiss';
import { CascadeConfirmDialog } from '../../src/workbench/LineagePanel';
import { ConfirmationRequiredError } from '../../src/api/open';
import type { SheetMeta } from '../../src/api/types';
import { installDialogPolyfill } from '../support/domPolyfills';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project',
  api: { projectApi: api },
});

installDialogPolyfill();

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function ResidentPanel({
  onClose,
  typingGuard,
}: {
  onClose: () => void;
  typingGuard?: boolean;
}) {
  useEscapeDismiss(onClose, { typingGuard });
  return <div data-testid="resident-panel">resident</div>;
}

describe('useEscapeDismiss (the shared overlay dismissal registry)', () => {
  it('closes the resident panel on Escape when nothing is layered above it', () => {
    const onClose = vi.fn();
    render(<ResidentPanel onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('defers to an open top-layer popover/dialog: first Escape dismisses only the overlay, second reaches the resident panel', () => {
    const onClose = vi.fn();
    render(<ResidentPanel onClose={onClose} />);

    // Stand in for a real top-layer popover/`<dialog>` currently being open
    // above the resident panel -- the exact structural check the hook makes.
    const querySelectorSpy = vi
      .spyOn(document, 'querySelector')
      .mockImplementation((selector: string) =>
        selector === ':popover-open, :modal' ? document.createElement('div') : null,
      );

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();

    // The overlay above it closes (leaves the top layer) -- the second Escape
    // now reaches the resident panel, unblocked.
    querySelectorSpy.mockRestore();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('typing guard: Escape inside a real, visible/sized editor does not close the panel', () => {
    const onClose = vi.fn();
    render(
      <>
        <ResidentPanel onClose={onClose} typingGuard />
        <textarea data-testid="real-editor" />
      </>,
    );
    const editor = screen.getByTestId('real-editor');
    // A real editor is visible and sized -- unlike glide's proxy, whose
    // default (0,0) jsdom rect already matches the invisible case below.
    vi.spyOn(editor, 'getBoundingClientRect').mockReturnValue({
      width: 200,
      height: 40,
      top: 0,
      left: 0,
      right: 200,
      bottom: 40,
      x: 0,
      y: 0,
      toJSON() {
        return {};
      },
    });

    fireEvent.keyDown(editor, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("typing guard: Escape whose target is glide's invisible 0x0 markdown-cell proxy still closes the panel", () => {
    const onClose = vi.fn();
    render(
      <>
        <ResidentPanel onClose={onClose} typingGuard />
        {/* jsdom's default getBoundingClientRect is already all-zero, matching
            the real `.gdg-md-edit-textarea` proxy's `width:0; height:0;
            opacity:0` -- no rect mock needed to reproduce the invisible case. */}
        <textarea data-testid="proxy" className="gdg-md-edit-textarea" />
      </>,
    );
    fireEvent.keyDown(screen.getByTestId('proxy'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('CascadeConfirmDialog Escape contract', () => {
  function sheet(id: string, name: string): SheetMeta {
    return { id, name, rowCount: 0, columns: [] };
  }

  it('Escape CANCELS -- never confirms/runs -- the cascade', () => {
    const onClose = vi.fn();
    const refreshSheets = vi.fn();
    render(
      <CascadeConfirmDialog
        staleDeepestFirst={[sheet('sheet-1', 'Vendors')]}
        onClose={onClose}
        refreshSheets={refreshSheets}
      />,
    );
    expect(screen.getByTestId('cascade-stale-sheet-1')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
    // No cascade side-effect: refreshSheets (the run path) never fires.
    expect(refreshSheets).not.toHaveBeenCalled();
  });

  it('starts every stale-sheet refresh unconfirmed so a model-backed sheet can return its priced 402', async () => {
    const refresh = vi.spyOn(api, 'refreshSheet').mockResolvedValue({} as never);
    render(
      <CascadeConfirmDialog
        staleDeepestFirst={[sheet('sheet-1', 'Vendors')]}
        onClose={vi.fn()}
        refreshSheets={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId('cascade-confirm-run'));

    await waitFor(() => expect(refresh).toHaveBeenCalled());
    expect(refresh).toHaveBeenNthCalledWith(1, 'sheet-1', undefined);
  });

  it('reopens a changed 402 and only retries with the newest promise hash', async () => {
    const refreshSheets = vi.fn();
    const onClose = vi.fn();
    const refresh = vi
      .spyOn(api, 'refreshSheet')
      .mockRejectedValueOnce(
        new ConfirmationRequiredError(
          { cost: 0.1, rows: 1, promise_set_hash: 'promise-1' },
          'First quote',
        ),
      )
      .mockRejectedValueOnce(
        new ConfirmationRequiredError(
          { cost: 0.2, rows: 1, promise_set_hash: 'promise-2' },
          'Quote changed',
        ),
      )
      .mockResolvedValue({} as never);
    render(
      <CascadeConfirmDialog
        staleDeepestFirst={[sheet('sheet-1', 'Vendors')]}
        onClose={onClose}
        refreshSheets={refreshSheets}
      />,
    );

    fireEvent.click(screen.getByTestId('cascade-confirm-run'));
    await screen.findByText('First quote');
    fireEvent.click(screen.getByTestId('cost-gate-confirm'));

    await waitFor(() =>
      expect(refresh).toHaveBeenNthCalledWith(2, 'sheet-1', 'promise-1'),
    );
    await screen.findByText('Quote changed');
    fireEvent.click(screen.getByTestId('cost-gate-confirm'));

    await waitFor(() =>
      expect(refresh).toHaveBeenNthCalledWith(3, 'sheet-1', 'promise-2'),
    );
    await waitFor(() => expect(refreshSheets).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
