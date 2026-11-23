// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SheetMeta, View, ViewDefinitionInput, ViewSelection } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { FriendlyFiltersPanel } from '../../src/components/FriendlyFiltersPanel';
import type { ViewsEditor } from '../../src/state/viewsStore';
import { SheetFilterBar } from '../../src/workspace/SheetFilterBar';
import { ViewsPanel } from '../../src/workspace/ViewsPanel';
import { sheetMeta } from '../support/actionFormFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
vi.spyOn(api, 'columnValuesPreview').mockResolvedValue({
  sheetId: '1',
  columnId: '2',
  inputColumn: 'status',
  totalRows: 10,
  distinct: 2,
  missing: 0,
  values: [
    { value: 'open', count: 6 },
    { value: 'closed', count: 4 },
  ],
  offset: 0,
  limit: 21,
  truncated: false,
  valueHash: 'hash-status',
  search: null,
  distribution: null,
});
const { render: renderInWorkspace } = createWorkspaceTestHarness({
  projectId: 'test-project',
  api: { projectApi: api },
});

const SHEET: SheetMeta = sheetMeta(
  [{ id: '2', name: 'status', type: 'text' }],
  { id: '1', name: 'Cases', rowCount: 10 },
);

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const savedView: View = {
  id: 42,
  name: 'Open rows',
  sheet_id: 1,
  spec: {
    selection: {
      scope: { kind: 'sheet', sheet_id: 1 },
      filters: [{ column_id: 2, condition: { eq: 'open' } }],
      order: [],
      limit: null,
    },
    presentation: { columns: null, column_groups: null },
  },
  unavailable_reason: null,
  change_id: null,
};

function baseSelection(overrides: Partial<ViewSelection> = {}): ViewSelection {
  return {
    scope: { kind: 'sheet', sheet_id: 1 },
    filters: [],
    order: [],
    limit: null,
    ...overrides,
  };
}

function ViewsHarness({
  initialEditor = null,
  currentDefinition = null,
  onSave = vi.fn(),
  onUpdate = vi.fn(),
  onCancel = vi.fn(),
}: {
  initialEditor?: ViewsEditor | null;
  currentDefinition?: ViewDefinitionInput | null;
  onSave?: () => void;
  onUpdate?: () => void;
  onCancel?: () => void;
}) {
  const [editor, setEditor] = useState<ViewsEditor | null>(initialEditor);
  const [viewName, setViewName] = useState(initialEditor?.kind === 'edit' ? initialEditor.view.name : '');
  return (
    <ViewsPanel
      views={[savedView]}
      viewName={viewName}
      editor={editor}
      canEdit
      currentDefinition={currentDefinition}
      onNameChange={setViewName}
      onStartCreating={() => {
        setViewName('');
        setEditor({ kind: 'create' });
      }}
      onSave={onSave}
      onUpdate={onUpdate}
      onCancel={() => {
        setEditor(null);
        onCancel();
      }}
      onEdit={(view) => {
        setViewName(view.name);
        setEditor({ kind: 'edit', view });
      }}
      onApply={() => {}}
      onStartDefinitionUpdate={() => {}}
      onStartDelete={() => {}}
      onConfirmDefinitionUpdate={() => {}}
      onConfirmDelete={() => {}}
      onCancelConfirmation={() => {}}
    />
  );
}

describe('ViewsPanel save-what-I-see workflow', () => {
  it('omits query controls from ViewsPanel and focuses View name when saving a view', () => {
    const onSave = vi.fn();
    const filteredDefinition: ViewDefinitionInput = {
      selection: baseSelection({
        filters: [{ column_id: 2, condition: { eq: 'open' } }],
      }),
      presentation: { columns: null, column_groups: null },
    };
    render(<ViewsHarness currentDefinition={filteredDefinition} onSave={onSave} />);

    expect(screen.queryByRole('button', { name: 'Show all rows' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('View search text')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('View result limit')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('create-view'));
    const input = screen.getByTestId('view-name-input');
    expect(input).toHaveFocus();
    expect(screen.getByTestId('view-create-summary')).toHaveTextContent('Saving: 1 filter');

    // Blank name ignores Enter; non-blank submits on Enter.
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'Active cases' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('summarizes unfiltered sheets clearly when creating a view and focuses on rename', () => {
    const onUpdate = vi.fn();
    const unfilteredDefinition: ViewDefinitionInput = {
      selection: baseSelection(),
      presentation: { columns: null, column_groups: null },
    };
    render(
      <ViewsHarness
        initialEditor={{ kind: 'create' }}
        currentDefinition={unfilteredDefinition}
        onUpdate={onUpdate}
      />,
    );

    expect(screen.getByTestId('view-name-input')).toHaveFocus();
    expect(screen.getByTestId('view-create-summary')).toHaveTextContent('Saving: All rows (no filters)');

    fireEvent.keyDown(screen.getByTestId('view-name-input'), { key: 'Escape' });
    expect(screen.queryByTestId('view-create')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('edit-view'));
    const renameInput = screen.getByTestId('view-name-input');
    expect(renameInput).toHaveFocus();
    fireEvent.keyDown(renameInput, { key: 'Enter' });
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });
});

describe('SheetFilterBar above-grid active chips', () => {
  it('renders nothing above the grid when no filters, search, similarity, limit, sort, or scope are active', () => {
    const { container } = render(
      <SheetFilterBar
        currentSelection={baseSelection()}
        onSelectionChange={vi.fn()}
        activeGridFilter={null}
        activeGridFilterValueLabel={null}
        activeGridSort={null}
        scopeRowIds={null}
        onClearGridFilter={vi.fn()}
        onClearGridSort={vi.fn()}
        onClearScopeRowIds={vi.fn()}
      />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId('sheet-filter-search')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sheet-filter-limit')).not.toBeInTheDocument();
  });

  it('renders dismissible chips only for active search, similarity, limit, filter, sort, and row scope', () => {
    const onSelectionChange = vi.fn();
    const onClearGridFilter = vi.fn();
    const onClearGridSort = vi.fn();
    const onClearScopeRowIds = vi.fn();
    const activeSelection = baseSelection({
      search: { text: 'albany' },
      similarity: { index_id: 9, query_row_id: 3, top_k: 20 },
      limit: 25,
      order: [
        { score: 'search', dir: 'desc' },
        { score: 'similarity', dir: 'desc' },
      ],
    });

    render(
      <SheetFilterBar
        currentSelection={activeSelection}
        onSelectionChange={onSelectionChange}
        activeGridFilter={{ status: { eq: 'open' } }}
        activeGridFilterValueLabel={null}
        activeGridSort={[{ column: 'status', dir: 'asc' }]}
        scopeRowIds={[1, 2]}
        onClearGridFilter={onClearGridFilter}
        onClearGridSort={onClearGridSort}
        onClearScopeRowIds={onClearScopeRowIds}
      />,
    );

    expect(screen.getByTestId('grid-active-bar')).toBeVisible();
    expect(screen.getByTestId('active-grid-search')).toHaveTextContent('Search: “albany”');
    expect(screen.getByTestId('active-grid-similarity')).toHaveTextContent('Similarity filter');
    expect(screen.getByTestId('active-grid-limit')).toHaveTextContent('Limit: 25 rows');
    expect(screen.getByTestId('active-grid-filter')).toHaveTextContent('Filter: status eq open');
    expect(screen.getByTestId('active-grid-sort')).toHaveTextContent('Sort: status asc');

    fireEvent.click(screen.getByTestId('clear-grid-search'));
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        search: undefined,
        order: [{ score: 'similarity', dir: 'desc' }],
      }),
    );

    fireEvent.click(screen.getByTestId('clear-grid-similarity'));
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        similarity: undefined,
        order: [{ score: 'search', dir: 'desc' }],
      }),
    );

    fireEvent.click(screen.getByTestId('clear-grid-limit'));
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: null }),
    );

    fireEvent.click(screen.getByTestId('clear-grid-filter'));
    expect(onClearGridFilter).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('clear-grid-sort'));
    expect(onClearGridSort).toHaveBeenCalledTimes(1);
  });
});

describe('FriendlyFiltersPanel query controls in Filter sidebar', () => {
  it('flushes the pending query when programmatic navigation unmounts the panel', () => {
    vi.useFakeTimers();
    const onSelectionChange = vi.fn();
    const view = renderInWorkspace(
      <FriendlyFiltersPanel sheets={[SHEET]} activeSheetId="1"
        currentSelection={baseSelection()} onSelectionChange={onSelectionChange} />,
    );
    fireEvent.change(screen.getByTestId('sheet-filter-search'), { target: { value: 'albany' } });
    vi.advanceTimersByTime(100);
    view.unmount();
    vi.advanceTimersByTime(500);
    expect(onSelectionChange).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ search: { text: 'albany' } }),
    );
  });

  it('commits the final search on blur before navigation unmounts the panel', () => {
    vi.useFakeTimers();
    const onSelectionChange = vi.fn();
    const view = renderInWorkspace(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="1"
        currentSelection={baseSelection()}
        onSelectionChange={onSelectionChange}
      />,
    );
    const input = screen.getByTestId('sheet-filter-search');
    fireEvent.change(input, { target: { value: 'albany' } });
    vi.advanceTimersByTime(100);
    fireEvent.blur(input);
    view.unmount();
    vi.advanceTimersByTime(500);
    expect(onSelectionChange).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ search: { text: 'albany' } }),
    );
  });

  it('hides Sort results and Show all rows until active, then wires debounced search, sort, limit, and reset', () => {
    vi.useFakeTimers();
    const onSelectionChange = vi.fn();
    const onClearSelection = vi.fn();

    const view = renderInWorkspace(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="1"
        currentSelection={baseSelection()}
        onSelectionChange={onSelectionChange}
        onClearSelection={onClearSelection}
      />,
    );

    const searchInput = screen.getByTestId('sheet-filter-search');
    const limitInput = screen.getByTestId('sheet-filter-limit');
    expect(searchInput).toBeVisible();
    expect(limitInput).toBeVisible();
    expect(searchInput.closest('.friendly-query-row')).toBe(limitInput.closest('.friendly-query-row'));
    expect(screen.queryByTestId('facets-blurb')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sheet-filter-sort')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sheet-filter-show-all')).not.toBeInTheDocument();

    fireEvent.change(searchInput, { target: { value: 'albany' } });
    expect(onSelectionChange).not.toHaveBeenCalled();
    vi.advanceTimersByTime(180);
    expect(onSelectionChange).toHaveBeenCalledWith(
      expect.objectContaining({ search: { text: 'albany' } }),
    );

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="1"
        currentSelection={baseSelection({ search: { text: 'albany' } })}
        onSelectionChange={onSelectionChange}
        onClearSelection={onClearSelection}
      />,
    );

    const sortSelect = screen.getByTestId('sheet-filter-sort');
    expect(sortSelect).toBeVisible();
    fireEvent.change(sortSelect, { target: { value: 'search' } });
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        search: { text: 'albany' },
        order: [{ score: 'search', dir: 'desc' }],
      }),
    );

    fireEvent.change(limitInput, { target: { value: '25' } });
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 25 }),
    );

    const resetButton = screen.getByTestId('sheet-filter-show-all');
    expect(resetButton).toBeVisible();
    fireEvent.click(resetButton);
    expect(onClearSelection).toHaveBeenCalledTimes(1);
    expect(searchInput).toHaveValue('');
  });

  it('clears a draft after blur without retaining or replaying the committed search', () => {
    vi.useFakeTimers();
    const onSelectionChange = vi.fn();
    renderInWorkspace(
      <FriendlyFiltersPanel sheets={[SHEET]} activeSheetId="1"
        currentSelection={baseSelection()} onSelectionChange={onSelectionChange} />,
    );
    const input = screen.getByTestId('sheet-filter-search');
    fireEvent.change(input, { target: { value: 'albany' } });
    fireEvent.blur(input);
    fireEvent.click(screen.getByTestId('sheet-filter-search-clear'));
    vi.advanceTimersByTime(500);
    expect(onSelectionChange).toHaveBeenCalledTimes(2);
    expect(onSelectionChange).toHaveBeenLastCalledWith(expect.objectContaining({ search: undefined }));
    expect(input).toHaveValue('');
  });

  it('commits immediately on Enter and clears via Escape or the inline clear button without losing focus', () => {
    vi.useFakeTimers();
    const onSelectionChange = vi.fn();

    const view = renderInWorkspace(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="1"
        currentSelection={baseSelection()}
        onSelectionChange={onSelectionChange}
        onClearSelection={vi.fn()}
      />,
    );

    const searchInput = screen.getByTestId('sheet-filter-search');
    fireEvent.change(searchInput, { target: { value: 'budget' } });
    fireEvent.keyDown(searchInput, { key: 'Enter' });
    expect(onSelectionChange).toHaveBeenCalledWith(
      expect.objectContaining({ search: { text: 'budget' } }),
    );

    view.rerender(
      <FriendlyFiltersPanel
        sheets={[SHEET]}
        activeSheetId="1"
        currentSelection={baseSelection({
          search: { text: 'budget' },
          order: [{ score: 'search', dir: 'desc' }],
        })}
        onSelectionChange={onSelectionChange}
        onClearSelection={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId('sheet-filter-search-clear'));
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ search: undefined, order: [] }),
    );
    expect(searchInput).toHaveFocus();

    fireEvent.change(searchInput, { target: { value: 'draft' } });
    fireEvent.keyDown(searchInput, { key: 'Escape' });
    expect(searchInput).toHaveValue('');
    expect(searchInput).toHaveFocus();
  });
});
