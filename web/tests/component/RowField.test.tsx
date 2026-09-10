// @vitest-environment jsdom
//
// The row drawer's per-field action rail (copy / edit / explain) is
// hover-revealed React state
// (`actionsVisible = hovered || focused || editing` in RowField), not CSS-only,
// so we mount RowField and drive mouseEnter/mouseLeave directly.
//
// row-cell-actions-stable-layout.spec.ts stays KEEP-NON-CORE: its unique
// assertion is zero-layout-shift geometry (boundingBox height/width stability)
// and slot x-ordering, which jsdom has no layout engine to measure.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { RowField } from '../../src/components/RowDrawer';
import { createProjectApi } from '../../src/api/real';
import type { CellProvenance } from '../../src/api/types';
import { aiMeta, columnDef, row } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('row-field');
const { render } = createWorkspaceTestHarness({
  projectId: 'row-field',
  api: { projectApi: projectApi },
});

// The explain panel is a native `popover="manual"` top-layer element
// (useNativePopover) — same gap-fill ModelPicker.test.tsx installs, so
// showPopover()/:popover-open behave for real instead of throwing (jsdom has
// no built-in Popover API).
beforeAll(() => {
  installPopoverPolyfill();
});

// RowField mounts CellEvidenceBlock, which fetches evidence on mount. Isolate
// the render from the network — the fetch failure lands in its own catch and is
// irrelevant to the action-rail assertions here.
beforeEach(() => {
  vi.stubGlobal('fetch', () => Promise.reject(new Error('no network in unit test')));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const noopEdit = () => Promise.resolve();

/** Renders a hovered AI-column RowField with its explain panel open, having
 *  first stubbed `getBoundingClientRect` on the trigger so the anchored
 *  positioning math (`useAnchoredPosition`) runs against a caller-chosen
 *  rect instead of jsdom's always-zeroed one. Returns the panel element. */
function openExplainPanelAt(rect: Partial<DOMRect>): { field: HTMLElement; panel: HTMLElement } {
  const fullRect: DOMRect = {
    x: 0, y: 0, width: 20, height: 20, top: 0, bottom: 20, left: 0, right: 20,
    toJSON() { return this; },
    ...rect,
  };
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    // Only the explain trigger's own rect matters for this math — every
    // other element in the tree can keep jsdom's default zeroed rect.
    if (this.dataset.testid === 'cell-action-explain') return fullRect;
    return originalRect.call(this);
  };
  const col = columnDef({ id: 'topic', name: 'topic', type: 'text', ai: aiMeta() });
  const provenance: Record<string, CellProvenance> = {
    topic: {
      currentValueRef: { kind: 'run_result', rowId: 'row-1', columnId: 'topic', runId: 'run-9', opId: null },
      model: 'gemini/gemini-2.5-flash',
      actionName: 'Classify',
      cost: 0,
      confidence: null,
      justification: '',
      runId: 'run-9',
    },
  };
  render(
    <RowField
      col={col}
      columns={[col]}
      row={row({ topic: 'infrastructure' }, { provenance })}
      selected
      onEdit={noopEdit}
    />,
  );
  const field = screen.getByTestId('row-field-topic');
  fireEvent.mouseEnter(field);
  fireEvent.click(within(field).getByTestId('cell-action-explain'));
  const panel = within(field).getByTestId('explain-panel');
  HTMLElement.prototype.getBoundingClientRect = originalRect;
  return { field, panel };
}

describe('row drawer field action rail', () => {
  it('reveals copy + edit icon actions on hover for a raw column, hidden otherwise', () => {
    const col = columnDef({ id: 'snippet', name: 'snippet', type: 'text' });
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ snippet: 'City hall awarded a paving contract.' })}
        selected
        onEdit={noopEdit}
      />,
    );
    const field = screen.getByTestId('row-field-snippet');

    // Pre-hover: the buttons are not rendered (fixed placeholder slots hold space).
    expect(within(field).queryByTestId('cell-action-copy')).toBeNull();
    expect(within(field).queryByTestId('cell-edit-snippet')).toBeNull();

    fireEvent.mouseEnter(field);
    const copy = within(field).getByTestId('cell-action-copy');
    const edit = within(field).getByTestId('cell-edit-snippet');
    expect(copy).toHaveAttribute('aria-label', 'Copy snippet cell');
    expect(edit).toHaveAttribute('aria-label', 'Edit snippet');

    // Un-hover hides the whole rail again.
    fireEvent.mouseLeave(field);
    expect(within(field).queryByTestId('cell-action-copy')).toBeNull();
    expect(within(field).queryByTestId('cell-edit-snippet')).toBeNull();
  });

  it('edits source-bound timestamp lists as paste-friendly lines, preserving the anchor', async () => {
    const col = columnDef({ id: 'cuts', name: 'cuts', type: 'timeline_points', ai: aiMeta() });
    const timeline = {
      artifact_stable_id: 'source_artifact:interview-video',
      fingerprint: `sha256:${'c'.repeat(64)}`,
      duration_ms: 90_000,
    };
    const value = {
      schema_version: 'frisket.timeline_points.v1',
      timeline,
      items: [{ id: 'scene-1', at_ms: 12_500, label: 'Opening claim' }],
    };
    const onEdit = vi.fn(() => Promise.resolve());
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ cuts: value })}
        selected
        onEdit={onEdit}
      />,
    );
    const field = screen.getByTestId('row-field-cuts');
    fireEvent.mouseEnter(field);
    fireEvent.click(within(field).getByTestId('cell-edit-cuts'));

    const editor = within(field).getByTestId('cell-editor-cuts');
    expect(editor).toHaveValue('0:12.500,Opening claim');
    expect(editor).not.toHaveValue(expect.stringContaining('schema_version'));

    fireEvent.change(editor, { target: { value: '0:20,Claim\n0:45,Response' } });
    fireEvent.click(within(field).getByTestId('cell-save-cuts'));
    await waitFor(() => expect(onEdit).toHaveBeenCalledTimes(1));

    const saved = onEdit.mock.calls[0][2] as Record<string, unknown>;
    expect(saved.timeline).toEqual(timeline);
    expect(saved.items).toEqual([
      { id: 'manual-1', at_ms: 20_000, label: 'Claim' },
      { id: 'manual-2', at_ms: 45_000, label: 'Response' },
    ]);
  });

  it('edits a singular source-bound timestamp and sends one typed object', async () => {
    const col = columnDef({ id: 'moment', name: 'moment', type: 'timeline_point' });
    const value = {
      schema_version: 'frisket.timeline_point.v1',
      timeline: {
        artifact_stable_id: 'source_artifact:interview-video',
        fingerprint: `sha256:${'e'.repeat(64)}`,
        duration_ms: 60_000,
      },
      item: { id: 'moment-1', at_ms: 5_000 },
    };
    const onEdit = vi.fn(() => Promise.resolve());
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ moment: value })}
        selected
        onEdit={onEdit}
      />,
    );
    const field = screen.getByTestId('row-field-moment');
    fireEvent.mouseEnter(field);
    fireEvent.click(within(field).getByTestId('cell-edit-moment'));
    expect(within(field).getByTestId('cell-editor-moment')).toHaveValue('0:05');
    fireEvent.change(within(field).getByTestId('cell-editor-moment'), {
      target: { value: '0:06' },
    });
    fireEvent.click(within(field).getByTestId('cell-save-moment'));

    await waitFor(() => expect(onEdit).toHaveBeenCalledTimes(1));
    expect(onEdit.mock.calls[0][2]).toEqual({
      ...value,
      item: { id: 'manual-1', at_ms: 6_000 },
    });
    expect(typeof onEdit.mock.calls[0][2]).toBe('object');
  });

  it('keeps an invalid range draft in the friendly editor and blocks save', () => {
    const col = columnDef({ id: 'topics', name: 'topics', type: 'timeline_ranges' });
    const value = JSON.stringify({
      schema_version: 'frisket.timeline_ranges.v1',
      timeline: {
        artifact_stable_id: 'source_artifact:interview-video',
        fingerprint: `sha256:${'d'.repeat(64)}`,
        duration_ms: 60_000,
      },
      items: [{ id: 'topic-1', start_ms: 5_000, end_ms: 20_000 }],
    });
    const onEdit = vi.fn(() => Promise.resolve());
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ topics: value })}
        selected
        onEdit={onEdit}
      />,
    );
    const field = screen.getByTestId('row-field-topics');
    fireEvent.mouseEnter(field);
    fireEvent.click(within(field).getByTestId('cell-edit-topics'));
    fireEvent.change(within(field).getByTestId('cell-editor-topics'), {
      target: { value: '0:50,1:10' },
    });

    expect(within(field).getByTestId('cell-editor-error-topics')).toHaveTextContent('after the source ends');
    expect(within(field).getByTestId('cell-save-topics')).toBeDisabled();
    expect(onEdit).not.toHaveBeenCalled();
  });

  it('reveals copy + explain on hover for an AI column and opens the explain panel', () => {
    const col = columnDef({ id: 'topic', name: 'topic', type: 'text', ai: aiMeta() });
    const provenance: Record<string, CellProvenance> = {
      topic: {
        currentValueRef: {
          kind: 'run_result',
          rowId: 'row-1',
          columnId: 'topic',
          runId: 'run-9',
          opId: null,
        },
        model: 'gemini/gemini-2.5-flash',
        actionName: 'Classify',
        cost: 0,
        confidence: null,
        justification: '',
        runId: 'run-9',
      },
    };
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ topic: 'infrastructure' }, { provenance })}
        selected
        onEdit={noopEdit}
      />,
    );
    const field = screen.getByTestId('row-field-topic');

    expect(within(field).queryByTestId('cell-action-explain')).toBeNull();
    // an AI column offers explain, not the raw-column edit button
    fireEvent.mouseEnter(field);
    const explain = within(field).getByTestId('cell-action-explain');
    expect(explain).toHaveAttribute('aria-label', 'Explain topic cell');
    // The trigger is icon-only — it needs a native tooltip saying what it
    // does, same as its cell-edit/cell-action-copy siblings in the rail.
    expect(explain).toHaveAttribute('title', expect.stringContaining('Explain topic cell'));
    expect(within(field).getByTestId('cell-action-copy')).toHaveAttribute('title', 'Copy topic');
    expect(within(field).queryByTestId('cell-edit-topic')).toBeNull();

    fireEvent.click(explain);
    // Not toBeVisible(): the panel is a native `popover="manual"` element
    // now, and jsdom's default stylesheet hides `[popover]` unconditionally
    // (it has no real `:popover-open` engine) — same reason
    // ModelPicker.test.tsx's menu assertions use toBeInTheDocument().
    expect(within(field).getByTestId('explain-panel')).toBeInTheDocument();
  });

  it('clamps the explain panel inside the viewport instead of letting it render off-screen', () => {
    const originalInnerWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 });
    try {
      // The trigger sits hard against the right edge — the panel's old
      // inline `margin-top` flow had nowhere to grow it but off-screen.
      const { panel } = openExplainPanelAt({ top: 10, bottom: 30, left: 380, right: 400 });
      const left = parseFloat(panel.style.left);
      const width = parseFloat(panel.style.width);
      expect(Number.isNaN(left)).toBe(false);
      expect(left).toBeGreaterThanOrEqual(0);
      expect(left + width).toBeLessThanOrEqual(window.innerWidth);
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalInnerWidth });
    }
  });

  it('flips the explain panel above its trigger when there is no room below', () => {
    const originalInnerHeight = window.innerHeight;
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 200 });
    try {
      // The trigger sits hard against the bottom edge — downward is out of room.
      const { panel } = openExplainPanelAt({ top: 180, bottom: 200, left: 10, right: 30 });
      expect(panel.style.top).toBe('');
      expect(panel.style.bottom).not.toBe('');
    } finally {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalInnerHeight });
    }
  });

  it('dismisses the explain panel on Escape but not on an arbitrary keypress', () => {
    const { field } = openExplainPanelAt({});
    expect(within(field).getByTestId('explain-panel')).toBeInTheDocument();

    // The panel used to have no scoped dismissal at all; the owner's ask was
    // explicit dismissal only, so an unrelated key must be a no-op.
    fireEvent.keyDown(document, { key: 'a' });
    expect(within(field).getByTestId('explain-panel')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(within(field).queryByTestId('explain-panel')).toBeNull();
  });

  it('dismisses the explain panel via its own close button', () => {
    const { field } = openExplainPanelAt({});
    const closeButton = within(field).getByTestId('explain-panel-close');
    expect(closeButton).toHaveAttribute('aria-label', 'Close explain panel');
    fireEvent.click(closeButton);
    expect(within(field).queryByTestId('explain-panel')).toBeNull();
  });
});

// For a terminal empty_output retry, the
// failure is real and automation never revisits it; the drawer offers a
// deliberate per-row "Retry anyway" with honest copy — never "this will fix
// it". Gated on the cell's outcome bucket, not on error text.
describe('row drawer terminal empty_output retry', () => {
  const aiCol = () => columnDef({ id: 'translation', name: 'translation', type: 'text', ai: aiMeta() });
  const emptyOutputRow = () =>
    row(
      { translation: null },
      {
        cellStates: { translation: 'error' },
        cellErrors: { translation: 'engine returned empty output' },
        cellOutcomes: { translation: 'empty_output' },
      },
    );

  it('offers Retry anyway with honest copy and retries the exact cell', async () => {
    const col = aiCol();
    let settle!: () => void;
    const onRetryCell = vi.fn(() => new Promise<void>((resolve) => { settle = resolve; }));
    render(
      <RowField
        col={col}
        columns={[col]}
        row={emptyOutputRow()}
        selected
        onEdit={noopEdit}
        onRetryCell={onRetryCell}
      />,
    );
    const retry = screen.getByTestId('cell-retry-translation');
    expect(retry).toHaveTextContent(
      'The engine returned empty output for this row — retrying may well do the same.',
    );

    const button = within(retry).getByTestId('cell-retry-button-translation');
    expect(button).toHaveTextContent('Retry anyway');
    fireEvent.click(button);
    expect(onRetryCell).toHaveBeenCalledExactlyOnceWith('translation');

    // Busy until the retry run settles — a second click cannot double-launch.
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Retrying…');
    settle();
    await waitFor(() => expect(button).toBeEnabled());
  });

  it('does not offer retry for a retryable model_error failure', () => {
    const col = aiCol();
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row(
          { translation: null },
          {
            cellStates: { translation: 'error' },
            cellErrors: { translation: 'provider 500' },
            cellOutcomes: { translation: 'model_error' },
          },
        )}
        selected
        onEdit={noopEdit}
        onRetryCell={vi.fn(() => Promise.resolve())}
      />,
    );
    expect(screen.queryByTestId('cell-retry-translation')).toBeNull();
  });

  it('does not render without an onRetryCell handler', () => {
    const col = aiCol();
    render(
      <RowField col={col} columns={[col]} row={emptyOutputRow()} selected onEdit={noopEdit} />,
    );
    expect(screen.queryByTestId('cell-retry-translation')).toBeNull();
  });
});
