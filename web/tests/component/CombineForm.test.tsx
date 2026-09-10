// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createProjectApi } from '../../src/api/real';
import type { ColumnValuesPreview, SheetMeta } from '../../src/api/types';
import { CombineForm } from '../../src/components/resolve/CombineForm';
import type { CombineParams } from '../../src/generated/actionTypes';
import { ParamsBodyHarness } from '../support/generatedParamsBodyHarness';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});
const sheet: SheetMeta = {
  id: '7', name: 'People', rowCount: 2542,
  columns: [
    { id: '11', name: 'employer', type: 'text' },
    { id: '12', name: 'notes', type: 'text' },
    { id: '13', name: 'score', type: 'number' },
  ],
};
const VALUES = [
  { value: 'Acme Corp', count: 312 }, { value: 'Hooli', count: 210 },
  { value: 'ACME CORP.', count: 201 }, { value: 'Acme, Inc.', count: 139 },
  { value: 'Umbrella Co', count: 88 }, { value: 'GLOBEX', count: 81 },
];
const preview = (overrides: Partial<ColumnValuesPreview> = {}): ColumnValuesPreview => ({
  sheetId: '7', columnId: '11', inputColumn: 'employer', totalRows: 2542,
  distinct: 6, missing: 0, values: VALUES, offset: 0, limit: 2000,
  truncated: false, valueHash: 'sha256:combine', search: null, ...overrides,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function mount(initialParams: CombineParams = {
  source: 'employer', groups: [], unmatched: 'keep',
}) {
  const onParams = vi.fn();
  render(<ParamsBodyHarness Body={CombineForm} sheet={sheet}
    initialParams={initialParams} onParams={onParams} />);
  return onParams;
}
const latest = (spy: ReturnType<typeof vi.fn>) => spy.mock.calls.at(-1)?.[0] as CombineParams;
const unassigned = () => screen.getByTestId('resolve-combine-unassigned');
const rows = () => within(unassigned()).getAllByTestId('resolve-value-row');
function rowFor(value: string) {
  const row = rows().find((candidate) => candidate.getAttribute('data-value') === value);
  if (!row) throw new Error(`Missing ${value}`);
  return row;
}
function makeBucket(values: string[]) {
  for (const value of values) fireEvent.click(rowFor(value));
  fireEvent.click(screen.getByTestId('resolve-combine-new-bucket'));
}

describe('CombineForm Params body', () => {
  it('groups selected values, renames/promotes, and emits canonical Params', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview());
    const onParams = mount();
    await screen.findByTestId('resolve-combine-unassigned');
    makeBucket(['Acme Corp', 'ACME CORP.']);
    expect(screen.getByTestId('resolve-group-canonical-input')).toHaveValue('Acme Corp');
    expect(rows().map((row) => row.getAttribute('data-value')))
      .toEqual(['Hooli', 'Acme, Inc.', 'Umbrella Co', 'GLOBEX']);

    const promotes = screen.getAllByTestId('resolve-group-member-promote');
    fireEvent.click(promotes[1]);
    await waitFor(() => expect(latest(onParams)).toMatchObject({
      source: 'employer',
      groups: [{ canonical: 'ACME CORP.', members: ['Acme Corp', 'ACME CORP.'] }],
      unmatched: 'keep',
    }));
  });

  it('adds selections to a bucket and merges duplicate bucket names', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview());
    const onParams = mount();
    await screen.findByTestId('resolve-combine-unassigned');
    makeBucket(['Acme Corp', 'ACME CORP.']);
    makeBucket(['Hooli', 'GLOBEX']);
    const names = screen.getAllByTestId('resolve-group-canonical-input');
    fireEvent.change(names[0], { target: { value: 'Acme Corp' } });
    fireEvent.keyDown(names[0], { key: 'Enter' });
    expect(screen.getByTestId('resolve-combine-dup-nudge')).toHaveTextContent('merge them');
    fireEvent.click(screen.getByTestId('resolve-combine-dup-merge'));
    await waitFor(() => expect(latest(onParams).groups).toEqual([{
      canonical: 'Acme Corp', members: ['Hooli', 'GLOBEX', 'Acme Corp', 'ACME CORP.'],
    }]));
  });

  it('adds unassigned values to a group and dissolves it after its last member is removed', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview());
    const onParams = mount();
    await screen.findByTestId('resolve-combine-unassigned');
    makeBucket(['Acme Corp']);

    fireEvent.click(rowFor('Hooli'));
    fireEvent.click(screen.getByTestId('resolve-combine-add-to'));
    fireEvent.click(screen.getByTestId('resolve-combine-add-to-option'));
    expect(screen.getByTestId('resolve-combine-bucket')).toHaveTextContent('Hooli');

    fireEvent.change(screen.getByTestId('resolve-combine-bucket-add-value-input'), {
      target: { value: 'Umbrella' },
    });
    fireEvent.click(screen.getByTestId('resolve-combine-bucket-add-value-option'));
    expect(screen.getByTestId('resolve-combine-bucket')).toHaveTextContent('Umbrella Co');

    while (screen.queryByTestId('resolve-combine-bucket')) {
      fireEvent.click(within(screen.getByTestId('resolve-combine-bucket'))
        .getAllByTestId('resolve-group-member-remove')[0]);
    }
    await waitFor(() => expect(latest(onParams).groups).toEqual([]));
  });

  it('authors all remainder policies and hydrates saved groups', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview({
      inputColumn: 'notes', columnId: '12',
    }));
    const onParams = mount({
      source: 'notes', groups: [{ canonical: 'Acme', members: ['Acme Corp'] }],
      unmatched: 'value', unmatched_value: 'Other',
    });
    await screen.findByTestId('resolve-combine-unassigned');
    expect(screen.getByTestId('resolve-combine-column-select')).toHaveValue('notes');
    expect(screen.getByTestId('resolve-combine-bucket')).toHaveTextContent('Acme');
    expect(screen.getByTestId('resolve-combine-remainder-input')).toHaveValue('Other');
    fireEvent.change(screen.getByTestId('resolve-combine-remainder-policy'), {
      target: { value: 'null' },
    });
    await waitFor(() => expect(latest(onParams)).toMatchObject({ unmatched: 'null' }));
    expect(latest(onParams)).not.toHaveProperty('unmatched_value');
  });

  it('reloads values without dropping surviving groups and reports vanished members', async () => {
    const spy = vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview());
    const onParams = mount();
    await screen.findByTestId('resolve-combine-unassigned');
    makeBucket(['Acme Corp', 'ACME CORP.']);
    spy.mockResolvedValue(preview({
      distinct: 5, values: VALUES.filter(({ value }) => value !== 'ACME CORP.'),
      valueHash: 'sha256:fresh',
    }));
    fireEvent.click(screen.getByTestId('resolve-combine-reload'));
    expect(await screen.findByTestId('resolve-combine-reload-notice'))
      .toHaveTextContent('1 grouped value no longer in the column was dropped');
    await waitFor(() => expect(latest(onParams).groups).toEqual([
      { canonical: 'Acme Corp', members: ['Acme Corp'] },
    ]));
  });

  it('searches the server only for truncated enumerations and rejects a later snapshot', async () => {
    vi.useFakeTimers();
    const spy = vi.spyOn(api, 'columnValuesPreview').mockImplementation(async (input) => {
      if (input.search !== undefined) return preview({
        distinct: 4200, values: [{ value: 'Globex GmbH', count: 7 }],
        search: input.search, limit: 50, valueHash: 'sha256:later',
      });
      return preview({ distinct: 4200, truncated: true });
    });
    mount();
    await act(async () => {});
    fireEvent.change(screen.getByTestId('resolve-combine-unassigned-search'), {
      target: { value: 'globex' },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(spy).toHaveBeenLastCalledWith({
      sheetId: '7', inputColumn: 'employer', search: 'globex', limit: 50,
    });
    expect(screen.getByTestId('resolve-combine-search-stale')).toHaveTextContent('Reload values');
    expect(rows().map((row) => row.getAttribute('data-value'))).toEqual(['GLOBEX']);
  });

  it('keeps only the latest same-snapshot server search and can group its hit', async () => {
    vi.useFakeTimers();
    const pending: Array<{
      query: string;
      resolve(value: ColumnValuesPreview): void;
    }> = [];
    vi.spyOn(api, 'columnValuesPreview').mockImplementation((input) => {
      if (input.search === undefined) {
        return Promise.resolve(preview({ distinct: 4200, truncated: true }));
      }
      return new Promise((resolve) => pending.push({ query: input.search ?? '', resolve }));
    });
    mount();
    await act(async () => {});
    const search = screen.getByTestId('resolve-combine-unassigned-search');
    fireEvent.change(search, { target: { value: 'glo' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    fireEvent.change(search, { target: { value: 'globex' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(pending.map(({ query }) => query)).toEqual(['glo', 'globex']);

    await act(async () => pending[1].resolve(preview({
      distinct: 4200, values: [{ value: 'Globex GmbH', count: 7 }],
      search: 'globex', limit: 50,
    })));
    await act(async () => pending[0].resolve(preview({
      distinct: 4200, values: [{ value: 'Globex stale', count: 9 }],
      search: 'glo', limit: 50,
    })));
    expect(rows().map((row) => row.getAttribute('data-value')))
      .toEqual(['GLOBEX', 'Globex GmbH']);
    fireEvent.click(rowFor('Globex GmbH'));
    fireEvent.click(screen.getByTestId('resolve-combine-new-bucket'));
    expect(screen.getByTestId('resolve-combine-bucket')).toHaveTextContent('Globex GmbH');
  });
});
