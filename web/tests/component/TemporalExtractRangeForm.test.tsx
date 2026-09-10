// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState, type ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import { TemporalExtractParamsBody } from '../../src/components/action-panel/TemporalExtractParamsBody';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

afterEach(cleanup);
type Props = ComponentProps<typeof TemporalExtractParamsBody>;
type Params = Props['params'];
const sheet = sheetMeta([
  columnDef({ id: '11', name: 'recording', type: 'video' }),
  columnDef({ id: '12', name: 'audio_only', type: 'audio' }),
  columnDef({ id: '13', name: 'reviewed_range', type: 'timeline_range' }),
  columnDef({ id: '14', name: 'reviewed_ranges', type: 'timeline_ranges' }),
  columnDef({ id: '15', name: 'uploaded_file', type: 'file' }),
], { id: '7', rowCount: 3 });
const request: Props['request'] = {
  scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 102] },
  output_names: { clip: 'excerpt' },
};
const initial: Params = { source: 'recording',
  selection: { kind: 'draft_range', start_ms: 10_000, end_ms: 20_000, repeat_for_rows: false } };

function Harness({ value = initial, context = request, metadata = sheet }: {
  value?: Params; context?: Props['request']; metadata?: typeof sheet;
}) {
  const [params, setParams] = useState(value);
  const Field: Props['Field'] = ({ name, testId }) => name === 'source'
    ? <select data-testid={testId} value={params.source}
        onChange={(event) => setParams({ ...params, source: event.target.value })}>
        {metadata.columns.filter((c) => c.type === 'audio' || c.type === 'video').map((c) => (
          <option key={c.id} value={c.name}>{c.name}</option>
        ))}
      </select>
    : <pre data-testid="saved-selection">{JSON.stringify(params.selection)}</pre>;
  return <>
    <TemporalExtractParamsBody params={params} setParams={setParams}
      sheet={metadata} request={context} Field={Field} errors={{}} />
    <output data-testid="params">{JSON.stringify(params)}</output>
  </>;
}
function current(): Params { return JSON.parse(screen.getByTestId('params').textContent!); }

it('retains source/timecode controls while leaving names and Run to the host', () => {
  render(<Harness />);
  expect(within(screen.getByTestId('extract-range-source')).getAllByRole('option').map((o) => o.textContent))
    .toEqual(['recording', 'audio_only']);
  expect(screen.queryByTestId('extract-range-run')).toBeNull();
  expect(screen.queryByTestId('extract-range-output')).toBeNull();
  fireEvent.change(screen.getByTestId('extract-range-start'), { target: { value: '1m' } });
  fireEvent.change(screen.getByTestId('extract-range-end'), { target: { value: '90' } });
  expect(current()).toEqual({ source: 'recording', selection: {
    kind: 'draft_range', start_ms: 60_000, end_ms: 90_000, repeat_for_rows: false,
  } });
});

it('invalid interval cannot retain the last valid submitted range', () => {
  render(<Harness />);
  fireEvent.change(screen.getByTestId('extract-range-end'), { target: { value: 'bad' } });
  expect(screen.getByTestId('extract-range-error')).toBeInTheDocument();
  expect(current().selection).toMatchObject({ start_ms: 0, end_ms: 0 });
});

it('output discovery and renaming do not change acknowledged literal repetition', async () => {
  const view = render(<Harness context={{ ...request, output_names: {} }} />);
  await userEvent.click(screen.getByTestId('extract-range-batch-confirm'));
  expect(current().selection).toMatchObject({ repeat_for_rows: true });
  view.rerender(<Harness context={{ ...request, output_names: { clip: 'clip' } }} />);
  expect(current().selection).toMatchObject({ repeat_for_rows: true });
  view.rerender(<Harness context={{ ...request, output_names: { clip: 'renamed' } }} />);
  expect(current().selection).toMatchObject({ repeat_for_rows: true });
});

it('reopens saved repetition unchecked and requires explicit canonical acknowledgement', async () => {
  render(<Harness value={{ ...initial, selection: { ...initial.selection, repeat_for_rows: true } }} />);
  expect(screen.getByTestId('extract-range-batch-confirm')).not.toBeChecked();
  await userEvent.click(screen.getByTestId('extract-range-batch-confirm'));
  expect(current().selection).toMatchObject({ repeat_for_rows: true });
});

it.each(['range', 'source', 'scope'] as const)('resets repetition when %s changes', async (change) => {
  const view = render(<Harness />);
  await userEvent.click(screen.getByTestId('extract-range-batch-confirm'));
  expect(current().selection).toMatchObject({ repeat_for_rows: true });
  if (change === 'range') fireEvent.change(screen.getByTestId('extract-range-end'), { target: { value: '30' } });
  if (change === 'source') fireEvent.change(screen.getByTestId('extract-range-source'), { target: { value: 'audio_only' } });
  if (change === 'scope') view.rerender(<Harness context={{ ...request,
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103] } }} />);
  expect(screen.getByTestId('extract-range-batch-confirm')).not.toBeChecked();
  expect(current().selection).toMatchObject({ repeat_for_rows: false });
});

it('uses only single-range columns and never retargets missing saved selection', async () => {
  const view = render(<Harness />);
  await userEvent.click(screen.getByTestId('extract-range-selection-column'));
  expect(current().selection).toEqual({ kind: 'column', column: 'reviewed_range' });
  expect(screen.queryByTestId('extract-range-batch-confirm')).toBeNull();
  expect(within(screen.getByTestId('extract-range-column-select')).getAllByRole('option')).toHaveLength(1);
  view.rerender(<Harness metadata={{ ...sheet, columns: sheet.columns.filter((c) => c.name !== 'reviewed_range') }} />);
  expect(screen.getByTestId('extract-range-column-select')).toHaveValue('');
  expect(current().selection).toEqual({ kind: 'column', column: 'reviewed_range' });
});

it('does not claim all-row literal scope but explains all-row column selection', async () => {
  render(<Harness context={{ scope: { kind: 'sheet_rows', sheet_id: 7 } }} />);
  expect(screen.getByTestId('extract-range-row-required')).toBeInTheDocument();
  await userEvent.click(screen.getByTestId('extract-range-selection-column'));
  expect(screen.queryByTestId('extract-range-row-required')).toBeNull();
  expect(screen.getByTestId('temporal-extract-params')).toHaveTextContent('all 3 rows');
});

it('preserves a saved typed timeline value until the user replaces it', () => {
  const selection = { kind: 'typed_value', value: { schema_version: 'frisket.timeline_range.v1',
    start_ms: 1000, end_ms: 2000 }, repeat_for_rows: false } as Params['selection'];
  render(<Harness value={{ source: 'recording', selection }} />);
  expect(screen.getByTestId('saved-selection')).toBeInTheDocument();
  expect(current().selection).toEqual(selection);
});
