// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SheetMeta, View, ViewSelection } from '../../src/api/open';
import type { ViewsEditor } from '../../src/state/viewsStore';
import { FilterRowsControls, type FilterRowsControlsProps } from '../../src/workspace/FilterSidebar';
import { RowAddedToastView } from '../../src/workspace/RowAddedToast';
import { ViewsPanel } from '../../src/workspace/ViewsPanel';
import { sheetMeta } from '../support/actionFormFixtures';

const SHEET: SheetMeta = sheetMeta(
  [
    { id: '2', name: 'status', type: 'text' },
    { id: '3', name: 'body', type: 'text' },
    { id: '4', name: 'size', type: 'number' },
  ],
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
  currentSummary = null,
  currentViewId = null,
  allRowsCurrent = false,
  onApplyAllRows = vi.fn(),
  onApply = vi.fn(),
  onSave = vi.fn(),
  onUpdate = vi.fn(),
  onCancel = vi.fn(),
}: {
  initialEditor?: ViewsEditor | null;
  currentSummary?: string | null;
  currentViewId?: number | null;
  allRowsCurrent?: boolean;
  onApplyAllRows?: () => void;
  onApply?: (view: View) => void;
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
      currentSummary={currentSummary}
      currentViewId={currentViewId}
      allRowsCurrent={allRowsCurrent}
      columnCount={5}
      onApplyAllRows={onApplyAllRows}
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
      onApply={onApply}
      onStartDefinitionUpdate={() => {}}
      onStartDelete={() => {}}
      onConfirmDefinitionUpdate={() => {}}
      onConfirmDelete={() => {}}
      onCancelConfirmation={() => {}}
    />
  );
}

describe('ViewsPanel save-what-I-see workflow', () => {
  it('saves the current view from a focused name field: Enter saves, blank names never do', () => {
    const onSave = vi.fn();
    render(<ViewsHarness currentSummary="Search “the” · 5 columns" onSave={onSave} />);

    expect(screen.getByTestId('create-view')).toHaveTextContent('Save current view');
    fireEvent.click(screen.getByTestId('create-view'));
    const input = screen.getByTestId('view-name-input');
    expect(input).toHaveFocus();
    expect(screen.getByTestId('view-create-summary')).toHaveTextContent('Saving: Search “the” · 5 columns');
    expect(screen.getByTestId('save-view-button')).toBeDisabled();

    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'Active cases' } });
    expect(screen.getByTestId('save-view-button')).toBeEnabled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('cancels with Escape and renames with Enter', () => {
    const onUpdate = vi.fn();
    render(<ViewsHarness initialEditor={{ kind: 'create' }} onUpdate={onUpdate} />);

    expect(screen.getByTestId('view-name-input')).toHaveFocus();
    fireEvent.keyDown(screen.getByTestId('view-name-input'), { key: 'Escape' });
    expect(screen.queryByTestId('view-create')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('edit-view'));
    const renameInput = screen.getByTestId('view-name-input');
    expect(renameInput).toHaveFocus();
    fireEvent.keyDown(renameInput, { key: 'Enter' });
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it('lists All rows first and highlights the view that matches the current state', () => {
    const onApply = vi.fn();
    const onApplyAllRows = vi.fn();
    const view = render(<ViewsHarness currentViewId={42} onApply={onApply} onApplyAllRows={onApplyAllRows} />);
    const items = screen.getByTestId('views-list').querySelectorAll('.view-item');
    expect(items[0]).toHaveTextContent('All rows');
    expect(items[0]).toHaveTextContent('No filters · 5 columns');
    expect(items[0]).not.toHaveAttribute('data-current');
    expect(screen.getByTestId('view-item')).toHaveAttribute('data-current', 'true');
    expect(screen.getByTestId('apply-view')).toHaveAttribute('aria-current', 'true');

    fireEvent.click(screen.getByTestId('apply-view'));
    expect(onApply).toHaveBeenCalledWith(savedView);
    fireEvent.click(screen.getByTestId('apply-all-rows'));
    expect(onApplyAllRows).toHaveBeenCalledTimes(1);

    view.rerender(<ViewsHarness allRowsCurrent />);
    expect(screen.getByTestId('view-item-all-rows')).toHaveAttribute('data-current', 'true');
    expect(screen.getByTestId('view-item')).not.toHaveAttribute('data-current');
  });
});

function renderRows(overrides: Partial<FilterRowsControlsProps> = {}) {
  const props: FilterRowsControlsProps = {
    sheet: SHEET,
    selection: baseSelection(),
    filter: null,
    sort: null,
    focusSearch: 0,
    draftScope: 'all',
    onSearch: vi.fn(),
    onDraftScope: vi.fn(),
    onLimit: vi.fn(),
    onOrder: vi.fn(),
    ...overrides,
  };
  return { props, view: render(<FilterRowsControls {...props} />) };
}

describe('Filter sidebar Rows section', () => {
  it('debounces search, commits on Enter, and clears from the box', () => {
    vi.useFakeTimers();
    const { props, view } = renderRows();
    const search = screen.getByTestId('sheet-filter-search');
    fireEvent.change(search, { target: { value: 'albany' } });
    expect(props.onSearch).not.toHaveBeenCalled();
    vi.advanceTimersByTime(180);
    expect(props.onSearch).toHaveBeenLastCalledWith({ text: 'albany', scope: 'all' });

    fireEvent.change(search, { target: { value: 'budget' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(props.onSearch).toHaveBeenLastCalledWith({ text: 'budget', scope: 'all' });

    view.rerender(<FilterRowsControls {...props} selection={baseSelection({ search: { text: 'budget' } })} />);
    fireEvent.click(screen.getByTestId('sheet-filter-search-clear'));
    expect(props.onSearch).toHaveBeenLastCalledWith({ text: '', scope: 'all' });
    expect(search).toHaveFocus();
  });

  it('scopes search to one text column, reading a column search back from its condition', () => {
    const { props } = renderRows({ filter: { body: { contains: 'late' } } });
    expect(screen.getByTestId('sheet-filter-search')).toHaveValue('late');
    const scope = screen.getByTestId('sheet-filter-scope');
    expect(scope).toHaveValue('body');
    expect(Array.from(scope.querySelectorAll('option')).map((option) => option.textContent))
      .toEqual(['All text columns', 'status', 'body']);
    fireEvent.change(scope, { target: { value: 'all' } });
    expect(props.onDraftScope).toHaveBeenCalledWith('all');
    expect(props.onSearch).toHaveBeenLastCalledWith({ text: 'late', scope: 'all' });
  });

  it('enables the direction only once a sort column is chosen', () => {
    const { props, view } = renderRows();
    const direction = screen.getByTestId('sheet-filter-sort-direction');
    expect(direction.closest('[data-inactive]')).toHaveAttribute('data-inactive', 'true');
    for (const button of direction.querySelectorAll('button')) expect(button).toBeDisabled();
    fireEvent.change(screen.getByTestId('sheet-filter-sort'), { target: { value: 'column:size' } });
    expect(props.onOrder).toHaveBeenLastCalledWith({ column: 'size', dir: 'asc' });

    view.rerender(<FilterRowsControls {...props} sort={[{ column: 'size', dir: 'asc' }]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Desc' }));
    expect(props.onOrder).toHaveBeenLastCalledWith({ column: 'size', dir: 'desc' });
    fireEvent.change(screen.getByTestId('sheet-filter-sort'), { target: { value: '' } });
    expect(props.onOrder).toHaveBeenLastCalledWith(null);
  });

  it('offers search score while a search is active', () => {
    const { props } = renderRows({ selection: baseSelection({ search: { text: 'x' } }) });
    fireEvent.change(screen.getByTestId('sheet-filter-sort'), { target: { value: 'score:search' } });
    expect(props.onOrder).toHaveBeenLastCalledWith({ score: 'search' });
  });

  it('switches to First when the row count is focused, and back to All rows', () => {
    const { props, view } = renderRows();
    const count = screen.getByTestId('sheet-filter-limit');
    fireEvent.focus(count);
    expect(props.onLimit).toHaveBeenLastCalledWith(100);
    view.rerender(<FilterRowsControls {...props} selection={baseSelection({ limit: 100 })} />);
    fireEvent.change(count, { target: { value: '3' } });
    expect(props.onLimit).toHaveBeenLastCalledWith(3);
    fireEvent.click(screen.getByRole('button', { name: 'All rows' }));
    expect(props.onLimit).toHaveBeenLastCalledWith(null);
  });
});

describe('Row added toast', () => {
  it('announces the new row, offers Undo, and dismisses itself after five seconds', () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    const onUndo = vi.fn();
    render(<RowAddedToastView addedRow={{ rowNumber: 7, changeId: '21' }} onDismiss={onDismiss} onUndo={onUndo} />);
    expect(screen.getByRole('status')).toHaveTextContent('Row 7 added');
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(onUndo).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(4999);
    expect(onDismiss).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('offers no Undo when the change cannot be named', () => {
    render(<RowAddedToastView addedRow={{ rowNumber: 2, changeId: null }} onDismiss={vi.fn()} onUndo={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument();
  });
});
