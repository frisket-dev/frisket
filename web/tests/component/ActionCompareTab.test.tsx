// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionComparisonCell, ActionComparisonContext } from '../../src/api/actionComparison';
import { createActionCompareSession } from '../../src/workbench/actionCompareSession';
import { ActionCompareTab } from '../../src/workbench/ActionCompareTab';

afterEach(cleanup);

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

describe('ActionCompareTab', () => {
  it('renders typed grouped results, visibility controls, and uses only evaluated settings', async () => {
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

    render(<ActionCompareTab session={session} onUseSettings={onUseSettings}
      labelForDraft={(_draft, displayKey) => displayKey === 'B' ? 'Claude Sonnet 4.5' : 'Current values'} />);

    expect(screen.getByText('Comparing 2 of 237 selected rows')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'party' })).toBeInTheDocument();
    expect(within(screen.getByTestId('action-compare-variant-B')).getByText('Claude Sonnet 4.5')).toBeInTheDocument();
    expect(screen.getAllByRole('columnheader').some((header) => header.textContent === 'B · Claude Sonnet 4.5')).toBe(true);
    expect(screen.queryByRole('columnheader', { name: 'note' })).not.toBeInTheDocument();
    expect(screen.getByText('Edited')).toBeInTheDocument();
    expect(screen.getByText('Empty')).toBeInTheDocument();
    expect(screen.getByText(/Provider refused this row/)).toBeInTheDocument();
    expect(screen.getByText('Includes other runs.')).toBeInTheDocument();
    expect(screen.getByText('Includes edited values.')).toBeInTheDocument();
    expect(screen.queryByText('Manual edits will be kept.')).not.toBeInTheDocument();
    expect(screen.getByText('A shows current values')).toBeInTheDocument();
    expect(screen.getByText('Nothing changes until you use a variant.')).toBeInTheDocument();

    const changed = screen.getByTestId('action-compare-cell-10-party-B');
    expect(changed).toHaveAttribute('data-comparison', 'different');
    expect(within(changed).getByText('Differs from A.')).toHaveClass('sr-only');

    const different = screen.getByTestId('action-compare-cell-11-party-B');
    expect(different).toHaveAttribute('data-cell-state', 'error');
    expect(different).not.toHaveAttribute('data-comparison');
    fireEvent.click(screen.getByRole('button', { name: 'Hide difference highlighting' }));
    expect(different).not.toHaveAttribute('data-comparison');

    fireEvent.click(screen.getByRole('button', { name: 'Show note' }));
    expect(screen.getByRole('columnheader', { name: 'note' })).toBeInTheDocument();

    const bChip = screen.getByTestId('action-compare-variant-B');
    fireEvent.click(within(bChip).getByRole('button', { name: 'Use settings from variant B' }));
    expect(onUseSettings).toHaveBeenCalledWith(expect.objectContaining({
      variantLabel: 'B',
      context: expect.objectContaining({ expected_output_signature: 'sig' }),
      capturedSelection: { rowIds: [10, 11, 12], totalCount: 237 },
    }));
  });

  it('does not expose Use on an existing baseline and opens the injected editor for a challenger', () => {
    const session = createActionCompareSession({ context: fixture() }, { runVariant: vi.fn() });
    const b = session.addVariant()!;
    const renderVariantEditor = vi.fn(({ label }) => <div>Editor for {label}</div>);
    render(<ActionCompareTab session={session} onUseSettings={vi.fn()} renderVariantEditor={renderVariantEditor} />);

    expect(screen.queryByRole('button', { name: 'Use settings from variant A' })).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId('action-compare-variant-B')).getByRole('button', { name: 'Configure variant B' }));
    expect(screen.getByText('Editor for B')).toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Configure variant B' }).parentElement)
      .toHaveClass('action-compare-toolbar-shell');
    expect(renderVariantEditor).toHaveBeenCalledWith(expect.objectContaining({
      variantId: b,
      lockedParams: ['source'],
      structureLocked: true,
      lockReason: 'Fixed for this comparison',
    }));
  });

  it('clamps long inline values until the reader expands them', () => {
    const context = fixture();
    context.source_rows['10']!.body = cell('Long source text '.repeat(30));
    const session = createActionCompareSession({ context }, { runVariant: vi.fn() });
    render(<ActionCompareTab session={session} onUseSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    expect(screen.getByRole('button', { name: 'Collapse' })).toBeInTheDocument();
  });
});
