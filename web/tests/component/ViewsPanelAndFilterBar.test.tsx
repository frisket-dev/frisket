// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { View, ViewDefinitionInput, ViewSelection } from '../../src/api/open';
import type { ViewsEditor } from '../../src/state/viewsStore';
import { SheetFilterBar } from '../../src/workspace/SheetFilterBar';
import { ViewsPanel } from '../../src/workspace/ViewsPanel';

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

describe('SheetFilterBar shaping controls', () => {
  it('hides score sort and Show all rows until shaping is active, then wires search, sort, limit, and reset', () => {
    vi.useFakeTimers();
    const onSelectionChange = vi.fn();
    const onClearSelection = vi.fn();
    const initial = baseSelection();

    const { rerender } = render(
      <SheetFilterBar
        currentSelection={initial}
        onSelectionChange={onSelectionChange}
        onClearSelection={onClearSelection}
        activeGridFilter={null}
        activeGridFilterValueLabel={null}
        activeGridSort={null}
        scopeRowIds={null}
        activeViewId={null}
        onClearGridFilter={vi.fn()}
        onClearGridSort={vi.fn()}
        onClearScopeRowIds={vi.fn()}
      />,
    );

    const searchInput = screen.getByLabelText('Search sheet content');
    const limitInput = screen.getByLabelText('Limit results');
    expect(searchInput).toBeVisible();
    expect(limitInput).toBeVisible();
    expect(screen.queryByLabelText('Sort results')).not.toBeInTheDocument();
    expect(screen.queryByTestId('show-all-rows')).not.toBeInTheDocument();

    fireEvent.change(searchInput, { target: { value: 'albany' } });
    expect(onSelectionChange).not.toHaveBeenCalled();
    vi.advanceTimersByTime(180);
    expect(onSelectionChange).toHaveBeenCalledWith(
      expect.objectContaining({ search: { text: 'albany' } }),
    );

    const searchedSelection = baseSelection({ search: { text: 'albany' } });
    rerender(
      <SheetFilterBar
        currentSelection={searchedSelection}
        onSelectionChange={onSelectionChange}
        onClearSelection={onClearSelection}
        activeGridFilter={null}
        activeGridFilterValueLabel={null}
        activeGridSort={null}
        scopeRowIds={null}
        activeViewId={null}
        onClearGridFilter={vi.fn()}
        onClearGridSort={vi.fn()}
        onClearScopeRowIds={vi.fn()}
      />,
    );

    const sortSelect = screen.getByTestId('sheet-score-order');
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

    const resetButton = screen.getByTestId('show-all-rows');
    expect(resetButton).toBeVisible();
    fireEvent.click(resetButton);
    expect(onClearSelection).toHaveBeenCalledTimes(1);
    expect(searchInput).toHaveValue('');
  });

  it('clears search immediately via the inline clear button without losing focus', () => {
    const onSelectionChange = vi.fn();
    render(
      <SheetFilterBar
        currentSelection={baseSelection({ search: { text: 'budget' }, order: [{ score: 'search', dir: 'desc' }] })}
        onSelectionChange={onSelectionChange}
        onClearSelection={vi.fn()}
        activeGridFilter={null}
        activeGridFilterValueLabel={null}
        activeGridSort={null}
        scopeRowIds={null}
        activeViewId={null}
        onClearGridFilter={vi.fn()}
        onClearGridSort={vi.fn()}
        onClearScopeRowIds={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId('clear-sheet-search'));
    expect(onSelectionChange).toHaveBeenCalledWith(
      expect.objectContaining({ search: undefined, order: [] }),
    );
    expect(screen.getByTestId('sheet-search-input')).toHaveFocus();
  });
});
