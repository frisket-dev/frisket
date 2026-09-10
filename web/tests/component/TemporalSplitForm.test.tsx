// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { useState, type ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { SheetMeta } from '../../src/api/open';
import { MediaSegmentsParamsBody } from '../../src/components/action-panel/TranscriptSegmentsParamsBody';

afterEach(cleanup);

type Props = ComponentProps<typeof MediaSegmentsParamsBody>;
type Params = Props['params'];
type Request = Props['request'];

const sheet: SheetMeta = {
  id: '7', name: 'Interviews', rowCount: 4, citedColumnIds: [],
  columns: [
    { id: '1', name: 'recording', type: 'video' },
    { id: '2', name: 'alternate', type: 'audio' },
    { id: '3', name: 'chapters', type: 'timeline_points' },
    { id: '4', name: 'excerpts', type: 'timeline_ranges' },
    { id: '5', name: 'old_segments', type: 'json' },
  ],
};
const request: Request = {
  scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 102] },
  sheet_name: 'Segments', output_names: { clip: 'Excerpt' },
};
const points: Params = {
  source: 'recording',
  selection: { kind: 'draft_points', items: [{ at_ms: 30_000 }], repeat_for_rows: false },
};

function Harness({ initial = points, context = request, metadata = sheet }: {
  initial?: Params; context?: Request; metadata?: SheetMeta;
}) {
  const [params, setParams] = useState(initial);
  const Field: Props['Field'] = ({ name, testId }) => name === 'source'
    ? <select data-testid={testId} value={params.source}
        onChange={(event) => setParams({ ...params, source: event.target.value })}>
        {metadata.columns.filter((c) => c.type === 'video' || c.type === 'audio').map((c) => (
          <option key={c.id} value={c.name}>{c.name}</option>
        ))}
      </select>
    : <pre data-testid="saved-selection">{JSON.stringify(params.selection)}</pre>;
  return <>
    <MediaSegmentsParamsBody params={params} setParams={setParams}
      request={context} sheet={metadata} Field={Field} errors={{}} />
    <output data-testid="params">{JSON.stringify(params)}</output>
  </>;
}

function current(): Params {
  return JSON.parse(screen.getByTestId('params').textContent!);
}

describe('MediaSegmentsParamsBody', () => {
  it('preserves media range and point controls without owning names or Run', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.queryByTestId('split-run')).toBeNull();
    expect(screen.queryByTestId('split-target-sheet')).toBeNull();
    expect(screen.queryByRole('heading')).toBeNull();
    await user.click(screen.getByTestId('split-mode-ranges'));
    fireEvent.change(screen.getByTestId('split-ranges-textarea'), {
      target: { value: '10s,20s,Opening\n15s,30s,Overlapping\n10s,20s,Repeated' },
    });
    expect(screen.getByTestId('split-range-overlap-warning')).toBeInTheDocument();
    expect(screen.getByTestId('split-range-duplicate-warning')).toBeInTheDocument();
    expect(screen.getByTestId('split-draft-preview')).toHaveTextContent('Clips are created in this order');
    expect(current()).toEqual({ source: 'recording', selection: {
      kind: 'draft_ranges', repeat_for_rows: false,
      items: [
        { start_ms: 10_000, end_ms: 20_000, label: 'Opening' },
        { start_ms: 15_000, end_ms: 30_000, label: 'Overlapping' },
        { start_ms: 10_000, end_ms: 20_000, label: 'Repeated' },
      ],
    } });
    await user.click(screen.getByTestId('split-mode-points'));
    fireEvent.change(screen.getByTestId('split-points-textarea'), {
      target: { value: '1m,Opening\n60s\tReporter note\n90s,"Label, with comma"' },
    });
    expect(screen.getByTestId('split-duplicate-warning')).toBeInTheDocument();
    expect(screen.getByTestId('split-draft-preview')).toHaveTextContent('3 segments');
    expect(current().selection).toEqual({ kind: 'draft_points', repeat_for_rows: false, items: [
      { at_ms: 60_000, label: 'Opening' }, { at_ms: 60_000, label: 'Reporter note' },
      { at_ms: 90_000, label: 'Label, with comma' },
    ] });
  });

  it('invalid or edge-only text clears the previously valid canonical selection', () => {
    render(<Harness />);
    fireEvent.change(screen.getByTestId('split-points-textarea'), { target: { value: '20s\nbad' } });
    expect(screen.getByTestId('split-points-error')).toHaveTextContent('Line 2');
    expect(current().selection).toMatchObject({ kind: 'draft_points', items: [] });
    fireEvent.change(screen.getByTestId('split-points-textarea'), { target: { value: '0s' } });
    expect(screen.getByTestId('split-points-error')).toHaveTextContent('after 0:00');
    expect(current().selection).toMatchObject({ kind: 'draft_points', items: [] });
  });

  it('reopens a saved repeat choice unchecked and uses only canonical checkbox state', async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ ...points, selection: { ...points.selection, repeat_for_rows: true } }} />);
    expect(screen.getByTestId('split-batch-confirm')).not.toBeChecked();
    expect(current().selection).toMatchObject({ repeat_for_rows: false });
    await user.click(screen.getByTestId('split-batch-confirm'));
    expect(screen.getByTestId('split-batch-confirm')).toBeChecked();
    expect(current().selection).toMatchObject({ repeat_for_rows: true });
  });

  it.each(['scope', 'source', 'selection'] as const)(
    'clears repeat acknowledgement after %s changes', async (change) => {
      const user = userEvent.setup();
      const view = render(<Harness />);
      await user.click(screen.getByTestId('split-batch-confirm'));
      expect(current().selection).toMatchObject({ repeat_for_rows: true });
      if (change === 'scope') view.rerender(<Harness context={{ ...request,
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103, 104] },
      }} />);
      else if (change === 'source') fireEvent.change(screen.getByTestId('split-source'), { target: { value: 'alternate' } });
      else fireEvent.change(screen.getByTestId('split-points-textarea'), { target: { value: '40s' } });
      expect(screen.getByTestId('split-batch-confirm')).not.toBeChecked();
      expect(current().selection).toMatchObject({ repeat_for_rows: false });
    },
  );

  it('materialization names do not change acknowledged literal repetition', async () => {
    const view = render(<Harness />);
    await userEvent.click(screen.getByTestId('split-batch-confirm'));
    view.rerender(<Harness context={{ ...request, sheet_name: 'Renamed', output_names: { clip: 'Renamed' } }} />);
    expect(current().selection).toMatchObject({ repeat_for_rows: true });
  });

  it('column selection uses all-row scope without literal acknowledgement or retargeting missing input', async () => {
    const user = userEvent.setup();
    const view = render(<Harness context={{ ...request, scope: { kind: 'sheet_rows', sheet_id: 7 } }} />);
    expect(screen.getByTestId('split-row-required')).toBeInTheDocument();
    await user.click(screen.getByTestId('split-selection-column'));
    expect(current().selection).toEqual({ kind: 'column', column: 'chapters' });
    expect(screen.queryByTestId('split-batch-confirm')).toBeNull();
    expect(screen.queryByTestId('split-row-required')).toBeNull();
    expect(screen.getByTestId('media-segments-params')).toHaveTextContent('all 4 rows');
    expect(within(screen.getByTestId('split-selection-column-select')).getAllByRole('option')).toHaveLength(1);
    view.rerender(<Harness context={{ ...request, scope: { kind: 'sheet_rows', sheet_id: 7 } }}
      metadata={{ ...sheet, columns: sheet.columns.filter((c) => c.name !== 'chapters') }} />);
    expect(screen.getByTestId('split-selection-column-select')).toHaveValue('');
    expect(current().selection).toEqual({ kind: 'column', column: 'chapters' });
  });

  it.each([
    { kind: 'draft_range', start_ms: 1000, end_ms: 2000, label: 'Exact', repeat_for_rows: false },
    { kind: 'typed_value', value: { schema_version: 'frisket.timeline_ranges.v1', items: [] }, repeat_for_rows: false },
  ] as Params['selection'][])('retains saved $kind payload until explicitly edited', (selection) => {
    render(<Harness initial={{ source: 'recording', selection }} />);
    expect(screen.getByTestId('saved-selection')).toBeInTheDocument();
    expect(current().selection).toEqual(selection);
  });
});
