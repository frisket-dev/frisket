// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FriendlyFiltersPanel } from '../../src/components/FriendlyFiltersPanel';
import type { ColumnValuesPreview, SheetMeta } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const columnValuesPreview = vi.spyOn(api, 'columnValuesPreview');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SHEET: SheetMeta = {
  id: '7',
  name: 'Cases',
  rowCount: 100,
  columns: [
    { id: '11', name: 'status', type: 'text' },
    { id: '12', name: 'region', type: 'category' },
    { id: '13', name: 'amount', type: 'number' },
    { id: '14', name: 'filed', type: 'date' },
    { id: '15', name: 'agencies', type: 'json' },
    {
      id: '16',
      name: 'mentions',
      type: 'json',
      semanticType: 'entity_mentions',
    },
  ],
};

function valuesPreview(
  inputColumn: string,
  overrides: Partial<ColumnValuesPreview> = {},
): ColumnValuesPreview {
  return {
    sheetId: '7',
    columnId: String(SHEET.columns.find((column) => column.name === inputColumn)?.id ?? '11'),
    inputColumn,
    totalRows: 100,
    distinct: 2,
    missing: 5,
    values: [
      { value: 'open', count: 60 },
      { value: 'closed', count: 40 },
    ],
    offset: 0,
    limit: 21,
    truncated: false,
    valueHash: `hash-${inputColumn}`,
    search: null,
    distribution: null,
    ...overrides,
  };
}

function mockFacets() {
  columnValuesPreview.mockImplementation(async (input) => {
    if (input.inputColumn === 'region') {
      if (input.search) {
        return valuesPreview('region', {
          distinct: 25,
          values: [{ value: 'closed-late', count: 3 }],
          search: input.search,
        });
      }
      return valuesPreview('region', {
        distinct: 25,
        values: Array.from({ length: 21 }, (_, index) => ({
          value: `region-${index + 1}`,
          count: 25 - index,
        })),
        truncated: true,
      });
    }
    if (input.inputColumn === 'amount') {
      return valuesPreview('amount', {
        values: [{ value: '10', count: 1 }, { value: '100', count: 1 }],
        distribution: {
          kind: 'number', min: 10, max: 100,
          bins: [{ start: 10, end: 55, count: 40 }, { start: 55, end: 100, count: 60 }],
        },
      });
    }
    if (input.inputColumn === 'filed') {
      return valuesPreview('filed', {
        distribution: {
          kind: 'date', min: '2026-01-01', max: '2026-04-01',
          bins: [{ start: '2026-01-01', end: '2026-02-15', count: 40 }, { start: '2026-02-16', end: '2026-04-01', count: 60 }],
        },
      });
    }
    return valuesPreview('status');
  });
}

describe('FriendlyFiltersPanel', () => {
  it('shows an honest empty state when no sheet is open', () => {
    render(<FriendlyFiltersPanel sheets={[SHEET]} activeSheetId={null} />);
    expect(screen.getByText('Open a sheet to explore its filters.')).toBeVisible();
  });

  it('disables filter mutations when the host does not provide applySpec', async () => {
    mockFacets();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { eq: 'open' } }}
      />,
    );

    expect(await screen.findByTestId('facet-check-status-open')).toBeDisabled();
    expect(screen.getAllByRole('slider')).not.toHaveLength(0);
    for (const slider of screen.getAllByRole('slider')) expect(slider).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Clear all' })).toBeDisabled();
    expect(screen.getByTestId('facets-clear-status')).toBeDisabled();
    expect(
      within(screen.getByTestId('friendly-facet-status'))
        .getByRole('button', { name: 'Clear status filter' }),
    ).toBeDisabled();
  });

  it('promotes registry-preferred category facets ahead of ordinary scalar columns', async () => {
    mockFacets();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={() => {}}
      />,
    );

    await screen.findByTestId('facet-check-region-region-1');
    expect(screen.getByTestId('friendly-filters-panel').firstElementChild)
      .toHaveAttribute('data-testid', 'friendly-facet-region');
  });

  it('pre-populates counted checkbox facets and preserves filters from other columns', async () => {
    mockFacets();
    const onApply = vi.fn();

    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ region: { eq: 'region-1' } }}
        onApplyFilterSpec={onApply}
      />,
    );

    const open = await screen.findByTestId('facet-check-status-open');
    expect(within(screen.getByTestId('friendly-facet-status')).getByText('60')).toBeVisible();
    fireEvent.click(open);
    expect(onApply).toHaveBeenCalledWith({
      region: { eq: 'region-1' },
      status: { eq: 'open' },
    });
    expect(columnValuesPreview).toHaveBeenCalledWith({
      sheetId: '7', inputColumn: 'status', limit: 21,
    });
  });

  it('clears one column from either affordance without widening the others', async () => {
    mockFacets();
    const onApply = vi.fn();

    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { in: ['open', 'closed'] }, amount: { between: { start: '25', end: '75' } } }}
        onApplyFilterSpec={onApply}
      />,
    );

    expect(await screen.findAllByTestId('facets-active-filter')).toHaveLength(2);
    const facetStack = screen.getByTestId('friendly-filters-panel');
    const activeFilters = screen.getByTestId('facets-active-filters');
    expect(facetStack.nextElementSibling).toBe(activeFilters);
    expect(activeFilters).toHaveTextContent('status is open or closed');
    fireEvent.click(
      within(activeFilters).getByRole('button', { name: 'Clear status filter' }),
    );
    expect(onApply).toHaveBeenCalledWith({ amount: { between: { start: '25', end: '75' } } });

    onApply.mockClear();
    const amountFacet = screen.getByTestId('friendly-facet-amount');
    const amountToggle = within(amountFacet).getByTestId('facet-header-amount');
    expect(amountToggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(
      within(amountFacet).getByRole('button', { name: 'Clear amount filter' }),
    );
    expect(amountToggle).toHaveAttribute('aria-expanded', 'true');
    expect(onApply).toHaveBeenCalledWith({ status: { in: ['open', 'closed'] } });
  });

  it('ORs checked values within one facet and narrows again when one is unchecked', async () => {
    mockFacets();
    const onApply = vi.fn();
    const view = render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    fireEvent.click(await screen.findByTestId('facet-check-status-open'));
    expect(onApply).toHaveBeenLastCalledWith({ status: { eq: 'open' } });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );
    fireEvent.click(screen.getByTestId('facet-check-status-closed'));
    expect(onApply).toHaveBeenLastCalledWith({ status: { in: ['open', 'closed'] } });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { in: ['open', 'closed'] } }}
        onApplyFilterSpec={onApply}
      />,
    );
    fireEvent.click(screen.getByTestId('facet-check-status-open'));
    expect(onApply).toHaveBeenLastCalledWith({ status: { eq: 'closed' } });
  });

  it('facets scalar list members and round-trips an OR selection without replacing other filters', async () => {
    columnValuesPreview.mockImplementation(async (input) => {
      if (input.inputColumn !== 'agencies') return valuesPreview(input.inputColumn);
      return {
        ...valuesPreview('agencies', {
          // Whole-cell inventory remains the resolve authoring contract. The
          // list facet is additive and must not reinterpret these values.
          distinct: 83,
          truncated: true,
          values: [
            { value: '["NYPD","FDNY"]', count: 20 },
            { value: '["NYPD"]', count: 17 },
          ],
        }),
        listFacet: {
          distinct: 2,
          offset: 0,
          limit: 21,
          truncated: false,
          search: null,
          choices: [
            {
              key: 'scalar:string:NYPD',
              label: 'NYPD',
              count: 37,
              selector: { kind: 'scalar', value: 'NYPD' },
            },
            {
              key: 'scalar:string:FDNY',
              label: 'FDNY',
              count: 20,
              selector: { kind: 'scalar', value: 'FDNY' },
            },
          ],
        },
      } as ColumnValuesPreview;
    });
    const onApply = vi.fn();
    const view = render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );

    const agencies = await screen.findByTestId('friendly-facet-agencies');
    fireEvent.click(within(agencies).getByTestId('facet-header-agencies'));
    const nypd = await within(agencies).findByRole('checkbox', { name: 'NYPD' });
    expect(within(agencies).queryByRole('searchbox')).not.toBeInTheDocument();
    expect(within(agencies).queryByText(/Showing first/)).not.toBeInTheDocument();
    expect(within(agencies).getByText('37')).toBeVisible();
    fireEvent.click(nypd);
    expect(onApply).toHaveBeenLastCalledWith({
      status: { eq: 'open' },
      agencies: {
        list_contains_any: [{ kind: 'scalar', value: 'NYPD' }],
      },
    });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{
          status: { eq: 'open' },
          agencies: {
            list_contains_any: [{ kind: 'scalar', value: 'NYPD' }],
          },
        } as never}
        onApplyFilterSpec={onApply}
      />,
    );
    expect(within(agencies).getByRole('checkbox', { name: 'NYPD' })).toBeChecked();
    fireEvent.click(within(agencies).getByRole('checkbox', { name: 'FDNY' }));
    expect(onApply).toHaveBeenLastCalledWith({
      status: { eq: 'open' },
      agencies: {
        list_contains_any: [
          { kind: 'scalar', value: 'NYPD' },
          { kind: 'scalar', value: 'FDNY' },
        ],
      },
    });
  });

  it('distinguishes entity-object list choices with the same text by type', async () => {
    columnValuesPreview.mockImplementation(async (input) => {
      if (input.inputColumn !== 'mentions') return valuesPreview(input.inputColumn);
      return {
        ...valuesPreview('mentions', {
          values: [{ value: '[{"type":"organization","text":"NYPD"}]', count: 12 }],
        }),
        listFacet: {
          distinct: 2,
          offset: 0,
          limit: 21,
          truncated: false,
          search: null,
          choices: [
            {
              key: 'entity:organization:NYPD',
              label: 'NYPD',
              count: 12,
              selector: { kind: 'entity', type: 'organization', text: 'NYPD' },
            },
            {
              key: 'entity:person:NYPD',
              label: 'NYPD',
              count: 3,
              selector: { kind: 'entity', type: 'person', text: 'NYPD' },
            },
          ],
        },
      } as ColumnValuesPreview;
    });
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    const mentions = await screen.findByTestId('friendly-facet-mentions');
    fireEvent.click(within(mentions).getByTestId('facet-header-mentions'));
    const organization = await within(mentions).findByRole('checkbox', {
      name: 'NYPD (Organization)',
    });
    expect(within(mentions).getByRole('checkbox', { name: 'NYPD (Person)' })).toBeVisible();
    expect(organization).toHaveAccessibleName('NYPD (Organization)');
    fireEvent.click(organization);
    expect(onApply).toHaveBeenLastCalledWith({
      mentions: {
        list_contains_any: [{
          kind: 'entity', type: 'organization', text: 'NYPD',
        }],
      },
    });
  });

  it('offers keyboard-reachable row actions for selecting only or removing a value', async () => {
    mockFacets();
    const onApply = vi.fn();
    const view = render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { eq: 'open' }, region: { eq: 'region-1' } }}
        onApplyFilterSpec={onApply}
      />,
    );

    const onlyClosed = await screen.findByRole('button', { name: 'Only closed in status' });
    expect(onlyClosed).toHaveAttribute('type', 'button');
    fireEvent.click(onlyClosed);
    expect(onApply).toHaveBeenLastCalledWith({
      status: { eq: 'closed' },
      region: { eq: 'region-1' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Remove open from status filter' }));
    expect(onApply).toHaveBeenLastCalledWith({ region: { eq: 'region-1' } });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { in: ['open', 'closed'] }, region: { eq: 'region-1' } }}
        onApplyFilterSpec={onApply}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Remove open from status filter' }));
    expect(onApply).toHaveBeenLastCalledWith({
      status: { eq: 'closed' },
      region: { eq: 'region-1' },
    });
  });

  it('keeps facet row actions behind the existing mutation gate', async () => {
    mockFacets();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { eq: 'open' } }}
      />,
    );

    expect(await screen.findByRole('button', { name: 'Only closed in status' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove open from status filter' })).toBeDisabled();
    expect(screen.getAllByRole('button', { name: 'Select all shown' })[0]).toBeDisabled();
  });

  it('snapshots exactly the accepted shown values without carried selections', async () => {
    mockFacets();
    const onApply = vi.fn();
    const shown = Array.from({ length: 21 }, (_, index) => `region-${index + 1}`);
    const view = render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{
          region: { in: ['carried-selection', 'region-1'] },
          status: { eq: 'open' },
        }}
        onApplyFilterSpec={onApply}
      />,
    );

    const region = screen.getByTestId('friendly-facet-region');
    expect(await within(region).findByTestId('facet-check-region-carried-selection'))
      .toBeChecked();
    expect(within(region).getByTestId('facet-check-region-region-1')).toBeChecked();
    expect(within(region).getByText('Showing first 21 matches ·', { exact: false }))
      .toBeVisible();
    fireEvent.click(within(region).getByRole('button', { name: 'Select all shown' }));
    expect(onApply).toHaveBeenLastCalledWith({
      region: { in: shown },
      status: { eq: 'open' },
    });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ region: { in: [...shown].reverse() }, status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );
    expect(within(region).getByRole('button', { name: 'Select all shown' })).toBeDisabled();
  });

  it('keeps searched snapshots distinct from a live contains filter', async () => {
    mockFacets();
    const onApply = vi.fn();
    const view = render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ region: { eq: 'region-1' }, status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );

    const region = screen.getByTestId('friendly-facet-region');
    fireEvent.change(await within(region).findByRole('searchbox'), {
      target: { value: ' late ' },
    });
    const searched = await within(region).findByTestId('facet-check-region-closed-late');
    expect(searched).not.toBeChecked();
    expect(within(region).getByTestId('facet-check-region-region-1')).toBeChecked();

    fireEvent.click(within(region).getByRole('button', { name: 'Select all shown' }));
    expect(onApply).toHaveBeenLastCalledWith({
      region: { eq: 'closed-late' },
      status: { eq: 'open' },
    });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ region: { eq: 'closed-late' }, status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );
    expect(screen.getByTestId('facets-active-filters')).toHaveTextContent('region eq closed-late');

    fireEvent.click(within(region).getByRole('button', { name: 'Add “late” filter' }));
    expect(onApply).toHaveBeenLastCalledWith({
      region: { contains: 'late' },
      status: { eq: 'open' },
    });

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ region: { contains: 'late' }, status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );
    expect(screen.getByTestId('facets-active-filters')).toHaveTextContent('region contains late');
  });

  it('refuses oversized snapshots instead of slicing or changing semantics', async () => {
    columnValuesPreview.mockImplementation(async (input) => valuesPreview(input.inputColumn, {
      distinct: 101,
      values: Array.from({ length: 101 }, (_, index) => ({
        value: `value-${index + 1}`,
        count: 1,
      })),
      truncated: true,
    }));
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    const status = screen.getByTestId('friendly-facet-status');
    expect(await within(status).findByRole('button', { name: 'Select all shown' }))
      .toBeDisabled();
    expect(within(status).getByText('Maximum of 100 selected values reached.')).toBeVisible();
    expect(onApply).not.toHaveBeenCalled();
  });

  it('offers an accepted nonblank contains query even when it has no current matches', async () => {
    columnValuesPreview.mockImplementation(async (input) => {
      if (input.inputColumn === 'region') {
        return valuesPreview('region', {
          distinct: 25,
          values: input.search ? [] : [{ value: 'region-1', count: 25 }],
          search: input.search ?? null,
        });
      }
      return valuesPreview(input.inputColumn);
    });
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ status: { eq: 'open' } }}
        onApplyFilterSpec={onApply}
      />,
    );

    const region = screen.getByTestId('friendly-facet-region');
    fireEvent.change(await within(region).findByRole('searchbox'), {
      target: { value: 'future' },
    });
    const contains = await within(region).findByRole('button', {
      name: 'Add “future” filter',
    });
    expect(within(region).getByRole('button', { name: 'Select all shown' })).toBeDisabled();
    expect(contains).toBeEnabled();
    fireEvent.click(contains);
    expect(onApply).toHaveBeenLastCalledWith({
      region: { contains: 'future' },
      status: { eq: 'open' },
    });
  });

  it('searches high-cardinality values on the server', async () => {
    mockFacets();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={() => {}}
      />,
    );

    const search = await screen.findByTestId('facet-search-region');
    fireEvent.change(search, { target: { value: 'late' } });

    await waitFor(() => {
      expect(columnValuesPreview).toHaveBeenCalledWith({
        sheetId: '7', inputColumn: 'region', limit: 50, search: 'late',
      });
    });
    await screen.findByTestId('facet-check-region-closed-late');
    expect(screen.getByText('closed-late')).toBeVisible();
  });

  it('keeps the previous value list in place while facet search refreshes', async () => {
    let resolveSearch!: (preview: ColumnValuesPreview) => void;
    const pendingSearch = new Promise<ColumnValuesPreview>((resolve) => {
      resolveSearch = resolve;
    });
    columnValuesPreview.mockImplementation(async (input) => {
      if (input.inputColumn === 'region') {
        if (input.search) return pendingSearch;
        return valuesPreview('region', {
          distinct: 25,
          values: Array.from({ length: 21 }, (_, index) => ({
            value: `region-${index + 1}`,
            count: 25 - index,
          })),
          truncated: true,
        });
      }
      return valuesPreview(input.inputColumn);
    });
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ region: { eq: 'region-1' } }}
        onApplyFilterSpec={() => {}}
      />,
    );

    const search = await screen.findByTestId('facet-search-region');
    const values = screen.getByTestId('facet-values-region');
    expect(screen.getByTestId('facet-check-region-region-1')).toBeChecked();
    fireEvent.change(search, { target: { value: 'late' } });

    await waitFor(() => {
      expect(columnValuesPreview).toHaveBeenCalledWith({
        sheetId: '7', inputColumn: 'region', limit: 50, search: 'late',
      });
    });
    expect(screen.queryByText('Loading values…')).not.toBeInTheDocument();
    expect(screen.getByTestId('facet-values-region')).toBe(values);
    expect(values).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('facet-check-region-region-1')).toBeChecked();
    expect(
      within(screen.getByTestId('friendly-facet-region'))
        .getByRole('button', { name: 'Select all shown' }),
    ).toBeDisabled();

    await act(async () => {
      resolveSearch(valuesPreview('region', {
        distinct: 25,
        values: [{ value: 'closed-late', count: 3 }],
        search: 'late',
      }));
      await pendingSearch;
    });
    expect(screen.getByTestId('facet-values-region')).toBe(values);
    expect(values).not.toHaveAttribute('aria-busy');
    expect(screen.getByTestId('facet-check-region-region-1')).toBeChecked();
    expect(screen.getByTestId('facet-check-region-closed-late')).toBeVisible();
    expect(
      within(screen.getByTestId('friendly-facet-region'))
        .getByRole('button', { name: 'Add “late” filter' }),
    ).toBeEnabled();
  });

  it('disables both accepted-page actions until a replacement search resolves', async () => {
    let resolveSecond!: (preview: ColumnValuesPreview) => void;
    const pendingSecond = new Promise<ColumnValuesPreview>((resolve) => {
      resolveSecond = resolve;
    });
    columnValuesPreview.mockImplementation(async (input) => {
      if (input.inputColumn !== 'region') return valuesPreview(input.inputColumn);
      if (input.search === 'second') return pendingSecond;
      if (input.search === 'first') {
        return valuesPreview('region', {
          distinct: 25,
          values: [{ value: 'first-result', count: 2 }],
          search: 'first',
        });
      }
      return valuesPreview('region', {
        distinct: 25,
        values: [{ value: 'initial', count: 3 }],
      });
    });
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    const region = screen.getByTestId('friendly-facet-region');
    const search = await within(region).findByRole('searchbox');
    fireEvent.change(search, { target: { value: 'first' } });
    const firstContains = await within(region).findByRole('button', {
      name: 'Add “first” filter',
    });
    expect(firstContains).toBeEnabled();

    fireEvent.change(search, { target: { value: 'second' } });
    expect(within(region).getByRole('button', { name: 'Select all shown' })).toBeDisabled();
    expect(firstContains).toBeDisabled();
    await waitFor(() => {
      expect(columnValuesPreview).toHaveBeenCalledWith({
        sheetId: '7', inputColumn: 'region', limit: 50, search: 'second',
      });
    });
    expect(within(region).getByTestId('facet-values-region')).toHaveAttribute('aria-busy', 'true');
    expect(within(region).getByRole('button', { name: 'Select all shown' })).toBeDisabled();
    expect(firstContains).toBeDisabled();

    await act(async () => {
      resolveSecond(valuesPreview('region', {
        distinct: 25,
        values: [{ value: 'second-result', count: 1 }],
        search: 'second',
      }));
      await pendingSecond;
    });
    expect(within(region).getByRole('button', { name: 'Add “second” filter' })).toBeEnabled();
    fireEvent.click(within(region).getByRole('button', { name: 'Select all shown' }));
    expect(onApply).toHaveBeenLastCalledWith({ region: { eq: 'second-result' } });
  });

  it('applies dual-handle and typed range changes immediately without a button', async () => {
    mockFacets();
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    const amount = screen.getByTestId('friendly-facet-amount');
    const start = await within(amount).findByTestId('facet-range-start-amount');
    const end = screen.getByTestId('facet-range-end-amount');
    const dualHandle = within(amount).getByTestId('facet-range-slider-amount');
    const sliders = within(dualHandle).getAllByRole('slider');
    const histogram = within(amount).getByLabelText('Value distribution');
    expect(dualHandle.querySelectorAll('.friendly-range-slider-track')).toHaveLength(1);
    expect(sliders).toHaveLength(2);
    expect(histogram.children[0]).not.toHaveAttribute('data-excluded');
    expect(histogram.children[1]).not.toHaveAttribute('data-excluded');

    fireEvent.change(sliders[0], { target: { value: '10' } });
    expect(start).toHaveValue('60');
    expect(onApply).toHaveBeenLastCalledWith({ amount: { between: { start: '60', end: '100' } } });
    expect(histogram.children[0]).toHaveAttribute('data-excluded', 'true');
    expect(histogram.children[1]).not.toHaveAttribute('data-excluded');

    fireEvent.change(sliders[1], { target: { value: '4' } });
    expect(end).toHaveValue('60');
    fireEvent.change(sliders[0], { target: { value: sliders[0].getAttribute('max') } });
    expect(start).toHaveValue('60');

    fireEvent.change(start, { target: { value: '25' } });
    fireEvent.change(end, { target: { value: '75' } });
    expect(onApply).toHaveBeenLastCalledWith({ amount: { between: { start: '25', end: '75' } } });
    expect(within(amount).queryByRole('button', { name: 'Apply range' })).not.toBeInTheDocument();
    expect(histogram.children).toHaveLength(2);
  });

  it('formats large values, uses nice interior stops, and clamps both entry directions', async () => {
    columnValuesPreview.mockImplementation(async (input) => input.inputColumn === 'amount'
      ? valuesPreview('amount', {
        distribution: {
          kind: 'number', min: 123_456, max: 2_000_000,
          bins: [{ start: 123_456, end: 1_000_000, count: 1 }, { start: 1_000_000, end: 2_000_000, count: 1 }],
        },
      })
      : valuesPreview(input.inputColumn));
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    const amount = screen.getByTestId('friendly-facet-amount');
    const start = await within(amount).findByRole('textbox', { name: 'amount minimum' });
    const end = within(amount).getByRole('textbox', { name: 'amount maximum' });
    const sliders = within(amount).getAllByRole('slider');
    const selection = amount.querySelector('.friendly-range-slider-track span');
    expect(start).toHaveValue('123,456');
    expect(end).toHaveValue('2,000,000');
    expect(start.closest('label')).toHaveTextContent('Min');
    expect(end.closest('label')).toHaveTextContent('Max');
    expect(sliders[0]).toHaveValue('0');
    expect(sliders[1]).toHaveValue(sliders[1].getAttribute('max'));
    expect(sliders[1]).toHaveAttribute('aria-valuetext', '2,000,000');
    expect(selection).toHaveStyle({ left: '0%', right: '0%' });

    fireEvent.change(sliders[0], { target: { value: '1' } });
    expect(start).toHaveValue('200,000');
    expect(onApply).toHaveBeenLastCalledWith({
      amount: { between: { start: '200000', end: '2000000' } },
    });

    fireEvent.change(start, { target: { value: '445,432' } });
    expect(onApply).toHaveBeenLastCalledWith({
      amount: { between: { start: '445432', end: '2000000' } },
    });
    expect(start).toHaveValue('445,432');

    fireEvent.change(start, { target: { value: '3,000,000' } });
    fireEvent.blur(start);
    expect(start).toHaveValue('2,000,000');
    expect(onApply).toHaveBeenLastCalledWith({
      amount: { between: { start: '2000000', end: '2000000' } },
    });

    fireEvent.change(end, { target: { value: '100,000' } });
    fireEvent.blur(end);
    expect(end).toHaveValue('2,000,000');
    expect(within(amount).queryByRole('button', { name: 'Apply range' })).not.toBeInTheDocument();
  });

  it('does not add stale selected values outside the observed slider range', async () => {
    mockFacets();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        gridFilter={{ amount: { between: { start: '-50', end: '150' } } }}
        onApplyFilterSpec={() => {}}
      />,
    );

    const sliders = await within(screen.getByTestId('friendly-facet-amount')).findAllByRole('slider');
    expect(sliders[0]).toHaveValue('0');
    expect(sliders[1]).toHaveValue('18');
    expect(sliders[1]).toHaveAttribute('max', '18');
  });

  it('uses epoch-day handles and immediately greys excluded date bins', async () => {
    mockFacets();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={() => {}}
      />,
    );

    const filed = screen.getByTestId('friendly-facet-filed');
    const start = await within(filed).findByTestId('facet-range-start-filed');
    const sliders = within(filed).getAllByRole('slider');
    const histogram = within(filed).getByLabelText('Value distribution');
    const marchFirst = Math.floor(new Date('2026-03-01T00:00:00Z').getTime() / 86_400_000);
    expect(sliders[0]).toHaveAttribute('step', '1');

    fireEvent.change(sliders[0], { target: { value: String(marchFirst) } });
    expect(start).toHaveValue('2026-03-01');
    expect(histogram.children[0]).toHaveAttribute('data-excluded', 'true');
    expect(histogram.children[1]).not.toHaveAttribute('data-excluded');
  });

  it('uses exact text controls for integer endpoints beyond JavaScript safe range', async () => {
    const integerSheet: SheetMeta = {
      id: '8',
      name: 'Identifiers',
      rowCount: 2,
      columns: [{ id: '21', name: 'identifier', type: 'integer' }],
    };
    columnValuesPreview.mockResolvedValue({
      ...valuesPreview('identifier'),
      sheetId: '8',
      columnId: '21',
      inputColumn: 'identifier',
      distribution: {
        kind: 'integer',
        min: '-9223372036854775808',
        max: '9223372036854775807',
        bins: [
          {
            start: '-9223372036854775808',
            end: '-1',
            count: 1,
          },
          {
            start: '0',
            end: '9223372036854775807',
            count: 1,
          },
        ],
      },
    });

    render(
      <FriendlyFiltersPanel
        sheets={[integerSheet]}
        activeSheetId="8"
        onApplyFilterSpec={() => {}}
      />,
    );

    const start = await screen.findByTestId('facet-range-start-identifier');
    const end = screen.getByTestId('facet-range-end-identifier');
    expect(start).toHaveAttribute('type', 'text');
    expect(start).toHaveValue('-9,223,372,036,854,775,808');
    expect(end).toHaveValue('9,223,372,036,854,775,807');
    expect(within(screen.getByTestId('friendly-facet-identifier')).queryAllByRole('slider'))
      .toHaveLength(0);
    fireEvent.change(start, { target: { value: '0' } });
    const histogram = within(screen.getByTestId('friendly-facet-identifier'))
      .getByLabelText('Value distribution');
    expect(histogram.children[0]).toHaveAttribute('data-excluded', 'true');
    expect(histogram.children[1]).not.toHaveAttribute('data-excluded');
  });

  it('keeps numeric slider steps finite when the raw span overflows', async () => {
    columnValuesPreview.mockImplementation(async (input) => input.inputColumn === 'amount'
      ? valuesPreview('amount', {
        distribution: {
          kind: 'number', min: -1e308, max: 1e308,
          bins: [{ start: -1e308, end: 1e308, count: 2 }],
        },
      })
      : valuesPreview(input.inputColumn));
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={() => {}}
      />,
    );

    const amount = screen.getByTestId('friendly-facet-amount');
    const sliders = await within(amount).findAllByRole('slider');
    for (const slider of sliders) {
      const step = slider.getAttribute('step');
      expect(step).not.toBeNull();
      expect(Number.isFinite(Number(step))).toBe(true);
      expect(Number(step)).toBeGreaterThan(0);
    }
  });

  it('resets advanced state when a column type changes and resets month to relative defaults', async () => {
    mockFacets();
    const view = render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={() => {}}
      />,
    );
    const status = screen.getByTestId('friendly-facet-status');
    await within(status).findByTestId('facet-check-status-open');
    fireEvent.click(within(status).getByRole('button', { name: 'More filter options' }));
    fireEvent.change(screen.getByTestId('facet-advanced-operator-status'), {
      target: { value: 'contains' },
    });

    const dateSheet: SheetMeta = {
      ...SHEET,
      columns: SHEET.columns.map((column) => column.name === 'status'
        ? { ...column, type: 'date' }
        : column),
    };
    view.rerender(
      <FriendlyFiltersPanel
        sheets={[dateSheet]}
        activeSheetId="7"
        onApplyFilterSpec={() => {}}
      />,
    );
    fireEvent.click(within(screen.getByTestId('friendly-facet-status'))
      .getByRole('button', { name: 'More filter options' }));
    const operator = screen.getByTestId('facet-advanced-operator-status');
    expect(operator).toHaveValue('eq');
    fireEvent.change(operator, { target: { value: 'date_month' } });
    expect(screen.getByLabelText('Month')).toHaveValue(String(new Date().getUTCMonth() + 1));
    fireEvent.change(operator, { target: { value: 'date_relative' } });
    expect(screen.getByTestId('facet-relative-amount-status')).toHaveValue(30);
    expect(screen.getByTestId('facet-relative-unit-status')).toHaveValue('days');
  });

  it('authors relative and preset date filters from the friendly advanced editor', async () => {
    mockFacets();
    const onApply = vi.fn();
    render(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="7"
        onApplyFilterSpec={onApply}
      />,
    );

    const filed = screen.getByTestId('friendly-facet-filed');
    await within(filed).findByLabelText('Value distribution');
    fireEvent.click(within(filed).getByRole('button', { name: 'More filter options' }));
    fireEvent.change(screen.getByTestId('facet-advanced-operator-filed'), {
      target: { value: 'date_month' },
    });
    expect(screen.getByLabelText('Month')).toHaveValue(String(new Date().getUTCMonth() + 1));
    fireEvent.change(screen.getByTestId('facet-advanced-operator-filed'), {
      target: { value: 'date_relative' },
    });
    expect(screen.getByTestId('facet-relative-amount-filed')).toHaveValue(30);
    expect(screen.getByTestId('facet-relative-unit-filed')).toHaveValue('days');

    fireEvent.change(screen.getByTestId('facet-relative-amount-filed'), {
      target: { value: '0' },
    });
    expect(screen.getByTestId('facet-advanced-apply-filed')).toBeDisabled();
    fireEvent.change(screen.getByTestId('facet-relative-amount-filed'), {
      target: { value: '6' },
    });
    fireEvent.change(screen.getByTestId('facet-relative-unit-filed'), {
      target: { value: 'months' },
    });
    fireEvent.click(screen.getByTestId('facet-advanced-apply-filed'));
    expect(onApply).toHaveBeenLastCalledWith({
      filed: { date_relative: { amount: 6, unit: 'months' } },
    });

    fireEvent.change(screen.getByTestId('facet-advanced-operator-filed'), {
      target: { value: 'date_ytd' },
    });
    expect(screen.getByTestId('facet-preset-hint-filed')).toHaveTextContent(
      'updates automatically',
    );
    fireEvent.click(screen.getByTestId('facet-advanced-apply-filed'));
    expect(onApply).toHaveBeenLastCalledWith({ filed: { date_ytd: 'true' } });
  });
});
