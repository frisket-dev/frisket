// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  GridCellKind,
  type DataEditorProps,
} from '@glideapps/glide-data-grid';

import type { ActionComparisonCell, ActionComparisonContext } from '../../src/api/actionComparison';
import { createActionCompareSession } from '../../src/workbench/actionCompareSession';
import { ActionCompareTab } from '../../src/workbench/ActionCompareTab';

const editor = vi.hoisted(() => ({ props: null as DataEditorProps | null }));
vi.mock('@glideapps/glide-data-grid', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@glideapps/glide-data-grid')>();
  const { forwardRef } = await import('react');
  return {
    ...actual,
    DataEditor: forwardRef((_props: DataEditorProps, ref) => {
      void ref;
      editor.props = _props;
      return null;
    }),
  };
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); editor.props = null; });

function actionCompareGridCellFacts(cell: object): readonly string[] {
  return (cell as { actionCompareFacts?: readonly string[] }).actionCompareFacts ?? [];
}

function rect(top: number, bottom: number): DOMRect {
  return { x: 0, y: top, top, bottom, left: 0, right: 1200, width: 1200,
    height: bottom - top, toJSON: () => ({}) };
}

const origin: ActionComparisonCell['origin'] = {
  kind: 'run_result', run_id: 5, op_id: 7, review_decision: null,
};

const cell = (value: string | null, patch: Partial<ActionComparisonCell> = {}): ActionComparisonCell => ({
  state: value === null ? 'null' : 'value',
  ...(value === null ? {} : { value }),
  origin,
  ...patch,
});

function fixture(): ActionComparisonContext {
  const draft = {
    action_id: 'map.extract', scope: { kind: 'sheet_rows' as const, sheet_id: 3 },
    params: { source: 'body', model: 'local/qwen' }, output_names: { party: 'party', note: 'note' },
  };
  return {
    schema_version: 'frisket.action_compare_context.v1', available: true, reason: null,
    limits: { max_sample_rows: 20, max_challengers: 3 }, baseline_mode: 'current_values',
    comparison_basis: { kind: 'run', run_id: 5, column_id: 30 }, draft,
    sheet: { id: 3, name: 'Cases' }, sample_row_ids: [10, 11], sample_fingerprint: 'fp',
    locked_params: ['source'],
    source_columns: [{ param: 'source', column_id: 2, name: 'body', column_type: 'text', format: null }],
    source_rows: { 10: { body: cell('First document') }, 11: { body: cell('Second document') } },
    output_columns: [
      { logical_key: 'party', column_id: 30, current_name: 'party', column_type: 'text', format: null, hidden: false, schema: null },
      { logical_key: 'note', column_id: 31, current_name: 'note', column_type: 'text', format: null, hidden: true, schema: null },
    ],
    expected_output_signature: 'sig',
    replacement_targets: [
      { logical_key: 'party', current_name: 'party', column_id: 30 },
      { logical_key: 'note', current_name: 'note', column_id: 31 },
    ],
    baseline_rows: {
      10: { party: cell('Acme', { origin: { kind: 'manual_edit', run_id: 5, op_id: 9, review_decision: 'edit' } }), note: cell('hidden') },
      11: { party: cell(null), note: cell('hidden') },
    },
    origin_summary: { mixed_runs: true, includes_manual_edits: true, includes_reject_clear: false },
  };
}

const gridPreferences = { rowHeight: 48, wrapText: false, frozenColumnCount: 1 } as const;

describe('ActionCompareTab', () => {
  it('projects typed grouped results through the shared grid and uses only evaluated settings', async () => {
    const session = createActionCompareSession(
      { context: fixture(), capturedSelection: { rowIds: [10, 11, 12], totalCount: 237 } },
      {
        runVariant: async () => ({
          status: 'ready', previewId: 'preview-B', result: {
            rows: {
              10: { party: cell('Other party') },
              11: { party: { state: 'error', error: 'Provider refused this row', origin } },
            },
          },
        }),
      },
    );
    session.addVariant();
    await session.runPending();
    const onUseSettings = vi.fn();

    render(<ActionCompareTab projectId="project-1" {...gridPreferences}
      session={session} onUseSettings={onUseSettings}
      labelForDraft={(_draft, displayKey) => displayKey === 'B' ? 'Claude Sonnet 4.5' : 'Current values'}
      describeVariantChanges={() => [{ label: 'Context', before: 'Old instructions', after: 'New instructions' }]} />);

    expect(screen.getByText('2 of 237 selected rows')).toBeInTheDocument();
    expect(within(screen.getByTestId('action-compare-variant-B')).getByText('Claude Sonnet 4.5')).toBeInTheDocument();
    expect(screen.getByTestId('action-compare-grid')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText('A shows current values')).not.toBeInTheDocument();
    expect(screen.queryByText('Nothing changes until you use a variant.')).not.toBeInTheDocument();

    const grid = editor.props!;
    expect(grid.headerHeight).toBe(34);
    expect(grid.groupHeaderHeight).toBe(28);
    expect(grid.rowHeight).toBe(48);
    expect(grid.rowSelectionMode).toBe('multi');
    expect(grid.rowMarkers).toEqual({ kind: 'both', width: 40 });
    expect(grid.freezeColumns).toBe(1);
    expect(grid.columns[0].width).toBe(140);
    expect(grid.columns[1].width).toBe(170);
    expect(screen.getByTestId('action-compare-grid')).toHaveClass('grid-host');
    expect(grid.columns.map((column) => column.title)).toEqual([
      'body', 'A · Current values', 'B · Claude Sonnet 4.5',
    ]);
    expect(grid.getGroupDetails?.(grid.columns[1].group!)).toMatchObject({ name: 'party', icon: 'aiBolt' });
    expect(grid.getGroupDetails?.('')).toEqual({ name: '' });

    fireEvent.mouseEnter(screen.getByTestId('action-compare-variant-B'));
    const tooltip = screen.getByRole('tooltip');
    expect(within(tooltip).getByText('Context')).toBeInTheDocument();
    expect(within(tooltip).getByText('Old instructions').tagName).toBe('DEL');
    expect(within(tooltip).getByText('New instructions').tagName).toBe('INS');
    expect(within(tooltip).queryByText('→')).not.toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByTestId('action-compare-variant-B'));

    const source = grid.getCellContent([0, 0]);
    expect(source).toMatchObject({ kind: GridCellKind.Text, displayData: 'First document', readonly: true });
    const edited = grid.getCellContent([1, 0]);
    expect(edited).toMatchObject({ kind: GridCellKind.Text, displayData: 'Acme', readonly: true });
    expect(actionCompareGridCellFacts(edited)).toContain('Edited');
    const changed = grid.getCellContent([2, 0]);
    expect(changed).toMatchObject({ kind: GridCellKind.Text, displayData: 'Other party', readonly: true });
    expect(actionCompareGridCellFacts(changed)).toContain('Differs from A');
    expect(changed.themeOverride?.bgCell).toBeTruthy();
    const failed = grid.getCellContent([2, 1]);
    expect(failed).toMatchObject({ kind: GridCellKind.Text, displayData: '⚠ Provider refused this row' });
    expect(actionCompareGridCellFacts(failed)).toContain('Not compared');

    fireEvent.click(screen.getByRole('button', { name: 'Hide difference highlighting' }));
    const unhighlighted = editor.props!.getCellContent([2, 0]);
    expect(unhighlighted.themeOverride?.bgCell).not.toBe(changed.themeOverride?.bgCell);
    expect(actionCompareGridCellFacts(unhighlighted)).toContain('Differs from A');

    fireEvent.click(screen.getByRole('button', { name: 'Show note' }));
    expect(editor.props!.columns.map((column) => column.title)).toEqual([
      'body', 'A · Current values', 'B · Claude Sonnet 4.5',
      'A · Current values', 'B · Claude Sonnet 4.5',
    ]);

    act(() => editor.props!.onColumnResize?.(editor.props!.columns[0], 287));
    expect(editor.props!.columns[0].width).toBe(287);

    const bChip = screen.getByTestId('action-compare-variant-B');
    fireEvent.click(within(bChip).getByRole('button', { name: 'Use settings from variant B' }));
    expect(onUseSettings).toHaveBeenCalledWith(expect.objectContaining({
      variantLabel: 'B',
      context: expect.objectContaining({ expected_output_signature: 'sig' }),
      capturedSelection: { rowIds: [10, 11, 12], totalCount: 237 },
    }));

    const b = session.getSnapshot().variants.find((variant) => variant.displayKey === 'B')!;
    act(() => session.updateVariant(b.id, { ...b.draft, params: { ...b.draft.params, model: 'new/model' } }));
    const previous = editor.props!.getCellContent([2, 0]);
    expect(previous).toMatchObject({ displayData: 'Other party', style: 'faded' });
    expect(actionCompareGridCellFacts(previous)).toContain('Previous settings');
    expect(actionCompareGridCellFacts(previous)).not.toContain('Differs from A');
    act(() => editor.props!.onCellActivated?.([2, 0]));
    expect(screen.getByText('Previous settings')).toBeInTheDocument();
  });

  it('does not expose Use on an existing baseline and opens the injected editor for a challenger', () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function measure() {
      if (this.classList.contains('action-compare')) return rect(200, 720);
      if (this.classList.contains('action-compare-toolbar-shell')) return rect(200, 260);
      if (this.tagName === 'FOOTER') return rect(672, 720);
      return rect(0, 0);
    });
    const session = createActionCompareSession({ context: fixture() }, { runVariant: vi.fn() });
    const b = session.addVariant()!;
    const renderVariantEditor = vi.fn(({ label }) => <div>Editor for {label}</div>);
    render(<ActionCompareTab projectId="project-1" {...gridPreferences}
      session={session} onUseSettings={vi.fn()}
      renderVariantEditor={renderVariantEditor} />);

    expect(editor.props!.getCellContent([2, 0])).toMatchObject({
      kind: GridCellKind.Custom,
      data: { kind: 'frisket-pending' },
    });

    expect(screen.queryByRole('button', { name: 'Use settings from variant A' })).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId('action-compare-variant-B')).getByRole('button', { name: 'Configure variant B' }));
    expect(screen.getByText('Editor for B')).toBeInTheDocument();
    const editorPanel = screen.getByRole('complementary', { name: 'Configure variant B' });
    expect(editorPanel.parentElement)
      .toHaveClass('action-compare-toolbar-shell');
    expect(editorPanel).toHaveStyle({ maxHeight: '448px' });
    expect(within(editorPanel).getByRole('button', { name: 'Close variant editor' })
      .closest('.action-compare-editor-body')).toBeNull();
    expect(screen.getByText('Editor for B').closest('.action-compare-editor-body')).toBeInTheDocument();
    expect(renderVariantEditor).toHaveBeenCalledWith(expect.objectContaining({
      variantId: b,
      lockedParams: ['source'],
      structureLocked: true,
      lockReason: 'Fixed for this comparison',
    }));
  });

  it('uses the shared renderer with project identity and generated-text display semantics', () => {
    const context = fixture();
    const digest = 'b'.repeat(64);
    context.source_columns[0] = {
      ...context.source_columns[0], column_type: 'image',
    };
    context.source_rows['10']!.body = cell(JSON.stringify({
      blob: digest, mime: 'image/png', filename: 'source.png',
    }));
    context.baseline_rows!['10']!.party = cell('First\\n\\nsecond');
    const session = createActionCompareSession({ context }, { runVariant: vi.fn() });

    render(<ActionCompareTab projectId="project-1" {...gridPreferences} wrapText
      session={session} onUseSettings={vi.fn()} />);

    expect(editor.props!.getCellContent([0, 0])).toMatchObject({
      kind: GridCellKind.Image,
      data: [`/api/projects/project-1/blobs/${digest}`],
    });
    expect(editor.props!.getCellContent([1, 0])).toMatchObject({
      kind: GridCellKind.Text,
      data: 'First\\n\\nsecond',
      displayData: 'First\n \nsecond',
      allowWrapping: true,
    });
  });

  it('routes bounded and structured activation to the existing reader callbacks', () => {
    const context = fixture();
    context.source_rows['10']!.body = {
      state: 'value', value: null, origin,
      bounded_value: {
        kind: 'current_cell', row_id: 10, column_id: 2, preview: 'Long source text',
        value_kind: 'text', total_characters: 50_000, total_bytes: 50_000,
        retrieval_revision: 4, truncated: true,
      },
    };
    context.output_columns[0] = {
      ...context.output_columns[0], column_type: 'json', semantic_type: 'entity_mentions',
    };
    context.baseline_rows!['10']!.party = cell('unused', {
      state: 'value', value: { party: { name: 'Acme', roles: ['buyer', 'seller'] } },
    });
    const session = createActionCompareSession({ context }, { runVariant: vi.fn() });
    const onOpenBoundedValue = vi.fn();
    const onOpenStructuredValue = vi.fn();
    render(<ActionCompareTab projectId="project-1" {...gridPreferences}
      session={session} onUseSettings={vi.fn()}
      onOpenBoundedValue={onOpenBoundedValue} onOpenStructuredValue={onOpenStructuredValue} />);

    act(() => editor.props!.onCellActivated?.([0, 0]));
    expect(onOpenBoundedValue).toHaveBeenCalledWith(expect.objectContaining({
      value: expect.objectContaining({ kind: 'current_cell' }),
      previewId: null,
      rowId: 10,
      columnKey: 'body',
    }));

    act(() => editor.props!.onCellActivated?.([1, 0]));
    expect(onOpenStructuredValue).toHaveBeenCalledWith(expect.objectContaining({
      column: expect.objectContaining({
        name: 'party', type: 'json', semanticType: 'entity_mentions',
      }),
      row: expect.objectContaining({ id: '10', cells: expect.any(Object) }),
    }));
    expect(onOpenStructuredValue.mock.calls[0][0].row.cells)
      .toEqual(expect.objectContaining({
        [onOpenStructuredValue.mock.calls[0][0].column.id]: JSON.stringify({
          party: { name: 'Acme', roles: ['buyer', 'seller'] },
        }),
      }));
  });

  it('renders comparison statuses as text without turning successful nulls into statuses', async () => {
    const context = fixture();
    context.draft.output_names = {
      amount: 'amount', approved: 'approved', category: 'category',
    };
    context.output_columns = [
      { logical_key: 'amount', column_id: 30, current_name: 'amount',
        column_type: 'number', format: null, hidden: false, schema: null },
      { logical_key: 'approved', column_id: 31, current_name: 'approved',
        column_type: 'boolean', format: null, hidden: false, schema: null },
      { logical_key: 'category', column_id: 32, current_name: 'category',
        column_type: 'category', format: null, hidden: false, schema: null },
    ];
    context.replacement_targets = context.output_columns.map((output) => ({
      logical_key: output.logical_key,
      current_name: output.current_name,
      column_id: output.column_id!,
    }));
    const missing: ActionComparisonCell = { state: 'missing', origin };
    context.source_rows['11'] = { body: missing };
    context.baseline_rows = {
      10: { amount: cell(null), approved: cell(null), category: cell(null) },
      11: { amount: missing, approved: missing, category: missing },
    };
    const session = createActionCompareSession(
      { context },
      { runVariant: async () => ({ status: 'cancelled' }) },
    );
    session.addVariant();
    await session.runPending();
    session.addVariant();

    render(<ActionCompareTab projectId="project-1" {...gridPreferences}
      session={session} onUseSettings={vi.fn()} />);
    const grid = editor.props!;

    for (const columnIndex of [2, 5, 8]) {
      expect(grid.getCellContent([columnIndex, 0])).toMatchObject({
        kind: GridCellKind.Text,
        data: '',
        displayData: 'Cancelled',
        copyData: '',
        themeOverride: expect.any(Object),
      });
      expect(grid.getCellContent([columnIndex, 0])).not.toHaveProperty('style');
    }
    for (const columnIndex of [3, 6, 9]) {
      expect(grid.getCellContent([columnIndex, 0])).toMatchObject({
        kind: GridCellKind.Custom,
        data: { kind: 'frisket-pending' },
      });
    }
    for (const columnIndex of [1, 4, 7]) {
      expect(grid.getCellContent([columnIndex, 1])).toMatchObject({
        kind: GridCellKind.Text,
        data: '',
        displayData: 'No result',
        copyData: '',
        themeOverride: expect.any(Object),
      });
      expect(grid.getCellContent([columnIndex, 1])).not.toHaveProperty('style');
    }
    expect(grid.getCellContent([0, 1])).toMatchObject({
      kind: GridCellKind.Text,
      data: '',
      displayData: '—',
    });
    expect(grid.getCellContent([1, 0])).toMatchObject({
      kind: GridCellKind.Text,
      displayData: '—',
    });
    expect(grid.getCellContent([4, 0])).toMatchObject({
      kind: GridCellKind.Boolean,
      data: undefined,
    });
    expect(grid.getCellContent([7, 0])).toMatchObject({
      kind: GridCellKind.Bubble,
      data: [],
    });
  });
});
